import logging
import re
from typing import Any, Dict, List, Optional

from neo4j import AsyncDriver, RoutingControl
from neo4j.exceptions import Neo4jError
from pydantic import Field

from fastmcp.server import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent, ToolAnnotations

logger = logging.getLogger('mcp_neo4j_memory')
logger.setLevel(logging.INFO)

MAX_NEIGHBOR_DEPTH = 4
DEFAULT_NEIGHBOR_DEPTH = 2
MAX_PATH_DEPTH = 5
DEFAULT_PATH_DEPTH = 5
NEIGHBOR_NODE_LIMIT = 200

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def clamp_depth(depth: Optional[int], maximum: int, default: int) -> int:
    if depth is None:
        depth = default
    return max(1, min(int(depth), maximum))


def _safe_relation_types(edge_types: Optional[List[str]]) -> List[str]:
    """Filter to identifiers matching the same pattern Relation.relationType enforces.

    edge_types may come straight from tool input, not through the pydantic-validated
    Relation model, so this is the one place that has to defend against a caller trying
    to smuggle arbitrary Cypher into a relationship-type pattern (which - unlike node
    property values - cannot be parameterized and must be embedded as literal text).
    """
    if not edge_types:
        return []
    safe = [t for t in edge_types if _SAFE_IDENTIFIER.match(t)]
    dropped = set(edge_types) - set(safe)
    if dropped:
        logger.warning(f"traversal: dropped invalid edge_types {dropped}")
    return safe


