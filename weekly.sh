#!/usr/bin/env bash
# Weekly sweep: refresh the hiring-without-whiteboards company list, then run a
# full scan that also includes those take-home/pair companies.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
# Best-effort refresh; if it fails (network/parse), fall back to the cached list.
.venv/bin/python -m job_scan resolve-nw || echo "resolve-nw failed; using cached company list"
exec .venv/bin/python -m job_scan run --with-no-whiteboard "$@"
