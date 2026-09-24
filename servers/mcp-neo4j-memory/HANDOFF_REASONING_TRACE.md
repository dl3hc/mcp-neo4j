# Handoff: Reasoning-Trace Memory Type (Vector Search)

Status: **not started**. This document exists so a future session can pick this up without
re-deriving the research already done on 2026-09-24. It is a handoff, not a plan — the open
decisions below need a real conversation with the user before code gets written.

## Context

`neo4j-labs/agent-memory` and the Microsoft-Agent-Framework blog post both describe a third
memory type alongside Short-Term (conversation) and Long-Term (entities/facts/preferences):
**Reasoning** — tool-call sequences and their outcomes, retrievable by similarity so a future
task can reuse a past solution pattern instead of starting cold. Our system has nothing like
this today; it was flagged as a real gap in `Memory System -- Three-Type Model Gap Analysis`
(see the Neo4j memory graph — `find_memories_by_name(["Memory System -- Three-Type Model Gap
Analysis"])` for the full history), but building it was explicitly deferred rather than
bundled into the traversal/locking work done the same day.

## Already decided (don't re-litigate these)

- **Neo4j native Vector Index, not FAISS.** Verified directly against the official Cypher
  Manual (2026-09-24): vector indexes are available in **Community Edition** since Neo4j 5.11,
  storing embeddings as a `LIST<FLOAT>` node property (only the newer native `VECTOR`
  block-storage type is Enterprise/Aura-exclusive — irrelevant here). A web-search summary
  claimed Community Edition wasn't supported; that was wrong and the direct doc fetch is what
  should be trusted. Rationale for avoiding FAISS: it has no persistence or server of its own,
  so it would need a hand-rolled layer to keep it in sync with the graph — exactly the
  dual-system consistency gap the locking design (same day) deliberately avoided by keeping
  the lock state on the node itself instead of a second system.
- **Embeddings via local Ollama**, not a cloud embedding API. Ollama already runs on `typhon`
  and is already used by the Dream Engine's oracle (`DREAM_LLM_BASE_URL=http://typhon:11434`).
  No new infrastructure, no per-call cost, no data leaves the network.

## Open decisions — resolve these before writing code

1. **Node shape.** Reuse the existing single `:Memory` label with `type="trace"` (minimal
   change — `read_graph`/`search_memories`/etc. are all hardcoded to `:Memory` today), or give
   traces a genuinely separate shape? A trace has structurally different data (tool name,
   arguments, outcome, an embedding vector) than an entity's `name`/`type`/`observations`.
   **Leaning:** start with `type="trace"` on `:Memory` to avoid touching every existing tool's
   Cypher; revisit only if the shape mismatch turns out to matter in practice.
2. **Sync vs. async embedding.**
   - *Sync*: compute the embedding inside the `record_trace` tool call itself (a blocking HTTP
     call to Ollama). Simplest, but it would be the MCP server's first synchronous external
     dependency, and adds real latency (typically 100-300ms) to every write.
   - *Async*: leave `embedding` null at write time; a new Dream Engine pipeline stage finds
     `:Memory{type:"trace", embedding: null}` nodes and backfills them during the next
     consolidation run.
   **Leaning:** async, because it matches the architecture principle already on record in the
   memory graph — *"MCP = Ingestion+Retrieval (live, in the chat loop) | Dream Engine =
   Stabilization (offline, authoritative rewrite)"*. Adding a live external HTTP dependency to
   the request/response MCP server would be a step backward from that split.
3. **Embedding model + dimensions.** The vector index's `vector.dimensions` must match the
   chosen model's output size exactly, so this has to be pinned before the index is created.
   Candidates (all pullable via `ollama pull <name>` on typhon):
   | Model | Dimensions | Tradeoff |
   |---|---|---|
   | `nomic-embed-text` | 768 | Good general-purpose default |
   | `all-minilm` | 384 | Smallest/fastest, lower quality |
   | `mxbai-embed-large` | 1024 | Best quality, slowest, most storage |

   No default has been chosen — pick one deliberately, don't just take the first one that works.
