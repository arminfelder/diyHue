"""
Hue API COMPLIANCE SCORECARD — run anytime to measure how close diyHue is to
the real Philips Hue v1 + CLIP v2 behaviour.

Unlike the rest of the suite (which asserts hard), every check here is a *live
probe* that records pass/fail into a weighted scoreboard. Non-compliant probes
(including ones that crash) are reported as `xfail` so the suite stays green;
compliant ones report as `pass`. At the end of the run a SCORECARD is printed
(see conftest.pytest_terminal_summary) and written to
`tests/compliance_report.txt`.

    pytest tests/test_spec_compliance.py -q            # just the scorecard
    pytest tests/test_spec_compliance.py -q -rx        # + reason per gap
    pytest tests/ -q                                   # whole suite + scorecard

As gaps get fixed, their probe flips from xfail→pass and the score rises
automatically — nothing here is hardcoded to the current state. A regression
gate (`test_scorecard_meets_baseline`) fails if the weighted score drops below
`BASELINE_WEIGHTED_PCT`; bump that number up as compliance improves.

Spec sources: docs/real-bridge-research.md  +  tests/fixtures/openhue_spec.yaml
"""
import json
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, "BridgeEmulator")

from test_api_contract import (  # reuse the contract harness
    _get, _put, _post, _delete, _body,
    _add_rdm002, _add_room, validate,
    _load_spec, _make_resolver, _build_resource_schema_map,
)


# Session fixtures for jsonschema checks — defined locally so this module runs
# standalone (they also exist in test_api_contract for that module's own tests).
@pytest.fixture(scope="session")
def spec():
    return _load_spec()


@pytest.fixture(scope="session")
def resolver(spec):
    return _make_resolver(spec)


@pytest.fixture(scope="session")
def resource_schema_map(spec):
    return _build_resource_schema_map(spec)

# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------

SEVERITY_WEIGHT = {"HIGH": 3, "MED": 2, "LOW": 1}

# id -> {area, severity, spec, passed, detail}
RESULTS: dict[str, dict] = {}

# Regression gate. Now at 100% — hold a 95% floor so a single probe slipping
# still passes CI while any real regression trips it. Raise toward 100 to tighten.
BASELINE_WEIGHTED_PCT = 95.0


@contextmanager
def probe(cid, area, severity, spec):
    """
    Run a single compliance probe. The body sets b.passed / b.detail.
    Any exception inside the body is treated as a (non-compliant) crash.
    Result is recorded and, if not compliant, the test is marked xfail.
    """
    b = SimpleNamespace(passed=False, detail="")
    try:
        yield b
    except Exception as e:  # a crashing path is itself a compliance failure
        b.passed = False
        b.detail = f"crashed: {type(e).__name__}: {e}"
    RESULTS[cid] = {"area": area, "severity": severity, "spec": spec,
                    "passed": bool(b.passed), "detail": b.detail}
    if not b.passed:
        pytest.xfail(f"[{severity}|{area}] {b.detail}  (spec: {spec})")


def compute_scorecard() -> dict:
    by_area: dict[str, list[int]] = {}
    by_sev: dict[str, list[int]] = {}
    w_total = w_pass = n_total = n_pass = 0
    for r in RESULTS.values():
        w = SEVERITY_WEIGHT[r["severity"]]
        p = 1 if r["passed"] else 0
        w_total += w
        w_pass += w * p
        n_total += 1
        n_pass += p
        by_area.setdefault(r["area"], [0, 0])
        by_area[r["area"]][0] += p
        by_area[r["area"]][1] += 1
        by_sev.setdefault(r["severity"], [0, 0])
        by_sev[r["severity"]][0] += p
        by_sev[r["severity"]][1] += 1
    weighted_pct = (100.0 * w_pass / w_total) if w_total else 0.0
    raw_pct = (100.0 * n_pass / n_total) if n_total else 0.0
    return {"weighted_pct": weighted_pct, "raw_pct": raw_pct,
            "n_pass": n_pass, "n_total": n_total,
            "by_area": by_area, "by_sev": by_sev}


