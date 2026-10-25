# Training Coach

A Home Assistant add-on for road and gravel training. It pulls rides from Strava or Garmin Connect, shows a
Strava-style feed with load and power statistics, and uses Claude to plan your week (Zwift workouts as `.zwo`, or
outdoor rides when the season and daylight allow) and to write an in-depth analysis, with a choice of three coaching
styles. You can chat with the coach to change the week, and it can use recovery data from Apple Health (through the
Home Assistant iPhone app) or Garmin.

## Install

In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
`https://github.com/johanlasses/training-coach`, then install **Training Coach**.
Setup steps are in [training_coach/DOCS.md](training_coach/DOCS.md).

## How it works

- **Sync** (`app/sync.py` over a `Source`: `app/strava.py` or `app/garmin.py`, picked by `State.source`): imports
  cycling activities and their power and HR streams (Garmin: parsed from the original FIT file). Only derived metrics
  are stored, not raw streams.
- **Garmin** (`app/garmin.py`): the official Personal API with the athlete's own key. The key mints a 24-hour token
  (a bare JWT); health reads (HRV `lastNightAvg`, sleep, resting HR, weight) backfill 35 days. Only a 401 marks the
  key refused; any other failure is treated as an outage. The OpenAPI spec is in `dev/fakes/garmin_personal_openapi.json`.
- **Metrics** (`app/metrics.py`): NP, IF, TSS (hrTSS when there's no power), power curve, eFTP (95% of best 20 min or best 60 min over 90 days), CP/W′, CTL/ATL/TSB, Pw:HR decoupling.
- **Coaches** (`app/coaches.py`, `app/coaches/*.md`): three coaching styles. The markdown is appended to the plan and analysis
  prompts. Per-coach style rules (back-to-back hard days, max hard sessions) feed the plan validator.
- **Coach** (`app/coach.py`): Claude returns a schema-constrained weekly plan. Code then checks availability, hard-day rules,
  weekly TSS vs. ramp limits and workout step totals. A failing plan is retried once with the errors.
- **Analysis** (`app/analysis.py`): a Markdown report built from a JSON snapshot of the athlete (`app/stats.py`).
- **Chat** (`app/chat.py`): a structured reply with optional plan changes (merged into the active plan, validated, and
  applied by the user as a new plan version), coach notes (`coach_notes` table) and memory facts (`athlete_memory`).
  The notes feed every later snapshot.
- **Memory** (`app/memory.py`): `athlete_history` in every snapshot and in the ride feedback context. It holds the coach
  timeline from the active plans, Profile changes (`athlete_log`, written by `apply_form`), notes that ended or were
  removed (notes are soft-deleted), FTP results and the memory facts. `coach_in_charge` decides who answers in chat and
  writes ride feedback: the week's plan's coach, unless a coach switch on Profile is newer than that plan.
- **FTP tests** (`app/ftptest.py`): plan days carry `test_protocol`, and the estimate comes from the test ride's best efforts.
- **Apple Health** (`app/health.py`): reads the HA iOS app's HealthKit sensors through the Supervisor API, stores daily
  values (`health_daily`), and adds 7- and 28-day trends to the snapshot.
- **Ride feedback** (`app/feedback.py`): after each sync, rides from the last 48 h get a Haiku-written verdict
  (planned vs actual per work segment, new bests, week so far, health) and a companion-app notification that opens
  the ride. Each ride is handled once (`activities.notified`).
- **Daily check-in** (`app/checkin.py`): on the first page view of the day with a plan or a recent ride
  (`State.maybe_checkin`, kv `checkin_tried`), Haiku writes a short message into the chat (`chat_messages.kind =
  'checkin'`) from the days since the last check-in, this week, form, readiness and today's session.
- **Overview status** (`app/overview.py`): form, week vs plan, CTL/FTP trend and the next event, from plain numbers.
  The gauges (`overview.gauges`) are server-rendered SVG; their health bands come from `health.READINESS`, the
  thresholds `coach.HEALTH_RULES` is written from.
- **Models** (`llm.MODELS`): Opus for plan and analysis, Sonnet for chat, Haiku for ride feedback and the check-in.
- **Scheduler** (`State.scheduler` in `app/main.py`): a one-minute tick with configurable slots. Every Claude call is logged
  in `llm_calls` for the usage table.
- **Athletes** (`Athletes`, `athlete()` in `app/main.py`): one `State` and SQLite DB per Home Assistant user, picked from
  Supervisor's `X-Remote-User-Id` ingress header. HA's owner owns the add-on and approves the others, who share the
  owner's Claude key and Strava app but nothing else.
- **UI**: FastAPI and Jinja behind HA ingress, Chart.js (vendored), with no build step.

## Develop

```bash
uv venv --python 3.13 .venv && uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest

# Run with demo data (no Strava needed)
TC_DATA_DIR=/tmp/tc-demo .venv/bin/python tools/seed_demo.py
cd training_coach && TC_DATA_DIR=/tmp/tc-demo ../.venv/bin/python -m uvicorn app.main:app --port 8099
```

Optional for local runs: `ANTHROPIC_API_KEY`, `HA_URL` + `HA_TOKEN` (notifications), and
`MQTT_HOST`/`MQTT_USER`/`MQTT_PASSWORD` (sensors).

Test against a local Home Assistant (real add-on image, HA Core, Mosquitto, fake Supervisor/Strava/Claude/Zwift),
needs Docker: `dev/devha.sh up && .venv/bin/python dev/smoke.py`. Details in `.claude/skills/local-ha/SKILL.md`.

Check the UI at iPhone size (needs the server above running; uses your installed Chrome):

```bash
uv pip install playwright
.venv/bin/python tools/mobile_check.py            # all pages: overflow check + screenshots in .shots/
```

Release: `/release-addon` in Claude Code (tests, version bump, commit, push).
