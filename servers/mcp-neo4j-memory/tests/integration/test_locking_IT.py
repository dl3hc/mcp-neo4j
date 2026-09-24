"""Integration tests for locking, optimistic concurrency, traversal, and write-side guards.

Follows the same fixture pattern as test_neo4j_memory_IT.py: a local `neo4j_driver`
fixture connects to NEO4J_URI (set by conftest.py's autouse `setup` testcontainer
fixture) and skips if no Neo4j is reachable. Requires Docker (via testcontainers) or a
locally running Neo4j with the APOC plugin.
"""

import asyncio
import os

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase

from mcp_neo4j_memory.guards import GuardViolation
from mcp_neo4j_memory.locking import Neo4jLocking
from mcp_neo4j_memory.neo4j_memory import Entity, Neo4jMemory, ObservationAddition, Relation
from mcp_neo4j_memory.traversal import Neo4jTraversal


def get_neo4j_driver():
    uri = os.environ.get("NEO4J_URI", "neo4j://localhost:7687")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "password")
    return AsyncGraphDatabase.driver(uri, auth=(user, password))


@pytest_asyncio.fixture(scope="function")
async def neo4j_driver():
    driver = get_neo4j_driver()
    try:
        await driver.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Could not connect to Neo4j: {e}")
    yield driver
    async with driver.session() as session:
        await session.run("MATCH (n:Memory) DETACH DELETE n")
    await driver.close()


@pytest_asyncio.fixture(scope="function")
async def memory(neo4j_driver) -> Neo4jMemory:
    mem = Neo4jMemory(neo4j_driver, enforce_locks=True)
    await mem.create_fulltext_index()
    await mem.ensure_constraints()
    return mem


@pytest_asyncio.fixture(scope="function")
async def locking(neo4j_driver) -> Neo4jLocking:
    return Neo4jLocking(neo4j_driver)


@pytest_asyncio.fixture(scope="function")
async def traversal(neo4j_driver) -> Neo4jTraversal:
    return Neo4jTraversal(neo4j_driver)


# --- A) Locking ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acquire_lock_on_nonexistent_entity_reports_not_acquired(locking: Neo4jLocking):
    result = await locking.acquire_lock(["DoesNotExist"], "agent-1")
    assert result == [{"name": "DoesNotExist", "acquired": False, "owner": None, "expiresAt": None, "version": None}]


@pytest.mark.asyncio
async def test_second_agent_cannot_acquire_active_lock(memory: Neo4jMemory, locking: Neo4jLocking):
    await memory.create_entities([Entity(name="Contested", type="thing", observations=[])])

    first = await locking.acquire_lock(["Contested"], "agent-1", ttl_seconds=30)
    assert first[0]["acquired"] is True
    assert first[0]["owner"] == "agent-1"

    second = await locking.acquire_lock(["Contested"], "agent-2", ttl_seconds=30)
    assert second[0]["acquired"] is False
    assert second[0]["owner"] == "agent-1"


@pytest.mark.asyncio
async def test_concurrent_acquire_only_one_winner(memory: Neo4jMemory, locking: Neo4jLocking):
    """Two real concurrent acquire_lock calls on the same node - Neo4j's own per-node
    write serialization must ensure exactly one of them wins, never both."""
    await memory.create_entities([Entity(name="HotNode", type="thing", observations=[])])

    results = await asyncio.gather(
        locking.acquire_lock(["HotNode"], "agent-a", ttl_seconds=30),
        locking.acquire_lock(["HotNode"], "agent-b", ttl_seconds=30),
    )
    acquired_flags = [r[0]["acquired"] for r in results]
    assert acquired_flags.count(True) == 1
    assert acquired_flags.count(False) == 1


@pytest.mark.asyncio
async def test_same_agent_can_renew_own_lock(memory: Neo4jMemory, locking: Neo4jLocking):
    await memory.create_entities([Entity(name="Renewable", type="thing", observations=[])])
    first = await locking.acquire_lock(["Renewable"], "agent-1", ttl_seconds=5)
    second = await locking.acquire_lock(["Renewable"], "agent-1", ttl_seconds=30)
    assert first[0]["acquired"] is True
    assert second[0]["acquired"] is True
    assert second[0]["expiresAt"] >= first[0]["expiresAt"]


