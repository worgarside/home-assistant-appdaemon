# Event-Driven Habit Completion — Plan and Handoff

This document has two parts:

- **Part A** — the implementation plan as originally written, reproduced verbatim.
- **Part B** — a handoff for whoever picks this up next, covering what is actually
  built, where the code deviates from Part A, and what to do first.

Part A is a historical record and is **not** updated as work lands. Where Part A and
Part B disagree, **Part B is correct**.

---
---

# Part A — Implementation plan (verbatim)

# Event-Driven Habit Completion — Implementation Plan

Extends the AppDaemon habit tracker (`feature/habits`) so a habit can complete itself
when a user-defined Jinja template evaluates truthy — either instantly, after an
unbroken block of N minutes, or after N minutes accumulated across the day.

**Agreed scope**

| Decision | Choice |
|---|---|
| Modes | `manual`, `instant`, `continuous`, `summed` |
| Habit types | Binary only (countable stays manual/instant) |
| Manual switch | Still works; template never un-completes; manual un-tick stops auto-completion for that day |
| Time in templates | Via `sensor.time` / `sensor.date`, never `now()` |

Findings below were verified against the live instance on 2026-07-28.

---

## Landmine: bumping `SCHEMA_VERSION` destroys the store

`StoreData.from_dict` raises `ValueError` when `schema_version != SCHEMA_VERSION`
(`models.py:269-271`). `HabitStore._load` catches `ValueError` and **quarantines** the
file — then does the same to the backup, and falls through to `StoreData.empty()`
(`store.py:47-53`). So changing `SCHEMA_VERSION` from `1` to `2` silently wipes every
habit config and all completion history on first boot.

Fix before touching the schema — accept old versions and upgrade in place:

```python
SCHEMA_VERSION: Final[int] = 2

@classmethod
def from_dict(cls, value: dict[str, Any]) -> Self:
    version = _integer(value, "schema_version")
    if version > SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version: {version}")
    if version < SCHEMA_VERSION:
        value = _migrate(value, from_version=version)
    ...
```

`_migrate` for 1→2 is a no-op beyond stamping the new version, because every field added
below has a default. But the mechanism must exist *before* the first bump.

This is Phase 0 and is worth doing regardless of whether the rest gets built.

---

## Design

### Completion modes

| Mode | Behaviour |
|---|---|
| `manual` | Current behaviour. Template ignored. |
| `instant` | Rising edge false→true completes the habit. |
| `continuous` | Truthy for `duration_minutes` unbroken completes the habit. |
| `summed` | Truthy for `duration_minutes` total across the local day completes the habit. |

### How the timers work

`self.run_in(callback, seconds)` schedules a one-shot callback and returns a handle;
`self.cancel_timer(handle)` drops it. `ReminderManager` already does exactly this — one
handle per slot, cancel-and-replace on change. Duration tracking is the same shape:

- Template goes **true** → arm a timer for the remaining time.
- Template goes **false** before it fires → cancel the timer.
- Timer **fires** → the condition held for the full window → complete the habit.

No polling loop, no tick counting. The timer firing *is* the proof the window elapsed.
`summed` differs only in arming for `target − already_banked` rather than the full
duration.

### `sensor.time` is the reconciliation poll

Verified on the live instance: `sensor.time` updates exactly on the minute
(`last_changed` = `01:07:00.014`). Any template referencing it therefore emits a
state-change event every 60 seconds — which is precisely the reconciliation poll this
design needs, delivered by the event bus for free.

Because time is expressed via `sensor.time`/`sensor.date` rather than `now()`, every
template dependency is a real entity. That means:

- regex extraction + `listen_state` genuinely covers all dependencies
- there is no `listeners.all`-style opacity to work around
- **no hand-rolled `run_every` poll is needed anywhere**

One refinement: for `continuous` and `summed` modes, **always subscribe to `sensor.time`
whether or not the template references it.** Guaranteed minute-tick reconciliation
bounding any drift from a missed transition, at the cost of one extra listener.

> **Rejected: HA websocket `render_template` subscription.** It returns an authoritative
> `listeners: {all, entities, domains, time}` and pushes re-renders, which would remove
> regex extraction entirely. But its value is concentrated in correct `now()` handling
> and opacity detection — neither of which applies here. Not worth a second connection,
> a long-lived token, and reconnect logic. Revisit only if `now()` becomes unavoidable.

