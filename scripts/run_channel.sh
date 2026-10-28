#!/usr/bin/env bash
# R_A_Glance 후보 생성(게시 아님). launchd 와 수동 실행 모두 안전합니다.
#
# 엔진 실행(run_pipeline.sh) 뒤에 이어서 돌립니다. 이 스크립트는 후보를 만들어 사람 승인 대기
# 상태로 두기만 합니다. 게시는 phase 3 전환과 승인이 있어야 하며 여기서 하지 않습니다.
# 실패하면 logs/channel_alerts.log 와 macOS 알림에 남깁니다.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

PYTHON="$PROJECT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Now a standalone launchd job (Wed/Sun): load the same .env the engine wrapper loads (LLM + Instagram).
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

echo "[channel] $(date -u +%FT%TZ) starting"
"$PYTHON" -m channel.src.pipeline "$@"
status=$?
echo "[channel] $(date -u +%FT%TZ) finished with exit code $status"

# 종료 코드 1 은 "승인된 후보 없음"으로, 파이프라인이 이유와 함께 이미 알렸습니다(autopublish.finish_run).
# 2 이상은 실행 자체의 실패이므로 여기서 알립니다.
if [[ $status -ge 2 ]]; then
  "$PYTHON" -m channel.src.notify "R_A_Glance" \
    "채널 실행 종료 코드 $status — logs/channel.log 와 approval run-report 를 확인하세요" || true
fi
exit $status
