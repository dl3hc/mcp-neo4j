import logging
from typing import Any, Dict, List, Optional

from neo4j import AsyncDriver, RoutingControl
from pydantic import BaseModel, Field

from . import guards

# Set up logging
logger = logging.getLogger('mcp_neo4j_memory')
logger.setLevel(logging.INFO)

# Models for our knowledge graph
class Entity(BaseModel):
    """Represents a memory entity in the knowledge graph.

    Example:
    {
        "name": "John Smith",
        "type": "person",
        "observations": ["Works at Neo4j", "Lives in San Francisco", "Expert in graph databases"]
    }
    """
    name: str = Field(
        description="Unique identifier/name for the entity. Should be descriptive and specific.",
        min_length=1,
        examples=["John Smith", "Neo4j Inc", "San Francisco"]
    )
    type: str = Field(
        description="Category or classification of the entity. Common types: 'person', 'company', 'location', 'concept', 'event'",
        min_length=1,
        examples=["person", "company", "location", "concept", "event"],
        pattern=r'^[A-Za-z_][A-Za-z0-9_]*$'
    )
    observations: List[str] = Field(
        description="List of facts, observations, or notes about this entity. Each observation should be a complete, standalone fact.",
        examples=[["Works at Neo4j", "Lives in San Francisco"], ["Headquartered in Sweden", "Graph database company"]]
    )
    expectedVersion: Optional[int] = Field(
        default=None,
        description="Optimistic-concurrency guard: if set, the write is rejected unless the entity's current _version matches. Ignored on read responses."
    )

class Relation(BaseModel):
    """Represents a relationship between two entities in the knowledge graph.

    Example:
    {
        "source": "John Smith",
        "target": "Neo4j Inc",
        "relationType": "WORKS_AT"
    }
    """
    source: str = Field(
        description="Name of the source entity (must match an existing entity name exactly)",
        min_length=1,
        examples=["John Smith", "Neo4j Inc"]
    )
    target: str = Field(
        description="Name of the target entity (must match an existing entity name exactly)",
        min_length=1,
        examples=["Neo4j Inc", "San Francisco"]
    )
    relationType: str = Field(
        description="Type of relationship between source and target. Use descriptive, uppercase names with underscores.",
        min_length=1,
        examples=["WORKS_AT", "LIVES_IN", "MANAGES", "COLLABORATES_WITH", "LOCATED_IN"],
        pattern=r'^[A-Za-z_][A-Za-z0-9_]*$'
    )

class KnowledgeGraph(BaseModel):
    """Complete knowledge graph containing entities and their relationships."""
    entities: List[Entity] = Field(
        description="List of all entities in the knowledge graph",
        default=[]
    )
    relations: List[Relation] = Field(
        description="List of all relationships between entities",
        default=[]
    )

class ObservationAddition(BaseModel):
    """Request to add new observations to an existing entity.

    Example:
    {
        "entityName": "John Smith",
        "observations": ["Recently promoted to Senior Engineer", "Speaks fluent German"]
    }
    """
    entityName: str = Field(
        description="Exact name of the existing entity to add observations to",
        min_length=1,
        examples=["John Smith", "Neo4j Inc"]
    )
    observations: List[str] = Field(
        description="New observations/facts to add to the entity. Each should be unique and informative.",
        min_length=1
    )
    expectedVersion: Optional[int] = Field(
        default=None,
        description="Optimistic-concurrency guard: if set, the write is rejected unless the entity's current _version matches."
    )

class ObservationDeletion(BaseModel):
    """Request to delete specific observations from an existing entity.

    Example:
    {
        "entityName": "John Smith",
        "observations": ["Old job title", "Outdated contact info"]
    }
    """
    entityName: str = Field(
        description="Exact name of the existing entity to remove observations from",
        min_length=1,
        examples=["John Smith", "Neo4j Inc"]
    )
    observations: List[str] = Field(
        description="Exact observation texts to delete from the entity (must match existing observations exactly)",
        min_length=1
    )
    expectedVersion: Optional[int] = Field(
        default=None,
        description="Optimistic-concurrency guard: if set, the write is rejected unless the entity's current _version matches."
    )

