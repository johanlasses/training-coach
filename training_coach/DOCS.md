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

Then open **Profile** and set your weight, threshold heart rate and availability. The coach never plans outside those limits.

The first sync imports 12 months of rides. Power detail (NP, power curve, zones) is fetched gradually so Strava's rate limits aren't hit, which can take a few hours.

## What runs automatically

| When | What |
|---|---|
| Every 30 min | Sync new rides from Strava |
| Sunday 18:00 | Plan the coming week |
| Monday 06:00 | Weekly AI analysis |
| Daily 07:00 | Push today's session to your notify service |

## Zwift workouts

On the **Plan** page, Zwift sessions have a **Download .zwo** button. Save the file to
`Documents/Zwift/Workouts/<your Zwift id>/`. It shows up in Zwift under *Custom Workouts*.

## Sensors

If the MQTT integration is set up, the add-on publishes `sensor.training_ftp`, `_wkg`, `_ctl`,
`_atl`, `_tsb`, `_week_hours`, `_week_km` and `_today_session` for use on other dashboards.

## Data

Rides, settings, keys and tokens are stored in the add-on's private `/data` folder and are included
in Home Assistant backups. Only cycling activities are imported (Ride, VirtualRide, GravelRide, MountainBikeRide).