@pytest.mark.asyncio
async def test_expired_lock_is_auto_overwritten(memory: Neo4jMemory, locking: Neo4jLocking):
    await memory.create_entities([Entity(name="Expiring", type="thing", observations=[])])
    await locking.acquire_lock(["Expiring"], "agent-1", ttl_seconds=5)

    # Simulate expiry without sleeping: push the recorded expiry into the past directly.
    await locking.driver.execute_query(
        "MATCH (e:Memory {name: 'Expiring'}) SET e._lock_expires_at = timestamp() - 1000"
    )

    second = await locking.acquire_lock(["Expiring"], "agent-2", ttl_seconds=30)
    assert second[0]["acquired"] is True
    assert second[0]["owner"] == "agent-2"


@pytest.mark.asyncio
async def test_release_lock_only_by_owner(memory: Neo4jMemory, locking: Neo4jLocking):
    await memory.create_entities([Entity(name="Releasable", type="thing", observations=[])])
    await locking.acquire_lock(["Releasable"], "agent-1")

    wrong_owner = await locking.release_lock(["Releasable"], "agent-2")
    assert wrong_owner[0]["released"] is False

    right_owner = await locking.release_lock(["Releasable"], "agent-1")
    assert right_owner[0]["released"] is True

    status = await locking.lock_status(["Releasable"])
    assert status[0]["locked"] is False


@pytest.mark.asyncio
async def test_lock_status_reports_owner_and_expiry(memory: Neo4jMemory, locking: Neo4jLocking):
    await memory.create_entities([Entity(name="Watched", type="thing", observations=[])])
    await locking.acquire_lock(["Watched"], "agent-1", ttl_seconds=30, reason="editing")

    status = await locking.lock_status(["Watched", "DoesNotExist"])
    by_name = {s["name"]: s for s in status}
    assert by_name["Watched"]["locked"] is True
    assert by_name["Watched"]["owner"] == "agent-1"
    assert by_name["Watched"]["exists"] is True
    assert by_name["DoesNotExist"]["exists"] is False


# --- B) Optimistic concurrency + write guards -------------------------------------

@pytest.mark.asyncio
async def test_locked_entity_blocks_add_observations_when_enforced(memory: Neo4jMemory, locking: Neo4jLocking):
    await memory.create_entities([Entity(name="Guarded", type="thing", observations=["seed"])])
    await locking.acquire_lock(["Guarded"], "owner-agent")

    with pytest.raises(GuardViolation) as exc_info:
        await memory.add_observations([ObservationAddition(entityName="Guarded", observations=["new fact"])], agent_id="someone-else")
    assert exc_info.value.blocked[0]["entityName"] == "Guarded"

    # The lock owner itself is unaffected.
    applied, warnings = await memory.add_observations(
        [ObservationAddition(entityName="Guarded", observations=["owner's fact"])], agent_id="owner-agent"
    )
    assert applied[0]["addedObservations"] == ["owner's fact"]


@pytest.mark.asyncio
async def test_soft_mode_logs_but_does_not_block(neo4j_driver):
    """enforce_locks=False (the default): a locked entity's write still goes through."""
    soft_memory = Neo4jMemory(neo4j_driver, enforce_locks=False)
    await soft_memory.create_entities([Entity(name="SoftGuarded", type="thing", observations=[])])
    locking = Neo4jLocking(neo4j_driver)
    await locking.acquire_lock(["SoftGuarded"], "owner-agent")

    applied, warnings = await soft_memory.add_observations(
        [ObservationAddition(entityName="SoftGuarded", observations=["slipped through"])], agent_id="intruder"
    )
    assert applied[0]["addedObservations"] == ["slipped through"]


@pytest.mark.asyncio
async def test_expected_version_mismatch_rejected_match_applied(memory: Neo4jMemory):
    await memory.create_entities([Entity(name="Versioned", type="thing", observations=["v0"])])

    with pytest.raises(GuardViolation):
        await memory.add_observations([
            ObservationAddition(entityName="Versioned", observations=["stale write"], expectedVersion=99)
        ])

    applied, _ = await memory.add_observations([
        ObservationAddition(entityName="Versioned", observations=["fresh write"], expectedVersion=1)
    ])
    assert applied[0]["addedObservations"] == ["fresh write"]


