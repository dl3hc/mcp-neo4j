import logging
from typing import Any, Dict, List, Optional

from neo4j import AsyncDriver, RoutingControl

logger = logging.getLogger('mcp_neo4j_memory')
logger.setLevel(logging.INFO)

MIN_TTL_SECONDS = 5
MAX_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 30


def clamp_ttl(ttl_seconds: Optional[int]) -> int:
    """Clamp a requested TTL into [MIN_TTL_SECONDS, MAX_TTL_SECONDS].

    A missing/invalid value falls back to DEFAULT_TTL_SECONDS. The cap keeps a single
    agent from parking an exclusive hold on a hot node indefinitely - long operations
    are expected to re-acquire (heartbeat) instead.
    """
    if ttl_seconds is None:
        ttl_seconds = DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(int(ttl_seconds), MAX_TTL_SECONDS))


class Neo4jLocking:
    """Property-based advisory locking on :Memory nodes.

    Locks live as properties on the node itself (`_lock_owner`, `_lock_expires_at`,
    `_lock_reason`) so they die automatically with the node (DETACH DELETE removes them
    for free) and never need a separate cleanup pass. Expiry is evaluated lazily against
    Neo4j's own `timestamp()` on every access - self-healing, no background sweeper.
    Contention is per-node (Neo4j's own record locks provide the compare-and-swap), so
    agents working on different entities never block each other.
    """

    def __init__(self, neo4j_driver: AsyncDriver):
        self.driver = neo4j_driver

    async def acquire_lock(
        self,
        entity_names: List[str],
        agent_id: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        reason: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Try to acquire (or renew, if already held by the same agent_id) a lock on each name.

        Fails fast: an entity already locked by a different, non-expired owner is
        reported with acquired=False and the current owner/expiry - callers retry with
        their own backoff rather than blocking the server.
        """
        ttl_ms = clamp_ttl(ttl_seconds) * 1000
        query = """
        UNWIND $names AS name
        MATCH (e:Memory {name: name})
        WITH e,
             (e._lock_owner IS NULL
              OR e._lock_expires_at < timestamp()
              OR e._lock_owner = $agent_id) AS available
        FOREACH (_ IN CASE WHEN available THEN [1] ELSE [] END |
          SET e._lock_owner = $agent_id,
              e._lock_expires_at = timestamp() + $ttl_ms,
              e._lock_reason = $reason
        )
        RETURN e.name AS name,
               available AS acquired,
               e._lock_owner AS owner,
               e._lock_expires_at AS expiresAt,
               coalesce(e._version, 0) AS version
        """
        result = await self.driver.execute_query(
            query,
            {
                "names": entity_names,
                "agent_id": agent_id,
                "ttl_ms": ttl_ms,
                "reason": reason,
            },
            routing_control=RoutingControl.WRITE,
        )
        rows = [
            {
                "name": record.get("name"),
                "acquired": record.get("acquired"),
                "owner": record.get("owner"),
                "expiresAt": record.get("expiresAt"),
                "version": record.get("version"),
            }
            for record in result.records
        ]
        found = {row["name"] for row in rows}
        for missing in [n for n in entity_names if n not in found]:
            rows.append({"name": missing, "acquired": False, "owner": None, "expiresAt": None, "version": None})
        return rows

    async def release_lock(self, entity_names: List[str], agent_id: str) -> List[Dict[str, Any]]:
        """Release the lock on each name, but only where agent_id is the current owner."""
        query = """
        UNWIND $names AS name
        MATCH (e:Memory {name: name})
        WITH e, (e._lock_owner = $agent_id) AS owns
        FOREACH (_ IN CASE WHEN owns THEN [1] ELSE [] END |
          REMOVE e._lock_owner, e._lock_expires_at, e._lock_reason
        )
        RETURN e.name AS name, owns AS released
        """
        result = await self.driver.execute_query(
            query,
            {"names": entity_names, "agent_id": agent_id},
            routing_control=RoutingControl.WRITE,
        )
        rows = [
            {"name": record.get("name"), "released": record.get("released")}
            for record in result.records
        ]
        found = {row["name"] for row in rows}
        for missing in [n for n in entity_names if n not in found]:
            rows.append({"name": missing, "released": False})
        return rows

    async def lock_status(self, entity_names: List[str]) -> List[Dict[str, Any]]:
        """Read-only: current lock/version state for each name. Missing names report exists=False."""
        query = """
        UNWIND $names AS name
        OPTIONAL MATCH (e:Memory {name: name})
        RETURN name,
               e IS NOT NULL AS exists,
               coalesce(e._version, 0) AS version,
               CASE WHEN e._lock_expires_at IS NOT NULL AND e._lock_expires_at >= timestamp()
                    THEN true ELSE false END AS locked,
               CASE WHEN e._lock_expires_at IS NOT NULL AND e._lock_expires_at >= timestamp()
                    THEN e._lock_owner END AS owner,
               e._lock_expires_at AS expiresAt
        """
        result = await self.driver.execute_query(
            query, {"names": entity_names}, routing_control=RoutingControl.READ,
        )
        return [
            {
                "name": record.get("name"),
                "exists": record.get("exists"),
                "version": record.get("version"),
                "locked": record.get("locked"),
                "owner": record.get("owner"),
                "expiresAt": record.get("expiresAt"),
            }
            for record in result.records
        ]
