#!/bin/bash
# 검수용 미리보기 서버: For_Review 폴더를 Tailscale 인터페이스에만 연다 (공용 네트워크·0.0.0.0 노출 없음).
# 사용: scripts/serve_review.sh [port]   → http://<tailscale-ip>:<port>/<output-id>/preview.html
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-8000}"

# tailscale CLI 가 PATH 에 없으면 앱 번들의 것을 쓴다
TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
IP="$("$TS" ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$IP" ]; then
  echo "Tailscale IPv4 를 찾지 못했습니다. Tailscale 이 켜져 있는지 확인하세요." >&2
  exit 1
fi

echo "serving $ROOT/For_Review at http://$IP:$PORT/"
exec python3 -m http.server "$PORT" --bind "$IP" --directory "$ROOT/For_Review"
