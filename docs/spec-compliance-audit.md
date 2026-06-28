# diyHue — Hue API Spec Compliance Audit

Audit of the diyHue codebase against the real Philips Hue API (v1 legacy + CLIP v2) and observed
real-bridge behaviour (see [`real-bridge-research.md`](./real-bridge-research.md) for the spec
sources). Read-only analysis — no code was changed.

**Test suite at time of audit:** `python -m pytest tests/` → **44 pass / 38 fail**. None of the 38
failures are test-harness/mock artifacts — every one traces to production behaviour. They split
into **8 genuine bugs** (spec-contract failures) and **30 unimplemented features** the spec tests
describe (red/aspirational tests for `device_children`, MQTT ieee-dedup, and the RDM002 per-button +
rotary behavior-instance redesign).

Severity: **HIGH** = wrong/crashing vs spec or breaks real clients · **MED** = observable deviation ·
**LOW** = cosmetic/edge.

---

## A. Confirmed correct (good fidelity)

- v2 `dimming.brightness` scaling: `bri/2.54` → 0–100 float, round-trips (`HueObjects/__init__.py:16,28`, `Light.py:326`). ✓
- `color_temperature` structure: emits `mirek` + `mirek_schema{153,500}` + `mirek_valid` (`Light.py:311-320`). ✓
- grouped_light semantics: `on` = any member on; `brightness` = average of ON members (`Group.py:132-143,301`). ✓
- Entertainment transport: `openssl s_server -dtls -psk <client_key> -psk_identity <username> -accept 2100`, parses `HueStream`, v1+v2 packet formats, colorspace byte (`entertainment.py:90,129-140`). DTLS-PSK on UDP 2100, identity=app-key, key=clientkey — matches spec. ✓
- v1 auth: error type 101 until link button, `{"username":...}` + `clientkey` on `generateclientkey` (`restful.py:84-101`). ✓
- Schedule recurring `W<bitmask>/T<hh:mm:ss>`, one-shot `PT...`, random `/A...` (`scheduler.py:24-42`). ✓

---

## B. HIGH severity

| # | Area | File:line | Spec vs code |
|---|---|---|---|
| H1 | v1 light PUT | `HueObjects/Light.py:152-186` | **No range clamping on direct PUT.** `bri`/`ct`/`hue`/`sat`/`xy` written straight to `self.state`. `{"bri":999}` or `{"ct":50}` stored unvalidated. Only inc-path + protocol min/max clamp. Spec: bri 1–254, ct 153–500, sat 0–254, hue mod 65536, xy [0,1]. |
| H2 | v1 rules | `functions/rules.py` | **`stable` / `not stable` operators not implemented.** Rules using them silently never match (swallowed by broad `except`). |
| H3 | v2 SSE | `services/eventStreamer.py:26-27` | **Events not batched.** Emits one SSE frame per queued message; real bridge batches all cascaded resources into a single event's `data[]` array. One PUT → many frames. |
| H4 | v2 SSE | `HueObjects/Light.py:208-223` | **No grouped_light cascade on single-light change.** Real bridge emits the light update + rollup `grouped_light` events for owning room, zone(s), and `bridge_home` (group 0). diyHue emits only `light`+`device`; clients tracking grouped_light go stale. |
| H5 | v2 device | `HueObjects/Sensor.py:191-251`, `Light.py:225-252` | **`device` resources omit the spec-required `owner` field.** Fails DeviceGet schema. (test_api_contract device schema test) |
| H6 | v2 behavior_instance | `HueObjects/BehaviorInstance.py:51` | **Any spec-shaped POST 500s.** `getV2Api` unconditionally reads `configuration["where"]`; a valid CLIP-v2 payload (`configuration.device`, no top-level `where`) → `KeyError: 'where'`. Genuine production crash. |
| H7 | v2 group | `HueObjects/Group.py:11-32,219-252,316-324` | **`device_children` unimplemented.** Room/zone `children[]` render only `self.lights`; non-light device children (switches/sensors) added via room PUT (`v2restapi.py:671-675`) never appear, aren't deduped, aren't persisted. (entire test_group_device_children suite) |

---

## C. MEDIUM severity

