"""
Tests for functions/behavior_instance.py

Covers:
  - findTriggerTime          — time-slot selection
  - checkBehaviorInstances   — button dispatch (RDM002 per-button format)
  - checkBehaviorInstances   — rotary dispatch
  - checkBehaviorInstances   — disabled instances are skipped
  - checkBehaviorInstances   — unknown device type is ignored
"""
import sys
import types
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _setup_stubs(bi=None, scenes=None, groups=None, sensors=None):
    log_mod = types.ModuleType("logManager")
    log_mod.logger = MagicMock()
    log_mod.logger.get_logger.return_value = MagicMock()
    sys.modules["logManager"] = log_mod

    cfg = {
        "lights": {},
        "groups": groups or {},
        "scenes": scenes or {},
        "sensors": sensors or {},
        "behavior_instance": bi or {},
        "config": {},
        "apiUsers": {},
    }
    cfg_mod = types.ModuleType("configManager")
    bc = MagicMock()
    bc.yaml_config = cfg
    cfg_mod.bridgeConfig = bc
    sys.modules["configManager"] = cfg_mod
    return cfg


def _import_bi(cfg):
    for key in list(sys.modules.keys()):
        if key in ("functions.behavior_instance", "functions"):
            del sys.modules[key]
    sys.path.insert(0, "BridgeEmulator")
    import functions.behavior_instance as bi_mod
    bi_mod.bridgeConfig = cfg
    return bi_mod


def _make_sensor(sensor_type, id_v2=None, parent_id_v2=None, buttonevent=None, direction=None):
    s = MagicMock()
    s.type = sensor_type
    s.id_v2 = id_v2 or str(uuid.uuid4())
    s.parent_id_v2 = parent_id_v2
    s.uniqueid = "00:11:22:33:44:55:66:77-01-1000"
    s.state = {}
    if buttonevent is not None:
        s.state["buttonevent"] = buttonevent
    if direction is not None:
        s.state["direction"] = direction
    return s


def _make_group(group_id_v2=None, any_on=False, avr_bri=100):
    g = MagicMock()
    g.id_v2 = group_id_v2 or str(uuid.uuid4())
    g.update_state.return_value = {"any_on": any_on, "all_on": any_on, "avr_bri": avr_bri}
    g.setV1Action = MagicMock()
    return g


def _make_room_rid(group_id_v2, rtype="room"):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, group_id_v2 + rtype))


def _make_instance(device_id, button_cfg, enabled=True, rotary_cfg=None):
    inst = MagicMock()
    inst.enabled = enabled
    cfg = {
        "device": {"rtype": "device", "rid": device_id},
    }
    cfg.update(button_cfg)
    if rotary_cfg:
        cfg["rotary"] = rotary_cfg
    inst.configuration = cfg
    return inst


# ---------------------------------------------------------------------------
# findTriggerTime
# ---------------------------------------------------------------------------

