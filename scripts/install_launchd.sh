#!/usr/bin/env bash
# Install the weekly launchd schedule for the current user:
#   com.contentops.pipeline  Monday 08:30        engine (collect/RAG/verify) + this week's two topics
#   com.contentops.channel   Wednesday+Sunday 08:30  one card -> auto-approval gate -> (auto) post
#
# This only touches ~/Library/LaunchAgents for your own user account; it does not
# require sudo and does not install a system-wide daemon.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"
LABELS=("com.contentops.pipeline" "com.contentops.channel")

mkdir -p "$TARGET_DIR" "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/scripts/run_pipeline.sh" "$PROJECT_DIR/scripts/run_channel.sh"

for LABEL in "${LABELS[@]}"; do
  TEMPLATE="$PROJECT_DIR/scripts/$LABEL.plist"
  TARGET="$TARGET_DIR/$LABEL.plist"
  if [[ ! -f "$TEMPLATE" ]]; then
    echo "Template not found: $TEMPLATE" >&2
    exit 1
  fi
  sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" "$TEMPLATE" > "$TARGET"
  echo "Wrote $TARGET"

  # bootout is a no-op the first time; ignore its error.
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$TARGET"
  launchctl enable "gui/$(id -u)/$LABEL"
done

echo
echo "Installed. Schedule (local time):"
echo "  Monday    08:30  com.contentops.pipeline  (engine + weekly topic list)"
echo "  Wednesday 08:30  com.contentops.channel   (card + gate + post)"
echo "  Sunday    08:30  com.contentops.channel"
echo "  status : launchctl print gui/$(id -u)/com.contentops.channel | head -20"
echo "  run now: launchctl kickstart -k gui/$(id -u)/com.contentops.pipeline   # or .channel"
echo "  logs   : tail -f $PROJECT_DIR/logs/launchd.out.log $PROJECT_DIR/logs/launchd.channel.out.log"
echo "  alerts : $PROJECT_DIR/logs/channel_alerts.log"
echo "  pause posting: touch $PROJECT_DIR/logs/PAUSE_PUBLISHING"
echo
echo "To change the schedule, edit scripts/<label>.plist and re-run this script."
