# Changelog

## 0.4.3 (2026-10-16)

The add-on can now talk to Home Assistant. A startup bug had blocked that in every earlier version, so these
features never worked:

- **Apple Health:** Profile now lists your iPhone's health sensors and fills them in. Before, it said "Couldn't read
  the sensor list from Home Assistant".
- **Sensors:** `sensor.training_ftp`, `_wkg`, `_ctl`, `_atl`, `_tsb`, `_week_hours`, `_week_km` and
  `_today_session` appear in Home Assistant after the first sync. This needs the MQTT integration.
- **Notifications:** the morning session reminder and the notices about the automatic plan and analysis are sent
  to the notify service set on Settings.
- **Schedules now run on your local time.** Until now the add-on ran on UTC, so every time on
  Settings → Schedules fired 2 hours late in summer and 1 hour late in winter. Check your times after updating. If
  you set them early to make up for the delay, set them back.
- Saving Profile, an FTP change or a plan no longer waits for the Home Assistant sensors to update.

## 0.4.2 (2026-10-16)

- **Generate plan** works again. Since 0.4.0 it showed "Internal Server Error", and the optional automatic weekly
  plan failed the same way.
- The Apple Health sensors from the Home Assistant iPhone app are now found automatically. The first time you open
  **Profile → Apple Health**, resting HR, HRV, sleep and weight are filled in for you. Check them and press
  **Save profile**. Before, the list only showed sensors with "health" in their name, so the iPhone app's sensors
  were missing.
- Sensors in the wrong unit (a 3D printer's filament weight, for example) are never suggested. Neither are sensors
  that have no value yet.

## 0.4.1 (2026-10-16)

Fixes the "Internal Server Error" after updating to 0.4.0.

- Sync no longer gets stuck on a ride that Strava has no data streams for, such as a manually added activity.
  Previously one of these rides made every sync fail, so new rides never got an FTP.
- The home page and ride pages load again. They crashed on rides that had power data but no FTP yet.
- If another stream error stops a sync, the rides fetched so far still get their FTP and TSS. The ride that
  failed is retried on the next sync.
- A failing automatic sync waits a full sync interval before it tries again. Before, it retried every minute
  and used up the Strava rate limit.

## 0.4.0 (2026-10-15)

- **Coach tab:** chat with the coach of the active plan. When you ask for plan changes, the coach proposes
  them with each day shown before and after. Applying a proposal creates a new plan version, and it is
  checked against the same rules as a generated plan. Trips, events and injuries become coach notes that
  every later plan and analysis takes into account.
- **FTP tests per coach:** CTS 2×8 min, ramp, and 20 min / lab. A completed test sets your FTP from the test
  date for 12 weeks. Older rides keep the FTP they had.
- **Apple Health:** pick the HealthKit sensors from the HA iOS app on Profile. Resting HR, HRV and sleep
  trends feed the coach as a readiness signal. Each trend compares the last 7 days with the 28 days before.
- **Settings → Schedules:** sync interval, Zwift refresh, notification time, and an optional automatic weekly
  plan and analysis (off by default). A Claude usage table shows the tokens used per plan, analysis and chat.

## 0.3.1 (2026-10-12)

- Strava sync now respects the daily rate limit too. A rate-limited sync shows a note instead of
  "Sync failed".
- More consistent layout: buttons and inputs share one height, and Profile and Settings use two columns on
  wide screens.

## 0.3.0 (2026-10-12)

- Pick a coach for each plan and analysis: Pogačar, Carmichael or Next Level. Goals, level and sessions per
  week are set on Profile.
- **Send to Zwift:** after you connect your Zwift account, workouts go straight to
  Custom Workouts → Training Coach.
- Workouts show on-screen messages in your coach's voice.

## 0.2.1 (2026-10-12)

- Sunrise and sunset times are now correct. They were 30–40 minutes late, and some sunsets showed an hour
  early.

## 0.2.0 (2026-10-12)

- New Profile tab for athlete data and availability. Settings keeps only the connections and notifications.
- Weather-based planning is removed. Outdoor vs Zwift now comes from season and daylight.

## 0.1.1 (2026-10-11)

- Mobile layout fixes for the analysis page, the header and number wrapping.
