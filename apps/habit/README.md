# Habit and Mood Tracker

This AppDaemon app provides a GUI-managed habit and mood tracker for Home
Assistant. AppDaemon owns the entities through MQTT discovery, schedules reminders,
records completions, calculates streaks, and persists everything across restarts.

Habits are not defined in Python or `apps.yaml`. A user creates a habit by naming the
spare `text.<user>_habit_<slot>_name` entity in Home Assistant. The app immediately
creates another spare slot, so adding habits never requires a code or configuration
change.

## Structure

- `habit_tracker.py` coordinates AppDaemon callbacks, MQTT commands, reminders,
  completion actions, daily rollover, legacy migration, and AI context.
- `models.py` defines the persisted schema and calculates habit and mood streaks.
- `mqtt.py` publishes Home Assistant MQTT discovery, state, attributes, and
  availability.
- `reminders.py` owns absolute next-fire timers per habit slot.
- `store.py` writes the versioned JSON store atomically and keeps a backup plus an
  append-only completion audit log.

## Home Assistant entities

Each user has one MQTT device containing their habits and mood entities. Habit entity
IDs use stable numbered slots so changing a habit's name does not change its identity.

Each slot exposes:

- A text entity for the habit name.
- A select entity for `binary` or `countable` tracking.
- A switch for a binary habit, or a number for a countable habit.
- A reminder-time entity (daily template for the first fire).
- A next-reminder datetime entity (editable absolute fire time).
- Repeat count and interval controls.
- A minimum-days-per-week streak control.
- An AI-reminder switch.
- Configurable icons for complete, incomplete, active, and zero states.
- A streak sensor with completion age and 28-day completion rate attributes.

The app maintains exactly one unnamed spare slot per user. Clearing a habit's name
deletes that habit and its completion history. Unused discovery entities are retired
from Home Assistant automatically.

Each user also receives:

- `select.<user>_mood_today`
- `text.<user>_mood_note`
- `sensor.<user>_mood_streak`
- `sensor.<user>_mood_summary` (seven-day cards and compact 28-day chart data)
- `switch.<user>_mood_context_prompts`
- `number.<user>_mood_context_cooldown`
- `time.<user>_mood_reminder_time` (daily template for the first fire)
- `switch.<user>_mood_reminders` (enable/disable mood reminder notifications)
- `datetime.<user>_mood_next_reminder` (editable absolute fire time)
- `number.<user>_mood_repeat_count` and `number.<user>_mood_repeat_interval`
- `sensor.<user>_habits_binary_count` and `sensor.<user>_habits_countable_count`
  (configured inventory counts by habit type; spare/unnamed slots are excluded)

The existing unsuffixed mood select, note, streak, and reminder IDs are preserved.
Hidden editor entities expose a selected check-in, logical date, optional time, mood,
note, save action, and delete action for the current and previous six logical days.

## Persistence

The default data directory is:

```text
/homeassistant/.appdaemon/habits
```

`store.json` is the source of truth. Writes are atomic, the previous valid file is
retained as `store.json.backup`, and invalid files are quarantined. Completion changes
are additionally appended to `completions.jsonl`.

Mood data uses schema v3 timestamped check-ins. Each record has a stable ID, logical
date, optional occurrence time, mood, note, source, and audit timestamps. Logical mood
days run from 04:00 to 03:59 in `Europe/London`; entries and notes are retained
indefinitely, while changes are limited to the current and previous six logical days.
Migration from schema v2 is deterministic, keeps unknown historical times empty,
attaches the current note to its matching entry, and writes a one-time
`store.json.schema-v2-backup` before accepting v3 writes. Mood mutations are also
written to `mood-checkins.jsonl`.

Daily mood summaries include average, minimum, maximum, count, first-to-last
direction, and a mixed flag when the range is at least two points. Current and longest
streaks count distinct logical days; the summary also reports 7-day and 28-day
consistency. Completed days with notes receive a cached Qwen narrative after 04:00.
Editing a completed day invalidates and regenerates its narrative; AI failures never
replace or block deterministic statistics.

Habit completion history is stored by local calendar date:

- Binary habits store `1` when complete.
- Countable habits store their daily count.
- A count greater than zero counts as completion for streak purposes.

With a seven-day minimum, streaks require daily completion. Lower settings use a
**weekly-grace** model that is intentionally different from the legacy SQL streak
sensors:

- The current/anchor week contributes every calendar day from Monday through the
  anchor once the anchor day itself is complete (individual gaps earlier in that
  week do not truncate the count).
- Each prior week that meets `streak_min_days_per_week` adds a full 7 days, even
  if some days in that week were missed.
- The legacy SQL `first_incomplete_day` CTE broke the streak on any individually
  missed day regardless of week totals, which made weekly grace almost a no-op.
  The AppDaemon algorithm treats the threshold as real weekly grace instead.

Default configs use `streak_min_days_per_week = 7`, so most habits stay on the
strict daily path and are unaffected. Numbers can change on cutover only for habits
that already used a lower threshold.

## Reminders

