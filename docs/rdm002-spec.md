# RDM002 (Hue Tap Dial Switch) — diyHue Handling Specification

Authoritative spec for how diyHue ingests, models, and reacts to the Philips Hue
Tap Dial Switch (model **RDM002**, variants `8719514440937`, `8719514440999`,
`9290035001`, `9290035003`). Written to replace ad-hoc incremental fixes with one
coherent design. Source of truth for the implementation and its tests.

Grounded in: live Zigbee2MQTT captures (this repo's logs),
`zigbee-herdsman-converters` over-the-air RE (the `0xFC00` `hueNotification`
frame), and the Hue CLIP v2 resource model.

---

## 1. Device model

One physical RDM002 is **one v2 `device`** exposing several services. Internally
diyHue represents it as **two sensor objects** that share one `uniqueid`:

| Sensor object | v2 role |
|---|---|
| `ZLLSwitch` (the **primary**) | owns the `device` resource; exposes `button`×4, `device_power`, `zigbee_connectivity`, and the `relative_rotary` service |
| `ZLLRelativeRotary` (a **sub-sensor**) | NOT its own device; provides the `relative_rotary` resource; linked to the switch via `parent_id_v2` |

Rules:
- `ZLLRelativeRotary.getDevice()` returns **`None`** (it is part of the switch device).
- `ZLLRelativeRotary` does **not** emit `zigbee_connectivity` (returns `None`).
- The switch device's `services[]` includes the rotary's `relative_rotary`
  service id, computed from the **switch** device id.
- `relative_rotary.owner.rid` = the **parent** (switch) device id.
- `SUB_SENSOR_TYPES = {"ZLLRelativeRotary"}` is the single list of child types.

### Parent linking
`parent_id_v2` is set whenever the pair is known:
1. **On pairing** (`bridge/devices`): the rotary created in the same loop gets
   `parent_id_v2` = the switch sensor's `id_v2`.
2. **On config load** (`configHandler`): any sub-sensor with no `parent_id_v2` is
   linked to the non-sub sensor sharing its `uniqueid` (back-fills pre-existing
   installs).
`parent_id_v2` is persisted in `Sensor.save()`.

---

## 2. The Zigbee2MQTT event streams

Per the over-the-air RE, the dial emits a Philips manufacturer frame
(cluster `0xFC00`, mfr `0x100B`, command `hueNotification`) **and** standard ZCL
Level Control. Z2M surfaces three relevant `action` families on the one MQTT
topic `zigbee2mqtt/<friendly_name>`:

### 2.1 Buttons (`ZLLSwitch`)
`action`: `button_<N>_press | _hold | _press_release | _hold_release` (N=1..4).
Mapped to a v1 `buttonevent` integer `N*1000 + d`:

| Z2M action | `d` | buttonevent | v2 event |
|---|---|---|---|
| `button_N_press` | 000 | N000 | `initial_press` |
| `button_N_hold` | 001 | N001 | `long_press` (fires while held) |
| `button_N_press_release` | 002 | N002 | `short_release` |
| `button_N_hold_release` | 003 | N003 | `long_release` |

(`buttonevent % 1000` → event via `{0:initial_press, 1:repeat→long_press, 2:short_release, 3:long_release, 10:long_press}`.)
Because Z2M already sends discrete `_hold` repeats, diyHue must **not** start its
own `longPressButton` repeat thread for these (avoid double-fire).

### 2.2 Dial — two redundant streams
Per physical detent, Z2M emits **both**:

- **`dial_rotate_<dir>_<speed>`** — proprietary telemetry.
  `action_direction` ∈ {`left`,`right`}; `action_time` 0–255 (magnitude);
  `action_type` ∈ {`step`,`rotate`}; `action_step_size` = `null`.
- **`brightness_step_up` / `brightness_step_down`** — standard Level Control.
  `action_step_size` = the **device's own brightness delta** (e.g. 8/44/87);
  `action_direction`/`action_time` = `null`; `action_transition_time` ≈ 0.04 s.

**Decision: `brightness_step_*` is canonical for dimming; `dial_rotate_*` is
dropped** (returned early) so the same turn is not counted twice. Rationale:
`brightness_step` carries the device-computed exact delta (`action_step_size`),
matching how the bulbs are actually driven; `action_time` is only a speed proxy.

> Every MQTT payload may also carry an OTA `update` object — ignore it.

---

## 3. Sensor state model

`ZLLRelativeRotary.state` (defaults from `sensor_types`):
```
rotaryevent          1 = start, 2 = repeat
direction            "clock_wise" | "counter_clock_wise"   (v2 enum, stored directly)
rotary_step_size     int 0–254   (= action_step_size; default 8)
expectedrotation     int         (rotation magnitude, for the v2 report)
expectedeventduration int
lastupdated          ISO-8601
```
`ZLLSwitch.state`: `buttonevent`, `lastupdated`.

`brightness_step_down` → `direction="counter_clock_wise"`;
`brightness_step_up` → `direction="clock_wise"`;
`rotary_step_size = action_step_size`.

---

## 4. Event processing pipeline (`on_message`)

```
zigbee2mqtt/<name>  →  getObject(name)  →  resolve a sensor (switch or rotary)
  → look up dataConversion[action]  →  convertedPayload
  → if rotaryevent in payload:
        if action starts with "dial_rotate_": RETURN (drop; telemetry only)
        direction   = counter_clock_wise if "down" in action else clock_wise
        rotary_step_size = action.action_step_size (default 8)
        retarget device → the ZLLRelativeRotary sibling (same friendly_name)
  → write only keys already present in state (guard with `key in state`)
  → device.state.update(convertedPayload)
  → skip self-managed long-press repeat when Z2M sends _hold events
  → rulesProcessor(device)
  → if buttonevent or rotaryevent changed: checkBehaviorInstances(device)
```

Robustness:
- `action = data.get(rootKey)` — a no-`action` message must not `KeyError`.
- State diff guarded with `key in device.state`.
- Automations run **only** on a real input event (a `buttonevent`/`rotaryevent`
  in this message), never on battery/OTA-status messages (else a stale
  `buttonevent` re-fires the last scene).

---

## 5. Behaviour semantics (`checkBehaviorInstances`)

### 5.1 Instance matching
Build the candidate id set for the event's device:
`{ device.id_v2, device.parent_id_v2, every sensor.id_v2 with the same uniqueid
and same type }`. An instance matches if its `configuration.device.rid` (or
`.source.rid`) is in that set. This links a rotary event to the instance
configured on its sibling switch, including installs paired before linking.

### 5.2 Buttons (new per-button schema; legacy `buttons` kept as fallback)
For `buttonN` config (`N = buttonevent // 1000`) pick the action by
`buttonevent % 1000` → `{0:on_initial_press, 1:on_long_press, 2:on_short_release, 3:on_long_release}`.
Resolve the group from the button's `where`, then run the action:
- `time_based_extended` → if group already on and `with_off.enabled`, turn off;
  else recall the scene for the current time slot (`findTriggerTime`).
- `recall_single_extended` → recall the listed scene(s).
- `scene_cycle_extended` → recall one scene from the slot list (cycle).
- `action: all_off | dim_up | dim_down` → `{on:false}` / `bri_inc ±30`.

### 5.3 Rotary (the dial)
Read `direction` and `step = rotary_step_size`. Resolve the group from
`configuration.rotary.where`. Then:

- **counter_clock_wise (dim down):** compare the group's current brightness to
  the step (both on a 0–100 % scale; `step_pct = step/254*100`). If
  `current ≤ step_pct` (would cross to zero) → run **`on_dim_off`**
  (`action: all_off` → `{on:false}`). Otherwise `setV1Action({bri_inc: -step, transitiontime: 4})`.
- **clock_wise (dim up):** if the group is **off** → run **`on_dim_on`**
  (turn on at minimum: `{on:true, bri:1, transitiontime:4}`). Otherwise
  `setV1Action({bri_inc: +step, transitiontime: 4})`.

The real `configuration.rotary` is
`{where, on_dim_off:{action:all_off}, on_dim_on:{recall_single:[{action:last_on}]}}` —
there is no plain "dim" action; the continuous dim is driven by `step`, the
behaviour config only governs the on/off edges.

---

## 6. Discovery, dedup, aliases

- **Model aliases**: `MODEL_ALIASES` (in `sensor_types`) maps every variant id to
  `RDM002`; both `sensorTypes` and the MQTT `standardSensors` table are extended
  from it so any variant id resolves.
- **Dedup on `bridge/devices`**: match existing sensors by `ieeeAddr`; keep one
  per type (prefer the entry already named `friendly_name`), delete the rest,
  and rename the keeper in place. Prevents duplicate sensors when a device is
  renamed in Z2M.

---

## 7. v2 resource shape (what clients see)

- `GET /clip/v2/resource/device` → **one** RDM002 device, `services[]` =
  4×`button`, `device_power`, `zigbee_connectivity`, `relative_rotary`, plus an
  `owner`.
- `GET /clip/v2/resource/relative_rotary` → one item, `owner.rid` = the switch
  device id, `rotary_report.rotation.direction` ∈
  {`clock_wise`,`counter_clock_wise`}, `steps`/`duration` from state.
- `GET /clip/v2/resource/button/<id>` → flat single resource; `button_report.event`
  reflects the current `buttonevent`.
- Rooms/zones may list the switch device as a non-light child
  (`Group.device_children`).

---

## 8. Acceptance criteria (drive the tests)

1. A `dial_rotate_*` message updates rotary telemetry but **does not** dim.
2. A `brightness_step_down`/`_up` dims the bound group by the device's exact
   `action_step_size`, routed to the rotary sensor, no `KeyError`.
3. Dim-down that would cross zero turns the room **off** (`on_dim_off`).
4. Dim-up from an off room turns it **on at min** (`on_dim_on`).
5. A rotary event matches the instance configured on the sibling switch even
   with no `parent_id_v2`.
6. A battery/OTA-only message fires **no** automation.
7. `button_N_hold` → `long_press`, `_hold_release` → `long_release`; no
   self-managed repeat thread double-fires.
8. Renaming a device in Z2M dedupes to one sensor per type (no duplicates).
9. The device resource exposes all four service types incl. `relative_rotary`;
   the rotary is not a second device.
10. CLIP v2 conformance scorecard stays at 100%.
```
