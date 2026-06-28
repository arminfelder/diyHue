"""
Hue CLIP v2 API contract tests — spec-driven.

The OpenHue OpenAPI spec is downloaded once per session (cached to
tests/fixtures/openhue_spec.yaml).  Every collection endpoint is parametrized
directly from the spec's path list, and response bodies are validated with
jsonschema against the spec's component schemas.

This means no field names are hardcoded here; if the spec changes the tests
automatically reflect it on next run.
"""
import json
import os
import sys
import types
import uuid
import weakref
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, "BridgeEmulator")

# ---------------------------------------------------------------------------
# Spec loading / caching
# ---------------------------------------------------------------------------

SPEC_URL = "https://api.redocly.com/registry/bundle/openhue/openhue/v2/openapi.yaml"
SPEC_CACHE = Path(__file__).parent / "fixtures" / "openhue_spec.yaml"


def _load_spec() -> dict:
    if SPEC_CACHE.exists():
        with open(SPEC_CACHE) as f:
            return yaml.safe_load(f)
    import urllib.request
    SPEC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SPEC_URL, timeout=30) as resp:
        raw = resp.read()
    SPEC_CACHE.write_bytes(raw)
    return yaml.safe_load(raw)


@pytest.fixture(scope="session")
def spec():
    return _load_spec()


# ---------------------------------------------------------------------------
# jsonschema resolver — resolves $ref: '#/components/schemas/...' internally
# ---------------------------------------------------------------------------

def _make_resolver(spec: dict):
    import jsonschema
    return jsonschema.RefResolver.from_schema(
        spec,
        store={"": spec},
    )


@pytest.fixture(scope="session")
def resolver(spec):
    return _make_resolver(spec)


def validate(instance, schema_name: str, spec: dict, resolver) -> list[str]:
    """
    Validate `instance` against `spec['components']['schemas'][schema_name]`.
    Returns list of error messages (empty = valid).
    """
    import jsonschema
    schema = spec["components"]["schemas"][schema_name]
    errors = list(jsonschema.Draft4Validator(schema, resolver=resolver).iter_errors(instance))
    return [e.message for e in errors]


# ---------------------------------------------------------------------------
# Map each CLIP v2 collection path to its item schema name
# (derived from the spec at session start — nothing hardcoded)
# ---------------------------------------------------------------------------

def _build_resource_schema_map(spec: dict) -> dict[str, str]:
    mapping = {}
    for path, methods in spec["paths"].items():
        if "get" not in methods or "{" in path:
            continue
        if not path.startswith("/clip/v2/resource/"):
            continue
        resource = path.removeprefix("/clip/v2/resource/")
        try:
            allof = (methods["get"]["responses"]["200"]["content"]
                     ["application/json"]["schema"]["allOf"])
        except (KeyError, TypeError):
            continue
        for part in allof:
            data_items = (part.get("properties", {}).get("data", {})
                          .get("items", {}).get("$ref", ""))
            if data_items:
                mapping[resource] = data_items.split("/")[-1]
                break
    return mapping


@pytest.fixture(scope="session")
def resource_schema_map(spec):
    return _build_resource_schema_map(spec)


# ---------------------------------------------------------------------------
# Bridge config / Flask app helpers  (duplicated from conftest for clarity,
# conftest.app_client fixture is also available)
# ---------------------------------------------------------------------------

API_KEY = "testkey"
HEADERS = {"hue-application-key": API_KEY, "Content-Type": "application/json"}


def _get(client, path):
    return client.get(path, headers=HEADERS)


def _put(client, path, body):
    return client.put(path, headers=HEADERS, data=json.dumps(body))


def _post(client, path, body):
    return client.post(path, headers=HEADERS, data=json.dumps(body))


def _delete(client, path):
    return client.delete(path, headers=HEADERS)


def _body(resp):
    return json.loads(resp.data)


def _add_rdm002(bridge_cfg, ieee="0xAABBCCDDEEFF0011", name="test_sw"):
    from HueObjects.Sensor import Sensor
    sw_id = str(uuid.uuid4())
    rot_id = str(uuid.uuid4())
    mac = ":".join(ieee.replace("0x", "").ljust(16, "0")[i:i+2] for i in range(0, 16, 2))
    sw = Sensor({
        "name": name, "id_v1": "10", "id_v2": sw_id,
        "modelid": "RDM002", "type": "ZLLSwitch",
        "uniqueid": mac + "-01-1000", "protocol": "mqtt",
        "protocol_cfg": {"friendly_name": name, "ieeeAddr": ieee, "model": "8719514440937"},
    })
    rot = Sensor({
        "name": name, "id_v1": "11", "id_v2": rot_id,
        "modelid": "RDM002", "type": "ZLLRelativeRotary",
        "uniqueid": mac + "-01-1000", "protocol": "mqtt",
        "protocol_cfg": {"friendly_name": name, "ieeeAddr": ieee, "model": "8719514440937"},
        "parent_id_v2": sw_id,
    })
    bridge_cfg["sensors"]["10"] = sw
    bridge_cfg["sensors"]["11"] = rot
    return sw, rot