### Template return types

Verified: a **single pure expression returns a native JSON boolean**.

```jinja
{{ states('sensor.time') >= '18:00' and is_state('binary_sensor.will_at_work','off') }}
→ false          (native bool)
```

Anything with literal text or multiple expressions returns a **string**:

```jinja
int:{{ ... }}|cmp:{{ ... }}
→ "int:100|cmp:False"
```

So `coerce_truthy` is still required — `bool("False")` is `True` — but the common
one-expression case needs no coercion at all.

Failure modes degrade safely: `states('sensor.missing') | float(0) > 1` returns `False`
rather than raising, and `| int(0)` correctly handles float-strings like `"100.0"`
(as emitted by `sensor.will_s_pixel_6_pro_daily_steps`). A renamed entity fails falsy
rather than erroring, provided filter defaults are used.

### The 255-character limit

HA states cap at 255 characters and the template lives in a `text` entity, so that is the
ceiling. In practice it is roomy — a realistic three-clause condition against one of the
longest entity IDs in the config is ~155 characters:

```jinja
{{ states('sensor.time') >= '18:00' and is_state('binary_sensor.will_at_work','off') and states('sensor.will_s_pixel_6_pro_daily_steps')|float(0) > 8000 }}
```

Validate length on the MQTT command path, reject over-long templates with a clear log
line, and surface `template_error` in the slot attributes. See the appendix for an
uncapped escape hatch if it ever becomes a real constraint.

### Restart recovery

Rule: **banked time survives a restart; in-flight time across a restart is discarded.**

- `accumulated_seconds` was earned and observed — keep it.
- The open `truthy_since` block cannot be verified as unbroken while the app was down —
  discard the gap, re-stamp `truthy_since = now` if the template is truthy at startup.

Conservative, never over-credits, needs no heartbeat.

---

## Data model

### `models.py`

```python
class CompletionMode(StrEnum):
    MANUAL = "manual"
    INSTANT = "instant"
    CONTINUOUS = "continuous"
    SUMMED = "summed"

MAX_TEMPLATE_LENGTH: Final[int] = 255
TIME_TICK_ENTITY: Final[str] = "sensor.time"
```

New `HabitConfig` fields (all defaulted, so v1 stores parse unchanged):

```python
completion_mode: CompletionMode = CompletionMode.MANUAL
completion_template: str = ""
completion_duration_minutes: int = 30
```

`__post_init__` additions:

- `len(completion_template) <= MAX_TEMPLATE_LENGTH`
- `1 <= completion_duration_minutes <= 1440`
- if `completion_mode is not MANUAL` then `completion_template.strip()` must be non-empty
- if `completion_mode` is a duration mode then `habit_type` must be `BINARY`

New dataclass:

```python
@dataclass(slots=True)
class TemplateProgress:
    day: str                         # ISO local date this accumulator belongs to
    accumulated_seconds: int = 0     # banked truthy time today (summed only)
    truthy_since: str | None = None  # UTC ISO, set while currently truthy
    suppressed_day: str | None = None  # set on manual un-tick; blocks re-completion
```

Current truthiness is derived from `truthy_since is not None` — no separate flag to keep
in sync.

`UserData` gains `template_progress: dict[int, TemplateProgress]`, serialised like
`pending_reminders` (string slot keys). Drop it in `normalize_spare_slot` so a reused
slot never inherits stale progress.

### New module: `apps/habit/templates.py`

Mirrors `reminders.py` — owns extraction, coercion, and timer handles; no AppDaemon
imports beyond a `Protocol`, so the pure parts stay unit-testable.

```python
ENTITY_PATTERN = re.compile(r"\b([a-z_]{3,})\.([a-z0-9_]+)\b")

def extract_entities(template: str) -> tuple[str, ...]: ...
def coerce_truthy(value: object) -> bool: ...
```

`coerce_truthy` must be explicit:

- `True` for `True`, `"true"`, `"on"`, `"yes"`, `"1"`, numeric > 0
- `False` for `False`, `"false"`, `"off"`, `"no"`, `"0"`, `""`, `"none"`, `"unknown"`,
  `"unavailable"`, numeric 0
