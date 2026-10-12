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

## What runs automatically

Plans and analyses never run on their own. Claude is only called when you click.

| When | What |
|---|---|
| Every 30 min | Sync new rides from Strava |
| Daily 07:00 | Push today's session to your notify service |

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
