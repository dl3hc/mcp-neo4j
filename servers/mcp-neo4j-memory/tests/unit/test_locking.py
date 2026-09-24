"""Unit tests for pure-Python logic in locking.py and guards.py (no Neo4j required).

DB-backed behavior (actual lock contention, TTL expiry, CAS enforcement) is covered by
tests/integration/test_locking_IT.py against a real Neo4j testcontainer.
"""

from mcp_neo4j_memory import guards
from mcp_neo4j_memory.locking import DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, MIN_TTL_SECONDS, clamp_ttl


class TestClampTtl:
    def test_none_defaults(self):
        assert clamp_ttl(None) == DEFAULT_TTL_SECONDS

    def test_within_range_unchanged(self):
        assert clamp_ttl(60) == 60

    def test_below_minimum_clamped_up(self):
        assert clamp_ttl(1) == MIN_TTL_SECONDS

    def test_above_maximum_clamped_down(self):
        assert clamp_ttl(10_000) == MAX_TTL_SECONDS

    def test_boundaries_are_inclusive(self):
        assert clamp_ttl(MIN_TTL_SECONDS) == MIN_TTL_SECONDS
        assert clamp_ttl(MAX_TTL_SECONDS) == MAX_TTL_SECONDS


class TestSelfLoop:
    def test_same_name_is_self_loop(self):
        assert guards.is_self_loop("Alice", "Alice") is True

    def test_different_names_not_self_loop(self):
        assert guards.is_self_loop("Alice", "Bob") is False


class TestRelationTypeWarning:
    def test_known_type_no_warning(self):
        assert guards.relation_type_warning("HAS_PART") is None

    def test_related_to_flagged(self):
        warning = guards.relation_type_warning("RELATED_TO")
        assert warning is not None
        assert "RELATED_TO" in warning

    def test_off_vocabulary_type_flagged(self):
        warning = guards.relation_type_warning("SOME_MADE_UP_TYPE")
        assert warning is not None
        assert "SOME_MADE_UP_TYPE" in warning

    def test_every_schema_type_is_recognized(self):
        # Guards against silent drift between guards.py and neo4j-schema.md's vocabulary.
        for relation_type in guards.KNOWN_RELATION_TYPES:
            assert guards.relation_type_warning(relation_type) is None


class TestDecomposeHint:
    def test_below_threshold_no_hint(self):
        assert guards.decompose_hint(guards.DECOMPOSE_OBSERVATION_THRESHOLD - 1) is None

    def test_at_threshold_hints(self):
        hint = guards.decompose_hint(guards.DECOMPOSE_OBSERVATION_THRESHOLD)
        assert hint is not None
        assert str(guards.DECOMPOSE_OBSERVATION_THRESHOLD) in hint

    def test_zero_observations_no_hint(self):
        assert guards.decompose_hint(0) is None


class TestDedupHint:
    def test_no_matches_no_hint(self):
        assert guards.dedup_hint([]) is None

    def test_best_match_used(self):
        hint = guards.dedup_hint([
            {"name": "Alice Johnson", "type": "person", "similarity": 0.94},
            {"name": "Alicia Johnson", "type": "person", "similarity": 0.91},
        ])
        assert hint is not None
        assert "Alice Johnson" in hint
        assert "0.94" in hint


class TestGuardClauses:
    def test_lock_guard_clause_references_var_and_agent_param(self):
        clause = guards.lock_guard_clause("e")
        assert "e._lock_owner" in clause
        assert "e._lock_expires_at" in clause
        assert "$agent_id" in clause

    def test_dangling_target_clause_references_var(self):
        clause = guards.dangling_target_clause("to")
        assert "to.observations" in clause

    def test_enforce_gate_no_conditions_is_always_true_when_enforced(self):
        gate = guards.enforce_gate()
        assert "true" in gate
        assert "$enforce_locks" in gate

    def test_enforce_gate_combines_conditions_with_and(self):
        gate = guards.enforce_gate("a", "b")
        assert "(a)" in gate
        assert "(b)" in gate
        assert " AND " in gate


class TestGuardViolation:
    def test_carries_applied_blocked_and_warnings(self):
        violation = guards.GuardViolation(applied=["ok"], blocked=[{"name": "bad"}], warnings=[{"x": 1}])
        assert violation.applied == ["ok"]
        assert violation.blocked == [{"name": "bad"}]
        assert violation.warnings == [{"x": 1}]
        assert "bad" in str(violation) or "1" in str(violation) or len(violation.blocked) == 1

    def test_defaults_warnings_to_empty_list(self):
        violation = guards.GuardViolation(applied=[], blocked=[{"name": "x"}])
        assert violation.warnings == []
