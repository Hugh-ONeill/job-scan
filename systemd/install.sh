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
systemctl --user daemon-reload
systemctl --user enable --now job-scan.timer
systemctl --user enable --now job-scan-weekly.timer
systemctl --user enable --now job-scan-web.service
echo "Installed. Status:"
systemctl --user status job-scan.timer job-scan-weekly.timer job-scan-web.service --no-pager || true
echo
echo "Next runs:"
systemctl --user list-timers job-scan.timer job-scan-weekly.timer --no-pager