- anything else → `False` + one log line

`TemplateWatcher` holds `dict[tuple[str, int], str]` timer handles plus listener handles,
exposing `arm`, `cancel`, `cancel_all`, `release` — same surface as `ReminderManager`.

---

## Wiring into `habit_tracker.py`

### Evaluation entry point

```python
def _evaluate_template(self, user: str, slot: int) -> None:
```

1. Return if `not self.template_evaluation_enabled`.
2. Return unless configured, mode is not `manual`, and template non-empty.
3. Return if `progress.suppressed_day == today` (manual un-tick honoured).
4. If complete today → clear progress, cancel timer, return.
5. Render via `self.render_template(...)` in `try/except` → failure is falsy, logged
   rate-limited, `template_error` set in attributes.
6. `now_truthy = coerce_truthy(result)`; `was_truthy = progress.truthy_since is not None`.
7. Dispatch on mode.

**`instant`** — `not was_truthy and now_truthy` → `self._set_completion(user, slot, 1)`.

**`continuous`**
- rising: `truthy_since = now`, arm timer for `duration * 60`
- falling: clear `truthy_since`, cancel timer
- timer fires: **re-render to confirm still truthy**, then complete

**`summed`**
- rising: `truthy_since = now`, arm for `target − accumulated`
- falling: `accumulated += now − truthy_since`, clear, cancel
- timer fires: bank elapsed; complete if `accumulated >= target`, else re-arm remainder

### Listener management

`_rebuild_template_listeners(user, slot)` — cancel existing handles, extract entities,
`listen_state` each onto the debounced evaluator. For duration modes, add
`TIME_TICK_ENTITY` unconditionally. Call on init, template change, mode change, and slot
retirement.

Publish the resolved list as a `watched_entities` attribute so a regex miss is visible
rather than mysterious.

### Debounce

Coalesce per slot with a short `run_in`, cancelling any pending evaluation handle first:

```python
def _schedule_evaluation(self, user: str, slot: int) -> None:  # ~2s debounce
```

Protects against several dependencies changing at once and against flapping templates
churning `store.save()`.

### Touchpoints in existing code

| Location | Change |
|---|---|
| `initialize` | Build watcher, rebuild listeners, deferred `run_in` recovery pass alongside `_restore_reminders_callback` |
| `_update_habit` | New keys `completion_mode`, `completion_template`, `completion_duration`; rebuild listeners and re-evaluate after any change |
| `_set_completion` | On completion, clear progress + cancel timer. On manual un-tick (`count == 0`), set `suppressed_day = today` and clear progress |
| `_midnight_rollover` | Reset `template_progress` to fresh `TemplateProgress(day=today)`; clear `suppressed_day`; re-evaluate every event-driven slot |
| `terminate` | `watcher.cancel_all()` before the existing store save |
| `normalize_spare_slot` | Drop `template_progress` for retired slots |

---

## MQTT surface (`mqtt.py`)

New `EntitySpec`s per slot, all `entity_category: config`:

| Component | Key | Notes |
|---|---|---|
| `select` | `completion_mode` | options `manual/instant/continuous/summed` |
| `text` | `completion_template` | `max: 255` |
| `number` | `completion_duration` | 1–1440, `unit_of_measurement: min` |

