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
- An opt-in end-of-day reminder switch.
- Configurable icons for complete, incomplete, active, and zero states.
- A streak sensor with completion age and 28-day completion rate attributes.

The app maintains exactly one unnamed spare slot per user. Clearing a habit's name
deletes that habit and its completion history. Unused discovery entities are retired
from Home Assistant automatically.

Each user also receives:

- `select.<user>_mood_today`
- `text.<user>_mood_note`
- `sensor.<user>_mood_streak`
- `time.<user>_mood_reminder_time` (daily template for the first fire)
- `switch.<user>_mood_reminders` (enable/disable mood reminder notifications)
- `datetime.<user>_mood_next_reminder` (editable absolute fire time)
- `number.<user>_mood_repeat_count` and `number.<user>_mood_repeat_interval`
- `sensor.<user>_habits_binary_count` and `sensor.<user>_habits_countable_count`
  (configured inventory counts by habit type; spare/unnamed slots are excluded)

## Persistence

The default data directory is:

```text
/homeassistant/.appdaemon/habits
```

`store.json` is the source of truth. Writes are atomic, the previous valid file is
retained as `store.json.backup`, and invalid files are quarantined. Completion changes
are additionally appended to `completions.jsonl`.

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
A mood reminder is also skipped once today's mood is set, and the pending chain
is cleared at that point. Midnight re-seeds an incomplete mood check-in for the
new day when reminders are enabled.

Notifications are sent through the user's configured `script.notify_*` script. Their
action button either marks a binary habit complete or increments a countable habit.

Each habit can also opt in to an independent end-of-day reminder with
`switch.<user>_habit_<slot>_end_of_day_reminder`. At 23:55 local time, the app sends
one final notification when that habit is still incomplete. This timer is derived
from the fixed daily time rather than stored in `pending_reminders`, and it neither
consumes nor changes the scheduled/repeating reminder chain. Enabling it after 23:55
sends the check immediately if the habit is still incomplete.

At local midnight, current values reset while historical completions remain available
for streak calculations. Mood and mood notes reset at the same time. Pending reminder
chains are cleared and incomplete habits are re-seeded for the new day.

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
