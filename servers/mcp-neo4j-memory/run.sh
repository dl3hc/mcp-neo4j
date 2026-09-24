#!/bin/bash
# Start the memory server directly from a clone of this repo.
#
#   git clone https://github.com/dl3hc/mcp-neo4j.git
#   cd mcp-neo4j/servers/mcp-neo4j-memory
#   cp .env.example .env   # fill in NEO4J_PASSWORD at minimum
#   ./run.sh
#
# That's the whole setup. `uv run` creates/syncs the venv from pyproject.toml/uv.lock on
# its own - there is no separate deployment directory or manual `pip install -e` step to
# keep in sync anymore. All server config comes from .env (see .env.example for every
# variable process_config() understands); no CLI flags needed here.
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
else
  echo "No .env found - copy .env.example to .env and fill in your Neo4j credentials first." >&2
  exit 1
fi

exec uv run mcp-neo4j-memory