Observability (cheap, and makes debugging *why* a habit isn't progressing much easier):

| Component | Key | Notes |
|---|---|---|
| `binary_sensor` | `condition` | live truthiness; attributes carry `template_error`, `watched_entities`, `accumulated_minutes` |
| `sensor` | `condition_progress` | minutes banked vs target, `unit_of_measurement: min` |

Add all new keys to `_slot_entity_keys()` so retirement cleans them up, and to
`publish_config_state`. Note `binary_sensor` needs no `command_topic` — the existing
`if spec.component != "sensor"` guard must become a set check.

---

## Config (`apps.yaml`)

```yaml
  template_evaluation_enabled: false   # cutover flag, mirrors reminders_enabled
  template_eval_debounce_seconds: 2
```

No poll interval — `sensor.time` provides the tick. Validate with the same bounds
treatment as `mqtt_port`.

---

## Validation rules

Deliberately narrow. Do **not** block on heuristics.

| Check | Action |
|---|---|
| Syntax error on set-time render | Reject, log, set `template_error` |
| Over 255 characters | Reject with a clear message |
| **Extracted entity list empty** | Flag loudly — the template is a constant and will never re-evaluate |
| Result not boolean-ish | Warn, treat via `coerce_truthy` |
| Duration mode on a countable habit | Reject in `__post_init__` |

---

## Phasing

Each phase is independently shippable and leaves the app working.

| Phase | Work | Risk |
|---|---|---|
| **0** | `_migrate` + version-tolerant `from_dict`; verify a v1 store loads and is not quarantined | Low, highest value |
| **1** | Model fields, `TemplateProgress`, MQTT entities, `_update_habit` keys. No evaluation | Low |
| **2** | `templates.py`, extraction/coercion, listeners, debounce, **`instant`** mode | Medium |
| **3** | **`continuous`** — timers, re-render confirmation, restart recovery, `sensor.time` subscription | Medium |
| **4** | **`summed`** — accumulator, midnight reset | Medium |
| **5** | `binary_sensor`/progress sensor, README, remove cutover flag | Low |

---

## Verification

No test harness exists (no `tests/`, dev group is `appdaemon` + `basedpyright`), and
`basedpyright` runs in **strict** mode via pre-commit — new code needs complete
annotations and no blanket `# type: ignore` (a pre-commit hook enforces that).

The pure functions in `templates.py` (`extract_entities`, `coerce_truthy`) and the
progress arithmetic are testable without AppDaemon. Worth adding a minimal `tests/`
directory with `pytest` for those plus the streak functions — most logic per line, least
framework coupling.

Manual verification per phase:

1. `pre-commit run --all-files` (ruff, basedpyright strict, codespell).
2. Deploy with `template_evaluation_enabled: false`; confirm entities appear, nothing fires.
3. Point a template at a hand-togglable helper — verify rising edge, falling edge, timer
   completion.
4. Restart mid-progress; confirm banked time survives and in-flight time resets.
5. Cross midnight with a persistently truthy template; confirm the accumulator resets.
6. Confirm `store.json.backup` stays valid and nothing lands in `*.invalid-*`.

---

## Appendix: uncapped template input, if 255 ever bites

The 255 limit applies to *states*, not to service-call data or MQTT payloads. A script
with a multiline field can publish straight to the existing command topic, bypassing the
text entity entirely:

```yaml
fields:
  template:
    selector:
      text:
        multiline: true

sequence:
  - action: mqtt.publish
    data:
      topic: "appdaemon/habits/{{ user }}/{{ slot }}/completion_template/set"
      payload: "{{ template }}"
```

AppDaemon already subscribes to `appdaemon/habits/+/+/+/set`, so this needs no new
handling. Display the stored value back via a sensor *attribute* (uncapped) rather than a
state. Not worth building until a real template needs it.

An alias layer (`$steps` → `states('sensor.will_s_pixel_6_pro_daily_steps')|int(0)`) would
also compress most of the length out of typical templates, but is unnecessary at current
lengths.

---

## Open questions

- **Instant + a persistently truthy template** re-completes at 00:00 daily. Probably
  desired for "steps > 8000", but should be a deliberate choice.
- **Flapping hysteresis** — a template hovering at a threshold resets `continuous`
  forever. A minimum-dwell before counting a falling edge would fix it, at the cost of
  another config knob.
- **Countable habits** are excluded from duration modes but could use `instant` to set a
  value; would need rules for what the template returns.

---
---

# Part B — Handoff

Written 2026-07-28. Phases 0, 1 and 2 are complete and committed.

## Where things stand

Branch `feature/habits`, working tree clean at time of writing.

| Commit | Contents |
|---|---|
| `bef4153` | Phase 0 — schema version migration handling |
| `7010faa` | Phase 1 — event-driven completion config surface |
| `1e3762b` | Phase 2 — template-driven completion (`instant`) |

Phases 3, 4 and 5 remain. **Start with Phase 3 (`continuous`).**

## What is actually built

**`models.py`**

- `SCHEMA_VERSION = 2`, `MIN_SCHEMA_VERSION = 1`.
- `UnsupportedSchemaVersionError(ValueError)` — distinguishes "intact but from another
  build" from "corrupt".
- `SCHEMA_MIGRATIONS: dict[int, Callable]` with `_migrate_1_to_2` registered (additive
  no-op), driven by `migrate_store_payload()`. A missing step is fatal, never skipped.
- `CompletionMode` with `is_event_driven` and `is_duration_based` properties.
- `HabitConfig.completion_mode` / `completion_template` / `completion_duration_minutes`,
  validated in `_validate_completion()`.
- `TemplateProgress` with `day`, `accumulated_seconds`, `truthy_since`, `suppressed_day`
  and an `is_truthy` property.
- `UserData.template_progress`, dropped for retired slots in `normalize_spare_slot`.

**`store.py`** — `_load` catches `UnsupportedSchemaVersionError` *before* the generic
`ValueError` branch, logs, and re-raises rather than quarantining. Corrupt files still
quarantine exactly as before.

**`templates.py`** (new) — `extract_candidate_entities`, `coerce_truthy`, and
`TemplateWatcher`.

**`mqtt.py`** — `completion_mode` (select), `completion_template` (text, `max: 255`) and
`completion_duration` (number) added to `publish_slot`, `publish_config_state` and
`_slot_entity_keys`.

**`habit_tracker.py`** — `initialize` guards store construction, builds the watcher, and
restores listeners via the deferred callback. `_evaluate_template` implements `instant`.
`_set_completion` maintains progress and suppression. `_update_habit` handles the three
new keys.

**`apps.yaml`** — `template_evaluation_enabled: false` (cutover flag) and
`template_eval_debounce_seconds: 2`.

## Where the code deviates from Part A

Read this section before trusting Part A's code snippets.

1. **Entity filtering uses live state, not a regex-only list.** Part A implies the regex
   result is used directly. In practice `extract_candidate_entities` returns *candidates*
   (it cannot tell `sensor.steps` from `value.split`), and
   `_rebuild_template_listeners` keeps only those where `self.get_state(name) is not None`.
   No domain allowlist to maintain. Consequence: an entity that does not exist yet is
   dropped and the slot logs "references no known entities".

2. **`ENTITY_PATTERN` differs.** Part A used `\b([a-z_]{3,})\.([a-z0-9_]+)\b`. Shipped is
   `(?<![\w.])([a-z_][a-z0-9_]*)\.([a-z0-9_]+)(?![\w.])`, so a dotted chain such as
   `states.sensor.foo` yields nothing rather than a bogus `states.sensor`.

3. **`coerce_truthy` returns `bool | None`, not `bool`.** `None` means "unrecognised", so
   the caller logs once and falls back to `False`. Part A folded this into `False`.

4. **`TemplateWatcher` has a different surface.** Part A said `arm` / `cancel` /
   `cancel_all` / `release`. Shipped is `watch` / `unwatch` / `schedule` /
   `cancel_scheduled` / `release` / `remove` / `cancel_all`. **It has no duration timer
   methods yet** — Phase 3 must add them.

5. **`_update_habit` snapshots and rolls back.** Not in Part A. Fields are mutated in
   place but cross-field rules only run in `__post_init__` afterwards, so a rejected
   command would otherwise leave the in-memory config dirty for a later save to persist.
   `snapshot = config.to_dict()` is taken up front and restored on `ValueError`.

6. **Midnight does not clear `template_progress`.** Part A said it should. Instead
   `_template_progress()` replaces any record whose `day` differs from today, so rollover
   is lazy and automatic — including `suppressed_day`. `_midnight_rollover` only schedules
   a re-evaluation of event-driven slots.

7. **Observability landed early, partially.** `watched_entities` and `completion_mode` are
   published on the slot's `name` attributes now. The `binary_sensor.condition` and
   `sensor.condition_progress` entities from Part A's Phase 5 are **not** built.

8. **`TIME_TICK_ENTITY` is already wired.** `_rebuild_template_listeners` appends
   `sensor.time` for duration modes today, even though duration modes do not yet act.

9. **Phase 0 was larger than Part A described.** It also added the non-destructive refusal
   in `store.py` and the inert-on-failure guard in `initialize`. Both matter: without them
   a future-version store is renamed to `.invalid-*` and then overwritten by an empty one.

## Next up — Phase 3 (`continuous`)

1. Add duration timers to `TemplateWatcher` (`arm_duration` / `cancel_duration`), keyed
   `(user, slot)`, mirroring `ReminderManager`.
2. In `_evaluate_template`, branch on `CompletionMode.CONTINUOUS`:
   - rising edge → `truthy_since = now`, arm for `completion_duration_minutes * 60`
   - falling edge → clear `truthy_since`, cancel timer
   - timer fires → **re-render to confirm still truthy**, then `_set_completion(..., 1)`
3. Restart recovery in `_restore_templates`: keep `accumulated_seconds`, discard the
   in-flight `truthy_since` gap, re-stamp `truthy_since = now` if currently truthy.
4. `terminate` already calls `templates.cancel_all()` — make sure it cancels duration
   timers too once they exist.

The re-render on timer fire is the safety net for a missed falling edge. Do not skip it.

Then Phase 4 (`summed` — bank on falling edge, arm for `target − accumulated`) and
Phase 5 (observability entities, README, drop the cutover flag).

## Environment gotchas

These cost time if rediscovered from scratch.

- **The sandbox runs Python 3.10; the project targets 3.12.** `StrEnum`, `typing.Self`
  and `datetime.UTC` are all 3.11+. There is no network access to install a newer
  interpreter (`uv python install` fails at the proxy).
- **`.venv` is macOS-only.** Its shebangs point at a macOS interpreter, so
  `.venv/bin/basedpyright` and friends cannot run from a Linux sandbox.
- **ruff and basedpyright have not been run on any of this work.** No network to install
  them. Everything parses and sits inside the 90-char limit, but
  **`pre-commit run --all-files` is still outstanding** and is the first thing to do.
  `templates.py` is a new file and its `Protocol` definitions are the most likely place
  strict mode complains.
- Relevant lint config: `line-length = 90`, `select = ["ALL"]` with `ANN`, `EM`, `BLE`,
  `TRY003`, `D107` ignored, `pydocstyle` google convention, `max-args = 10`.

## Verification approach

There are **no tests in the repo, deliberately** — the owner considers unit tests overkill
here and that decision stands. Verification was done with throwaway scripts held outside
the repo. To recreate them:

- Shim the three 3.11+ stdlib names before importing (`enum.StrEnum` as
  `class S(str, Enum)` with `__str__` returning the value, `typing.Self = Any`,
  `datetime.UTC = timezone.utc`).
- Stub `paho.mqtt.client` / `paho.mqtt.enums` and `appdaemon.plugins.hass.hassapi.Hass`
  in `sys.modules` — neither is installed and neither is exercised.
- Subclass `HabitTracker` and override only the AppDaemon surface (`log`, `error`,
  `datetime`, `get_state`, `render_template`, `listen_state`, `cancel_listen_state`,
  `run_in`, `cancel_timer`). This drives the shipped `_evaluate_template` rather than a
  reimplementation, which is the whole point.

What was covered: v1→v2 migration against a populated store, chained migrations, refusal
without quarantine, corrupt-still-quarantines, backup recovery, all five config
validation rejections, `TemplateProgress` round-trip and retirement, entity extraction
edge cases, the full `coerce_truthy` table, instant-mode rising edge, no double
completion, un-tick suppression and next-day reset, render exceptions, unusable values,
manual/disabled inertness, and listener resolution.

## Deployment notes

- **Back up `store.json` before first deploy of v2.** The first save rewrites it, and
  Phase 0 code would then refuse to start (safely — it will not touch the file).
- Deploy with `template_evaluation_enabled: false` first. Confirm the three new entities
  appear per habit slot and that nothing fires.
- Only then flip the flag, starting with one `instant` habit pointed at something you can
  toggle by hand.

## Open questions still unresolved

Carried over from Part A, none answered yet:

- `instant` with a persistently truthy template re-completes at 00:00 every day. Probably
  wanted, but it has not been confirmed.
- No flapping hysteresis. A template oscillating around a threshold will reset
  `continuous` indefinitely. Becomes real in Phase 3.
- Countable habits are excluded from duration modes and cannot currently be driven by a
  template at all.

## Unrelated repo state

At time of writing there were untracked `apps/chatgpt/`, `apps/openai/` and
`tools/chatgpt_usage/` directories, unrelated to this work. The working tree also gets
committed externally mid-session at times, so check `git log` before assuming your
changes are uncommitted.
