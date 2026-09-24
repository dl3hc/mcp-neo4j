# Running mcp-neo4j-memory as a systemd service

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) installed for the user that will run the service
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`). `run.sh` uses it to create/sync the venv
  on its own - nothing else needs installing by hand.
- A `.env` in `servers/mcp-neo4j-memory/` (copy `.env.example`, fill in your Neo4j credentials).
  Verify it manually first: `./run.sh`, confirm it starts and `Ctrl+C` it before installing the
  service - a broken `.env` is much easier to debug in the foreground.

## Install

```bash
# from servers/mcp-neo4j-memory/
sed -e "s|CHANGEME|$(whoami)|" -e "s|/path/to/mcp-neo4j|$(cd ../.. && pwd)|g" \
  contrib/mcp-neo4j-memory.service | sudo tee /etc/systemd/system/mcp-neo4j-memory.service

sudo systemctl daemon-reload
sudo systemctl enable --now mcp-neo4j-memory
sudo systemctl status mcp-neo4j-memory
```

The `sed` line fills in `User=` and the two path fields from your current checkout location and
user automatically. Double-check the generated file if your checkout isn't a plain `git clone`
(e.g. a symlinked or synced directory) - the substituted absolute path needs to be the one the
service will actually see at boot.

## Operate

```bash
sudo systemctl restart mcp-neo4j-memory   # after a `git pull` - uv resyncs the venv on next start
sudo systemctl stop mcp-neo4j-memory
journalctl -u mcp-neo4j-memory -f         # follow logs
```

## Upgrading

```bash
cd /path/to/mcp-neo4j
git fetch origin
git checkout <branch-or-tag>
git pull
sudo systemctl restart mcp-neo4j-memory
```

No separate install step - `run.sh` re-syncs the venv from `pyproject.toml`/`uv.lock` every time
it starts, so a `git pull` + restart is the entire upgrade.

## Prefer not to use systemd?

`./run.sh` alone (from a plain terminal, or under `tmux`/`screen`/`nohup ... &`) is the same
entry point the service file calls - nothing systemd-specific is required to just run the
server.
