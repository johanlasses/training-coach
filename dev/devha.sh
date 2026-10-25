#!/usr/bin/env bash
# Local Home Assistant stand-in: real HA Core + Mosquitto + the real add-on image, with fake Supervisor/Strava/Claude/Zwift.
#   up        build and start everything, bootstrap HA (idempotent)
#   rebuild   rebuild the add-on image after code changes and restart it only
#   health    push today's Apple Health sensor values from the Dev iPhone
#   smoke     end-to-end checks against the running stack
#   logs [svc]  follow logs (default: addon)
#   down      stop (keeps HA config and add-on data);  reset  stop and delete everything
set -euo pipefail
cd "$(dirname "$0")"
PY=../.venv/bin/python
C="docker compose -f compose.yaml"

case "${1:-}" in
  up)
    mkdir -p .state/ha
    [ -f mosquitto/passwd ] || docker run --rm eclipse-mosquitto:2 sh -c 'mosquitto_passwd -b -c /tmp/p dev dev >/dev/null && cat /tmp/p' > mosquitto/passwd
    [ -f .state/abb-root.pem ] && cp .state/abb-root.pem fakes/certs/ || true
    $C up -d --build
    $PY bootstrap.py
    ;;
  rebuild) $C up -d --build addon ;;
  health) $PY bootstrap.py health ;;
  smoke) shift; $PY smoke.py "$@" ;;
  logs) shift; $C logs -f --tail=100 "${1:-addon}" ;;
  down) $C down ;;
  reset) $C down -v; rm -rf .state/ha .state/ha_token .state/mobile.json mosquitto/passwd ;;
  *) sed -n 2,9p "$0"; exit 1 ;;
esac
