"""
Regression tests for RDM002 tap-dial event handling in services.mqtt.on_message,
driven by the exact Zigbee2MQTT payloads a real device emits.

Pins three bugs found from live logs:
  1. dial_rotate_* raised KeyError 'rotaryevent' (events resolved to the
     ZLLSwitch, whose state has no rotaryevent) -> now routed to the
     ZLLRelativeRotary sibling.
  2. battery/OTA/brightness_step messages re-fired the last button's scene
     (stale buttonevent) -> behavior now runs only on a real input event.
  3. button events still dispatch normally.
"""
import sys
import json
import types
import uuid
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, "BridgeEmulator")

NAME = "hue_livingroom_sw_0"
IEEE = "0x001788010f0b6370"


def _stub_config(sensors):
    log_mod = types.ModuleType("logManager")
    log_mod.logger = MagicMock()
    log_mod.logger.get_logger.return_value = MagicMock()
    sys.modules["logManager"] = log_mod

    cfg = {"lights": {}, "groups": {}, "scenes": {}, "sensors": sensors,
           "behavior_instance": {}, "rules": {}, "config": {"mqtt": {"enabled": True}},
           "apiUsers": {}}
    cfg_mod = types.ModuleType("configManager")
    bc = MagicMock()
    bc.yaml_config = cfg
    cfg_mod.bridgeConfig = bc
    sys.modules["configManager"] = cfg_mod
    return cfg


def _fresh_mqtt(cfg):
    for k in list(sys.modules.keys()):
        if k.startswith("HueObjects") or k.startswith("functions.") or k == "services.mqtt":
            del sys.modules[k]
    import services.mqtt as mqtt
    mqtt.bridgeConfig = cfg
    mqtt.devices_ids = {}                      # clear the friendly_name cache
    mqtt.rulesProcessor = MagicMock()          # isolate from the rules engine
    return mqtt


def _make_pair(cfg_sensors):
    from HueObjects.Sensor import Sensor
    sw_id, rot_id = str(uuid.uuid4()), str(uuid.uuid4())
    common = {"modelid": "RDM002", "protocol": "mqtt",
              "protocol_cfg": {"friendly_name": NAME, "ieeeAddr": IEEE}}
    sw = Sensor({**common, "name": NAME, "id_v1": "10", "id_v2": sw_id, "type": "ZLLSwitch"})
    rot = Sensor({**common, "name": NAME, "id_v1": "11", "id_v2": rot_id,
                  "type": "ZLLRelativeRotary", "parent_id_v2": sw_id})
    # switch inserted first, so getObject() resolves the switch (reproduces the bug)
    cfg_sensors["10"] = sw
    cfg_sensors["11"] = rot
    return sw, rot


def _msg(payload: dict):
    m = MagicMock()
    m.topic = "zigbee2mqtt/" + NAME
    m.payload = json.dumps(payload).encode()
    return m


def _setup():
    sensors = {}
    cfg = _stub_config(sensors)
    mqtt = _fresh_mqtt(cfg)
    sw, rot = _make_pair(sensors)
    calls = []
    mqtt.checkBehaviorInstances = lambda dev: calls.append(dev)
    return mqtt, sw, rot, calls


# Real payloads copied verbatim from the device logs.
DIAL_LEFT_FAST = {"action": "dial_rotate_left_fast", "action_direction": "left",
                  "action_step_size": None, "action_time": 166, "action_type": "rotate",
                  "battery": 100, "brightness": None, "linkquality": 216}
BRIGHTNESS_STEP_UP = {"action": "brightness_step_up", "action_direction": None,
                      "action_step_size": 87, "action_time": None,
                      "action_transition_time": 0.05, "action_type": None,
                      "battery": 100, "brightness": None, "linkquality": 216}
BUTTON_1_RELEASE = {"action": "button_1_press_release", "battery": 100, "linkquality": 216}
BATTERY_ONLY = {"battery": 95, "linkquality": 216}   # no "action" key


class TestRotaryRouting:
    def test_dial_rotate_is_dropped(self):
        """dial_rotate_* is redundant telemetry -> dropped, fires no automation."""
        mqtt, sw, rot, calls = _setup()
        mqtt.on_message(MagicMock(), None, _msg(DIAL_LEFT_FAST))
        assert calls == []
        # neither sensor crashed on a missing rotaryevent key
        assert "rotaryevent" not in sw.state

    def test_brightness_step_routes_to_rotary_with_device_step(self):
        mqtt, sw, rot, calls = _setup()
        sw.state["buttonevent"] = 1002          # a stale button event from earlier
        mqtt.on_message(MagicMock(), None, _msg(BRIGHTNESS_STEP_UP))
        # dims via the rotary sensor (not the switch's stale button scene)
        assert calls == [rot]
        assert rot.state["direction"] == "clock_wise"          # "up" -> clock_wise
        assert rot.state["rotary_step_size"] == 87             # = action_step_size
        assert rot.state["rotary_transition"] == 5             # 0.05 s -> 5 cs

    def test_non_input_message_does_not_fire_automations(self):
        mqtt, sw, rot, calls = _setup()
        sw.state["buttonevent"] = 1002          # stale
        mqtt.on_message(MagicMock(), None, _msg(BATTERY_ONLY))
        assert calls == []                       # no buttonevent/rotaryevent -> gated

    def test_button_event_still_dispatches(self):
        mqtt, sw, rot, calls = _setup()
        mqtt.on_message(MagicMock(), None, _msg(BUTTON_1_RELEASE))
        assert calls == [sw]
        assert sw.state["buttonevent"] == 1002
