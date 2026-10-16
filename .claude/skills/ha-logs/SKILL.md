---
name: ha-logs
description: Fetch the Training Coach add-on log from Home Assistant and pull out tracebacks. Use when the deployed add-on misbehaves (500s, failed syncs, plans that never finish) before guessing at causes.
---

# Read the add-on log

Get the real traceback before touching code. Local reproductions don't count until the log agrees.

1. **Reach HA.** Needs `HA_TOKEN` (long-lived, admin user) in the shell; `HA_URL` defaults to `http://192.168.68.156:8123`.
   If the host is unreachable (EHOSTDOWN, timeout), the laptop isn't on the home network or Tailscale is down. Say so and stop;
   don't work around it. Don't test live logins from the ABB laptop either (Zscaler breaks TLS).
2. **Fetch** through HA Core's Supervisor proxy. The slug is `<repo hash>_training_coach`; the hash for
   `https://github.com/johanlasses/training-coach` is `43394493`:
   ```bash
   HA=${HA_URL:-http://192.168.68.156:8123}
   curl -sf -H "Authorization: Bearer $HA_TOKEN" -H "Accept: text/plain" \
     "$HA/api/hassio/addons/43394493_training_coach/logs" > /tmp/tc-addon.log; echo "exit $?"
   ```
   A 404 means the slug is wrong (the repo was added under a different URL). Ask the user for the slug shown in the add-on page URL
   (Settings → Add-ons → Training Coach). A 401/403 means the token isn't an admin token.
   Fallback: the user pastes the log from the add-on's **Log** tab.
3. **Extract.** The log is the container's stdout: uvicorn plus `logging` lines in the format `time LEVEL name: message`.
   Access logs are off (`--no-access-log` in `run.sh`), so a 500 shows up only as a traceback.
   ```bash
   grep -nE 'Traceback|ERROR|Exception|429|Rate' /tmp/tc-addon.log | tail -40
   ```
   Read each traceback from `Traceback` to the exception line. The app doesn't log its version. If the traceback doesn't
   match current code, ask which version the add-on page shows. The user may not have updated yet.
4. **Report** the exception, the file and line under `/opt/training_coach/app/`, when it happened, and how often.
   Map it to the repo path (`training_coach/app/...`). Then fix it, with a regression test in `tests/`.

Never print `HA_TOKEN` or paste the log anywhere outside this session. It can contain athlete data.
