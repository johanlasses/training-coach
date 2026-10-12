# Training Coach

A Home Assistant add-on for road and gravel training. It pulls rides from Strava (Zwift rides arrive there
automatically), shows a Strava-style feed with load and power statistics, and uses Claude to plan your week
(Zwift workouts as `.zwo`, or outdoor rides when the season and daylight allow) and to write an in-depth
weekly analysis.

## Install

In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
`https://github.com/johanlasses/training-coach`, then install **Training Coach**.
Setup steps are in [training_coach/DOCS.md](training_coach/DOCS.md).

## How it works

- **Sync** (`app/strava.py`, `app/sync.py`): imports cycling activities and their power and HR streams. Only derived
  metrics are stored, not raw streams.
- **Metrics** (`app/metrics.py`): NP, IF, TSS (hrTSS when there's no power), power curve, eFTP (95% of best 20 min or best 60 min over 90 days), CP/W′, CTL/ATL/TSB, Pw:HR decoupling.
- **Coach** (`app/coach.py`): Claude returns a schema-constrained weekly plan. Code then checks availability, back-to-back hard days,
  weekly TSS vs. ramp limits and workout step totals. A failing plan is retried once with the errors.
- **Analysis** (`app/analysis.py`): a Markdown report built from a JSON snapshot of the athlete (`app/stats.py`).
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
