#!/usr/bin/env bash
# Remove the launchd schedule (leaves data and outputs untouched).
set -euo pipefail

for LABEL in com.contentops.pipeline com.contentops.channel; do
  TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if [[ -f "$TARGET" ]]; then
    rm "$TARGET"
    echo "Removed $TARGET"
  else
    echo "No plist at $TARGET; nothing to remove."
  fi
done
echo "Schedule uninstalled. Collected data and drafts were not deleted."