4. **What text actually gets embedded.** The raw task description? A summary of the tool-call
   sequence? Both concatenated? This is the single biggest lever on retrieval quality and needs
   a real decision, not "embed something and see".
5. **Retention policy.** Traces are append-only and will accumulate without bound otherwise.
   Needs an explicit choice (age-based expiry? a cap per task-type? left to a future Dream
   Engine stage, mirroring its existing macro-compression/singleton-collapse passes?) before
   this goes into real use.

## Proposed implementation steps (once the above is decided)

1. `ensure_vector_index()` — new method on `Neo4jMemory` (or a new small module), called from
   `main()` the same way `ensure_constraints()`/`create_fulltext_index()` are today:
   ```cypher
   CREATE VECTOR INDEX trace_embedding IF NOT EXISTS
   FOR (m:Memory) ON m.embedding
   OPTIONS {indexConfig: {`vector.dimensions`: <N>, `vector.similarity_function`: 'cosine'}}
   ```
2. **New module `traces.py`** (mirrors `traversal.py`'s structure): a `Neo4jTraces` class with
   - `record_trace(task, tool_calls, outcome, agent_id)` — creates a `:Memory{type:"trace"}`
     node; `embedding` left null if async was chosen.
   - `find_similar_traces(query_text, limit)` — embeds `query_text` (same model/dims as the
     index), then:
     ```cypher
     CALL db.index.vector.queryNodes('trace_embedding', $k, $queryVector)
     YIELD node, score
     RETURN node.name AS name, node.outcome AS outcome, score
     ```
3. **New module `trace_tools.py`** registering the MCP tools, following `lock_tools.py`'s
   `register_lock_tools(mcp, locking, namespace_prefix)` pattern exactly; wire into
   `server.py::create_mcp_server()` alongside the existing `register_lock_tools`/
   `register_traversal_tools` calls.
4. **If async was chosen**: new Dream Engine stage (e.g. `dream_engine/embedder.py`) that finds
   un-embedded trace nodes and calls Ollama's `/api/embeddings` endpoint — reuse the HTTP
   client pattern already in `semantic_oracle.py`/`mcp_client.py` rather than adding a new one.
   Writing the vector back is a raw property `SET`, not an `observations` append — it does not
   fit `add_observations`'s existing shape, so this needs its own guarded write path (respect
   the lock/version guards built the same day as the traversal/locking work, in `guards.py`).
5. **Tests**: unit tests with a fake/deterministic embedding function (no real Ollama call in
   CI); integration tests against a real Neo4j testcontainer, either with real Ollama calls (if
   reachable from CI) or the same fake embedding function.
6. **Only after it's built and actually working**, document the new type in
   `.claude/rules/neo4j-schema.md`. Do not document it before the write path exists and is
   verified — that is exactly the mistake just fixed with the dormant `MemSession`/`MemMessage`
   entries this same day. A schema doc describing something that doesn't exist yet is worse
   than no doc at all.

## Non-goals for v1

- No cross-project trace sharing beyond what the existing shared memory graph already provides.
- No trace editing/versioning — traces are append-only, immutable once recorded.
- No automatic "replay" of a past trace — retrieval only surfaces past traces as context; using
  them stays entirely the calling model's judgment call.

## Where this fits

- Server-side code: `E:\repos\memory\mcp-neo4j\servers\mcp-neo4j-memory\` — same fork as the
  2026-09-24 traversal/locking work. Suggest a fresh branch (`feature/reasoning-trace`) off
  `main` once that work has been reviewed/merged, rather than piling onto
  `feature/entity-locking-traversal`.
- Consolidation-side code (if async embedding is chosen): `E:\repos\dream_engine\dream_engine\`.
- Background/decision history: `Memory System -- Three-Type Model Gap Analysis` and
  `Memory System -- Multi-Agent Locking` entities in the Neo4j memory graph.
