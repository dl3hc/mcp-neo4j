import logging
from typing import Optional

from neo4j.exceptions import Neo4jError
from pydantic import Field

from fastmcp.server import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent, ToolAnnotations

from .locking import DEFAULT_TTL_SECONDS, Neo4jLocking

logger = logging.getLogger('mcp_neo4j_memory')
logger.setLevel(logging.INFO)


def register_lock_tools(mcp: FastMCP, locking: Neo4jLocking, namespace_prefix: str) -> None:
    """Register acquire_lock / release_lock / lock_status on the given FastMCP server."""

    @mcp.tool(
        name=namespace_prefix + "acquire_lock",
        annotations=ToolAnnotations(title="Acquire Lock", readOnlyHint=False,
                                     destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def acquire_lock(
        entity_names: list[str] = Field(..., description="Exact entity names to lock"),
        agent_id: str = Field(..., description="Your own identifier - used to recognize your own renewal and to report ownership to others"),
        ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, ge=1, description="Requested lock duration; server clamps to 5-300s. Re-call before it expires to extend (heartbeat) a long operation"),
        reason: Optional[str] = Field(default=None, description="Optional human-readable reason, surfaced to other agents that see this lock"),
    ) -> ToolResult:
        """Try to acquire an advisory lock on one or more entities before a multi-step edit.

        Fails fast per entity: one already locked by a different, non-expired agent_id is
        reported with acquired=false plus the current owner/expiry - back off and retry
        rather than expecting the server to block and wait for you. Calling again with the
        same agent_id on a lock you already hold renews it (heartbeat).

        This is a cooperative, advisory signal - agent_id is not cryptographically
        verified. It coordinates many trusted agent instances, not adversarial tenants.
        """
        logger.info(f"MCP tool: acquire_lock ({len(entity_names)} entities, agent={agent_id})")
        try:
            result = await locking.acquire_lock(entity_names, agent_id, ttl_seconds, reason)
            return ToolResult(content=[TextContent(type="text", text=str(result))], structured_content={"result": result})
        except Neo4jError as e:
            logger.error(f"Neo4j error in acquire_lock: {e}")
            raise ToolError(f"Neo4j error in acquire_lock: {e}")
        except Exception as e:
            logger.error(f"Error in acquire_lock: {e}")
            raise ToolError(f"Error in acquire_lock: {e}")

    @mcp.tool(
        name=namespace_prefix + "release_lock",
        annotations=ToolAnnotations(title="Release Lock", readOnlyHint=False,
                                     destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def release_lock(
        entity_names: list[str] = Field(..., description="Exact entity names to unlock"),
        agent_id: str = Field(..., description="Your own identifier - must match the owner recorded by acquire_lock"),
    ) -> ToolResult:
        """Release a lock you hold. A name you don't own reports released=false and is left untouched."""
        logger.info(f"MCP tool: release_lock ({len(entity_names)} entities, agent={agent_id})")
        try:
            result = await locking.release_lock(entity_names, agent_id)
            return ToolResult(content=[TextContent(type="text", text=str(result))], structured_content={"result": result})
        except Neo4jError as e:
            logger.error(f"Neo4j error in release_lock: {e}")
            raise ToolError(f"Neo4j error in release_lock: {e}")
        except Exception as e:
            logger.error(f"Error in release_lock: {e}")
            raise ToolError(f"Error in release_lock: {e}")

    @mcp.tool(
        name=namespace_prefix + "lock_status",
        annotations=ToolAnnotations(title="Lock Status", readOnlyHint=True,
                                     destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def lock_status(
        entity_names: list[str] = Field(..., description="Exact entity names to check"),
    ) -> ToolResult:
        """Read-only lock/version state for one or more entities - is this locked, by whom, until when, current version."""
        logger.info(f"MCP tool: lock_status ({len(entity_names)} entities)")
        try:
            result = await locking.lock_status(entity_names)
            return ToolResult(content=[TextContent(type="text", text=str(result))], structured_content={"result": result})
        except Neo4jError as e:
            logger.error(f"Neo4j error in lock_status: {e}")
            raise ToolError(f"Neo4j error in lock_status: {e}")
        except Exception as e:
            logger.error(f"Error in lock_status: {e}")
            raise ToolError(f"Error in lock_status: {e}")
