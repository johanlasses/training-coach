# Training Coach

A personal training dashboard, AI coach and Zwift workout planner for road and gravel riding.
It appears as **Training** in the Home Assistant sidebar.

## Setup

Open the panel and go to **Settings**:

1. **Claude API key**: create one at <https://console.anthropic.com/settings/keys>. It is checked when you save.
2. **Strava API app** (only if your rides come from Strava): create an API application at
   <https://www.strava.com/settings/api> with *Authorization Callback Domain* `localhost`, and enter its Client ID and
   Client secret.
3. Set a notify service (e.g. `notify.mobile_app_<your_iphone>`). You get a push after every ride with the coach's
   verdict, and a morning push with the day's session.

Then open **Profile**:

- Set your goals, level, sessions per week, weight, threshold heart rate and availability. The coach never plans
  outside the availability limits.
- Under **Connections**, pick where your rides come from (**Strava** or **Garmin Connect**) and your health data
  (**Apple Health** or **Garmin Connect**), and connect them:
  - **Strava**: click **Connect with Strava** and approve. Strava redirects to a `http://localhost/strava-callback?...`
    page that won't load. Copy that full address and paste it into the box below the button.
  - **Garmin**: see [Garmin Connect](#garmin-connect).
  - **Zwift**, to send workouts straight to Zwift: see [Zwift workouts](#zwift-workouts).

## Coaches

You pick a coach per sport on **Profile → Sports**. Cycling:

- **Pogačar (San Millán)**: lots of zone 2, few but hard sessions.
- **Carmichael (Time-Crunched)**: little time and high intensity, with 3–4 interval sessions a week.
- **Next Level**: takes you from your current level to the next with classic periodisation.

Running:

- **Almgren (double threshold)**: easy running, controlled threshold work, hills and strides; double threshold only
  with high running volume (about 75 km a week).
- **Next Level Run**: 80/20 running around work and family, with a growing long run, deloads and strength work.

### Two sports

Each sport is **Active** or **On hold**, with an optional end date after which it goes back on hold by itself (a
cyclist running for the two weeks the bike can't travel: running active until the trip ends, cycling on hold). At
least one sport stays active; when an end date puts the last one on hold, your **primary sport** comes back.

With both active, the primary sport's coach plans first and the other coach plans around those sessions. A day may
hold a ride and a run if both fit that day's minutes. Hard days and the weekly load cap count both sports: a hard
run the day after bike intervals is two hard days in a row. The Plan page shows one week, each session tagged
**Ride** or **Run**; runs are text only (pace, HR ceiling, structure). In the Coach chat a picker sets who you're
writing to, and each coach only changes their own sport's days.

**Run volume comes from your running history, not your cycling fitness.** A fit cyclist's heart is ready long before
tendons and bones are, so the coach keeps the week's running within about 10% of your last 4 weeks (with a
return-to-run floor of 60–90 minutes a week), no single run much longer than your longest of the last 30 days, and
no hard runs in your first 2 weeks back after a break. Set your threshold pace (what you could race for an hour,
e.g. `4:45`) and running threshold HR on Profile; without them, the pace is estimated from your runs and the HR is
your cycling threshold HR + 5.

Runs, walks and hikes sync from Strava or Garmin. Walks count toward load at an easy 20 TSS an hour, never notify,
and a walk of 45 minutes or more completes a planned recovery session.

**Plan** and **Analysis** work with your coach from Profile. Switch coach there and the Plan page shows that coach's
plan for the week, or offers to make one. A week your coach didn't plan (an old one, say) shows the plan that was
used, marked with who made it. The active plan drives today's session, the sensor and the morning notification;
if your coach's plan isn't the active one yet, **Use this plan** makes it so. Each coach's philosophy lives in
`app/coaches/<name>.md`.

The first sync imports 12 months of rides. Power detail (NP, power curve, zones) is fetched gradually so the rate
limits aren't hit, which can take a few hours.

## Overview and ride feedback

**Overview** opens with your status. A row of small gauges comes first:
- **Form**: TSB, coloured by the same bands the planner uses.
- **Week**: TSS done against the plan. The tick marks where you'd be if you'd ridden every session up to today.
- **Sessions**: sessions done of those planned this week, and how many were missed.
- **Fitness**: your CTL over the last six weeks as a small line, with today's CTL, your FTP and the 4-week change.
- **HRV** and **Sleep**, or resting HR when there's no sleep data: your last 7 days against your own 28-day
  baseline, coloured by the same readiness thresholds the coach plans with. They show once there's enough data
  (5 days in the last week, 14 in the baseline).

Below the gauges: your **next session** (when, how long, and what it's for in one sentence; tap it for the Plan)
and the countdown to the **next event** you told the coach about, with where CTL lands by then at the current ramp.
Then **Rides**: the latest first, with the coach's feedback, then the ones before it. The charts come after that.

Rides with power are drawn like the planned sessions: blocks coloured by zone. When the ride was a planned Zwift
workout, the planned profile sits under it on the same time axis and power scale, so a faded rep shows.

**After every new ride** the coach of that week's plan writes a short verdict: how it went against the plan, a
number that backs it up, and what's next. It reads your Apple Health trends too. The verdict is the notification:
title and 2–4 sentences, readable on the lock screen. Tap it to open the ride, where the longer version is.
This runs whenever a Claude API key is set. It isn't a schedule: it's tied to a ride you just did. Only rides from
the last 48 hours get one, so the first sync doesn't flood your phone. **Ask again** on a ride page writes it anew.

**Daily check-in.** The first time you open the add-on each day, your coach leaves a short, friendly message in the
**Coach** chat. Its job is to make you want to train, not to grade you:
- It credits what you did since the last check-in, with a number. A session moved to another day, a ride the day
  after a missed one, and today's session ridden early all count.
- A missed session gets no guilt. If it matters for the week, tap **Adjust my week** under the message, and the coach
  proposes a rebalanced week in the chat. It won't cram the missed work in, and you apply the proposal as usual.
- After a few missed days in a row, or when your HRV, sleep or resting HR are off, it asks how you're doing.
- If you've already ridden today, it doesn't tell you to ride again.

The add-on syncs your rides first, so last night's ride isn't counted as missed. A dot on the **Coach** tab shows a
check-in you haven't read. It needs a Claude API key, runs on Haiku (about $0.001 a day), and only when there's
something to talk about: a plan this week or a ride in the last 7 days.

## Coach chat

The **Coach** tab is a chat with your coach: the coach of this week's active plan, or the one you switched to on
**Profile** since that plan was made. A new coach takes over right away, even mid-week, and sees the earlier
conversation. Ask about your training, or tell it what's coming up: "I'm away Thursday to Saturday", "there's an
80 km gravel race on 8 November", "add an FTP test". The chat opens on the latest message. On a computer Enter sends
and Shift+Enter starts a new line; on the phone, tap **Send**.

- **Plan changes** come as a proposal listing each day before and after. Nothing changes until you press
  **Apply to plan**. That saves a new version of the week's plan and makes it the active one. The change is checked
  against the same rules as a generated plan, and any rule it still breaks is shown next to it.
- **Coach notes** are things that matter beyond this week: trips, secondary events, injuries. The coach saves them on
  its own. They're listed on the Coach and Profile pages, can be deleted there, and every later plan and analysis
  takes them into account until they end. Your main goal stays the one on **Profile**.
- Each message sends the last 20 messages plus your training data. **Clear** starts over and keeps the notes and
  the memory.

## What the coach remembers

The coach keeps a history of you, so a new coach knows where you come from. Every plan, analysis, chat reply and ride
verdict gets:

- **Who coached you** and for how many weeks, taken from the active plans and coach switches on **Profile**.
- **What changed on Profile** and when: goals, coach, level, sessions per week, weight, FTP override, threshold HR and
  availability.
- **Earlier notes** from the last year: past injuries, illnesses, trips and events, also the ones you deleted.
- **Your FTP test results.**
- **Lasting facts** the coach picked up in the chat, such as your background, what works for you, what doesn't, and
  constraints like shift work.

After a switch, the new coach builds on the previous coach's training instead of starting over. The weekly analysis
also reads the previous analysis and says what changed since. **Profile → What the coach remembers** lists the facts,
and you can remove any that are wrong. **Profile → History** shows the rest.

## FTP tests

Each coach has its own test, and plans it when you ask, or when a test is due under its approach:

- **Carmichael**: the CTS Field Test, 2 × 8 min all-out. FTP is 90% of the better effort.
- **Next Level**: the ramp test, 1-minute steps until you can't hold the target. FTP is 75% of your best minute.
  Zwift's built-in *Ramp Test* works too.
- **Pogačar (San Millán)**: a lab lactate test if you have access to one, otherwise a 20-minute test. FTP is
  95% of the 20 minutes.

Ride the all-out efforts with ERG off. Once the ride has synced, the test day on **Plan** shows the estimated FTP
and a **Set as FTP** button. That sets the FTP override on Profile, which stays until you clear it.

## Garmin Connect

Garmin can be the source of your rides, your health data, or both (**Profile → Connections**). It uses Garmin's
official Personal API with a key you create yourself:

1. On Garmin Connect in a browser, open **Personal API Key** (<https://connect.garmin.com/modern/settings/personalApiToken>),
   choose **Create Key**, accept the terms and pick how long it lasts. A year means fewer renewals.
2. Garmin shows the key once. Copy it and paste it into **Connect Garmin** under **Connections**. Once it's
   connected, Garmin becomes selectable under **Rides** and **Health**.
   The add-on checks it right away. It's stored only in the add-on and never shown again.

**Rides**: all cycling activities (road, gravel, MTB, indoor and Zwift rides that reach Garmin), with power and heart
rate from the original FIT file. Walks and other sports are left out for now.
**Health**: overnight HRV, sleep, resting heart rate and weight. The first read brings in the last five weeks, so the
coach sees trends right away. After that it reads every three hours.

When the key expires (Garmin emails you a week before), Profile says so: create a new one and paste it in.
Each athlete uses their own key: Garmin's Personal API is for your own data.

**Switching source** (Strava ↔ Garmin for rides, Apple Health ↔ Garmin for health) removes what the old source
brought in, and the new one fills it in again: 12 months of rides, or five weeks of health data from Garmin. That
keeps the same ride from counting twice, and Garmin's HRV is a different measurement from Apple's, so the two must
not share a baseline. Plans, chat, notes and memory are kept.

## Apple Health

With Apple Health data, the coach can see how recovered you are: resting heart rate, HRV, sleep, weight and VO2 max.
It eases off when HRV drops or resting HR rises over several days, not on a single bad night. Pick **Apple Health**
under **Profile → Connections**.

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
| Every 30 min | Sync new rides from Strava or Garmin (15 min to 4 h, or off) |
| Daily | Refresh the Zwift login |
| Hourly | Read the Apple Health sensors from Home Assistant, if set up (local, free) |
| Every 3 hours | Read health data from Garmin, if that's your health source |
| Daily 07:00 | Push today's session to your notify service |
| After each new ride | The coach's feedback, sent as a notification (needs a Claude API key; about $0.001 a ride) |
| First visit each day | The coach's daily check-in in the chat (needs a Claude API key; about $0.001 a day) |
| Off | Plan next week with your default coach (e.g. Sunday 20:00). Weeks that already have a plan are skipped |
| Off | Weekly analysis (e.g. Monday 07:00) |

A scheduled job only starts in the 3 hours after its time, so a restart in the afternoon doesn't run the morning's jobs.
**Claude usage** on the same page shows calls, tokens and the estimated cost per call type, by week, by month and in
total.

## Which Claude model does what

| Call | Model | Why |
|---|---|---|
| Weekly plan | Sonnet 5.5, high effort | Hard rules, a validator and a structured workout; it runs about once a week |
| Analysis | Sonnet 5.5, high effort | The deep read of your training; about once a week |
| Coach chat | Sonnet 5.5, medium effort | Interactive, so it's faster. Plan edits are still checked and retried |
| Ride feedback | Haiku 5.5 | The add-on computes the numbers; the model only judges and writes. It runs after every ride |
| Daily check-in | Haiku 5.5 | Same idea as ride feedback: the numbers are computed, the model writes 2–4 sentences |

## Indoor workouts: Zwift or MyWhoosh

Each indoor session carries on-screen messages in the coach's voice: the coach's own cues through the
workout, plus a rep counter with a cheer on every interval rep. Pick where they go under **Profile → Connections →
Indoor app**: **Zwift** or **MyWhoosh**, the free alternative. Plans are the same either way. Every indoor session
on the **Plan** page has a **Send to …** button, past sessions too, so a missed one can be ridden later. Until the
app is connected, the button says **Connect …** and takes you to its card.

**Send to MyWhoosh.** Sign in on the **MyWhoosh** card with your MyWhoosh email and password, then pick MyWhoosh
under **Indoor app**: a choice only becomes selectable once it's connected. The password is used once and not
stored. The workout shows up in MyWhoosh under *My Workouts*, marked as a favourite, with power as a share of the FTP
set in MyWhoosh. It also goes on your MyWhoosh calendar at 20:00 on its day (today, for a past session you're
riding late), so you don't have to search for it. Sending a day again replaces that day's workout and its
calendar entry. MyWhoosh has no public API, so the add-on signs in like its app
does. Workouts sent this way can only be deleted in the MyWhoosh app, and MyWhoosh limits how many custom workouts
you can have: if it's full, the button says so, and deleting a few old ones in the app makes room. If the login
expires, Profile asks you to sign in again.

**Send to Zwift.** Sign in on the **Zwift** card. Your password is used once to sign in and is not stored.
**Restart Zwift** after sending; the workout appears under *Workouts → Custom Workouts → Training Coach*.
Sending again replaces the workout. Zwift has no public API for this, so the add-on uses the same file
sync the Zwift app uses. If Zwift changes it, the button reports an error. If the login expires, Profile asks you
to sign in again.

## Several athletes

Other Home Assistant users can have their own Training Coach: their own Strava or Garmin and Zwift accounts, Profile
(training days, hours per session, goals), plan, coach chat, Apple Health and notifications. Home Assistant's owner
(the user who set HA up) owns the add-on and decides who gets in.

1. Create the person in **Settings → People** with login allowed and *not* an administrator.
2. They open **Training** in the sidebar and get an "ask for access" page. If a non-admin doesn't see **Training**
   at all (Home Assistant can keep the panel admin-only from an older version), turn **Show in sidebar** off and on
   again on the add-on's Info page, or restart Home Assistant. Don't make them an administrator for it. You approve them under
   **Settings → Athletes**, which also shows what each athlete's Claude calls cost this month.
3. They install the Home Assistant iPhone app and sign in **as themselves**. Their phone is then theirs in the add-on:
   only its notify service and Apple Health sensors can be picked on their Settings and Profile.
4. They connect their own rides on **Profile → Connections**: Garmin with their own key, or Strava. Strava goes
   through your Strava API app: raise its athlete capacity from 1 to 10 on
   [strava.com/settings/api](https://www.strava.com/settings/api) (no review needed). Its rate limit is shared by
   everyone. An athlete can enter their own Strava API app on Settings instead.

If something lands in the wrong place (say your own Strava was still signed in on their phone when they connected),
**Settings → Athletes → Reset … data** clears that athlete's synced activities, health data and/or Strava
connection. You type their name to confirm, their database is backed up first
(`athletes/<id>/training.db.before-reset-<time>`, kept for 30 days), and nobody else's data is touched. The add-on
also refuses a Strava account that's already connected for another athlete.

What's shared: your Claude API key (they can't see or change it) and, unless they set their own, the Strava app.
A Garmin key is always the athlete's own.
What isn't: everything else. Removing an athlete keeps their data, so approving them again brings it back.

**Home Assistant has no per-user permissions.** Hiding dashboards ("Visible only to admins") is cosmetic. A normal
user can still control every device through HA's API. For people outside your household, also put the user in HA's
read-only group (WebSocket `config/auth/update` with `group_ids: ["system-read-only"]`; there's no UI for it). They
can then still read every entity's state, for example who's home. If they connect over Tailscale, share only the HA
machine and allow only port 8123 for `autogroup:shared` in the tailnet policy. Otherwise MQTT, which accepts any HA
user's password, is reachable too.

## Sensors

If the MQTT integration is set up, the add-on publishes `sensor.training_ftp`, `_wkg`, `_ctl`,
`_atl`, `_tsb`, `_week_hours`, `_week_km` and `_today_session` for use on other dashboards. They're the owner's;
other athletes get no sensors.

## Data

Rides, settings, keys and tokens are stored in the add-on's private `/data` folder and are included
in Home Assistant backups. Only cycling activities are imported (Ride, VirtualRide, GravelRide, MountainBikeRide).