class Neo4jTraversal:
    """Read-only multi-hop traversal on top of the :Memory graph.

    Complements read_graph (full text dump) and find_memories_by_name (1-hop) with
    server-side BFS / shortest-path, so the model reasons over a right-sized subgraph
    instead of simulating traversal itself across many sequential 1-hop calls.
    """

    def __init__(self, neo4j_driver: AsyncDriver):
        self.driver = neo4j_driver

    @staticmethod
    def _relationship_filter(edge_types: Optional[List[str]], direction: str) -> str:
        """Build an APOC relationshipFilter value - passed as a query parameter, never embedded."""
        types = "|".join(edge_types) if edge_types else ""
        if direction == "outgoing":
            return f"{types}>" if types else ">"
        if direction == "incoming":
            return f"<{types}" if types else "<"
        return types

    async def get_neighbors(
        self,
        names: List[str],
        depth: int = DEFAULT_NEIGHBOR_DEPTH,
        edge_types: Optional[List[str]] = None,
        direction: str = "any",
        include_observations: bool = False,
    ) -> List[Dict[str, Any]]:
        """Depth-bounded neighborhood per seed name, via apoc.path.subgraphAll.

        Returns one entry per seed: {seed, found, nodes, relations}. depth is clamped to
        MAX_NEIGHBOR_DEPTH server-side regardless of what is requested.
        """
        depth = clamp_depth(depth, MAX_NEIGHBOR_DEPTH, DEFAULT_NEIGHBOR_DEPTH)
        rel_filter = self._relationship_filter(edge_types, direction)
        # Plain MATCH (not OPTIONAL MATCH): apoc.path.subgraphAll errors on a null start
        # node, so a seed that doesn't exist simply produces no row here - missing seeds
        # are filled in below on the Python side instead.
        query = """
        UNWIND $names AS name
        MATCH (start:Memory {name: name})
        CALL apoc.path.subgraphAll(start, {
            relationshipFilter: $relFilter,
            maxLevel: $depth,
            limit: $limit
        })
        YIELD nodes, relationships
        RETURN name AS seed,
               [n IN nodes WHERE n.name <> name | {
                   name: n.name, type: n.type,
                   observations: CASE WHEN $includeObs THEN n.observations ELSE null END
               }] AS nodes,
               [r IN relationships | {
                   source: startNode(r).name, target: endNode(r).name, relationType: type(r)
               }] AS relations
        """
        result = await self.driver.execute_query(
            query,
            {
                "names": names,
                "relFilter": rel_filter,
                "depth": depth,
                "limit": NEIGHBOR_NODE_LIMIT,
                "includeObs": include_observations,
            },
            routing_control=RoutingControl.READ,
        )
        rows = [
            {
                "seed": record.get("seed"),
                "found": True,
                "nodes": record.get("nodes") or [],
                "relations": record.get("relations") or [],
            }
            for record in result.records
        ]
        found = {row["seed"] for row in rows}
        for missing in [n for n in names if n not in found]:
            rows.append({"seed": missing, "found": False, "nodes": [], "relations": []})
        return rows

    async def find_path(
        self,
        from_name: str,
        to_name: str,
        max_depth: int = DEFAULT_PATH_DEPTH,
        edge_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Shortest path between two named entities, as an ordered, typed edge list.

        Existence of both endpoints is checked separately first: shortestPath() with a
        null endpoint is not a reliably-defined no-op across Neo4j versions, so this never
        risks invoking it on a missing node - a plain MATCH only runs once both are
        confirmed present.
        """
        max_depth = clamp_depth(max_depth, MAX_PATH_DEPTH, DEFAULT_PATH_DEPTH)

        existence = await self.driver.execute_query(
            "MATCH (n:Memory) WHERE n.name IN $names RETURN collect(n.name) AS found",
            {"names": [from_name, to_name]},
            routing_control=RoutingControl.READ,
        )
        found_names = set(existence.records[0].get("found") or []) if existence.records else set()
        from_found = from_name in found_names
        to_found = to_name in found_names
        if not (from_found and to_found):
            return {"found": False, "fromFound": from_found, "toFound": to_found, "nodes": [], "relations": [], "length": None}

        safe_types = _safe_relation_types(edge_types)
        type_pattern = (":" + "|".join(safe_types)) if safe_types else ""
        query = f"""
        MATCH (a:Memory {{name: $from}}), (b:Memory {{name: $to}})
        MATCH p = shortestPath((a)-[{type_pattern}*..{max_depth}]-(b))
        RETURN [n IN nodes(p) | {{name: n.name, type: n.type}}] AS nodes,
               [r IN relationships(p) | {{source: startNode(r).name, target: endNode(r).name, relationType: type(r)}}] AS relations,
               length(p) AS length
        """
        result = await self.driver.execute_query(
            query, {"from": from_name, "to": to_name}, routing_control=RoutingControl.READ,
        )
        if not result.records:
            return {"found": False, "fromFound": True, "toFound": True, "nodes": [], "relations": [], "length": None}
        record = result.records[0]
        return {
            "found": True,
            "fromFound": True,
            "toFound": True,
            "nodes": record.get("nodes") or [],
            "relations": record.get("relations") or [],
            "length": record.get("length"),
        }

    async def get_map(self) -> Dict[str, Any]:
        """Whole-graph orientation: names + types + edges only, no observation text.

        Cheaper than read_graph for planning/orientation; read_graph itself stays
        unchanged since the Dream Engine's snapshot loader depends on its full payload.
        """
        query = """
        MATCH (n:Memory)
        OPTIONAL MATCH (n)-[r]->(m:Memory)
        RETURN collect(DISTINCT {name: n.name, type: n.type}) AS nodes,
               collect(DISTINCT CASE WHEN r IS NOT NULL
                   THEN {source: startNode(r).name, target: endNode(r).name, relationType: type(r)}
               END) AS relations
        """
        result = await self.driver.execute_query(query, routing_control=RoutingControl.READ)
        if not result.records:
            return {"nodes": [], "relations": []}
        record = result.records[0]
        relations = [r for r in (record.get("relations") or []) if r is not None]
        return {"nodes": record.get("nodes") or [], "relations": relations}


def register_traversal_tools(mcp: FastMCP, traversal: Neo4jTraversal, namespace_prefix: str) -> None:
    """Register get_neighbors / find_path / get_map on the given FastMCP server."""

    @mcp.tool(
        name=namespace_prefix + "get_neighbors",
        annotations=ToolAnnotations(title="Get Neighbors", readOnlyHint=True,
                                     destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def get_neighbors(
        names: list[str] = Field(..., description="Seed entity names to expand the neighborhood from"),
        depth: int = Field(default=2, ge=1, description=f"Hops to traverse from each seed (server-capped at {MAX_NEIGHBOR_DEPTH})"),
        edge_types: Optional[list[str]] = Field(default=None, description="Restrict traversal to these relation types; omit for all types"),
        direction: str = Field(default="any", description="'any', 'outgoing', or 'incoming'"),
        include_observations: bool = Field(default=False, description="Include full observation text per node (default: structure only)"),
    ) -> ToolResult:
        """Depth-bounded neighborhood around one or more seed entities.

        Use this instead of chaining find_memories_by_name calls when you need more than
        1 hop of context. Returns, per seed, the reachable nodes and the typed edges
        connecting them - structure only unless include_observations is set.
        """
        logger.info(f"MCP tool: get_neighbors ({len(names)} seeds, depth={depth})")
        try:
            result = await traversal.get_neighbors(names, depth, edge_types, direction, include_observations)
            return ToolResult(content=[TextContent(type="text", text=str(result))], structured_content={"result": result})
        except Neo4jError as e:
            logger.error(f"Neo4j error in get_neighbors: {e}")
            raise ToolError(f"Neo4j error in get_neighbors: {e}")
        except Exception as e:
            logger.error(f"Error in get_neighbors: {e}")
            raise ToolError(f"Error in get_neighbors: {e}")

    @mcp.tool(
        name=namespace_prefix + "find_path",
        annotations=ToolAnnotations(title="Find Path", readOnlyHint=True,
                                     destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def find_path(
        from_entity: str = Field(..., description="Exact name of the starting entity"),
        to_entity: str = Field(..., description="Exact name of the target entity"),
        max_depth: int = Field(default=5, ge=1, description=f"Maximum hops to search (server-capped at {MAX_PATH_DEPTH})"),
        edge_types: Optional[list[str]] = Field(default=None, description="Restrict the path to these relation types; omit for all types"),
    ) -> ToolResult:
        """Shortest path between two named entities, as an ordered, typed edge list.

        Answers "how is A connected to C" directly, instead of requiring the model to
        manually chain 1-hop lookups and infer the connection from prose.
        """
        logger.info(f"MCP tool: find_path ({from_entity} -> {to_entity})")
        try:
            result = await traversal.find_path(from_entity, to_entity, max_depth, edge_types)
            return ToolResult(content=[TextContent(type="text", text=str(result))], structured_content={"result": result})
        except Neo4jError as e:
            logger.error(f"Neo4j error in find_path: {e}")
            raise ToolError(f"Neo4j error in find_path: {e}")
        except Exception as e:
            logger.error(f"Error in find_path: {e}")
            raise ToolError(f"Error in find_path: {e}")

    @mcp.tool(
        name=namespace_prefix + "get_map",
        annotations=ToolAnnotations(title="Get Map", readOnlyHint=True,
                                     destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def get_map() -> ToolResult:
        """Whole-graph orientation: every entity's name and type, plus every typed edge.

        No observation text - cheaper than read_graph when the goal is to orient (which
        entities exist, how are they connected) before deciding what to read in detail.
        """
        logger.info("MCP tool: get_map")
        try:
            result = await traversal.get_map()
            return ToolResult(content=[TextContent(type="text", text=str(result))], structured_content={"result": result})
        except Neo4jError as e:
            logger.error(f"Neo4j error in get_map: {e}")
            raise ToolError(f"Neo4j error in get_map: {e}")
        except Exception as e:
            logger.error(f"Error in get_map: {e}")
            raise ToolError(f"Error in get_map: {e}")
