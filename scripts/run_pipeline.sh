#!/usr/bin/env bash
# Wrapper used by launchd (and safe to run by hand).
#
# launchd starts jobs with a minimal environment and "/" as the working
# directory, so this script pins both, loads .env and logs to logs/.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "[contentops] .venv not found; falling back to python3 on PATH" >&2
  PYTHON="$(command -v python3)"
fi

# Homebrew paths are not in launchd's default PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

echo "[contentops] $(date -u +%FT%TZ) starting: $PYTHON -m src.pipeline.run $*"
# Under `set -e` a failing engine would end the script here silently (no log line, no alert), and
# nobody watches this job. Capture the status instead and announce failures.
status=0
"$PYTHON" -m src.pipeline.run "$@" || status=$?
echo "[contentops] $(date -u +%FT%TZ) finished with exit code $status"

if [[ $status -ne 0 ]]; then
  "$PYTHON" -m channel.src.notify "R_A_Glance 엔진 실패" \
    "월요일 수집·RAG 실행이 종료 코드 $status 로 끝났습니다. 이번 주 게시가 건너뛰어질 수 있습니다 — logs/launchd.err.log 를 확인하세요" || true
fi

# Engine succeeded -> pick this week's two topics and announce the list. The channel itself runs
# separately on Wednesday and Sunday (scripts/com.contentops.channel.plist -> run_channel.sh), so a
# topic is at most a few days old when it is rendered and posted.
if [[ $status -eq 0 ]]; then
  "$PYTHON" -m channel.src.weekly || echo "[contentops] weekly list failed (see logs/channel_alerts.log)" >&2
fi
exit $status
