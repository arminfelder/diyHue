"""
Tests for Group.device_children — the feature that lets non-light devices
(switches, sensors) be stored as room children and survive save/restore.
"""
import sys
import types
import uuid
import weakref
from unittest.mock import MagicMock
import pytest


# ---------------------------------------------------------------------------
# Module-level stubs (conftest autouse fixture handles cleanup, but we also
# need them present before the very first import in this file's scope)
# ---------------------------------------------------------------------------

def _setup_stubs():
    log_mod = types.ModuleType("logManager")
    log_mod.logger = MagicMock()
    log_mod.logger.get_logger.return_value = MagicMock()
    sys.modules.setdefault("logManager", log_mod)

    cfg_mod = types.ModuleType("configManager")
    bc = MagicMock()
    bc.yaml_config = {}
    cfg_mod.bridgeConfig = bc
    sys.modules.setdefault("configManager", cfg_mod)


_setup_stubs()

import sys
sys.path.insert(0, "BridgeEmulator")
from HueObjects.Group import Group  # noqa: E402


def _make_group(extra=None):
    data = {
        "name": "Test Room",
        "id_v1": "1",
        "id_v2": str(uuid.uuid4()),
        "type": "Room",
        "class": "living_room",
    }
    if extra:
        data.update(extra)
    return Group(data)


def _make_light_mock(dev_id=None):
    light = MagicMock()
    light.id_v1 = "1"
    light.id_v2 = str(uuid.uuid4())
    light.getDevice.return_value = {"id": dev_id or str(uuid.uuid4())}
    light.protocol_cfg = {}
    light.state = {"on": True, "bri": 200}
    light.updateLightState = MagicMock()
    light.setV1State = MagicMock()
    return light


# ---------------------------------------------------------------------------
# device_children initialisation
# ---------------------------------------------------------------------------

class TestDeviceChildrenInit:
    def test_default_empty(self):
        g = _make_group()
        assert g.device_children == []

    def test_loaded_from_data(self):
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        g = _make_group({"device_children": ids})
        assert g.device_children == ids

    def test_loaded_list_is_copy(self):
        ids = [str(uuid.uuid4())]
        g = _make_group({"device_children": ids})
        ids.append("extra")
        assert len(g.device_children) == 1  # mutation of source doesn't affect group


# ---------------------------------------------------------------------------
# getV2Room — children output
# ---------------------------------------------------------------------------

class TestGetV2RoomChildren:
    def test_no_children_returns_empty(self):
        g = _make_group()
        result = g.getV2Room()
        assert result["children"] == []

    def test_light_child_appears(self):
        g = _make_group()
        light = _make_light_mock()
        g.lights.append(weakref.ref(light))
        result = g.getV2Room()
        dev_ids = [c["rid"] for c in result["children"]]
        assert light.getDevice()["id"] in dev_ids

    def test_device_child_appears(self):
        switch_id = str(uuid.uuid4())
        g = _make_group({"device_children": [switch_id]})
        result = g.getV2Room()
        rids = [c["rid"] for c in result["children"]]
        assert switch_id in rids

    def test_device_child_rtype_is_device(self):
        switch_id = str(uuid.uuid4())
        g = _make_group({"device_children": [switch_id]})
        result = g.getV2Room()
        entry = next(c for c in result["children"] if c["rid"] == switch_id)
        assert entry["rtype"] == "device"

    def test_light_and_device_child_both_appear(self):
        switch_id = str(uuid.uuid4())
        g = _make_group({"device_children": [switch_id]})
        light = _make_light_mock()
        g.lights.append(weakref.ref(light))
        result = g.getV2Room()
        rids = [c["rid"] for c in result["children"]]
        assert light.getDevice()["id"] in rids
        assert switch_id in rids

    def test_no_duplicate_if_device_child_already_in_lights(self):
        shared_id = str(uuid.uuid4())
        light = _make_light_mock(dev_id=shared_id)
        g = _make_group({"device_children": [shared_id]})
        g.lights.append(weakref.ref(light))
        result = g.getV2Room()
        rids = [c["rid"] for c in result["children"]]
        assert rids.count(shared_id) == 1

    def test_multiple_device_children(self):
        ids = [str(uuid.uuid4()) for _ in range(3)]
        g = _make_group({"device_children": ids})
        result = g.getV2Room()
        rids = [c["rid"] for c in result["children"]]
        for sid in ids:
            assert sid in rids


# ---------------------------------------------------------------------------
# save / round-trip
# ---------------------------------------------------------------------------

class TestGroupSave:
    def test_save_includes_device_children(self):
        ids = [str(uuid.uuid4())]
        g = _make_group({"device_children": ids})
        saved = g.save()
        assert saved["device_children"] == ids

    def test_save_empty_device_children(self):
        g = _make_group()
        saved = g.save()
        assert saved["device_children"] == []

    def test_round_trip(self):
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        g1 = _make_group({"device_children": ids})
        saved = g1.save()
        saved["id_v1"] = "1"
        saved["type"] = "Room"
        saved["name"] = "Test Room"
        saved["class"] = "living_room"
        g2 = Group(saved)
        assert set(g2.device_children) == set(ids)
