#!/usr/bin/env bash
# Install the systemd user units. Idempotent.
set -euo pipefail
TARGET="$HOME/.config/systemd/user"
mkdir -p "$TARGET"
HERE="$(cd "$(dirname "$0")" && pwd)"
cp "$HERE/job-scan.service"        "$TARGET/job-scan.service"
cp "$HERE/job-scan.timer"          "$TARGET/job-scan.timer"
cp "$HERE/job-scan-weekly.service" "$TARGET/job-scan-weekly.service"
cp "$HERE/job-scan-weekly.timer"   "$TARGET/job-scan-weekly.timer"
cp "$HERE/job-scan-web.service"    "$TARGET/job-scan-web.service"
cp "$HERE/job-scan-watch.service"  "$TARGET/job-scan-watch.service"
cp "$HERE/job-scan-watch.timer"    "$TARGET/job-scan-watch.timer"
cp "$HERE/job-scan-picks.service"  "$TARGET/job-scan-picks.service"
cp "$HERE/job-scan-picks.timer"    "$TARGET/job-scan-picks.timer"
systemctl --user daemon-reload
systemctl --user enable --now job-scan.timer
systemctl --user enable --now job-scan-weekly.timer
systemctl --user enable --now job-scan-watch.timer
systemctl --user enable --now job-scan-picks.timer
systemctl --user enable --now job-scan-web.service
echo "Installed. Status:"
systemctl --user status job-scan.timer job-scan-weekly.timer job-scan-watch.timer job-scan-picks.timer job-scan-web.service --no-pager || true
echo
echo "Next runs:"
systemctl --user list-timers job-scan.timer job-scan-weekly.timer job-scan-watch.timer job-scan-picks.timer --no-pager
