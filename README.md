# Training Coach

A Home Assistant add-on for road and gravel training. It pulls rides from Strava (Zwift rides arrive there
automatically), shows a Strava-style feed with load and power statistics, and uses Claude to plan your week
(Zwift workouts as `.zwo`, or outdoor rides when the season and daylight allow) and to write an in-depth
analysis, with a choice of three coaching styles. You can chat with the coach to change the week, and it can use
Apple Health recovery data from the Home Assistant iPhone app.

## Install

In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
`https://github.com/johanlasses/training-coach`, then install **Training Coach**.
Setup steps are in [training_coach/DOCS.md](training_coach/DOCS.md).

## How it works

- **Sync** (`app/strava.py`, `app/sync.py`): imports cycling activities and their power and HR streams. Only derived
  metrics are stored, not raw streams.
- **Metrics** (`app/metrics.py`): NP, IF, TSS (hrTSS when there's no power), power curve, eFTP (95% of best 20 min or best 60 min over 90 days), CP/W′, CTL/ATL/TSB, Pw:HR decoupling.
- **Coaches** (`app/coaches.py`, `app/coaches/*.md`): three coaching styles. The markdown is appended to the plan and analysis
  prompts. Per-coach style rules (back-to-back hard days, max hard sessions) feed the plan validator.
- **Coach** (`app/coach.py`): Claude returns a schema-constrained weekly plan. Code then checks availability, hard-day rules,
  weekly TSS vs. ramp limits and workout step totals. A failing plan is retried once with the errors.
- **Analysis** (`app/analysis.py`): a Markdown report built from a JSON snapshot of the athlete (`app/stats.py`).
- **Chat** (`app/chat.py`): a structured reply with optional plan changes (merged into the active plan, validated, and
  applied by the user as a new plan version) and coach notes (`coach_notes` table). The notes feed every later snapshot.
- **FTP tests** (`app/ftptest.py`): plan days carry `test_protocol`, and the estimate comes from the test ride's best efforts.
- **Apple Health** (`app/health.py`): reads the HA iOS app's HealthKit sensors through the Supervisor API, stores daily
  values (`health_daily`), and adds 7- and 28-day trends to the snapshot.
- **Ride feedback** (`app/feedback.py`): after each sync, rides from the last 48 h get a Haiku-written verdict
  (planned vs actual per work segment, new bests, week so far, health) and a companion-app notification that opens
  the ride. Each ride is handled once (`activities.notified`).
- **Overview status** (`app/overview.py`): form, week vs plan, CTL/FTP trend and the next event, from plain numbers.
- **Models** (`llm.MODELS`): Opus for plan and analysis, Sonnet for chat, Haiku for ride feedback.
- **Scheduler** (`State.scheduler` in `app/main.py`): a one-minute tick with configurable slots. Every Claude call is logged
  in `llm_calls` for the usage table.
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

Check the UI at iPhone size (needs the server above running; uses your installed Chrome):

```bash
uv pip install playwright
.venv/bin/python tools/mobile_check.py            # all pages: overflow check + screenshots in .shots/
```

Release: `/release-addon` in Claude Code (tests, version bump, commit, push).