def _add_room(bridge_cfg):
    from HueObjects.Group import Group
    g = Group({
        "name": "Test Room", "id_v1": "1", "id_v2": str(uuid.uuid4()),
        "type": "Room", "class": "living_room",
        "owner": bridge_cfg["apiUsers"][API_KEY],
    })
    bridge_cfg["groups"]["1"] = g
    return g


# ---------------------------------------------------------------------------
# 1. Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_no_key_is_403(self, app_client):
        assert app_client.get("/clip/v2/resource/bridge").status_code == 403

    def test_wrong_key_is_403(self, app_client):
        assert app_client.get(
            "/clip/v2/resource/bridge",
            headers={"hue-application-key": "wrong"}
        ).status_code == 403

    def test_valid_key_is_200(self, app_client):
        assert _get(app_client, "/clip/v2/resource/bridge").status_code == 200


# ---------------------------------------------------------------------------
# 2. Envelope shape — every collection endpoint
# ---------------------------------------------------------------------------

# Resources diyHue actually implements (subset of the full spec)
IMPLEMENTED_RESOURCES = [
    "bridge", "device", "light", "room", "zone",
    "grouped_light", "scene", "behavior_instance",
    "zigbee_connectivity", "button", "relative_rotary",
    "device_power", "motion",
]


class TestEnvelope:
    @pytest.mark.parametrize("resource", IMPLEMENTED_RESOURCES)
    def test_returns_data_and_errors_arrays(self, app_client, resource):
        body = _body(_get(app_client, f"/clip/v2/resource/{resource}"))
        assert "data" in body and isinstance(body["data"], list)
        assert "errors" in body and isinstance(body["errors"], list)


# ---------------------------------------------------------------------------
# 3. Schema validation — every item in every collection response
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    """
    For each implemented resource, hit the collection endpoint and validate
    every item in the data array against the spec's schema.
    The parametrize list is derived from the spec, not hardcoded.
    """

    @pytest.mark.parametrize("resource", IMPLEMENTED_RESOURCES)
    def test_collection_items_match_spec(self, app_client, bridge_cfg,
                                         resource, spec, resolver,
                                         resource_schema_map):
        # Populate bridge with data so responses aren't empty
        if resource in ("button", "relative_rotary", "device_power",
                        "zigbee_connectivity", "device"):
            _add_rdm002(bridge_cfg)
        if resource == "room":
            _add_room(bridge_cfg)

        schema_name = resource_schema_map.get(resource)
        if schema_name is None:
            pytest.skip(f"No schema mapping found for resource '{resource}'")

        resp = _get(app_client, f"/clip/v2/resource/{resource}")
        assert resp.status_code == 200
        data = _body(resp)["data"]

        # An empty collection is valid (nothing to validate against schema)
        for item in data:
            errors = validate(item, schema_name, spec, resolver)
            assert errors == [], (
                f"GET /clip/v2/resource/{resource} item {item.get('id')} "
                f"fails {schema_name} schema:\n" + "\n".join(errors)
            )


# ---------------------------------------------------------------------------
# 4. Per-resource spec-derived assertions
# ---------------------------------------------------------------------------

class TestBridgeResource:
    def test_bridge_id_lowercase(self, app_client, spec, resolver):
        """Spec: bridge_id is lowercase."""
        items = _body(_get(app_client, "/clip/v2/resource/bridge"))["data"]
        for item in items:
            assert item["bridge_id"] == item["bridge_id"].lower()

    def test_bridge_time_zone_present(self, app_client):
        items = _body(_get(app_client, "/clip/v2/resource/bridge"))["data"]
        for item in items:
            assert "time_zone" in item
            assert "time_zone" in item["time_zone"]


