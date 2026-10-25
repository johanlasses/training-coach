# Changelog

## 0.9.0 (2026-10-26)

- **Garmin Connect as a source.** Your rides, your health data, or both can now come straight from Garmin, through
  Garmin's official Personal API with a key you create on Garmin Connect. Rides come with power and heart rate from
  the original file from your watch or bike computer. Health means overnight HRV, sleep, resting heart rate and
  weight, and the last five weeks come in right away, so the coach and the Overview gauges see your trends from day one.
  Strava and Apple Health work as before.
- **Your connections are on Profile now.** Profile → **Connections** is where you pick where rides and health data
  come from, and connect Strava, Garmin and Zwift. Settings keeps the Claude key, the Strava API app, notifications
  and schedules. Connect Garmin before you switch to it: switching removes what the old source brought in, and the
  new one fills it in again. Plans, chat, notes and memory are kept. A **Check again** button re-tests your Garmin key.
- E-bike rides are left out when they come from Garmin, like on Strava: the motor's help isn't your training load.
- The coach no longer compares HRV numbers across sources or with other people: only your own trend counts.
- If you record a Zwift ride on Zwift and on your watch, Garmin may have it twice: delete one of them in Garmin
  Connect for now.

## 0.8.0 (2026-10-25)

- **Your coach checks in every day.** The first time you open the add-on each day, your coach leaves a short,
  friendly message in the **Coach** chat: what went well since the last check-in, and what today is about. Moving a
  session to another day, getting back on the bike after a missed one, or riding today's session early all count.
  A missed session gets no blame. If it matters for the week, an **Adjust my week** button under the message asks
  the coach to rebalance the rest of the week, without cramming the missed work in. After a few missed days, or
  when your recovery numbers are off, the coach asks how you're doing. The add-on syncs your rides first, so last
  night's ride isn't counted as missed. A dot on the **Coach** tab shows a check-in you haven't read. It runs on Haiku
  with your Claude key, about $0.001 a day.
- **Readiness at a glance.** The Overview status card opens with small gauges: form, this week's TSS against the
  plan (with a tick for where you should be by now), and HRV and sleep against your own baseline (resting HR shows
  instead when that's the number that's off). The health gauges use the same thresholds the coach plans with, so
  they never disagree with the plan.
- The menu fits on smaller iPhones (SE and mini): "Settings" is no longer cut off.

## 0.7.0 (2026-10-25)

- **Other people in your household can have their own Training Coach.** Every Home Assistant user gets their own
  Strava, Zwift, Profile, plan, coach chat, Apple Health and notifications, kept apart from everyone else's.
  You stay the owner: when someone opens the panel for the first time they see a "no access" page, and you approve
  them under **Settings → Athletes**, where you also see what each one costs in Claude usage.
- **They use your Claude key and Strava app, but never see them.** Others can only send notifications to their own
  phone and read Apple Health sensors from their own phone.
- **The panel is now visible to all Home Assistant users**, not just admins. Only athletes you approved get in.
- Your own data and settings are untouched. The Overview sensors in Home Assistant stay yours.

## 0.6.1 (2026-10-22)

- **Notifications reach your iPhone again.** The add-on was sending to a notify service Home Assistant doesn't have,
  so ride feedback, the morning session and job notices never arrived. **Settings** now lists the notify services
  Home Assistant really has, and warns in red when the one you saved doesn't exist. When it can tell which phone you
  meant, it fills that in. **After updating, open Settings and press Save once** to switch to it.
- **Apple Health reads show in the add-on log.** Each hourly read logs which numbers were stored and why any were
  skipped, e.g. not reported today yet.

## 0.6.0 (2026-10-19)

- **The Coach chat looks like a messaging app.** It opens on the latest message, only the messages scroll, and the
  message box stays at the bottom. Messages are grouped by sender with the coach's initials and the time, with day
  dividers. The box grows as you type and no longer makes the iPhone zoom in. On the phone, your coach notes sit
  under **Notes** at the top. While the coach is typing, the page reloads once when the reply is in, and keeps
  anything you've started writing.
- **The coach remembers you.** Every plan, analysis, chat reply and ride verdict now knows who coached you before
  and for how long, what you changed on Profile (goals, weight, FTP override, availability, coach) and when, earlier
  injuries, illnesses, trips and events, your FTP tests, and lasting facts the coach picked up in the chat.
  **Profile → What the coach remembers** lists those facts, and you can remove any that are wrong.
  **Profile → History** shows the rest.
- **Switching coach takes effect right away.** Pick a new coach on Profile and it takes over the chat and ride
  feedback immediately, even mid-week. It knows it's taking over, builds on what your previous coach did and phases
  in its own approach instead of starting over. If it changes this week's plan, the rest of the week has to follow
  its rules.
- **The weekly analysis reads the previous one** and says what changed since, or that it's too early to tell.
- Deleted coach notes are kept in your history instead of being erased. After updating, don't go back to 0.5.0:
  it would bring deleted notes back.

## 0.5.0 (2026-10-16)

Built for the Home Assistant iPhone app.

- **A notification after every ride, with the coach's verdict.** When a new ride syncs, the coach of that week's plan
  judges it against the plan (rep by rep for Zwift workouts), with your form, the week so far and your Apple Health
  trends, and says what's next. The verdict is the notification: tap it to open the ride and read the longer version.
  It runs whenever a Claude API key is set, with no schedule to switch on. Only rides from the last 48 hours get one.
  **Ask again** on a ride page writes it anew.
- **A status card at the top of Overview:** form, this week against the plan (sessions done, TSS done of planned,
  what's next), CTL and FTP over the last weeks, and the countdown to your next event with where CTL lands by then.
- **The latest ride comes right after it**, with the coach's verdict. Then the five before it. Older rides fold away,
  and the charts come after the rides on phones.
- **Rides look like the plan:** power is drawn as blocks coloured by zone, like the planned sessions. When the ride was
  a planned Zwift workout, the plan sits under it on the same time axis and scale, so a faded rep shows. Power detail
  is now sampled about every 15 s on an hour's ride, so intervals stay visible. Rides synced before this update keep
  the coarser 30 s detail.
- **Cheaper Claude usage.** Plan and analysis stay on Opus 5.5, chat moves to Sonnet 5.5, and ride feedback uses
  Haiku 5.5 (about $0.001 a ride). **Settings → Claude usage** shows the model and estimated cost per call type.
- **The feedback reads the ride the way a coach would.** The plan is lined up with the ride first, so starting the
  workout a few minutes late doesn't fail every rep. A ride that didn't follow the workout is judged as a ride, not
  rep by rep. If every rep is off by the same amount, it says Zwift's FTP setting differs from yours, rather than
  blaming you. A session ridden on another day of the week is matched by its name. Tests are judged on pacing and
  result. Readiness from Apple Health follows the same rule as the planner.
- **Form on the status card** uses the planner's limits: high fatigue below −25, and "losing fitness" above +25.
- Notifications open the right page when tapped (ride, plan or analysis) and are grouped per kind on iOS.
- New sensor `sensor.training_last_ride` with the coach's headline on your last ride.

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
