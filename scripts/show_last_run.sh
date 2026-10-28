#!/usr/bin/env bash
# Show what the last run produced: status, counts, review verdict.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

LATEST="$(ls -1d outputs/drafts/*/ 2>/dev/null | sort | tail -1 || true)"

if [[ -z "$LATEST" ]]; then
  echo "No runs yet. Try: make run-dry"
  exit 0
fi

echo "Latest draft directory: $LATEST"
echo
ls -la "$LATEST"

if [[ -f "${LATEST}run_metadata.json" ]]; then
  echo
  echo "--- run_metadata.json ---"
  python3 -c "
import json, sys
data = json.load(open('${LATEST}run_metadata.json'))
for key in ('run_id', 'mode', 'status', 'llm_provider', 'embedder', 'vector_store'):
    print(f'{key:14} {data.get(key)}')
print('counts        ', data.get('counts'))
"
fi

if [[ -f "${LATEST}review.md" ]]; then
  echo
  echo "--- review.md (head) ---"
  head -20 "${LATEST}review.md"
fi
