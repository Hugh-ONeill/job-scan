#!/usr/bin/env bash
# Run the dashboard web server in the foreground. Stop with Ctrl-C.
# Or use the long-running systemd unit (systemd/job-scan-web.service).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python -m job_scan serve "$@"
