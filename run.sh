#!/usr/bin/env bash
# Daily run entry. Invoked by the systemd timer.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python -m job_scan run "$@"
