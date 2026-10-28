#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, database, smoke test.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Creating virtualenv (.venv) with $PYTHON_BIN"
"$PYTHON_BIN" -m venv .venv

echo "==> Installing core dependencies"
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  echo "==> Creating .env from .env.example (fill in your keys later)"
  cp .env.example .env
fi

echo "==> Initialising the database"
.venv/bin/python -m src.storage.db --init

echo "==> Running a dry-run smoke test"
.venv/bin/python -m src.pipeline.run --dry-run

cat <<'EOF'

Setup complete.

Next steps:
  1. Edit .env if you want a real LLM provider (default is the offline mock).
  2. make run              # live run against the configured sources
  3. make launchd-install  # schedule Monday/Friday runs

Optional upgrades (Chroma, local embeddings, LLM SDKs):
  make install-optional
EOF