@pytest.mark.asyncio
async def test_create_entities_merges_observations_not_overwrites(memory: Neo4jMemory):
    """Regression test for the pre-existing upstream bug this fork fixes: re-creating an
    entity with the same name used to silently discard its prior observations."""
    await memory.create_entities([Entity(name="Merged", type="thing", observations=["first"])])
    await memory.create_entities([Entity(name="Merged", type="thing", observations=["second"])])

    graph = await memory.find_memories_by_name(["Merged"])
    assert set(graph.entities[0].observations) == {"first", "second"}


@pytest.mark.asyncio
async def test_blocked_new_entity_leaves_no_stub_node(memory: Neo4jMemory):
    """A brand-new entity blocked by an (unusual) expectedVersion mismatch must not leave
    an empty :Memory stub behind - it would fail Entity's own validation on next read."""
    with pytest.raises(GuardViolation):
        await memory.create_entities([
            Entity(name="NeverExisted", type="thing", observations=["x"], expectedVersion=99)
        ])

    graph = await memory.find_memories_by_name(["NeverExisted"])
    assert graph.entities == []


@pytest.mark.asyncio
async def test_self_loop_relation_rejected(memory: Neo4jMemory):
    await memory.create_entities([Entity(name="Solo", type="thing", observations=["x"])])
    with pytest.raises(GuardViolation) as exc_info:
        await memory.create_relations([Relation(source="Solo", target="Solo", relationType="DEPENDS_ON")])
    assert "self-loop" in exc_info.value.blocked[0]["reason"]


@pytest.mark.asyncio
async def test_dangling_target_relation_rejected(memory: Neo4jMemory):
    await memory.create_entities([
        Entity(name="HasContent", type="thing", observations=["something"]),
        Entity(name="Empty", type="thing", observations=[]),
    ])
    with pytest.raises(GuardViolation) as exc_info:
        await memory.create_relations([Relation(source="HasContent", target="Empty", relationType="DEPENDS_ON")])
    assert "dangling" in exc_info.value.blocked[0]["reason"]


@pytest.mark.asyncio
async def test_related_to_flagged_as_warning_not_rejected(memory: Neo4jMemory):
    await memory.create_entities([
        Entity(name="A", type="thing", observations=["x"]),
        Entity(name="B", type="thing", observations=["y"]),
    ])
    applied, warnings = await memory.create_relations([Relation(source="A", target="B", relationType="RELATED_TO")])
    assert len(applied) == 1
    assert any("RELATED_TO" in w["warning"] for w in warnings)


@pytest.mark.asyncio
async def test_dedup_hint_on_near_duplicate_name(memory: Neo4jMemory):
    await memory.create_entities([Entity(name="Alice Johnson", type="person", observations=["x"])])
    applied, warnings = await memory.create_entities([
        Entity(name="Alice Johnsen", type="person", observations=["y"])
    ])
    assert len(applied) == 1  # soft warning only, never blocks
    assert any("dedupHint" in w and "Alice Johnson" in w["dedupHint"] for w in warnings)


@pytest.mark.asyncio
async def test_no_dedup_hint_for_dissimilar_names(memory: Neo4jMemory):
    await memory.create_entities([Entity(name="Alice Johnson", type="person", observations=["x"])])
    applied, warnings = await memory.create_entities([
        Entity(name="Completely Different Concept", type="concept", observations=["y"])
    ])
    assert not any("dedupHint" in w for w in warnings)


@pytest.mark.asyncio
async def test_no_dedup_hint_on_update_to_existing_entity(memory: Neo4jMemory):
    """Re-creating (merging into) an existing exact-name entity is not a 'new' entity -
    it must never warn about being similar to itself."""
    await memory.create_entities([Entity(name="Alice Johnson", type="person", observations=["x"])])
    applied, warnings = await memory.create_entities([
        Entity(name="Alice Johnson", type="person", observations=["y"])
    ])
    assert not any("dedupHint" in w for w in warnings)


