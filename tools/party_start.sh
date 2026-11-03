#!/usr/bin/env bash
# Start lighting party monitoring: read-only party_monitor.py with song titles from YARG's log on
# the Mac and the phone page on Tailscale :8733 (the X32 vocal monitor has :8732).
#
#   tools/party_start.sh                 # out/sessions/<YYYYMMDD>-party, 8 h
#   tools/party_start.sh --hours 5       # extra args go to party_monitor.py run
#   tools/party_start.sh --songs ''      # YARG not on the Mac: songs numbered from scene changes
set -euo pipefail
cd "$(dirname "$0")/.."
out="out/sessions/$(date +%Y%m%d)-party"
exec .venv/bin/python -m tools.party_monitor run --songs mac-mini --web 8733 --hours 8 --out "$out" "$@"