class Neo4jMemory:
    def __init__(self, neo4j_driver: AsyncDriver, enforce_locks: bool = False):
        self.driver = neo4j_driver
        # Rollout flag (plan E): while False, guard violations are computed and reported
        # but never block the write - flip once lock behavior has been observed across
        # projects without surprises.
        self.enforce_locks = enforce_locks

    async def create_fulltext_index(self):
        """Create a fulltext search index for entities if it doesn't exist."""
        try:
            query = "CREATE FULLTEXT INDEX search IF NOT EXISTS FOR (m:Memory) ON EACH [m.name, m.type, m.observations];"
            await self.driver.execute_query(query, routing_control=RoutingControl.WRITE)
            logger.info("Created fulltext search index")
        except Exception as e:
            # Index might already exist, which is fine
            logger.debug(f"Fulltext index creation: {e}")

    async def ensure_constraints(self):
        """Create the :Memory(name) uniqueness constraint and the lock-expiry index.

        Without the uniqueness constraint, concurrent create_entities calls racing on the
        same new name can both pass MERGE's match-miss and create two nodes with the same
        name - the constraint makes that impossible at the storage layer instead of
        relying on application-level care.
        """
        try:
            await self.driver.execute_query(
                "CREATE CONSTRAINT memory_name_unique IF NOT EXISTS FOR (m:Memory) REQUIRE m.name IS UNIQUE",
                routing_control=RoutingControl.WRITE,
            )
            await self.driver.execute_query(
                "CREATE INDEX memory_lock_expiry IF NOT EXISTS FOR (m:Memory) ON (m._lock_expires_at)",
                routing_control=RoutingControl.WRITE,
            )
            logger.info("Ensured :Memory(name) uniqueness constraint and lock-expiry index")
        except Exception as e:
            logger.debug(f"Constraint/index creation: {e}")

    async def load_graph(self, filter_query: str = "*"):
        """Load the entire knowledge graph from Neo4j."""
        logger.info("Loading knowledge graph from Neo4j")
        query = """
            CALL db.index.fulltext.queryNodes('search', $filter) yield node as entity, score
            OPTIONAL MATCH (entity)-[r]-(other)
            RETURN collect(distinct {
                name: entity.name,
                type: entity.type,
                observations: entity.observations
            }) as nodes,
            collect(distinct {
                source: startNode(r).name,
                target: endNode(r).name,
                relationType: type(r)
            }) as relations
        """

        result = await self.driver.execute_query(query, {"filter": filter_query}, routing_control=RoutingControl.READ)

        if not result.records:
            return KnowledgeGraph(entities=[], relations=[])

        record = result.records[0]
        nodes = record.get('nodes', list())
        rels = record.get('relations', list())

        entities = [
            Entity(
                name=node['name'],
                type=node['type'],
                observations=node.get('observations', list())
            )
            for node in nodes if node.get('name')
        ]

        relations = [
            Relation(
                source=rel['source'],
                target=rel['target'],
                relationType=rel['relationType']
            )
            for rel in rels if rel.get('relationType')
        ]

        logger.debug(f"Loaded entities: {entities}")
        logger.debug(f"Loaded relations: {relations}")

        return KnowledgeGraph(entities=entities, relations=relations)

    async def _find_fuzzy_duplicates(self, name: str) -> List[Dict[str, Any]]:
        """Near-duplicate names for a just-created entity, via APOC Jaro-Winkler similarity.

        Read-only, best-effort: scans all :Memory nodes (fine at today's graph size; would
        need an ANN/embedding index instead of a full scan if the graph grows to the point
        this becomes a real cost - not needed yet).
        """
        query = """
        MATCH (other:Memory)
        WHERE other.name <> $name
        WITH other, apoc.text.jaroWinklerDistance(other.name, $name) AS similarity
        WHERE similarity >= $threshold
        RETURN other.name AS name, other.type AS type, similarity
        ORDER BY similarity DESC
        LIMIT 3
        """
        result = await self.driver.execute_query(
            query,
            {"name": name, "threshold": guards.DEDUP_SIMILARITY_THRESHOLD},
            routing_control=RoutingControl.READ,
        )
        return [
            {"name": r.get("name"), "type": r.get("type"), "similarity": r.get("similarity")}
            for r in result.records
        ]

    async def create_entities(self, entities: List[Entity], agent_id: Optional[str] = None) -> tuple[List[Entity], List[Dict[str, Any]]]:
        """Create multiple new entities in the knowledge graph.

        Raises GuardViolation if any entity is blocked by an active foreign lock or a
        version mismatch (enforce_locks=True) - applied entities are still committed
        (no cross-item transaction).
        """
        logger.info(f"Creating {len(entities)} entities")
        applied: List[Entity] = []
        blocked: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        for entity in entities:
            gate = guards.enforce_gate("gateOk")
            query = f"""
            WITH $entity as entity
            MERGE (e:Memory {{ name: entity.name }})
            ON CREATE SET e._version = 0, e._justCreated = true
            WITH e, entity, coalesce(e._justCreated, false) AS wasCreated
            REMOVE e._justCreated
            WITH e, entity, wasCreated,
                 {guards.lock_guard_clause("e")} AS unlocked,
                 ($expected_version IS NULL OR coalesce(e._version,0) = $expected_version) AS versionOk,
                 e._lock_owner AS ownerAtCheck,
                 e._lock_expires_at AS expiresAtCheck,
                 e._version AS versionAtCheck
            WITH e, entity, wasCreated, unlocked, versionOk, ownerAtCheck, expiresAtCheck, versionAtCheck,
                 (unlocked AND versionOk) AS gateOk,
                 [o IN entity.observations WHERE NOT o IN coalesce(e.observations, [])] AS newObs
            WITH e, entity, wasCreated, unlocked, versionOk, ownerAtCheck, expiresAtCheck, versionAtCheck,
                 gateOk, newObs,
                 (size(coalesce(e.observations, [])) + size(newObs)) AS projectedObservationCount
            FOREACH (_ IN CASE WHEN {gate} THEN [1] ELSE [] END |
              SET e.observations = coalesce(e.observations, []) + newObs,
                  e._version = coalesce(e._version, 0) + 1,
                  e.type = entity.type
            )
            FOREACH (_ IN CASE WHEN {gate} THEN [1] ELSE [] END | SET e:`{entity.type}`)
            FOREACH (_ IN CASE WHEN NOT ({gate}) AND wasCreated THEN [1] ELSE [] END | DETACH DELETE e)
            RETURN entity.name AS name,
                   ({gate}) AS wouldApply,
                   wasCreated AS wasCreated,
                   projectedObservationCount AS observationCount,
                   CASE WHEN NOT unlocked THEN ownerAtCheck END AS blockedByOwner,
                   CASE WHEN NOT unlocked THEN expiresAtCheck END AS blockedUntil,
                   CASE WHEN NOT versionOk THEN versionAtCheck END AS currentVersion
            """
            result = await self.driver.execute_query(
                query,
                {
                    "entity": entity.model_dump(exclude={"expectedVersion"}),
                    "agent_id": agent_id,
                    "expected_version": entity.expectedVersion,
                    "enforce_locks": self.enforce_locks,
                },
                routing_control=RoutingControl.WRITE,
            )
            record = result.records[0]
            if record.get("wouldApply") or not self.enforce_locks:
                applied.append(entity)
                hint = guards.decompose_hint(record.get("observationCount") or len(entity.observations))
                if hint:
                    warnings.append({"entity": entity.name, "decomposeHint": hint})
                if record.get("wasCreated"):
                    dupes = await self._find_fuzzy_duplicates(entity.name)
                    hint = guards.dedup_hint(dupes)
                    if hint:
                        warnings.append({"entity": entity.name, "dedupHint": hint})
            else:
                blocked.append({
                    "name": entity.name,
                    "blockedByOwner": record.get("blockedByOwner"),
                    "blockedUntil": record.get("blockedUntil"),
                    "currentVersion": record.get("currentVersion"),
                })
            if not record.get("wouldApply"):
                logger.warning(f"create_entities: guard would have blocked '{entity.name}' (enforce_locks={self.enforce_locks})")

        if blocked:
            raise guards.GuardViolation(applied=applied, blocked=blocked, warnings=warnings)
        return applied, warnings

    async def create_relations(self, relations: List[Relation], agent_id: Optional[str] = None) -> tuple[List[Relation], List[Dict[str, Any]]]:
        """Create multiple new relations between entities."""
        logger.info(f"Creating {len(relations)} relations")
        applied: List[Relation] = []
        blocked: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        for relation in relations:
            if guards.is_self_loop(relation.source, relation.target):
                blocked.append({
                    "source": relation.source, "target": relation.target,
                    "relationType": relation.relationType, "reason": "self-loop rejected",
                })
                continue

            type_warning = guards.relation_type_warning(relation.relationType)
            if type_warning:
                warnings.append({"relation": relation.model_dump(), "warning": type_warning})

            gate = guards.enforce_gate(
                guards.lock_guard_clause("from"), guards.lock_guard_clause("to"),
            )
            query = f"""
            WITH $relation as relation
            MATCH (from:Memory),(to:Memory)
            WHERE from.name = relation.source
            AND  to.name = relation.target
            WITH from, to, relation,
                 ({guards.lock_guard_clause("from")} AND {guards.lock_guard_clause("to")}) AS unlocked,
                 {guards.dangling_target_clause("to")} AS targetHasContent
            FOREACH (_ IN CASE WHEN targetHasContent AND {gate} THEN [1] ELSE [] END |
              MERGE (from)-[r:`{relation.relationType}`]->(to)
            )
            RETURN unlocked, targetHasContent,
                   from._lock_owner AS fromOwner, from._lock_expires_at AS fromExpiresAt,
                   to._lock_owner AS toOwner, to._lock_expires_at AS toExpiresAt
            """
            result = await self.driver.execute_query(
                query,
                {"relation": relation.model_dump(), "agent_id": agent_id, "enforce_locks": self.enforce_locks},
                routing_control=RoutingControl.WRITE,
            )
            if not result.records:
                blocked.append({
                    "source": relation.source, "target": relation.target,
                    "relationType": relation.relationType, "reason": "source or target entity not found",
                })
                continue
            record = result.records[0]
            unlocked = record.get("unlocked")
            target_ok = record.get("targetHasContent")
            would_apply = bool(unlocked) and bool(target_ok)
            if not target_ok:
                blocked.append({
                    "source": relation.source, "target": relation.target,
                    "relationType": relation.relationType,
                    "reason": "dangling target (target has no observations) - populate the target first",
                })
                continue
            if would_apply or not self.enforce_locks:
                applied.append(relation)
            else:
                blocked.append({
                    "source": relation.source, "target": relation.target,
                    "relationType": relation.relationType, "reason": "locked",
                    "blockedByOwner": record.get("fromOwner") or record.get("toOwner"),
                    "blockedUntil": record.get("fromExpiresAt") or record.get("toExpiresAt"),
                })
            if not unlocked:
                logger.warning(
                    f"create_relations: guard would have blocked {relation.source}->{relation.target} "
                    f"(enforce_locks={self.enforce_locks})"
                )

        if blocked:
            raise guards.GuardViolation(applied=applied, blocked=blocked, warnings=warnings)
        return applied, warnings

    async def add_observations(self, observations: List[ObservationAddition], agent_id: Optional[str] = None) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Add new observations to existing entities."""
        logger.info(f"Adding observations to {len(observations)} entities")
        gate = guards.enforce_gate(
            guards.lock_guard_clause("e"),
            "obs.expectedVersion IS NULL OR coalesce(e._version,0) = obs.expectedVersion",
        )
        query = f"""
        UNWIND $observations as obs
        MATCH (e:Memory {{ name: obs.entityName }})
        WITH e, obs,
             {guards.lock_guard_clause("e")} AS unlocked,
             (obs.expectedVersion IS NULL OR coalesce(e._version,0) = obs.expectedVersion) AS versionOk,
             [o in obs.observations WHERE NOT o IN coalesce(e.observations,[])] as new
        FOREACH (_ IN CASE WHEN {gate} THEN [1] ELSE [] END |
          SET e.observations = coalesce(e.observations,[]) + new,
              e._version = coalesce(e._version,0) + 1
        )
        RETURN e.name as name, (unlocked AND versionOk) as wouldApply, new,
               size(coalesce(e.observations,[])) as observationCount,
               CASE WHEN NOT unlocked THEN e._lock_owner END as blockedByOwner,
               CASE WHEN NOT unlocked THEN e._lock_expires_at END as blockedUntil,
               CASE WHEN NOT versionOk THEN e._version END as currentVersion
        """
        result = await self.driver.execute_query(
            query,
            {
                "observations": [obs.model_dump() for obs in observations],
                "agent_id": agent_id,
                "enforce_locks": self.enforce_locks,
            },
            routing_control=RoutingControl.WRITE,
        )

        applied: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        for record in result.records:
            would_apply = record.get("wouldApply")
            if would_apply or not self.enforce_locks:
                applied.append({"entityName": record.get("name"), "addedObservations": record.get("new")})
                hint = guards.decompose_hint(record.get("observationCount") or 0)
                if hint:
                    warnings.append({"entity": record.get("name"), "decomposeHint": hint})
            else:
                blocked.append({
                    "entityName": record.get("name"),
                    "blockedByOwner": record.get("blockedByOwner"),
                    "blockedUntil": record.get("blockedUntil"),
                    "currentVersion": record.get("currentVersion"),
                })
            if not would_apply:
                logger.warning(f"add_observations: guard would have blocked '{record.get('name')}' (enforce_locks={self.enforce_locks})")

        if blocked:
            raise guards.GuardViolation(applied=applied, blocked=blocked, warnings=warnings)
        return applied, warnings

    async def delete_entities(self, entity_names: List[str], agent_id: Optional[str] = None) -> None:
        """Delete multiple entities and their associated relations."""
        logger.info(f"Deleting {len(entity_names)} entities")
        gate = guards.enforce_gate(guards.lock_guard_clause("e"))
        query = f"""
        UNWIND $entities as name
        MATCH (e:Memory {{ name: name }})
        WITH e, name, {guards.lock_guard_clause("e")} AS unlocked
        FOREACH (_ IN CASE WHEN {gate} THEN [1] ELSE [] END | DETACH DELETE e)
        RETURN name, (unlocked OR NOT $enforce_locks) as wouldApply, unlocked
        """
        result = await self.driver.execute_query(
            query,
            {"entities": entity_names, "agent_id": agent_id, "enforce_locks": self.enforce_locks},
            routing_control=RoutingControl.WRITE,
        )

        blocked = [
            {"name": r.get("name")}
            for r in result.records
            if not r.get("wouldApply")
        ]
        for r in result.records:
            if not r.get("unlocked"):
                logger.warning(f"delete_entities: guard would have blocked '{r.get('name')}' (enforce_locks={self.enforce_locks})")
        if blocked:
            raise guards.GuardViolation(applied=[], blocked=blocked)
        logger.info(f"Successfully deleted {len(entity_names)} entities")

    async def delete_observations(self, deletions: List[ObservationDeletion], agent_id: Optional[str] = None) -> None:
        """Delete specific observations from entities."""
        logger.info(f"Deleting observations from {len(deletions)} entities")
        gate = guards.enforce_gate(
            guards.lock_guard_clause("e"),
            "d.expectedVersion IS NULL OR coalesce(e._version,0) = d.expectedVersion",
        )
        query = f"""
        UNWIND $deletions as d
        MATCH (e:Memory {{ name: d.entityName }})
        WITH e, d,
             {guards.lock_guard_clause("e")} AS unlocked,
             (d.expectedVersion IS NULL OR coalesce(e._version,0) = d.expectedVersion) AS versionOk
        FOREACH (_ IN CASE WHEN {gate} THEN [1] ELSE [] END |
          SET e.observations = [o in coalesce(e.observations,[]) WHERE NOT o in d.observations],
              e._version = coalesce(e._version,0) + 1
        )
        RETURN e.name as name, (unlocked AND versionOk) as wouldApply, unlocked, versionOk
        """
        result = await self.driver.execute_query(
            query,
            {
                "deletions": [deletion.model_dump() for deletion in deletions],
                "agent_id": agent_id,
                "enforce_locks": self.enforce_locks,
            },
            routing_control=RoutingControl.WRITE,
        )

        blocked = [
            {"entityName": r.get("name")}
            for r in result.records
            if not r.get("wouldApply")
        ]
        for r in result.records:
            if not (r.get("unlocked") and r.get("versionOk")):
                logger.warning(f"delete_observations: guard would have blocked '{r.get('name')}' (enforce_locks={self.enforce_locks})")
        if blocked:
            raise guards.GuardViolation(applied=[], blocked=blocked)
        logger.info(f"Successfully deleted observations from {len(deletions)} entities")

    async def delete_relations(self, relations: List[Relation], agent_id: Optional[str] = None) -> None:
        """Delete multiple relations from the graph."""
        logger.info(f"Deleting {len(relations)} relations")
        blocked: List[Dict[str, Any]] = []
        gate = guards.enforce_gate(guards.lock_guard_clause("source"), guards.lock_guard_clause("target"))
        for relation in relations:
            query = f"""
            WITH $relation as relation
            MATCH (source:Memory)-[r:`{relation.relationType}`]->(target:Memory)
            WHERE source.name = relation.source
            AND target.name = relation.target
            WITH source, target, r,
                 ({guards.lock_guard_clause("source")} AND {guards.lock_guard_clause("target")}) AS unlocked
            FOREACH (_ IN CASE WHEN {gate} THEN [1] ELSE [] END | DELETE r)
            RETURN unlocked
            """
            result = await self.driver.execute_query(
                query,
                {"relation": relation.model_dump(), "agent_id": agent_id, "enforce_locks": self.enforce_locks},
                routing_control=RoutingControl.WRITE,
            )
            if result.records and not result.records[0].get("unlocked") and self.enforce_locks:
                blocked.append({"source": relation.source, "target": relation.target, "relationType": relation.relationType})
            elif result.records and not result.records[0].get("unlocked"):
                logger.warning(
                    f"delete_relations: guard would have blocked {relation.source}->{relation.target} "
                    f"(enforce_locks={self.enforce_locks})"
                )
        if blocked:
            raise guards.GuardViolation(applied=[], blocked=blocked)
        logger.info(f"Successfully deleted {len(relations)} relations")

    async def read_graph(self) -> KnowledgeGraph:
        """Read the entire knowledge graph."""
        return await self.load_graph()

    async def search_memories(self, query: str) -> KnowledgeGraph:
        """Search for memories based on a query with Fulltext Search."""
        logger.info(f"Searching for memories with query: '{query}'")
        return await self.load_graph(query)

    async def find_memories_by_name(self, names: List[str]) -> KnowledgeGraph:
        """Find specific memories by their names. This does not use fulltext search."""
        logger.info(f"Finding {len(names)} memories by name")
        query = """
        MATCH (e:Memory)
        WHERE e.name IN $names
        RETURN  e.name as name,
                e.type as type,
                e.observations as observations
        """
        result_nodes = await self.driver.execute_query(query, {"names": names}, routing_control=RoutingControl.READ)
        entities: list[Entity] = list()
        for record in result_nodes.records:
            entities.append(Entity(
                name=record['name'],
                type=record['type'],
                observations=record.get('observations', list())
            ))

        # Get relations for found entities
        relations: list[Relation] = list()
        if entities:
            query = """
            MATCH (source:Memory)-[r]->(target:Memory)
            WHERE source.name IN $names OR target.name IN $names
            RETURN  source.name as source,
                    target.name as target,
                    type(r) as relationType
            """
            result_relations = await self.driver.execute_query(query, {"names": names}, routing_control=RoutingControl.READ)
            for record in result_relations.records:
                relations.append(Relation(
                    source=record["source"],
                    target=record["target"],
                    relationType=record["relationType"]
                ))

        logger.info(f"Found {len(entities)} entities and {len(relations)} relations")
        return KnowledgeGraph(entities=entities, relations=relations)