| # | Area | File:line | Spec vs code |
|---|---|---|---|
| M1 | v1 inc | `HueObjects/__init__.py:130-133` | `hue_inc` wrap off-by-one (`-= 65535` vs mod 65536) and can't handle large overshoot. |
| M2 | v1 inc | `HueObjects/__init__.py` incProcess | `xy_inc` not implemented (silently ignored). Inc operators mutually exclusive (`elif`) — real bridge allows multiple `*_inc` per PUT. |
| M3 | v1 rules | `functions/rules.py:42` | `not in` operator absent → condition ignored, rule may wrongly trigger. |
| M4 | v1 rules | `functions/rules.py:31` | `eq` forces `int()` cast; non-numeric state (string status) raises → condition silently dropped. |
| M5 | v1 schedule | `services/scheduler.py:43-50` | `R<n>/PT…` finite-repeat schedules never fire (only infinite `R/PT` matched). |
| M6 | v1 light | `HueObjects/Light.py:152` | `bri:0` accepted/stored (spec: 1–254, 0 invalid). |
| M7 | v2 mirek | `HueObjects/Light.py:319` | `mirek_valid` uses strict `< 500 and > 153` → wrongly false at the valid boundaries 153/500. |
| M8 | v2 envelope | `v2restapi.py:429,552,717` | Error responses drop the `data` key; spec requires `{"errors":[...],"data":[...]}` always (empty array). |
| M9 | v2 envelope | `v2restapi.py:724,744` | PUT/DELETE success responses drop the `errors` key. |
| M10 | v2 group | `HueObjects/Group.py:306` | `getV2GroupedLight` owner = `{rid:self.id_v2, rtype:"device"}` (not a real device); should be the owning room/zone. |
| M11 | v2 button/rotary | `v2restapi.py:600,602` | GET-by-id wraps a list: `data:[object.getButtons()]` → `data:[[...]]` (nested). Spec data is a flat resource array. |
| M12 | v2 device | `HueObjects/Sensor.py:191-251` | RDM002 modelled as **two** devices; switch device lacks `relative_rotary` service. Spec = one device exposing button+relative_rotary+device_power+zigbee. |
| M13 | v2 rotary | `HueObjects/Sensor.py:385` | `direction` hardcoded `"right"`; spec enum is `clock_wise`/`counter_clock_wise` (real state commented out). |
| M14 | v2 SSE | `services/eventStreamer.py:27` | `id` = `int(time())}:{queue_index`; spec `:0`. `int(time())` truncates → duplicate/non-monotonic ids within a second. |
| M15 | v2 SSE | `services/eventStreamer.py:10-17,31` | Unlocked global `eventstream` list; `messageBroker` blanks it every 0.3s while the SSE route reads it → dropped events; multiple clients race one queue (not fanned out per-connection). |
| M16 | rate limit | (whole codebase) | **No throttling/coalescing/429 anywhere.** Real bridge caps ~10 light cmds/s, ~1 group cmd/s, returns HTTP 429. Apps tuned to that back-pressure get none. |
| M17 | entertainment | `entertainment.py:137` | v2 36-byte entertainment_configuration UUID never read/validated (hardcoded offset 52); concurrent configs can't be disambiguated. |
| M18 | mqtt | `services/mqtt.py:307-324` | `bridge/devices` only ADDS sensors — no rename-on-ieee-match, no dedup-by-ieee; a renamed Z2M device spawns duplicate sensors. (`getSensorsByIeeeAddr` absent — whole test_mqtt_dedup suite) |
| M19 | v2 behavior | `functions/behavior_instance.py:71-122` | `checkBehaviorInstances` uses old schema (`configuration["buttons"]`, top-level `where`, `time_based`); no per-button (`button1..4`) keys, no `recall_single_extended`/`time_based_extended`, no `ZLLRelativeRotary` branch, no `parent_id_v2` rotary match. (whole RDM002 test_behavior_instance suite) |

---

## D. LOW severity

- `Light.py:33` event emits misspelled `"type": "entertainent"`.
- `Light.py:236` `service_id` placed on the `device` resource (belongs on services).
- `Light.py:327` `min_dim_level: 0.1` hardcoded (real bridge per-model).
- `Group.py:142,296` grouped_light brightness truncated to int (spec float).
- `v2restapi.py:572` unknown-id GET returns 200 with empty envelope (spec 404).
- `restful.py:42` unknown *resource* path KeyErrors → 500 instead of type 3.
- `restful.py:78` missing `devicetype` returns type 6 (spec type 5).
- `restful.py:55-68` GET `/config` omits `linkbutton` bool.
- `Rule.py:27` `add_conditions` references nonexistent `self.condition` (would AttributeError; appears unused).
- v1 `sat` lower clamp 1 vs spec floor 0 (`__init__.py:140-141`).
- `eventStreamer.py:24` SSE self-terminates after ~1000 iters (~6–7 min) instead of staying open.
- `entertainment.py:90` server side omits `-cipher PSK-AES128-GCM-SHA256` (relies on openssl default); the outbound client (`:456`) does pin it.
- `entertainment.py:110` init header sync uses byte-set membership (`in b'HueStream'`) not sequence order; can mis-sync.
- `__init__.py:18` `v1StateToV2` nests a stray `color_temperature_delta` inside `color_temperature`.

---

## E. Test-suite triage (38 failures)

| Classification | Count | Cause |
|---|---|---|
| **CODE_BUG** | 8 | All in `test_api_contract.py`: device `owner` missing (H5); RDM002 split / missing relative_rotary (M12); rotary direction hardcoded (M13); room PUT device-child not rendered (H7); behavior_instance POST `KeyError:'where'` ×4 (H6). |
| **SPEC_ASPIRATIONAL** | 30 | Unimplemented features the tests describe: `Group.device_children` (test_group_device_children, ~10); MQTT ieee dedup/rename (test_mqtt_dedup, 8); RDM002 per-button + rotary behavior redesign (test_behavior_instance, 10); 2 `getV2Room` light-rid tests borderline-bug (`Group.py:224` uses `uuid5(id_v2+'device')` instead of `getDevice()["id"]`). |
| **TEST_HARNESS** | 0 | No failures are mock/fixture artifacts. |

**Single highest-value fix:** `BehaviorInstance.py:51` (`KeyError:'where'`) — any spec-shaped
`behavior_instance` POST currently 500s.

---

## F. Suggested fix priority

1. **H6** behavior_instance POST crash — production 500, smallest blast radius.
2. **H5** device `owner` field — one-field addition, unblocks v2 device schema conformance.
3. **H1** v1 PUT range clamping — correctness for every light command.
4. **H3/H4** SSE batching + grouped_light cascade — real-client state-sync fidelity.
5. **H7** `Group.device_children` — unblocks ~10 tests + room/zone child correctness.
6. MEDIUM envelope (M8/M9), rules operators (H2/M3/M4), then the aspirational feature suites
   (M18 mqtt dedup, M19 RDM002 behavior) as scoped features.

*Findings produced by static + targeted-dynamic analysis against the cited spec sources; line
numbers are at audit time and may drift as code changes.*
