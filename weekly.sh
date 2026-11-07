#!/usr/bin/env bash
# Weekly sweep: refresh the hiring-without-whiteboards company list, then run a
# full scan that also includes those take-home/pair companies.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
# The timer can fire before the network is up (Persistent=true catch-up right
# after resume-from-suspend; user-level network-online.target is inert). Wait
# until an ATS host actually resolves, up to ~5 min, then run regardless.
for _ in $(seq 1 60); do
  getent hosts boards-api.greenhouse.io >/dev/null 2>&1 && break
  sleep 5
done
# Best-effort refresh; if it fails (network/parse), fall back to the cached list.
.venv/bin/python -m job_scan resolve-nw || echo "resolve-nw failed; using cached company list"
.venv/bin/python -m job_scan resolve-agg || echo "resolve-agg failed; using cached board list"
exec .venv/bin/python -m job_scan run --with-no-whiteboard "$@"
