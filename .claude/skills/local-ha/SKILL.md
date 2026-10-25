---
name: local-ha
description: Run Training Coach in the local Home Assistant dev stack (real add-on image, HA Core, Mosquitto, fake Supervisor/Strava/Claude/Zwift) and verify changes end to end. Use before calling work on HA, Strava, Claude, MQTT, notifications or ingress done, and whenever the real HA box is unreachable.
---

# Local HA stack (`dev/`)

Needs Docker Desktop signed in (ABB account). All commands from the repo root.

```bash
dev/devha.sh up        # build + start + bootstrap HA (idempotent; first run pulls images, a few minutes)
.venv/bin/python dev/smoke.py          # ~3 min; starts from an empty add-on every time. --mobile adds tools/mobile_check.py, --keep skips the reset
dev/devha.sh rebuild   # after changing app code: rebuild and restart only the add-on
dev/devha.sh logs      # add-on log (or: logs fakes | homeassistant | mosquitto)
dev/devha.sh health    # push fresh Apple Health sensor values from the Dev iPhone
dev/devha.sh down | reset
```

- UI: `http://localhost:8124/api/hassio_ingress/dev/` (fake ingress, sets `X-Ingress-Path` and `X-Remote-User-*`). HA: `http://localhost:8123`, `dev` / `dev-password-1`.
- Second athlete: `dev2` / `dev2-password-1`, a non-admin in HA's read-only group with Dev iPhone 2. In a browser, `localhost:8124/_dev/as/dev2` switches the ingress user (`/_dev/as/dev` back; any other name is an unknown user). The fake Supervisor sits on 172.30.32.2 like the real one, so the add-on's ingress IP check runs unchanged.
- The add-on only sits on an internal Docker network: any call to a real service fails. Strava, Claude and Zwift are fakes in `dev/fakes/`.
- Fake controls: Strava `localhost:8101/_dev/{ride,fail,reset}`, Claude `localhost:8102/_dev/{mode,requests}` (modes: ok, invalid-plan, invalid-plan-always, 429, 500, refusal), Zwift `localhost:8103/_dev/files`, pushes to the Dev iPhone `localhost:8124/_dev/push`.
- Point the `home-assistant` MCP server at it: `HA_URL=http://localhost:8123 HA_TOKEN=$(cat dev/.state/ha_token)`.

When you add a feature that touches HA, Strava, Claude or the ingress, extend `dev/smoke.py` with a check for it (and the fake, if it needs a new response shape) in the same change. A failing smoke check is a bug until proven to be a fake's fault. Fix real bugs with a regression test in `tests/`.

What this does not prove: real Supervisor behaviour (AppArmor, roles, the HA frontend iframe), APNs delivery, and that the fakes match the real Strava and Anthropic responses. After a release that touches those, still check on the box.
