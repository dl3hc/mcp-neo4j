# Deploy: entity-locking-traversal fork → typhon

The actual running MCP memory server is on `typhon` (192.168.2.96), reached from this Windows
machine only through an SSH port-forward (`ssh typhon`, `LocalForward 8000 localhost:8000` in
`~/.ssh/config`). `E:\repos\memory\mcp-neo4j-memory` (`.venv`/`run.sh`/`.env`) on this Windows
box is a **local mirror**, not the live install — deploying means changing what's on typhon.

Fork branch is pushed: `github.com/dl3hc/mcp-neo4j`, branch `feature/entity-locking-traversal`.

## The simple path (recommended)

The fork now has a self-contained run path — see `run.sh`, `.env.example`, and
`contrib/mcp-neo4j-memory.service` (all added after the rest of this doc was originally
written). This replaces the old separate-venv deployment layout entirely; there is no longer a
reason to keep a `pip install -e`'d copy in a different directory in sync by hand.

```bash
ssh typhon
cd ~   # or wherever you keep repos on typhon
git clone https://github.com/dl3hc/mcp-neo4j.git   # or, if already cloned: git pull
cd mcp-neo4j/servers/mcp-neo4j-memory
git checkout feature/entity-locking-traversal      # if not already on it
cp .env.example .env
# edit .env: at minimum NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD/NEO4J_DATABASE for the real DB
./run.sh   # foreground first, to confirm it actually connects - Ctrl+C once you see it's up
```

Once that works, either install it as a systemd service (`contrib/README.md` has the exact
steps) or just run `./run.sh` under `tmux`/`screen`/`nohup` as before — same entry point either
way.

**If an old separate-venv deployment (a directory with its own `.venv`/`.env`/`run.sh`, `pip
install`ed rather than a git checkout) is still what's actually running**, stop that process and
point traffic at this new checkout's `run.sh` instead, rather than trying to patch the old
venv in place. Whatever port/host it was bound to (`.env`'s `NEO4J_MCP_SERVER_PORT`, etc.),
match it in the new `.env` so nothing else (like the SSH tunnel config) needs to change.

Do **not** set `NEO4J_MEMORY_ENFORCE_LOCKS=true` in `.env` yet — keep the default (off: guard
violations are logged, never block a write) until the new behavior has been observed for a
while in real use.

## Phase 4 — verify

From the Windows machine (goes through the same existing SSH tunnel, nothing else needed):

```bash
curl -s -X POST http://127.0.0.1:8000/mcp/ \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | grep -o '"name":"[a-zA-Z_]*"' | sort -u
```

Expect **15** tool names: the original 9 (`read_graph`, `create_entities`, `create_relations`,
`add_observations`, `delete_entities`, `delete_observations`, `delete_relations`,
`search_memories`, `find_memories_by_name`) plus 6 new ones (`get_neighbors`, `find_path`,
`get_map`, `acquire_lock`, `release_lock`, `lock_status`).

Then do one live smoke call, e.g. `get_map` (read-only, safe against production data):

```bash
curl -s -X POST http://127.0.0.1:8000/mcp/ \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_map","arguments":{}}}'
```
Should return real node/relation data from the production graph, not an error.

## Rollback

If anything looks wrong:

```bash
cd "$APP_DIR"
source .venv/bin/activate
pip uninstall mcp-neo4j-memory -y
pip install mcp-neo4j-memory==0.4.5
deactivate
```
Then restart the same way as Phase 3. This returns to the exact stock package that was running
before this deploy — no data changes were made to the graph itself by the code change alone.

## After this is confirmed working

Report back (or just note it) so `.claude/rules/memory.md`'s Traversal Protocol section can be
updated to point agents at `get_neighbors`/`find_path`/`get_map` as the primary path — that
update is deliberately held until the tools are confirmed live, so no rules doc ever describes
a tool that doesn't exist yet (the same mistake just fixed for `MemSession`/`MemMessage`).

**Do not** run `tests/integration/test_locking_IT.py` against this production instance — its
fixtures `DETACH DELETE` all `:Memory` nodes as cleanup after every test, which would wipe the
real memory graph. It also needs Docker (`tests/integration/conftest.py` spins up a
`Neo4jContainer` via testcontainers), which this environment doesn't have either.

Docker-free options for real integration verification, in order of preference:
1. **A second, native Neo4j install on typhon** (plain tarball/package, not a container),
   bound to different ports (e.g. bolt `7688`), used only for tests. Community Edition only
   supports one user database per instance, so this needs a genuinely separate process, not
   just a second database name on the same one.
2. **Manual, careful smoke-testing against production**: the read-only tools
   (`get_map`/`get_neighbors`/`find_path`/`lock_status`) are safe to call directly. For the
   write-path guards (self-loop rejection, dangling-target rejection, lock contention,
   `expectedVersion` CAS), create clearly-named throwaway entities by hand
   (e.g. `_smoketest_...`), exercise the tool, then delete exactly those entities yourself with
   `delete_entities` — never run the automated test suite's fixtures against this graph.