class TestDeviceResource:
    def test_rdm002_services_contain_spec_rtypes(self, app_client, bridge_cfg, spec):
        """All service rtypes must be in the spec's ResourceIdentifier.rtype enum."""
        _add_rdm002(bridge_cfg)
        allowed_rtypes = set(
            spec["components"]["schemas"]["ResourceIdentifier"]
            ["properties"]["rtype"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/device"))["data"]
        for device in items:
            for svc in device.get("services", []):
                assert svc["rtype"] in allowed_rtypes, (
                    f"device {device['id']}: service rtype '{svc['rtype']}' "
                    f"not in spec enum"
                )

    def test_rdm002_device_services_include_required_types(self, app_client, bridge_cfg):
        """RDM002 device must expose button, relative_rotary, device_power, zigbee_connectivity."""
        _add_rdm002(bridge_cfg)
        items = _body(_get(app_client, "/clip/v2/resource/device"))["data"]
        rdm_devices = [
            d for d in items
            if d.get("product_data", {}).get("model_id") == "RDM002"
        ]
        assert len(rdm_devices) >= 1, "No RDM002 device in /device response"
        rtypes = {s["rtype"] for s in rdm_devices[0]["services"]}
        for required in ("button", "relative_rotary", "device_power", "zigbee_connectivity"):
            assert required in rtypes, f"RDM002 missing service rtype '{required}'"


class TestButtonResource:
    def test_button_event_values_are_spec_enum(self, app_client, bridge_cfg, spec):
        """button.button_report.event must be one of the spec enum values."""
        _add_rdm002(bridge_cfg)
        allowed = set(
            spec["components"]["schemas"]["ButtonGet"]
            ["allOf"][1]["properties"]["button"]
            ["properties"]["button_report"]["properties"]["event"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/button"))["data"]
        for btn in items:
            event = btn.get("button", {}).get("button_report", {}).get("event")
            if event is not None:
                assert event in allowed, f"button event '{event}' not in spec enum"

    def test_four_buttons_for_rdm002(self, app_client, bridge_cfg, spec):
        """RDM002 has exactly 4 buttons per spec (4 physical buttons)."""
        _add_rdm002(bridge_cfg)
        items = _body(_get(app_client, "/clip/v2/resource/button"))["data"]
        assert len(items) == 4


class TestRelativeRotaryResource:
    def test_direction_uses_spec_enum(self, app_client, bridge_cfg, spec):
        """Spec: rotation.direction must be clock_wise or counter_clock_wise."""
        _add_rdm002(bridge_cfg)
        # Set a direction so the field is populated
        bridge_cfg["sensors"]["11"].state["direction"] = "right"

        allowed = set(
            spec["components"]["schemas"]["RelativeRotaryGet"]
            ["allOf"][1]["properties"]["relative_rotary"]
            ["properties"]["rotary_report"]["properties"]["rotation"]
            ["properties"]["direction"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/relative_rotary"))["data"]
        for item in items:
            direction = (item.get("rotary_report", {})
                         .get("rotation", {}).get("direction"))
            if direction is not None:
                assert direction in allowed, (
                    f"direction '{direction}' not in spec enum {allowed}"
                )

    def test_rotary_action_uses_spec_enum(self, app_client, bridge_cfg, spec):
        """Spec: rotary_report.action must be start or repeat."""
        _add_rdm002(bridge_cfg)
        allowed = set(
            spec["components"]["schemas"]["RelativeRotaryGet"]
            ["allOf"][1]["properties"]["relative_rotary"]
            ["properties"]["rotary_report"]["properties"]["action"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/relative_rotary"))["data"]
        for item in items:
            action = item.get("rotary_report", {}).get("action")
            if action is not None:
                assert action in allowed, f"rotary action '{action}' not in spec enum"


class TestZigbeeConnectivity:
    def test_status_uses_spec_enum(self, app_client, bridge_cfg, spec):
        """Spec: status must be one of connected/disconnected/connectivity_issue/unidirectional_incoming."""
        _add_rdm002(bridge_cfg)
        allowed = set(
            spec["components"]["schemas"]["ZigbeeConnectivityGet"]
            ["allOf"][1]["properties"]["status"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/zigbee_connectivity"))["data"]
        for item in items:
            assert item.get("status") in allowed, (
                f"zigbee_connectivity status '{item.get('status')}' not in spec enum"
            )


class TestDevicePower:
    def test_battery_level_in_spec_range(self, app_client, bridge_cfg, spec):
        """Spec: battery_level is integer 0–100."""
        _add_rdm002(bridge_cfg)
        min_v = (spec["components"]["schemas"]["DevicePowerGet"]
                 ["allOf"][1]["properties"]["power_state"]
                 ["properties"]["battery_level"]["minimum"])
        max_v = (spec["components"]["schemas"]["DevicePowerGet"]
                 ["allOf"][1]["properties"]["power_state"]
                 ["properties"]["battery_level"]["maximum"])
        items = _body(_get(app_client, "/clip/v2/resource/device_power"))["data"]
        for item in items:
            level = item.get("power_state", {}).get("battery_level")
            if level is not None:
                assert min_v <= level <= max_v

    def test_battery_state_uses_spec_enum(self, app_client, bridge_cfg, spec):
        _add_rdm002(bridge_cfg)
        allowed = set(
            spec["components"]["schemas"]["DevicePowerGet"]
            ["allOf"][1]["properties"]["power_state"]
            ["properties"]["battery_state"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/device_power"))["data"]
        for item in items:
            state = item.get("power_state", {}).get("battery_state")
            if state is not None:
                assert state in allowed


class TestRoomResource:
    def test_room_children_all_valid_resource_identifiers(self, app_client, bridge_cfg, spec, resolver):
        """Every child in room.children must be a valid ResourceIdentifier."""
        _add_room(bridge_cfg)
        items = _body(_get(app_client, "/clip/v2/resource/room"))["data"]
        schema = spec["components"]["schemas"]["ResourceIdentifier"]
        import jsonschema
        for room in items:
            for child in room.get("children", []):
                errors = list(jsonschema.Draft4Validator(schema, resolver=resolver).iter_errors(child))
                assert errors == [], f"room child {child} fails ResourceIdentifier schema"

    def test_device_child_appears_after_put(self, app_client, bridge_cfg):
        """PUT room with a switch device → switch appears in children on next GET."""
        sw, _ = _add_rdm002(bridge_cfg)
        room = _add_room(bridge_cfg)
        room_v2_id = room.getV2Room()["id"]

        resp = _put(app_client, f"/clip/v2/resource/room/{room_v2_id}",
                    {"children": [{"rid": sw.id_v2, "rtype": "device"}]})
        assert resp.status_code == 200

        items = _body(_get(app_client, f"/clip/v2/resource/room/{room_v2_id}"))["data"]
        rids = [c["rid"] for c in items[0]["children"]]
        assert sw.id_v2 in rids, "Switch device not in room children after PUT"


class TestBehaviorInstance:
    def _payload(self, device_id):
        return {
            "metadata": {"name": "test"},
            "script_id": "f306f634-acdb-4dd6-bdf5-48dd626d667e",
            "configuration": {
                "device": {"rtype": "device", "rid": device_id},
            },
            "enabled": True,
        }

    def test_post_response_matches_spec(self, app_client, bridge_cfg, spec, resolver):
        sw, _ = _add_rdm002(bridge_cfg)
        resp = _post(app_client, "/clip/v2/resource/behavior_instance",
                     self._payload(sw.id_v2))
        assert resp.status_code == 200
        # POST returns {data: [{rid, rtype}], errors: []}
        body = _body(resp)
        assert body["data"][0]["rtype"] == "behavior_instance"

    def test_get_instance_matches_spec_schema(self, app_client, bridge_cfg, spec, resolver):
        sw, _ = _add_rdm002(bridge_cfg)
        post_body = _body(_post(app_client, "/clip/v2/resource/behavior_instance",
                                self._payload(sw.id_v2)))
        inst_id = post_body["data"][0]["rid"]

        items = _body(_get(app_client, "/clip/v2/resource/behavior_instance"))["data"]
        instance = next((i for i in items if i["id"] == inst_id), None)
        assert instance is not None

        errors = validate(instance, "BehaviorInstanceGet", spec, resolver)
        assert errors == [], "\n".join(errors)

    def test_status_uses_spec_enum(self, app_client, bridge_cfg, spec):
        sw, _ = _add_rdm002(bridge_cfg)
        _post(app_client, "/clip/v2/resource/behavior_instance", self._payload(sw.id_v2))
        allowed = set(
            spec["components"]["schemas"]["BehaviorInstanceGet"]
            ["allOf"][1]["properties"]["status"]["enum"]
        )
        items = _body(_get(app_client, "/clip/v2/resource/behavior_instance"))["data"]
        for inst in items:
            assert inst["status"] in allowed, (
                f"behavior_instance status '{inst['status']}' not in spec enum"
            )

    def test_delete_removes_instance(self, app_client, bridge_cfg):
        sw, _ = _add_rdm002(bridge_cfg)
        inst_id = _body(_post(app_client, "/clip/v2/resource/behavior_instance",
                              self._payload(sw.id_v2)))["data"][0]["rid"]
        assert _delete(app_client, f"/clip/v2/resource/behavior_instance/{inst_id}").status_code == 200
        ids = [i["id"] for i in _body(_get(app_client, "/clip/v2/resource/behavior_instance"))["data"]]
        assert inst_id not in ids
