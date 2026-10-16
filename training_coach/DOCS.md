# Training Coach

A personal training dashboard, AI coach and Zwift workout planner for road and gravel riding.
It appears as **Training** in the Home Assistant sidebar.

## Setup

Open the panel and go to **Settings**:

1. **Claude API key**: create one at <https://console.anthropic.com/settings/keys>. It is checked when you save.
2. **Strava**:
   - Create an API application at <https://www.strava.com/settings/api> and set *Authorization Callback Domain* to `localhost`.
   - Enter its Client ID and Client secret, then click **Connect with Strava** and approve.
   - Strava redirects to a `http://localhost/strava-callback?...` page that won't load. Copy that full address and paste it into step 3.
3. Optionally set a notify service on **Settings** for a morning push with the day's session.

Then open **Profile** and set your goals, level, sessions per week, weight, threshold heart rate and availability.
The coach never plans outside the availability limits.

## Coaches

There are three coaching styles, and you pick the default one on **Profile**:

- **Pogačar (San Millán)**: lots of zone 2, few but hard sessions.
- **Carmichael (Time-Crunched)**: little time and high intensity, with 3–4 interval sessions a week.
- **Next Level**: takes you from your current level to the next with classic periodisation.

On **Plan** and **Analysis** you can run any coach, so you can compare them. Each coach's plan for a week has its own tab.
The first plan generated for a week is the active one, and it drives today's session, the sensor and the morning
notification. **Use this plan** switches it. Each coach's philosophy lives in `app/coaches/<name>.md`.

The first sync imports 12 months of rides. Power detail (NP, power curve, zones) is fetched gradually so Strava's rate limits aren't hit, which can take a few hours.

## Coach chat

The **Coach** tab is a chat with the coach of this week's active plan. Ask about your training, or tell it what's
coming up: "I'm away Thursday to Saturday", "there's an 80 km gravel race on 8 November", "add an FTP test".

- **Plan changes** come as a proposal listing each day before and after. Nothing changes until you press
  **Apply to plan**. That saves a new version of the week's plan and makes it the active one. The change is checked
  against the same rules as a generated plan, and any rule it still breaks is shown next to it.
- **Coach notes** are things that matter beyond this week: trips, secondary events, injuries. The coach saves them on
  its own. They're listed on the Coach and Profile pages, can be deleted there, and every later plan and analysis
  takes them into account until they end. Your main goal stays the one on **Profile**.
- Each message sends the last 20 messages plus your training data. **Clear chat** starts over and keeps the notes.

## FTP tests

Each coach has its own test, and plans it when you ask, or when a test is due under its approach:

- **Carmichael**: the CTS Field Test, 2 × 8 min all-out. FTP is 90% of the better effort.
- **Next Level**: the ramp test, 1-minute steps until you can't hold the target. FTP is 75% of your best minute.
  Zwift's built-in *Ramp Test* works too.
- **Pogačar (San Millán)**: a lab lactate test if you have access to one, otherwise a 20-minute test. FTP is
  95% of the 20 minutes.

Ride the all-out efforts with ERG off. Once the ride has synced, the test day on **Plan** shows the estimated FTP
and a **Set as FTP** button. That sets the FTP override on Profile, which stays until you clear it.

## Apple Health

With Apple Health data, the coach can see how recovered you are: resting heart rate, HRV, sleep, weight and VO2 max.
It eases off when HRV drops or resting HR rises over several days, not on a single bad night.

1. In the Home Assistant iPhone app (2026.8 or later): **Settings → Companion App → Sensors → Apple Health**.
   Enable the sensors you want and allow access in the iOS prompt. The feature is marked *Labs*.
   The ones the coach uses are Resting Heart Rate, Heart Rate Variability and Sleep Duration, plus Weight and VO2 Max.
   If you dismissed the prompt, allow access under iOS **Settings → Health → Data Access & Devices → Home Assistant**.
2. Open the Home Assistant app once, so the phone sends its first values. They appear in Home Assistant as
   `sensor.<phone>_resting_heart_rate`, `sensor.<phone>_heart_rate_variability`, `sensor.<phone>_sleep_duration`,
   `sensor.<phone>_weight` and `sensor.<phone>_vo2_max`.
3. In the add-on, open **Profile → Apple Health**. The first time, the sensors it found are filled in for you
   ("Found in Home Assistant"). Check them and press **Save profile**. You can also pick or type any other sensor.

The coach needs about a week of values before it sees a trend, and five weeks for the full baseline.

The add-on reads them from Home Assistant every hour and keeps one value per day. The iPhone app stamps a value
with the time it was sent, not the time it was measured, so a value counts for the day it was read.
If the iPhone app doesn't offer a metric, an app like *Health Auto Export* can push it to Home Assistant as a
sensor, and you can pick that sensor instead.

## What runs automatically

You control all of it on **Settings → Schedules**. Claude jobs are off until you switch them on.

| Default | What |
|---|---|
| Every 30 min | Sync new rides from Strava (15 min to 4 h, or off) |
| Daily | Refresh the Zwift login |
| Hourly | Read the Apple Health sensors from Home Assistant, if set up (local, free) |
| Daily 07:00 | Push today's session to your notify service |
| Off | Plan next week with your default coach (e.g. Sunday 20:00). Weeks that already have a plan are skipped |
| Off | Weekly analysis (e.g. Monday 07:00) |

A scheduled job only starts in the 3 hours after its time, so a restart in the afternoon doesn't run the morning's jobs.
**Claude usage** on the same page shows calls and tokens per plan, analysis and chat, by week, by month and in total.

## Zwift workouts

Each Zwift session carries on-screen messages in the coach's voice: the coach's own cues through the
workout, plus a rep counter with a cheer on every interval rep.

**Send to Zwift.** Connect your Zwift account under **Settings → Zwift**. Your password is used once to
sign in and is not stored. Upcoming Zwift sessions on the **Plan** page then get a **Send to Zwift** button.
**Restart Zwift** afterwards; the workout appears under *Workouts → Custom Workouts → Training Coach*.
Sending again replaces the workout. Zwift has no public API for this, so the add-on uses the same file
sync the Zwift app uses. If Zwift changes it, the button reports an error, and the download below still works.
If the login expires, Settings asks you to sign in again.

**Download .zwo.** You can always save the file to `Documents/Zwift/Workouts/<your Zwift id>/` yourself
instead.

## Sensors

If the MQTT integration is set up, the add-on publishes `sensor.training_ftp`, `_wkg`, `_ctl`,
`_atl`, `_tsb`, `_week_hours`, `_week_km` and `_today_session` for use on other dashboards.

## Data

Rides, settings, keys and tokens are stored in the add-on's private `/data` folder and are included
in Home Assistant backups. Only cycling activities are imported (Ride, VirtualRide, GravelRide, MountainBikeRide).
