#!/usr/bin/env bash
# Daily run entry. Invoked by the systemd timer.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
# The timer can fire before the network is up (Persistent=true catch-up right
# after resume-from-suspend; user-level network-online.target is inert). Wait
# until an ATS host actually resolves, up to ~5 min, then run regardless.
for _ in $(seq 1 60); do
  getent hosts boards-api.greenhouse.io >/dev/null 2>&1 && break
  sleep 5
done
exec .venv/bin/python -m job_scan run "$@"