class TestFindTriggerTime:
    def _import(self):
        cfg = _setup_stubs()
        return _import_bi(cfg)

    def test_returns_first_slot_actions_when_in_range(self):
        bi = self._import()
        actions_morning = [{"action": "recall_morning"}]
        actions_evening = [{"action": "recall_evening"}]
        slots = [
            {"hour": 7, "minute": 0, "actions": actions_morning},
            {"hour": 18, "minute": 0, "actions": actions_evening},
        ]
        with patch("functions.behavior_instance.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, 0)
            result = bi.findTriggerTime(slots)
        assert result is actions_morning

    def test_falls_through_to_last_slot(self):
        bi = self._import()
        actions_last = [{"action": "recall_night"}]
        slots = [
            {"hour": 7, "minute": 0, "actions": []},
            {"hour": 22, "minute": 0, "actions": actions_last},
        ]
        with patch("functions.behavior_instance.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 23, 0, 0)
            result = bi.findTriggerTime(slots)
        assert result is actions_last

    def test_single_slot_always_returns_it(self):
        bi = self._import()
        actions = [{"action": "always"}]
        slots = [{"hour": 0, "minute": 0, "actions": actions}]
        with patch("functions.behavior_instance.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, 0)
            result = bi.findTriggerTime(slots)
        assert result is actions


# ---------------------------------------------------------------------------
# checkBehaviorInstances — ZLLSwitch (RDM002 per-button format)
# ---------------------------------------------------------------------------

class TestCheckBehaviorInstancesSwitch:
    def _setup(self, buttonevent, action_cfg, any_on=False):
        group_id_v2 = str(uuid.uuid4())
        group = _make_group(group_id_v2, any_on=any_on)
        room_rid = _make_room_rid(group_id_v2)

        device_id = str(uuid.uuid4())
        sensor = _make_sensor("ZLLSwitch", id_v2=device_id, buttonevent=buttonevent)

        where = [{"group": {"rid": room_rid, "rtype": "room"}}]
        button_key = f"button{buttonevent // 1000}"
        last = buttonevent % 1000
        # Z2M buttonevent last digit -> per-button action (RDM002 spec mapping)
        action_map = {0: "on_initial_press", 1: "on_long_press", 2: "on_short_release", 3: "on_long_release"}
        action_key = action_map[last]

        instance = _make_instance(device_id, {button_key: {"where": where, action_key: action_cfg}})

        cfg = _setup_stubs(
            bi={"inst1": instance},
            groups={"1": group},
        )
        bi_mod = _import_bi(cfg)
        bi_mod.findGroup = lambda rid, rtype: group
        return bi_mod, sensor, group

    def test_recall_single_extended_calls_scene(self):
        scene_id = str(uuid.uuid4())
        action_cfg = {"recall_single_extended": {"actions": [{"action": {"recall": {"rtype": "scene", "rid": scene_id}}}]}}
        bi_mod, sensor, group = self._setup(buttonevent=1002, action_cfg=action_cfg)

        called_scene_ids = []
        bi_mod.callScene = lambda sid: called_scene_ids.append(sid)
        bi_mod.checkBehaviorInstances(sensor)

        assert scene_id in called_scene_ids

    def test_all_off_turns_off_group(self):
        action_cfg = {"action": "all_off"}
        bi_mod, sensor, group = self._setup(buttonevent=1003, action_cfg=action_cfg)
        bi_mod.checkBehaviorInstances(sensor)
        group.setV1Action.assert_called_once_with({"on": False})

    def test_dim_up_increments_brightness(self):
        action_cfg = {"action": "dim_up"}
        # button1 long_press (buttonevent 1003)
        bi_mod, sensor, group = self._setup(buttonevent=1003, action_cfg=action_cfg)
        bi_mod.checkBehaviorInstances(sensor)
        group.setV1Action.assert_called_with({"bri_inc": +30})

    def test_dim_down_decrements_brightness(self):
        action_cfg = {"action": "dim_down"}
        # button1 long_press (buttonevent 1003)
        bi_mod, sensor, group = self._setup(buttonevent=1003, action_cfg=action_cfg)
        bi_mod.checkBehaviorInstances(sensor)
        group.setV1Action.assert_called_with({"bri_inc": -30})

    def test_disabled_instance_is_skipped(self):
        scene_id = str(uuid.uuid4())
        action_cfg = {"recall_single_extended": {"actions": [{"action": {"recall": {"rtype": "scene", "rid": scene_id}}}]}}
        bi_mod, sensor, group = self._setup(buttonevent=1002, action_cfg=action_cfg)
        # disable the instance
        for inst in bi_mod.bridgeConfig["behavior_instance"].values():
            inst.enabled = False

        called = []
        bi_mod.callScene = lambda sid: called.append(sid)
        bi_mod.checkBehaviorInstances(sensor)
        assert called == []

    def test_wrong_button_key_is_skipped(self):
        # Instance has button1 config, device fires button4
        scene_id = str(uuid.uuid4())
        action_cfg = {"recall_single_extended": {"actions": [{"action": {"recall": {"rtype": "scene", "rid": scene_id}}}]}}
        bi_mod, sensor, group = self._setup(buttonevent=4002, action_cfg=action_cfg)
        # Reconfigure: instance only has button1 not button4
        for inst in bi_mod.bridgeConfig["behavior_instance"].values():
            inst.configuration = {
                "device": {"rtype": "device", "rid": sensor.id_v2},
                "button1": inst.configuration.get("button4", {}),
            }

        called = []
        bi_mod.callScene = lambda sid: called.append(sid)
        bi_mod.checkBehaviorInstances(sensor)
        assert called == []

    def test_time_based_extended_picks_correct_slot(self):
        scene_morning = str(uuid.uuid4())
        scene_evening = str(uuid.uuid4())
        action_cfg = {
            "time_based_extended": {
                "with_off": {"enabled": False},
                "slots": [
                    {"start_time": {"hour": 7, "minute": 0}, "actions": [{"action": {"recall": {"rtype": "scene", "rid": scene_morning}}}]},
                    {"start_time": {"hour": 18, "minute": 0}, "actions": [{"action": {"recall": {"rtype": "scene", "rid": scene_evening}}}]},
                ],
            }
        }
        bi_mod, sensor, group = self._setup(buttonevent=1002, action_cfg=action_cfg)
        called = []
        bi_mod.callScene = lambda sid: called.append(sid)
        with patch("functions.behavior_instance.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, 0)
            bi_mod.checkBehaviorInstances(sensor)
        assert scene_morning in called
        assert scene_evening not in called


# ---------------------------------------------------------------------------
# checkBehaviorInstances — ZLLRelativeRotary
# ---------------------------------------------------------------------------

class TestCheckBehaviorInstancesRotary:
    def _setup(self, direction, step=8, any_on=True, avr_bri=100, extra_cfg=None):
        group_id_v2 = str(uuid.uuid4())
        group = _make_group(group_id_v2, any_on=any_on, avr_bri=avr_bri)
        room_rid = _make_room_rid(group_id_v2)

        switch_id_v2 = str(uuid.uuid4())
        rotary = _make_sensor("ZLLRelativeRotary", parent_id_v2=switch_id_v2, direction=direction)
        rotary.state["rotary_step_size"] = step

        rotary_cfg = {"where": [{"group": {"rid": room_rid, "rtype": "room"}}],
                      "on_dim_off": {"action": "all_off"},
                      "on_dim_on": {"recall_single": [{"action": "last_on"}]}}
        if extra_cfg:
            rotary_cfg.update(extra_cfg)
        instance = _make_instance(switch_id_v2, {"rotary": rotary_cfg})

        cfg = _setup_stubs(bi={"inst1": instance}, groups={"1": group})
        bi_mod = _import_bi(cfg)
        bi_mod.findGroup = lambda rid, rtype: group
        return bi_mod, rotary, group

    def test_dim_up_steps_by_device_step(self):
        """clock_wise dims up by the device's rotary_step_size (action_step_size)."""
        bi_mod, rotary, group = self._setup("clock_wise", step=44, any_on=True)
        bi_mod.checkBehaviorInstances(rotary)
        group.setV1Action.assert_called_with({"bri_inc": +44, "transitiontime": 4})

    def test_dim_down_steps_by_device_step(self):
        bi_mod, rotary, group = self._setup("counter_clock_wise", step=8, any_on=True, avr_bri=80)
        bi_mod.checkBehaviorInstances(rotary)
        group.setV1Action.assert_called_with({"bri_inc": -8, "transitiontime": 4})

    def test_dim_down_holds_minimum_before_off(self):
        """At low-but-not-min brightness, keep dimming (don't turn off yet)."""
        bi_mod, rotary, group = self._setup("counter_clock_wise", step=8, any_on=True, avr_bri=10)
        bi_mod.checkBehaviorInstances(rotary)
        group.setV1Action.assert_called_with({"bri_inc": -8, "transitiontime": 4})

    def test_dim_down_at_minimum_turns_off(self):
        """Only once at the true minimum does on_dim_off fire (all_off)."""
        bi_mod, rotary, group = self._setup("counter_clock_wise", step=8, any_on=True, avr_bri=1)
        bi_mod.checkBehaviorInstances(rotary)
        group.setV1Action.assert_called_with({"on": False})

    def test_dim_up_from_off_recalls_last_on(self):
        """clock_wise from an off group restores the last on-state (not min)."""
        bi_mod, rotary, group = self._setup("clock_wise", any_on=False)
        bi_mod.checkBehaviorInstances(rotary)
        group.setV1Action.assert_called_with({"on": True, "transitiontime": 4})

    def test_uses_device_transition_time(self):
        """The per-detent transition follows the device's rotary_transition."""
        bi_mod, rotary, group = self._setup("clock_wise", step=8, any_on=True)
        rotary.state["rotary_transition"] = 5          # 0.05 s from action_transition_time
        bi_mod.checkBehaviorInstances(rotary)
        group.setV1Action.assert_called_with({"bri_inc": +8, "transitiontime": 5})

    def test_matches_via_parent_id_v2(self):
        """Instance configured on switch id_v2; rotary has it as parent_id_v2
        (set on pairing or back-filled from uniqueid at config load)."""
        bi_mod, rotary, group = self._setup("clock_wise", step=20, any_on=True)
        bi_mod.checkBehaviorInstances(rotary)
        assert group.setV1Action.called


# ---------------------------------------------------------------------------
# checkBehaviorInstances — device matching
# ---------------------------------------------------------------------------

class TestCheckBehaviorInstancesDeviceMatching:
    def test_matches_by_device_id_v2(self):
        device_id = str(uuid.uuid4())
        sensor = _make_sensor("ZLLSwitch", id_v2=device_id, buttonevent=1002)

        group_id_v2 = str(uuid.uuid4())
        group = _make_group(group_id_v2)
        room_rid = _make_room_rid(group_id_v2)
        scene_id = str(uuid.uuid4())
        action_payload = {"recall_single_extended": {"actions": [{"action": {"recall": {"rtype": "scene", "rid": scene_id}}}]}}
        instance = _make_instance(device_id, {"button1": {"where": [{"group": {"rid": room_rid, "rtype": "room"}}], "on_short_release": action_payload}})

        cfg = _setup_stubs(bi={"i": instance}, groups={"1": group})
        bi_mod = _import_bi(cfg)

        called = []
        bi_mod.callScene = lambda sid: called.append(sid)
        bi_mod.findGroup = lambda rid, rtype: group
        bi_mod.checkBehaviorInstances(sensor)
        assert scene_id in called

    def test_no_match_for_different_device_id(self):
        device_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())
        sensor = _make_sensor("ZLLSwitch", id_v2=device_id, buttonevent=1002)

        instance = _make_instance(other_id, {"button1": {}})
        cfg = _setup_stubs(bi={"i": instance})
        bi_mod = _import_bi(cfg)

        called = []
        bi_mod.callScene = lambda sid: called.append(sid)
        bi_mod.checkBehaviorInstances(sensor)
        assert called == []
