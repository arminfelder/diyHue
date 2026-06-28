"""
Tests for services.mqtt sensor deduplication logic:
  - getSensorsByIeeeAddr
  - bridge/devices handler: rename on ieee match, deduplicate when same ieee
    appears under two friendly names
"""
import sys
import types
import uuid
import json
from unittest.mock import MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _setup_stubs(sensors=None):
    log_mod = types.ModuleType("logManager")
    log_mod.logger = MagicMock()
    log_mod.logger.get_logger.return_value = MagicMock()
    sys.modules["logManager"] = log_mod

    cfg = {
        "lights": {},
        "groups": {},
        "scenes": {},
        "sensors": sensors if sensors is not None else {},
        "behavior_instance": {},
        "config": {
            "mqtt": {
                "enabled": True,
                "mqttServer": "localhost",
                "mqttPort": 1883,
                "mqttUser": "",
                "mqttPassword": "",
                "discoveryPrefix": "homeassistant",
                "mqttCaCerts": None,
                "mqttCertfile": None,
                "mqttKeyfile": None,
                "mqttTls": False,
                "mqttTlsInsecure": False,
            }
        },
        "apiUsers": {},
    }
    cfg_mod = types.ModuleType("configManager")
    bc = MagicMock()
    bc.yaml_config = cfg
    cfg_mod.bridgeConfig = bc
    sys.modules["configManager"] = cfg_mod
    return cfg


def _make_sensor(ieee, friendly_name, sensor_type="ZLLSwitch", id_v1=None):
    s = MagicMock()
    s.protocol = "mqtt"
    s.protocol_cfg = {"ieeeAddr": ieee, "friendly_name": friendly_name}
    s.type = sensor_type
    s.name = friendly_name
    s.id_v1 = id_v1 or str(uuid.uuid4())
    s.id_v2 = str(uuid.uuid4())
    s.modelid = "RDM002"
    return s


def _import_mqtt(cfg):
    # Remove cached module so it re-imports with current stubs
    for key in list(sys.modules.keys()):
        if "services.mqtt" in key or key == "services.mqtt":
            del sys.modules[key]
    sys.path.insert(0, "BridgeEmulator")
    import services.mqtt as mqtt_mod
    # point module's bridgeConfig at our cfg
    mqtt_mod.bridgeConfig = cfg
    return mqtt_mod


# ---------------------------------------------------------------------------
# getSensorsByIeeeAddr
# ---------------------------------------------------------------------------

class TestGetSensorsByIeeeAddr:
    def test_returns_matching_sensors(self):
        ieee = "0xABCDEF1234567890"
        s1 = _make_sensor(ieee, "sw_a", "ZLLSwitch", "1")
        s2 = _make_sensor(ieee, "sw_a", "ZLLRelativeRotary", "2")
        cfg = _setup_stubs({"1": s1, "2": s2})
        mqtt = _import_mqtt(cfg)
        result = mqtt.getSensorsByIeeeAddr(ieee)
        assert s1 in result
        assert s2 in result

    def test_ignores_different_ieee(self):
        s1 = _make_sensor("0xAAAA", "sw_a", "ZLLSwitch", "1")
        s2 = _make_sensor("0xBBBB", "sw_b", "ZLLSwitch", "2")
        cfg = _setup_stubs({"1": s1, "2": s2})
        mqtt = _import_mqtt(cfg)
        result = mqtt.getSensorsByIeeeAddr("0xAAAA")
        assert s1 in result
        assert s2 not in result

    def test_ignores_non_mqtt_sensors(self):
        s = _make_sensor("0xAAAA", "sw_a", "ZLLSwitch", "1")
        s.protocol = "none"
        cfg = _setup_stubs({"1": s})
        mqtt = _import_mqtt(cfg)
        result = mqtt.getSensorsByIeeeAddr("0xAAAA")
        assert result == []

    def test_returns_empty_when_no_match(self):
        cfg = _setup_stubs({})
        mqtt = _import_mqtt(cfg)
        assert mqtt.getSensorsByIeeeAddr("0xDEAD") == []


# ---------------------------------------------------------------------------
# bridge/devices handler — rename on ieee match
# ---------------------------------------------------------------------------

