# Deploy: entity-locking-traversal fork → typhon

The actual running MCP memory server is on `typhon` (192.168.2.96), reached from this Windows
machine only through an SSH port-forward (`ssh typhon`, `LocalForward 8000 localhost:8000` in
`~/.ssh/config`). `E:\repos\memory\mcp-neo4j-memory` (`.venv`/`run.sh`/`.env`) on this Windows
box is a **local mirror**, not the live install — deploying means changing what's on typhon.

Fork branch is pushed: `github.com/dl3hc/mcp-neo4j`, branch `feature/entity-locking-traversal`.

## Phase 0 — find out how it's actually running on typhon

```bash
ssh typhon
ps aux | grep -i mcp-neo4j-memory
# note the PID, then:
readlink /proc/<PID>/cwd          # the real working directory (may differ from the Windows mirror's relative path)
cat /proc/<PID>/cmdline | tr '\0' ' '   # confirm it's run.sh's `mcp-neo4j-memory --transport http ...`

# is it supervised, or just a bare process?
systemctl --user status mcp-neo4j-memory 2>&1
systemctl status mcp-neo4j-memory 2>&1
tmux ls 2>&1
screen -ls 2>&1
```

Everything below assumes you now know: the real working directory (call it `$APP_DIR`, contains
`.venv`, `.env`, `run.sh`) and how it's kept alive (foreground/tmux/screen/systemd/nohup).

## Phase 1 — get the fork's code onto typhon

```bash
# if you don't already have a checkout of the fork on typhon:
cd ~   # or wherever you keep repos on typhon
git clone https://github.com/dl3hc/mcp-neo4j.git
cd mcp-neo4j
git checkout feature/entity-locking-traversal

# if you already have one, just:
cd <path-to-mcp-neo4j-checkout-on-typhon>
git fetch origin
git checkout feature/entity-locking-traversal
git pull
```

## Phase 2 — install the fork into $APP_DIR's existing venv

This swaps what `run.sh`'s `mcp-neo4j-memory` entry point actually runs, without changing
`run.sh` itself.

```bash
cd "$APP_DIR"
source .venv/bin/activate
pip install -e /path/to/mcp-neo4j/servers/mcp-neo4j-memory
# (use `uv pip install -e ...` instead if this venv was built with uv - check for a uv.lock
# or how it was originally set up)

python -c "import mcp_neo4j_memory; print(mcp_neo4j_memory.__file__)"
# should now point INTO the git checkout (…/mcp-neo4j/servers/mcp-neo4j-memory/src/…),
# not into .venv/lib/python3.*/site-packages/ - if it still shows site-packages, the
# editable install didn't take; re-run pip install -e with -v to see why.
deactivate
```

## Phase 3 — restart

Do **not** add `--enforce-locks` to `run.sh` yet — keep the default (off: guard violations are
logged, never block a write) until the new behavior has been observed for a while.

- **Foreground terminal / nohup**: `kill <PID>` from Phase 0, then start it the same way it was
  running before (re-attach to the same tmux/screen window and re-run `./run.sh`, or
  `nohup ./run.sh > server.log 2>&1 & disown` if that's how it was started).
- **systemd**: `sudo systemctl restart mcp-neo4j-memory` (or whatever the unit is actually
  called per Phase 0).

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
real memory graph. Integration verification needs an isolated Neo4j (a testcontainer, or a
second throwaway database) — never the production bolt connection.
