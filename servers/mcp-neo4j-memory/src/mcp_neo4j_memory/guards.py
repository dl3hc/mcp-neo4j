import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger('mcp_neo4j_memory')
logger.setLevel(logging.INFO)

# Canonical relation vocabulary, mirrored from .claude/rules/neo4j-schema.md. This is a
# soft-typing-debt signal, not a hard allowlist - the schema doc explicitly invites new
# specific types ("add here before using"), so an off-vocabulary type is flagged in the
# response, never rejected outright. Keep this in sync with neo4j-schema.md by hand.
KNOWN_RELATION_TYPES = frozenset({
    "HAS_PART", "PART_OF", "DEPENDS_ON", "CAUSES", "PRECEDES", "OWNS", "USES",
    "CONNECTS_TO", "WORKS_ON", "LEADS", "PREFERS", "HAS_FACT", "HAS_PREFERENCE",
    "HAS_MESSAGE", "MENTIONS", "LOCATED_IN",
})

# entity-model.md: "~5 obs on a node = strong signal to apply the query-independence test."
DECOMPOSE_OBSERVATION_THRESHOLD = 5

# Jaro-Winkler similarity bar for the fuzzy-duplicate warning on newly created entities.
# Matches the Dream Engine canonicalizer's own char-similarity bar for offline merging
# (char-similarity >= 0.90, same entity_type) - same number, live write-time warning instead
# of an offline consolidation pass.
DEDUP_SIMILARITY_THRESHOLD = 0.90


class GuardViolation(Exception):
    """Raised when one or more items in a write batch were blocked by a guard.

    Carries both the items that *did* succeed (already committed - there is no
    cross-item transaction, consistent with the pre-existing "no bulk atomicity" MCP
    contract) and the ones that were blocked, so the caller can report both instead of
    silently swallowing the partial failure.
    """

    def __init__(self, applied: List[Any], blocked: List[Dict[str, Any]], warnings: Optional[List[Dict[str, Any]]] = None):
        self.applied = applied
        self.blocked = blocked
        self.warnings = warnings or []
        super().__init__(f"{len(blocked)} item(s) blocked: {blocked}")


def is_self_loop(source: str, target: str) -> bool:
    return source == target


def relation_type_warning(relation_type: str) -> Optional[str]:
    """Typing-debt warning for RELATED_TO or any type outside the curated vocabulary. None if clean."""
    if relation_type == "RELATED_TO":
        return "RELATED_TO is a last-resort type - pick a specific type from neo4j-schema.md if one fits"
    if relation_type not in KNOWN_RELATION_TYPES:
        return (
            f"'{relation_type}' is not in the curated vocabulary (neo4j-schema.md) - "
            "add it there if it is a genuinely new relation shape"
        )
    return None


def decompose_hint(observation_count: int) -> Optional[str]:
    """Soft nudge (never a hard block) once a node crosses the entity-model.md decompose signal."""
    if observation_count >= DECOMPOSE_OBSERVATION_THRESHOLD:
        return (
            f"{observation_count} observations on this entity - apply the query-independence "
            "test (entity-model.md): would a query for one of these also need the others? "
            "If not, consider decomposing into HAS_PART children."
        )
    return None


def dedup_hint(matches: List[Dict[str, Any]]) -> Optional[str]:
    """Soft nudge when a newly created entity's name is suspiciously close to an existing one.

    Never a hard block - two genuinely distinct concepts can have similar names, and only the
    calling model has the context to know whether this is the same thing under a slightly
    different name or a real, separate entity.
    """
    if not matches:
        return None
    best = matches[0]
    return (
        f"name is {best['similarity']:.2f} similar to existing entity '{best['name']}' "
        f"(type={best['type']}) - if this is the same concept, use add_observations on the "
        "existing entity instead of creating a near-duplicate"
    )


def lock_guard_clause(var: str) -> str:
    """Cypher boolean expression: True if `var` is unlocked or locked by the requesting agent.

    Embed into a WITH clause; the query must supply $agent_id as a parameter.
    """
    return (
        f"({var}._lock_owner IS NULL OR {var}._lock_expires_at < timestamp() "
        f"OR {var}._lock_owner = $agent_id)"
    )


def dangling_target_clause(var: str) -> str:
    """Cypher boolean expression: True if `var` carries at least one observation.

    Enforces the pre-existing neo4j-schema.md rule "never link to an entity with no
    observations and no summary - populate the target first." Always enforced, regardless
    of the lock-enforcement rollout flag - this is a data-integrity rule, not a
    concurrency-safety one.
    """
    return f"(size(coalesce({var}.observations, [])) > 0)"


def enforce_gate(*conditions: str, enforce_param: str = "$enforce_locks") -> str:
    """Combine lock/version guard conditions with the enforcement rollout flag.

    While NEO4J_MEMORY_ENFORCE_LOCKS is false (default), the write always proceeds - the
    underlying `conditions` are still computed and returned so the caller can log what
    *would* have been blocked once hard enforcement is switched on.
    """
    joined = " AND ".join(f"({c})" for c in conditions) if conditions else "true"
    return f"({enforce_param} = false OR ({joined}))"