@pytest.mark.asyncio
async def test_decompose_hint_appears_past_threshold(memory: Neo4jMemory):
    await memory.create_entities([Entity(name="Overloaded", type="thing", observations=["o1", "o2", "o3"])])
    applied, warnings = await memory.add_observations([
        ObservationAddition(entityName="Overloaded", observations=["o4", "o5"])
    ])
    assert any("decomposeHint" in w for w in warnings)


# --- C) Traversal --------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def linear_graph(memory: Neo4jMemory):
    """A -HAS_PART-> B -HAS_PART-> C -DEPENDS_ON-> D (D isolated from A/B by type filter)."""
    await memory.create_entities([
        Entity(name="A", type="thing", observations=["a"]),
        Entity(name="B", type="thing", observations=["b"]),
        Entity(name="C", type="thing", observations=["c"]),
        Entity(name="D", type="thing", observations=["d"]),
    ])
    await memory.create_relations([
        Relation(source="A", target="B", relationType="HAS_PART"),
        Relation(source="B", target="C", relationType="HAS_PART"),
        Relation(source="C", target="D", relationType="DEPENDS_ON"),
    ])


@pytest.mark.asyncio
async def test_get_neighbors_depth_bounded(traversal: Neo4jTraversal, linear_graph):
    result = await traversal.get_neighbors(["A"], depth=1)
    assert result[0]["found"] is True
    names = {n["name"] for n in result[0]["nodes"]}
    assert names == {"B"}  # depth=1 from A only reaches B, not C or D


@pytest.mark.asyncio
async def test_get_neighbors_missing_seed_reported(traversal: Neo4jTraversal, linear_graph):
    result = await traversal.get_neighbors(["A", "Ghost"], depth=1)
    by_seed = {r["seed"]: r for r in result}
    assert by_seed["Ghost"]["found"] is False
    assert by_seed["Ghost"]["nodes"] == []


@pytest.mark.asyncio
async def test_get_neighbors_depth_is_capped_server_side(traversal: Neo4jTraversal, linear_graph):
    from mcp_neo4j_memory.traversal import MAX_NEIGHBOR_DEPTH
    result = await traversal.get_neighbors(["A"], depth=999)
    names = {n["name"] for n in result[0]["nodes"]}
    # With depth capped at MAX_NEIGHBOR_DEPTH (>= 3), the whole chain must be reachable.
    assert MAX_NEIGHBOR_DEPTH >= 3
    assert {"B", "C", "D"}.issubset(names)


@pytest.mark.asyncio
async def test_find_path_returns_ordered_typed_edges(traversal: Neo4jTraversal, linear_graph):
    result = await traversal.find_path("A", "D")
    assert result["found"] is True
    assert result["length"] == 3
    edge_types = [r["relationType"] for r in result["relations"]]
    assert edge_types == ["HAS_PART", "HAS_PART", "DEPENDS_ON"]


@pytest.mark.asyncio
async def test_find_path_missing_endpoint_reported(traversal: Neo4jTraversal, linear_graph):
    result = await traversal.find_path("A", "Ghost")
    assert result["found"] is False
    assert result["fromFound"] is True
    assert result["toFound"] is False


@pytest.mark.asyncio
async def test_find_path_respects_edge_type_filter(traversal: Neo4jTraversal, linear_graph):
    # Restricting to HAS_PART only must not find a path all the way to D (needs DEPENDS_ON).
    result = await traversal.find_path("A", "D", edge_types=["HAS_PART"])
    assert result["found"] is False


@pytest.mark.asyncio
async def test_get_map_returns_structure_without_observations(traversal: Neo4jTraversal, linear_graph):
    result = await traversal.get_map()
    names = {n["name"] for n in result["nodes"]}
    assert {"A", "B", "C", "D"}.issubset(names)
    assert all("observations" not in n for n in result["nodes"])
    relation_types = {r["relationType"] for r in result["relations"]}
    assert relation_types == {"HAS_PART", "DEPENDS_ON"}


# --- D) Constraint / index setup -----------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_constraints_creates_uniqueness_constraint(memory: Neo4jMemory, neo4j_driver):
    result = await neo4j_driver.execute_query("SHOW CONSTRAINTS YIELD name WHERE name = 'memory_name_unique' RETURN count(*) AS c")
    assert result.records[0]["c"] == 1