class TestBridgeDevicesRename:
    def _make_msg(self, ieee, friendly_name, model_id="RDM002"):
        msg = MagicMock()
        msg.topic = "zigbee2mqtt/bridge/devices"
        device = {
            "ieee_address": ieee,
            "friendly_name": friendly_name,
            "model_id": model_id,
            "definition": {"model": "8719514440937"},
        }
        msg.payload = json.dumps([device]).encode()
        return msg

    def test_renames_sensor_when_ieee_found_under_old_name(self):
        ieee = "0x001788010f0b6370"
        old_name = ieee  # registered before user renamed in Z2M
        s_switch = _make_sensor(ieee, old_name, "ZLLSwitch", "1")
        s_rotary = _make_sensor(ieee, old_name, "ZLLRelativeRotary", "2")
        s_rotary.parent_id_v2 = s_switch.id_v2
        cfg = _setup_stubs({"1": s_switch, "2": s_rotary})
        mqtt = _import_mqtt(cfg)

        msg = self._make_msg(ieee, "hue_bathroom_sw")
        mqtt.on_message(MagicMock(), None, msg)

        assert s_switch.name == "hue_bathroom_sw"
        assert s_switch.protocol_cfg["friendly_name"] == "hue_bathroom_sw"
        assert s_rotary.name == "hue_bathroom_sw"

    def test_does_not_create_new_sensors_when_ieee_found(self):
        ieee = "0x001788010f0b6370"
        s = _make_sensor(ieee, ieee, "ZLLSwitch", "1")
        cfg = _setup_stubs({"1": s})
        mqtt = _import_mqtt(cfg)

        before_count = len(cfg["sensors"])
        msg = self._make_msg(ieee, "hue_bathroom_sw")
        mqtt.on_message(MagicMock(), None, msg)

        assert len(cfg["sensors"]) == before_count


# ---------------------------------------------------------------------------
# bridge/devices handler — deduplication
# ---------------------------------------------------------------------------

class TestBridgeDevicesDedup:
    def _make_msg(self, ieee, friendly_name):
        msg = MagicMock()
        msg.topic = "zigbee2mqtt/bridge/devices"
        device = {
            "ieee_address": ieee,
            "friendly_name": friendly_name,
            "model_id": "RDM002",
            "definition": {"model": "8719514440937"},
        }
        msg.payload = json.dumps([device]).encode()
        return msg

    def test_removes_stale_duplicate_keeps_correct(self):
        ieee = "0x001788010f0b6370"
        stale = _make_sensor(ieee, ieee, "ZLLSwitch", "6")
        correct = _make_sensor(ieee, "hue_bathroom_sw", "ZLLSwitch", "8")
        cfg = _setup_stubs({"6": stale, "8": correct})
        mqtt = _import_mqtt(cfg)

        msg = self._make_msg(ieee, "hue_bathroom_sw")
        mqtt.on_message(MagicMock(), None, msg)

        assert "8" in cfg["sensors"]
        assert "6" not in cfg["sensors"]

    def test_one_sensor_per_type_after_dedup(self):
        ieee = "0x001788010f0b6370"
        stale_sw = _make_sensor(ieee, ieee, "ZLLSwitch", "6")
        stale_rot = _make_sensor(ieee, ieee, "ZLLRelativeRotary", "7")
        correct_sw = _make_sensor(ieee, "hue_bathroom_sw", "ZLLSwitch", "8")
        correct_rot = _make_sensor(ieee, "hue_bathroom_sw", "ZLLRelativeRotary", "9")
        cfg = _setup_stubs({"6": stale_sw, "7": stale_rot, "8": correct_sw, "9": correct_rot})
        mqtt = _import_mqtt(cfg)

        msg = self._make_msg(ieee, "hue_bathroom_sw")
        mqtt.on_message(MagicMock(), None, msg)

        remaining = list(cfg["sensors"].values())
        switch_count = sum(1 for s in remaining if s.type == "ZLLSwitch")
        rotary_count = sum(1 for s in remaining if s.type == "ZLLRelativeRotary")
        assert switch_count == 1
        assert rotary_count == 1
