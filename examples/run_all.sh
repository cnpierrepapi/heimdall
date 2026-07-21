#!/usr/bin/env bash
# End-to-end run from a clean checkout: fresh venv, install, seed the world,
# run the agents, write trust back, prove the gateway. Nonzero exit on any
# failure. Expects the environment described in examples/README.md.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${LLM_BASE_URL:=https://openrouter.ai/api/v1}"
: "${LLM_MODEL:=qwen/qwen3-32b}"
: "${DATAHUB_GMS_URL:=http://localhost:8080}"
: "${LEDGER_DB:=$HOME/heimdall-demo.db}"
export LLM_BASE_URL LLM_MODEL DATAHUB_GMS_URL LEDGER_DB

PY="${PYTHON:-python3.11}"

echo "== fresh venv + install"
"$PY" -m venv .e2e-venv
.e2e-venv/bin/pip install -q -e .

echo "== stage 1: seed the world"
.e2e-venv/bin/python scripts/ingest_world.py

echo "== stage 2: agents demo"
.e2e-venv/bin/python scripts/run_agents_demo.py

echo "== stage 3: writeback"
.e2e-venv/bin/python scripts/run_writeback.py

echo "== stage 4: gateway e2e"
.e2e-venv/bin/python scripts/gateway_e2e.py

echo "== ALL STAGES PASSED"