Each configured habit has a durable `datetime.<user>_habit_<slot>_next_reminder`
entity. That datetime is the editable source of truth for when the next notification
fires. The `time.<user>_habit_<slot>_reminder_time` entity is only the recurring daily
template used to seed `next_reminder`.

Attempt metadata (`fire_at`, reminder index, final index) is persisted in
`store.json` under `pending_reminders`, so AppDaemon restarts can restore the same
chain. Editing `next_reminder` in Home Assistant updates the stored fire time, keeps
the current indices, cancels the in-memory timer, and reschedules. Past times fire
almost immediately.

Seeding happens on midnight rollover and when a habit becomes configured: if the habit
is incomplete and has no pending reminder, `next_reminder` is set to today at
`reminder_time` (or now if that time has already passed), with `next_index=1`. Changing
`reminder_time` only retargets a pending **first** reminder for today; mid-chain
repeats are left alone.

When a reminder fires, the app skips completed habits, sends the notification, then
either advances `next_reminder` by the repeat interval (while under the midnight
cutoff) or clears the pending chain. Completion, rename, delete, type change, or end
of chain clears the store entry, publishes an empty/`None` datetime state, and cancels
the timer.

`reminders_enabled: false` still no-ops sends and does not arm timers.

Mood reminders use the same durable `next_reminder` pattern with user-level
entities (`mood_reminders`, `mood_reminder_time`, `mood_next_reminder`, repeat
count/interval). Turning `mood_reminders` off clears any pending mood timer.
A mood reminder is also skipped once the logical day's first check-in is recorded,
and the pending chain is cleared at that point. The 04:00 rollover re-seeds an
incomplete mood check-in when reminders are enabled.

Mood prompts are sent as a grouped low/neutral notification and a grouped positive
notification. Both expire after 15 minutes and open the user's dashboard. Choosing a
mood clears the pair, creates exactly one check-in through the shared write path, and
sends a text-reply notification for a note tied to that check-in. Cleared companion
events remove the sibling notification where the mobile app exposes them.

Notifications are sent through the user's configured `script.notify_*` script. Their
action button either marks a binary habit complete or increments a countable habit.

At local midnight, habit values reset while historical completions remain available
for streak calculations. Mood draft state and reminder scheduling roll at 04:00.

## AI reminders

When a habit's AI-reminder switch is enabled, or when a mood reminder fires, the
app calls the configured `ai_task` entity and asks it for a short reminder.
Context can include:

- Current streak, completion age, and recent completion rate (habits).
- Mood streak and that today's mood is still unset (mood reminders).
- Mood and mood note (habit reminders).
- Broad location category derived from Home Assistant labels.
- Calendar availability for the next eight hours.
- Workday, current activity, and weather (habits).
- Soft local time context (weekday, month, and clock time — not a full date).

Unavailable context is omitted. If AI generation fails or returns an empty response,
the app sends a deterministic fallback reminder.

Mood context prompting is separate from reminder text generation. It starts only
after the day's first check-in, uses a configurable 15–360 minute cooldown (90 minutes
by default), coalesces triggers during the cooldown, and discards stale work. Calendar
blocks ignore cancelled/all-day events, merge overlaps or gaps of up to 15 minutes,
and use structured Qwen output to fail closed. Presence prompts fire 15 minutes after
returning from work or after another outing lasting at least two hours. Outing state is
persisted across AppDaemon restarts. Will's context prompts default on; Vic's scheduled
and contextual prompts default off.

This implementation ports the behavior from the legacy Home Assistant
`script.habit_send_reminder`. AppDaemon becomes the source of truth after cutover;
the legacy script is not called or retained, so reminder scheduling, context
collection, AI generation, fallback handling, and notification delivery remain one
cohesive workflow.

## Configuration

The app is registered in `apps/apps.yaml`. Its configuration contains only
installation-level concerns:

- Users and their notification/dashboard settings.
- Context entity IDs and label names.
- MQTT connection and discovery settings.
- Persistence directory.
- AI Task entity.
- The `reminders_enabled` cutover flag.

No individual habit belongs in `apps.yaml`.

`reminders_enabled` is intentionally `false` during migration so the existing Home
Assistant automations and AppDaemon cannot send duplicate reminders. Enable it only
after the MQTT entities, manually configured habits, test notifications, and
dashboards have been verified.

Habits are configured in the GUI after deploy — there is no import from the old
numbered `input_*` helpers. Keep the legacy helpers/automations until the new
entities and reminders are verified, then remove them at cutover.

AppDaemon uses its own slot-based mobile action IDs during the overlap period. This
prevents both the legacy Home Assistant action automation and AppDaemon from handling
the same countable-habit action and incrementing it twice. The legacy notification
action automations are removed during final cutover.

## Availability and recovery

All discovered entities share an MQTT last-will availability topic. If AppDaemon or
its MQTT connection stops, the entities become unavailable instead of displaying
stale state.

For troubleshooting, check:

1. The AppDaemon log for configuration, MQTT, calendar, or AI errors.
2. MQTT connectivity and the retained topics below `appdaemon/habits`.
3. The contents and permissions of the persistence directory.
4. That the configured Home Assistant labels and context entities exist.
5. That `reminders_enabled` has the intended value for the current migration stage.
