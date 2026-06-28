"""
Shared fixtures for the diyHue test suite.

Two fixture tiers:

1. Unit fixtures (patch_globals) — zero Flask, fast, used by unit tests.
2. API fixtures (app_client) — full Flask test client wired to an in-memory
   bridgeConfig, used by API contract tests.
"""
import sys
import types
from unittest.mock import MagicMock
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_stub():
    mod = types.ModuleType("logManager")
    mod.logger = MagicMock()
    mod.logger.get_logger.return_value = MagicMock()
    return mod


def _make_api_user(key="testkey"):
    user = MagicMock()
    user.username = key
    user.last_use_date = "2026-01-01T00:00:00"
    return user


def _minimal_bridge_config(api_key="testkey"):
    return {
        "apiUsers": {api_key: _make_api_user(api_key)},
        "lights": {},
        "groups": {},
        "scenes": {},
        "sensors": {},
        "behavior_instance": {},
        "geofence_clients": {},
        "smart_scene": {},
        "rules": {},
        "resourcelinks": {},
        "schedules": {},
        "config": {
            "bridgeid": "AABBCCDDEEFF0011",
            "name": "diyHue",
            "timezone": "Europe/Berlin",
            "apiversion": "1.67.0",
            "swversion": "1967054020",
            "ipaddress": "127.0.0.1",
            "netmask": "255.255.255.0",
            "gateway": "127.0.0.1",
            "mac": "AA:BB:CC:DD:EE:FF",
            "linkbutton": {"lastlinkbuttonpushed": 0},
            "zigbee_device_discovery_info": {"status": "ready"},
            "Remote API enabled": False,
            "discovery": True,
            "alarm": {"enabled": False, "lasttriggered": 0, "email": ""},
            "users": {},
            "mqtt": {
                "enabled": False,
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
            },
        },
        "temp": {
            "eventstream": [],
            "scanResult": {"lastscan": "none"},
            "detectedLights": [],
            "gradientStripLights": {},
        },
    }


# Prefixes of modules that hold module-level `bridgeConfig` references and
# must be evicted + re-imported when the fixture dict changes.
_EMULATOR_PREFIXES = (
    "HueObjects", "functions.", "services.", "flaskUI",
    "sensors.", "lights.", "configManager", "logManager", "HueEmulator3",
)


def _evict_emulator_modules():
    for key in list(sys.modules.keys()):
        if any(key == p.rstrip(".") or key.startswith(p) for p in _EMULATOR_PREFIXES):
            del sys.modules[key]


def _inject_stubs(bridge_cfg):
    log_mod = _log_stub()
    cfg_mod = types.ModuleType("configManager")
    bc = MagicMock()
    bc.yaml_config = bridge_cfg
    bc.save_config = MagicMock()
    cfg_mod.bridgeConfig = bc
    sys.modules["logManager"] = log_mod
    sys.modules["configManager"] = cfg_mod
    return bc


# ---------------------------------------------------------------------------
# Unit-test global patch (autouse — every test)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_globals(monkeypatch):
    """Inject minimal stub modules so unit tests never hit disk/network."""
    log_mod = _log_stub()
    monkeypatch.setitem(sys.modules, "logManager", log_mod)

    cfg_mod = types.ModuleType("configManager")
    bc = MagicMock()
    bc.yaml_config = _minimal_bridge_config()
    cfg_mod.bridgeConfig = bc
    monkeypatch.setitem(sys.modules, "configManager", cfg_mod)

    for key in list(sys.modules.keys()):
        if any(key.startswith(p) for p in (
            "HueObjects", "functions.", "services.mqtt", "flaskUI",
            "configManager", "sensors.", "lights.",
        )):
            monkeypatch.delitem(sys.modules, key, raising=False)


# ---------------------------------------------------------------------------
# Flask app fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_key():
    return "testkey"


@pytest.fixture()
def bridge_cfg(api_key):
    """Fresh in-memory bridgeConfig for each API test."""
    return _minimal_bridge_config(api_key)


@pytest.fixture()
def app_client(bridge_cfg):
    """
    Flask test client wired to bridge_cfg.

    Fully resets module state on every call so tests are isolated regardless
    of execution order.

    Usage:
        def test_something(app_client, api_key, bridge_cfg):
            resp = app_client.get("/clip/v2/resource/bridge",
                                  headers={"hue-application-key": api_key})
    """
    _evict_emulator_modules()
    _inject_stubs(bridge_cfg)

    sys.path.insert(0, "BridgeEmulator")
    import HueEmulator3
    HueEmulator3.bridgeConfig = bridge_cfg

    import flaskUI.v2restapi as v2api
    v2api.bridgeConfig = bridge_cfg
    import flaskUI.restful as v1api
    v1api.bridgeConfig = bridge_cfg

    app = HueEmulator3.app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

    # Restore clean stubs after test so unit tests that run after are unaffected.
    _evict_emulator_modules()
    _inject_stubs(_minimal_bridge_config())


# ---------------------------------------------------------------------------
# Compliance scorecard summary (printed after every run that exercised it)
# ---------------------------------------------------------------------------

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    mod = sys.modules.get("test_spec_compliance")
    if mod is None or not getattr(mod, "RESULTS", None):
        return
    report = mod.format_scorecard()
    if report:
        terminalreporter.write_line("")
        terminalreporter.write_line(report)
