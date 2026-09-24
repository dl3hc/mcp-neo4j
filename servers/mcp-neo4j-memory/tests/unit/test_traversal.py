"""Unit tests for pure-Python logic in traversal.py (no Neo4j required).

DB-backed behavior (actual get_neighbors/find_path/get_map results against a real graph)
is covered by tests/integration/test_locking_IT.py against a real Neo4j testcontainer.
"""

from mcp_neo4j_memory.traversal import (
    MAX_NEIGHBOR_DEPTH,
    MAX_PATH_DEPTH,
    Neo4jTraversal,
    _safe_relation_types,
    clamp_depth,
)


class TestClampDepth:
    def test_none_defaults(self):
        assert clamp_depth(None, MAX_NEIGHBOR_DEPTH, 2) == 2

    def test_within_range_unchanged(self):
        assert clamp_depth(3, MAX_NEIGHBOR_DEPTH, 2) == 3

    def test_above_maximum_clamped_down(self):
        assert clamp_depth(999, MAX_NEIGHBOR_DEPTH, 2) == MAX_NEIGHBOR_DEPTH

    def test_below_one_clamped_up(self):
        assert clamp_depth(0, MAX_NEIGHBOR_DEPTH, 2) == 1

    def test_path_depth_uses_its_own_cap(self):
        assert clamp_depth(999, MAX_PATH_DEPTH, 5) == MAX_PATH_DEPTH


class TestSafeRelationTypes:
    def test_valid_identifiers_pass_through(self):
        assert _safe_relation_types(["HAS_PART", "DEPENDS_ON"]) == ["HAS_PART", "DEPENDS_ON"]

    def test_none_returns_empty(self):
        assert _safe_relation_types(None) == []

    def test_empty_list_returns_empty(self):
        assert _safe_relation_types([]) == []

    def test_injection_attempt_is_dropped(self):
        # Relationship types can't be parameterized in Cypher and are embedded as literal
        # text - this is the one place that must defend against a caller trying to smuggle
        # a pattern/clause break out of the intended `[:TYPE1|TYPE2*..N]` shape.
        malicious = "X`]->(n) DETACH DELETE n //"
        assert _safe_relation_types(["HAS_PART", malicious]) == ["HAS_PART"]

    def test_all_invalid_returns_empty(self):
        assert _safe_relation_types(["has space", "has-dash"]) == []


class TestRelationshipFilter:
    def test_any_direction_no_types(self):
        assert Neo4jTraversal._relationship_filter(None, "any") == ""

    def test_any_direction_with_types(self):
        assert Neo4jTraversal._relationship_filter(["HAS_PART", "DEPENDS_ON"], "any") == "HAS_PART|DEPENDS_ON"

    def test_outgoing_no_types(self):
        assert Neo4jTraversal._relationship_filter(None, "outgoing") == ">"

    def test_outgoing_with_types(self):
        assert Neo4jTraversal._relationship_filter(["HAS_PART"], "outgoing") == "HAS_PART>"

    def test_incoming_no_types(self):
        assert Neo4jTraversal._relationship_filter(None, "incoming") == "<"

    def test_incoming_with_types(self):
        assert Neo4jTraversal._relationship_filter(["HAS_PART"], "incoming") == "<HAS_PART"