def _level(pct: float) -> str:
    if pct >= 90: return "A — near-complete"
    if pct >= 75: return "B — strong"
    if pct >= 60: return "C — usable, gaps remain"
    if pct >= 40: return "D — partial"
    return "E — early"


def format_scorecard() -> str:
    if not RESULTS:
        return ""
    s = compute_scorecard()
    lines = ["=" * 64, "  HUE API COMPLIANCE SCORECARD", "=" * 64]
    lines.append(f"  Overall (severity-weighted): {s['weighted_pct']:5.1f}%   "
                 f"grade {_level(s['weighted_pct'])}")
    lines.append(f"  Checks passed (unweighted):  {s['n_pass']}/{s['n_total']}  "
                 f"({s['raw_pct']:.0f}%)")
    lines.append("-" * 64)
    lines.append("  By severity:")
    for sev in ("HIGH", "MED", "LOW"):
        if sev in s["by_sev"]:
            p, t = s["by_sev"][sev]
            lines.append(f"      {sev:4}  {p}/{t}")
    lines.append("  By area:")
    for area in sorted(s["by_area"]):
        p, t = s["by_area"][area]
        lines.append(f"      {area:22} {p}/{t}")
    gaps = [(cid, r) for cid, r in RESULTS.items() if not r["passed"]]
    if gaps:
        lines.append("-" * 64)
        lines.append(f"  Open gaps ({len(gaps)}):")
        for cid, r in sorted(gaps, key=lambda x: (-SEVERITY_WEIGHT[x[1]["severity"]], x[0])):
            lines.append(f"      [{r['severity']:4}] {cid}: {r['detail']}")
    lines.append("=" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Local factories
# ---------------------------------------------------------------------------

def _make_light(modelid="LCT015", **state):
    from HueObjects.Light import Light
    l = Light({"name": "probe", "modelid": modelid, "id_v1": "1",
               "id_v2": str(uuid.uuid4()), "protocol": "dummy"})
    l.state.update(state)
    return l


def _add_light(bridge_cfg, **state):
    l = _make_light(**state)
    bridge_cfg["lights"]["1"] = l
    return l


def _find_key(obj, key):
    """Yield every value stored under `key` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


def _entertainment_source() -> str:
    return Path("BridgeEmulator/services/entertainment.py").read_text()


# ===========================================================================
# v1 STATE HANDLING
# ===========================================================================

class TestV1State:
    def test_bri_upper_clamp(self):
        with probe("v1.bri_upper_clamp", "v1-light", "HIGH", "bri 1..254") as b:
            l = _make_light()
            l.setV1State({"bri": 999})
            b.passed = l.state["bri"] == 254
            b.detail = f"PUT bri=999 stored as {l.state['bri']} (expected 254)"

    def test_ct_lower_clamp(self):
        with probe("v1.ct_lower_clamp", "v1-light", "MED", "ct 153..500") as b:
            l = _make_light()
            l.setV1State({"ct": 50})
            b.passed = l.state["ct"] == 153
            b.detail = f"PUT ct=50 stored as {l.state['ct']} (expected 153)"

    def test_bri_zero_rejected(self):
        with probe("v1.bri_zero_rejected", "v1-light", "MED", "bri 1..254 (0 invalid)") as b:
            l = _make_light()
            l.setV1State({"bri": 0})
            b.passed = l.state["bri"] >= 1
            b.detail = f"PUT bri=0 stored as {l.state['bri']} (spec min 1)"

    def test_hue_inc_wraps_mod_65536(self):
        with probe("v1.hue_inc_wrap", "v1-light", "MED", "hue wraps mod 65536") as b:
            from HueObjects import incProcess
            out = incProcess({"hue": 60000}, {"hue_inc": 60000})
            b.passed = out.get("hue") == 54464
            b.detail = f"60000+60000 → {out.get('hue')} (expected 54464)"

    def test_xy_inc_supported(self):
        with probe("v1.xy_inc_supported", "v1-light", "MED", "xy_inc delta") as b:
            from HueObjects import incProcess
            out = incProcess({"xy": [0.3, 0.3]}, {"xy_inc": [0.1, 0.1]})
            b.passed = "xy_inc" not in out and out.get("xy") not in (None, [0.3, 0.3])
            b.detail = "xy_inc not applied (operator unimplemented)"


# ===========================================================================
# v1 / v2 STATE CONVERSION
# ===========================================================================

class TestStateConversion:
    def test_v2_brightness_is_percent(self):
        with probe("v2.brightness_percent", "v2-convert", "HIGH",
                   "dimming.brightness 0..100") as b:
            from HueObjects import v1StateToV2
            val = v1StateToV2({"bri": 254})["dimming"]["brightness"]
            b.passed = val == 100.0
            b.detail = f"bri=254 → brightness={val} (expected 100.0)"

    def test_v2_brightness_roundtrip(self):
        with probe("v2.brightness_roundtrip", "v2-convert", "MED",
                   "bri↔brightness round-trip") as b:
            from HueObjects import v1StateToV2, v2StateToV1
            back = v2StateToV1(v1StateToV2({"bri": 254}))["bri"]
            b.passed = back == 254
            b.detail = f"254 round-tripped to {back}"


# ===========================================================================
# v2 ENVELOPE  (live Flask)
# ===========================================================================

class TestV2Envelope:
    def test_auth_required(self, app_client):
        with probe("v2.auth_403", "v2-auth", "HIGH", "missing key → 403") as b:
            code = app_client.get("/clip/v2/resource/bridge").status_code
            b.passed = code == 403
            b.detail = f"no-key GET returned {code} (expected 403)"

    def test_get_collection_envelope(self, app_client):
        with probe("v2.envelope_get", "v2-envelope", "MED", "{data[],errors[]}") as b:
            body = _body(_get(app_client, "/clip/v2/resource/light"))
            b.passed = isinstance(body.get("data"), list) and isinstance(body.get("errors"), list)
            b.detail = f"GET /light body keys={list(body)}"

    def test_put_response_has_errors_key(self, app_client, bridge_cfg):
        with probe("v2.envelope_put_errors", "v2-envelope", "MED",
                   "PUT body has errors[]") as b:
            l = _add_light(bridge_cfg)
            body = _body(_put(app_client, f"/clip/v2/resource/light/{l.id_v2}",
                              {"on": {"on": True}}))
            b.passed = "errors" in body
            b.detail = f"PUT /light body keys={list(body)} (spec needs both)"

    def test_unknown_resource_has_data_key(self, app_client):
        with probe("v2.envelope_unknown_data", "v2-envelope", "MED",
                   "error body keeps data[]") as b:
            body = _body(_get(app_client, "/clip/v2/resource/not_a_resource"))
            b.passed = "data" in body
            b.detail = f"unknown-resource body keys={list(body)}"


# ===========================================================================
# v2 SCHEMA CONFORMANCE  (jsonschema vs openhue spec)
# ===========================================================================

class TestV2Schema:
    def test_device_items_valid(self, app_client, bridge_cfg, spec, resolver,
                                 resource_schema_map):
        with probe("v2.device_schema", "v2-schema", "HIGH", "DeviceGet") as b:
            _add_rdm002(bridge_cfg)
            schema = resource_schema_map.get("device")
            data = _body(_get(app_client, "/clip/v2/resource/device"))["data"]
            errs = [e for item in data for e in validate(item, schema, spec, resolver)]
            b.passed = bool(data) and not errs
            b.detail = f"{len(errs)} schema error(s): {errs[:1]}"

    def test_light_items_valid(self, app_client, bridge_cfg, spec, resolver,
                               resource_schema_map):
        with probe("v2.light_schema", "v2-schema", "MED", "LightGet") as b:
            _add_light(bridge_cfg)
            schema = resource_schema_map.get("light")
            data = _body(_get(app_client, "/clip/v2/resource/light"))["data"]
            errs = [e for item in data for e in validate(item, schema, spec, resolver)]
            b.passed = bool(data) and not errs
            b.detail = f"{len(errs)} schema error(s): {errs[:1]}"


# ===========================================================================
# v2 STRUCTURAL DETAILS
# ===========================================================================

class TestV2Structure:
    def test_mirek_valid_at_boundary(self, app_client, bridge_cfg):
        with probe("v2.mirek_valid_boundary", "v2-light", "MED",
                   "mirek 153..500 inclusive") as b:
            _add_light(bridge_cfg, ct=153, colormode="ct", on=True)
            data = _body(_get(app_client, "/clip/v2/resource/light"))["data"]
            mv = data and data[0].get("color_temperature", {}).get("mirek_valid")
            b.passed = mv is True
            b.detail = f"mirek_valid={mv} at ct=153 (boundary should be valid)"

    def test_light_has_owner(self, app_client, bridge_cfg):
        with probe("v2.light_owner", "v2-light", "MED", "service owner→device") as b:
            _add_light(bridge_cfg)
            data = _body(_get(app_client, "/clip/v2/resource/light"))["data"]
            owner = data and data[0].get("owner", {})
            b.passed = bool(owner) and owner.get("rtype") == "device"
            b.detail = f"light owner={owner}"

    def test_grouped_light_owner_is_group(self, app_client, bridge_cfg):
        with probe("v2.grouped_light_owner", "v2-group", "MED",
                   "grouped_light owner=room/zone") as b:
            _add_room(bridge_cfg)
            data = _body(_get(app_client, "/clip/v2/resource/grouped_light"))["data"]
            owners = [d.get("owner", {}).get("rtype") for d in data]
            b.passed = bool(owners) and all(o in ("room", "zone", "bridge_home") for o in owners)
            b.detail = f"grouped_light owner rtypes={owners} (expected room/zone)"

    def test_rotary_direction_enum(self, app_client, bridge_cfg):
        with probe("v2.rotary_direction_enum", "v2-sensor", "MED",
                   "direction ∈ {clock_wise,counter_clock_wise}") as b:
            _add_rdm002(bridge_cfg)
            data = _body(_get(app_client, "/clip/v2/resource/relative_rotary"))["data"]
            dirs = list(_find_key(data, "direction"))
            b.passed = bool(dirs) and all(d in ("clock_wise", "counter_clock_wise") for d in dirs)
            b.detail = f"direction values={dirs}"

    def test_button_getbyid_is_flat(self, app_client, bridge_cfg):
        with probe("v2.button_getbyid_flat", "v2-sensor", "MED",
                   "data[] is flat resources") as b:
            _add_rdm002(bridge_cfg)
            coll = _body(_get(app_client, "/clip/v2/resource/button"))["data"]
            assert coll, "no button resources produced"
            bid = coll[0]["id"]
            item = _body(_get(app_client, f"/clip/v2/resource/button/{bid}"))["data"]
            b.passed = bool(item) and isinstance(item[0], dict)
            b.detail = (f"GET button/{{id}} data[0] is "
                        f"{type(item[0]).__name__ if item else 'none'} (nested list = bug)")

    def test_group_supports_device_children(self):
        with probe("v2.group_device_children", "v2-group", "HIGH",
                   "room/zone children include devices") as b:
            from HueObjects.Group import Group
            g = Group({"name": "r", "id_v1": "1", "type": "Room", "class": "living_room"})
            b.passed = hasattr(g, "device_children")
            b.detail = "Group has no device_children attribute (non-light children dropped)"


# ===========================================================================
# v2 BEHAVIOR INSTANCE
# ===========================================================================

class TestV2Behavior:
    def test_spec_shaped_post_not_500(self, app_client, bridge_cfg):
        with probe("v2.behavior_post_not_500", "v2-behavior", "HIGH",
                   "spec POST does not crash") as b:
            body = {
                "type": "behavior_instance", "enabled": True,
                "script_id": str(uuid.uuid4()),
                "configuration": {"device": {"rid": str(uuid.uuid4()), "rtype": "device"}},
                "metadata": {"name": "probe"},
            }
            code = _post(app_client, "/clip/v2/resource/behavior_instance", body).status_code
            b.passed = code != 500
            b.detail = f"spec-shaped behavior_instance POST → HTTP {code}"


# ===========================================================================
# RULES ENGINE  (needs app_client so configManager stub is wired)
# ===========================================================================

class TestRules:
    def test_unimplemented_operator_not_autosatisfied(self, app_client, bridge_cfg):
        with probe("rules.unknown_operator_safe", "v1-rules", "MED",
                   "'not in'/'stable' evaluated") as b:
            import functions.rules as rules
            bridge_cfg["sensors"]["1"] = SimpleNamespace(
                state={"status": 0}, dxState={"status": None})
            device = SimpleNamespace(
                getObjectPath=lambda: {"resource": "sensors", "id": "1"},
                name="probe", id_v1="1")
            # 'not in' a window that currently DOES include now → condition is
            # False, so a compliant engine returns [False]. Current code has no
            # branch for 'not in' and silently treats the rule as satisfied.
            rule = SimpleNamespace(
                name="probe", id_v1="1",
                conditions=[{"address": "/sensors/1/state/status",
                             "operator": "not in",
                             "value": "T00:00:00/T23:59:59"}])
            res = rules.checkRuleConditions(rule, device, datetime.now())
            satisfied = bool(res) and res[0] is True
            b.passed = not satisfied
            b.detail = "unimplemented operator silently satisfies the rule"


# ===========================================================================
# EVENT STREAM (SSE)
# ===========================================================================

class TestEventStream:
    def _emit(self):
        import HueObjects
        light = _make_light(on=False)        # construction emits its own events
        HueObjects.eventstream = []          # measure only the state change below
        light.setV1State({"on": True})
        return list(HueObjects.eventstream)

    def test_single_change_is_batched(self):
        with probe("sse.batched_event", "v2-sse", "MED",
                   "one change → one batched event") as b:
            events = self._emit()
            b.passed = len(events) == 1
            b.detail = (f"single light change produced {len(events)} SSE events "
                        "(spec batches into one data[])")

    def test_grouped_light_cascade(self):
        with probe("sse.grouped_light_cascade", "v2-sse", "HIGH",
                   "light change cascades grouped_light") as b:
            events = self._emit()
            b.passed = any("grouped_light" in json.dumps(e) for e in events)
            b.detail = "no grouped_light rollup emitted on single-light change"


# ===========================================================================
# COMMAND RATE LIMITING
# ===========================================================================

class TestRateLimit:
    def test_burst_yields_429(self, app_client, bridge_cfg):
        with probe("ratelimit.burst_429", "rate-limit", "MED",
                   "~10 light cmd/s else 429") as b:
            l = _add_light(bridge_cfg)
            codes = [
                _put(app_client, f"/clip/v2/resource/light/{l.id_v2}",
                     {"on": {"on": bool(i % 2)}}).status_code
                for i in range(30)
            ]
            b.passed = 429 in codes
            b.detail = f"30 rapid PUTs returned no 429 (codes seen: {sorted(set(codes))})"


# ===========================================================================
# ENTERTAINMENT TRANSPORT  (capability presence — static source check)
# ===========================================================================

class TestEntertainment:
    def test_dtls_port_2100(self):
        with probe("ent.port_2100", "entertainment", "MED", "DTLS UDP 2100") as b:
            b.passed = "2100" in _entertainment_source()
            b.detail = "entertainment service does not bind port 2100"

    def test_psk_identity_and_key(self):
        with probe("ent.psk", "entertainment", "MED",
                   "PSK identity=app-key,key=clientkey") as b:
            src = _entertainment_source()
            b.passed = "-psk" in src and "psk_identity" in src
            b.detail = "DTLS PSK identity/key not wired"

    def test_huestream_parsing(self):
        with probe("ent.huestream", "entertainment", "MED",
                   "HueStream v1+v2 packet parse") as b:
            b.passed = "HueStream" in _entertainment_source()
            b.detail = "HueStream header not parsed"


# ===========================================================================
# REGRESSION GATE  (keep last)
# ===========================================================================

def test_scorecard_meets_baseline():
    """Fails if weighted compliance regresses below the recorded baseline."""
    report = format_scorecard()
    if report:
        Path(__file__).parent.joinpath("compliance_report.txt").write_text(report + "\n")
    s = compute_scorecard()
    if s["n_total"] < 10:
        pytest.skip("scorecard incomplete (run the full module to gate)")
    assert s["weighted_pct"] >= BASELINE_WEIGHTED_PCT, (
        f"compliance regressed to {s['weighted_pct']:.1f}% "
        f"(baseline {BASELINE_WEIGHTED_PCT}%)\n{report}"
    )
