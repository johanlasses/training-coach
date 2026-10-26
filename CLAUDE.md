# Training Coach

A Home Assistant add-on (FastAPI + Jinja, served through HA ingress) for one road and gravel cyclist.
It covers Strava/Garmin sync, load metrics, a Claude-planned week sent to Zwift or MyWhoosh, and a weekly AI analysis.
See README.md for the architecture and `training_coach/DOCS.md` for the user-facing setup.

## Commands

```bash
.venv/bin/python -m pytest -q                                   # full suite, a few seconds
TC_DATA_DIR=/tmp/tc-demo .venv/bin/python tools/seed_demo.py    # demo data, no Strava needed
(cd training_coach && TC_DATA_DIR=/tmp/tc-demo ../.venv/bin/python -m uvicorn app.main:app --port 8099)
.venv/bin/python tools/mobile_check.py                          # iPhone-size overflow check + screenshots
.venv/bin/ruff check training_coach tests tools                 # bugs-only lint (no formatter: the style is compact on purpose)
```

Local HA stack (Docker; the real HA box may be unreachable): `dev/devha.sh up`, then `.venv/bin/python dev/smoke.py`. See the `local-ha` skill.

Project hooks (`.claude/hooks/`): pytest after edits under `training_coach/app/` or `tests/`; ruff and the template rules
(`{{ base }}` URLs, no inline grid styles) after each edit; blocks on plain `git push`, persisted git credentials and
`options:` in `config.yaml`; a Stop reminder when templates/CSS changed without a `mobile_check.py` run.
CI (`.github/workflows/test.yml`) runs ruff and pytest on push.

## Rules

- **Bump `version` in `training_coach/config.yaml` on every release.** HA won't offer the update otherwise. Use `/release-addon`.
- **Push with the personal GitHub account.** The shell's `GITHUB_TOKEN` is the ABB work account, which gets a 403 on
  `johanlasses/training-coach`. `/release-addon` has the one-off credential command. Never persist it.
- **The user is on an iPhone.** Any template or CSS change must pass the `check-mobile` skill before it's done.
  Don't use inline `style="grid-template-columns…"`, because it overrides the responsive media queries.
- **Multi-tenant: every route gets its State from `athlete(request)`**, never `request.app.state.st` (that's the owner's,
  for tests only; `tests/test_athletes.py` greps for it). One DB per HA user: the owner (HA's `is_owner` user) keeps
  `/data/training.db`, others `/data/athletes/<user id>/`. Non-owners get the owner's Claude key and Strava app through
  `State.shared` and must never have them saved (`State.save()`, not `save_config`). Anything a non-owner sets that the
  add-on's admin token acts on (notify service, entities to read) must be checked against their own phone (`own_phone`,
  `ha.user_devices`).
- **Settings live in the DB, not in the add-on options.** `training_coach/config.yaml` has no `options:`. All fields are in
  `app/config.py` (`Config`) and are saved by `apply_form` in `main.py`. The owner's shared credentials (Claude key,
  Strava API app), notifications and schedules go on **Settings** (`templates/settings.html`); athlete data, availability
  and the athlete's own connections (ride and health source, Strava sign-in, Garmin key, Zwift login) go on **Profile**
  (`templates/profile.html`, Connections). Never render secret values back into HTML.
- **Sources:** rides come from `State.source` (Strava or Garmin, `cfg.activity_source`), health from Apple Health via HA or
  Garmin (`cfg.health_source`, `health.enabled`). `sync.sync` takes any `Source`. Switching a source clears what the old
  one brought (ride ids could collide; Garmin rMSSD and Apple SDNN must not share a baseline).
- **Sports (0.11.0):** activities are rides, runs or walks by `sports.kind()`; never compare sport types elsewhere.
  Power numbers (NP, power curve, zones, eFTP, Send to Zwift) are rides only. Plan days carry `sport` and `coach`
  (none = cycling, pre-0.11.0); a date holds at most one session per sport, keyed by `sports.day_key`. One plan row per
  week holds both sports; `memory.plan_coach(plan, sport)` says who planned a sport. Run volume is guarded from
  running history (`stats.running`, `coach.run_errors`), never from CTL. With two sports, the hard-day limit is the
  primary coach's (`coach.week_rules`), whichever coach plans or chats.
- **No weather logic.** It was removed on purpose (too complex, little value). Outdoor vs Zwift comes from season and daylight
  (`app/season.py`), and the athlete swaps days themselves.
- **Ingress:** every URL in templates and redirects must be prefixed with `{{ base }}` / `back(request, …)`.
- **Anthropic SDK 1.x uses httpx2.** respx can't mock it. Inject `httpx2.MockTransport` (see `tests/test_llm.py`).
  One model per purpose in `llm.MODELS`: Sonnet 5.5 for plan and analysis (high effort) and chat (medium), Haiku 5.5
  for ride feedback and the check-in (since 0.11.0, to save money; Opus 5.5 before). Adaptive thinking;
  `fallbacks="default"` with beta `server-side-fallback-2026-07-01` only on Sonnet (Haiku has no server-side fallback).
- **Ride feedback is event-driven, not scheduled:** it runs after each sync for rides from the last 48 h
  (`feedback.after_sync`), once per ride (`activities.notified`). It's always on with an API key.
- **Strava rate limits:** stream fetches are capped per sync run and stop near the 15-minute limit. Keep it that way.
  Tests never hit real APIs.
- **Charts:** use the colour tokens in `static/app.css` (the dataviz reference palette). No dual axes; TSB has its own chart.
- **Verify changes that touch HA, Strava, Claude, MQTT, notifications or ingress with the `local-ha` skill** (`dev/smoke.py`), not just pytest.
  Extend the smoke test and the fakes in `dev/` with the feature.
- After changing `metrics.py`, `coach.py` rules or prompts, or `analysis.py`, run the `training-science-reviewer` agent.
- After changing `main.py`, `config.py`, templates or the Strava/HA/Zwift clients, run the `ha-addon-reviewer` agent.
- When the deployed add-on misbehaves, use the `ha-logs` skill to get the traceback before guessing.

## MCP servers (`.mcp.json`)

- `home-assistant`: HA's own MCP server. Needs `HA_TOKEN` (a long-lived token) exported in the shell; `HA_URL` defaults
  to `http://192.168.68.156:8123`. Use it to check `sensor.training_*` entities and notifications.
- `playwright`: interactive browser via the installed Chrome. For layout checks, prefer `tools/mobile_check.py`.
- `context7`: current library docs. Use it for FastAPI, anthropic 1.x and aiomqtt APIs instead of guessing from memory.
