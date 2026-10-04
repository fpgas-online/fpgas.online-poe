"""The /snmp/status and /snmp/toggle endpoints against the library's
in-process VirtualSwitch, configured the way the per-port-VLAN hosts are:
a switches file (the one infra renders to /etc/fpgas/switches.yml) plus a
per-switch SNMP write community in the environment.

Requires the net-snmp CLI tools (apt: snmp), like the switch_setup tests.
"""

import json
import os
import textwrap
import time
import types

import pytest
from django.core.cache import caches
from django.test import Client, override_settings
from netgear_switch.virtual.server import VirtualSwitch

from snmp_switch import switches
from tests import policy

PORT = 3  # PoE admin-enabled in the virtual S3300's seed
OTHER_PORT = 4  # an access port like PORT, but not one the site offers
# what write_switches() below says of every switch: not access ports at all
TRUNK_PORT = 51
UPLINK_PORT = 52


def post(path, body):
    return Client().post(path, data=json.dumps(body), content_type="application/json")


@pytest.fixture(autouse=True)
def no_switch_env(monkeypatch):
    """Start every test unconfigured: no legacy SNMP_SWITCH_* and no FPGAS_SWITCH*."""
    for k in list(os.environ):
        if k.startswith(("SNMP_SWITCH_", "FPGAS_SWITCH")):
            monkeypatch.delenv(k)


@pytest.fixture(autouse=True)
def site_offers_port(no_switch_env):
    """The site's policy offers PORT on switch 2 and nothing else, and no
    port has been power-cycled yet."""
    policy.OFFERED.clear()
    policy.OFFERED.add((2, PORT))
    policy.ASKED.clear()
    caches["poe-rate-limit"].clear()


@pytest.fixture()
def no_switch_traffic(monkeypatch):
    """Fail the test if anything is opened towards a switch, either scheme."""
    def refuse(*args, **kwargs):
        raise AssertionError("a refused request reached the switch client")
    monkeypatch.setattr(switches, "SyncSwitch", refuse)
    monkeypatch.setattr(switches, "snmp_get_state", refuse)
    monkeypatch.setattr(switches, "snmp_set_state", refuse)


@pytest.fixture()
def virtual_switch():
    vs = VirtualSwitch("gsm7228ps")  # the S3300, welland switch 2
    vs.start()
    yield vs
    vs.stop()


def write_switches(tmp_path, *switches):
    entries = "".join(textwrap.dedent(f"""
          - index: {index}
            model: {model}
            mgmt_host: {mgmt_host}
            access_ports: 48
            gateway_trunk_port: 51
            downstream_trunk_ports: []
            house_uplink_port: 52
        """) for index, model, mgmt_host in switches)
    cfg = tmp_path / "switches.yml"
    cfg.write_text("switches:" + entries)
    return cfg


@pytest.fixture()
def switch_2(virtual_switch, tmp_path, monkeypatch):
    cfg = write_switches(tmp_path, (2, "s3300", f"{virtual_switch.host}:{virtual_switch.port}"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", virtual_switch.community)
    return virtual_switch


@pytest.fixture()
def switch_2_untouchable(tmp_path, monkeypatch, no_switch_traffic):
    """Switch 2 configured as in switch_2, with no switch behind it: the
    request must be answered without one."""
    cfg = write_switches(tmp_path, (2, "s3300", "192.0.2.1:161"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", "not-used")


@pytest.fixture()
def legacy_switch(monkeypatch):
    """The legacy flat scheme (one SNMPv3 switch, no index), with the SNMP
    calls recorded instead of sent. The site offers its port 9."""
    calls = []

    async def get_state(**params):
        calls.append(("get", params["port"]))
        return {"state": "on"}

    async def set_state(state, **params):
        calls.append(("set", params["port"], state))
        return {"state": {"1": "on", "2": "off"}[state]}

    monkeypatch.setenv("SNMP_SWITCH_HOST", "192.0.2.2")
    monkeypatch.setattr(switches, "mk_params", dict)
    monkeypatch.setattr(switches, "snmp_get_state", get_state)
    monkeypatch.setattr(switches, "snmp_set_state", set_state)
    # the view's pause between off and on, without stopping the tests' clock
    monkeypatch.setattr("snmp_switch.views.time", types.SimpleNamespace(sleep=lambda seconds: None))
    policy.OFFERED.clear()
    policy.OFFERED.add((None, 9))
    return calls


def test_status_unconfigured_is_a_503_with_a_reason():
    r = post("/status", {"port": "42"})
    assert r.status_code == 503
    assert "not configured" in r.json()["error"]


def test_status_reports_the_port_poe_state(switch_2):
    r = post("/status", {"port": str(PORT), "switch": 2})
    assert r.status_code == 200
    assert r.json() == {"state": "on"}


def test_toggle_power_cycles_the_port(switch_2):
    r = post("/toggle", {"port": str(PORT), "switch": 2})
    assert r.status_code == 200
    assert r.json() == {str(PORT): ["off", "on"]}
    assert switch_2.state.poe[PORT].admin is True


def test_the_only_configured_switch_is_implied(switch_2):
    r = post("/status", {"port": str(PORT)})
    assert r.status_code == 200
    assert r.json() == {"state": "on"}


def test_switch_is_required_when_several_are_configured(virtual_switch, tmp_path, monkeypatch):
    host = f"{virtual_switch.host}:{virtual_switch.port}"
    cfg = write_switches(tmp_path, (1, "gsm7252ps", host), (2, "s3300", host))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY", virtual_switch.community)
    r = post("/status", {"port": str(PORT)})
    assert r.status_code == 400
    assert "switch" in r.json()["error"]


def test_unknown_switch_is_a_400(switch_2):
    r = post("/status", {"port": str(PORT), "switch": 7})
    assert r.status_code == 400
    assert "switch 7" in r.json()["error"]


# --- only a port the site offers as a board ------------------------------


@pytest.mark.parametrize("path", ["/status", "/toggle"])
@pytest.mark.parametrize("port", [TRUNK_PORT, UPLINK_PORT, OTHER_PORT, 48, 999])
def test_a_port_the_site_does_not_offer_is_refused_and_the_switch_never_asked(switch_2_untouchable, path, port):
    r = post(path, {"port": str(port), "switch": 2})
    assert r.status_code == 403
    assert r.json() == {"error": f"switch 2 port {port} is not a board this site offers; "
                                 "nothing was sent to the switch"}
    assert policy.ASKED == [(2, port)]


def test_the_policy_is_asked_about_the_switch_the_request_implies(switch_2):
    """One switch configured and none named: the policy still hears which."""
    assert post("/status", {"port": str(PORT)}).status_code == 200
    assert policy.ASKED == [(2, PORT)]


@pytest.mark.parametrize("path", ["/status", "/toggle"])
@pytest.mark.parametrize("unset", [None, ""])
def test_a_site_with_no_port_policy_refuses_everything(switch_2_untouchable, path, unset):
    """Fail closed: this package on a project that has not said which ports
    are boards must not be an open endpoint."""
    with override_settings(SNMP_SWITCH_PORT_POLICY=unset):
        r = post(path, {"port": str(PORT), "switch": 2})
    assert r.status_code == 503
    assert "SNMP_SWITCH_PORT_POLICY is not set" in r.json()["error"]


def test_a_port_policy_that_cannot_be_imported_refuses_everything(switch_2_untouchable):
    with override_settings(SNMP_SWITCH_PORT_POLICY="tests.policy.no_such_function"):
        r = post("/toggle", {"port": str(PORT), "switch": 2})
    assert r.status_code == 503
    assert "SNMP_SWITCH_PORT_POLICY" in r.json()["error"]


# --- the legacy flat scheme: one switch, no index -------------------------


def test_legacy_scheme_toggles_a_port_the_site_offers(legacy_switch):
    r = post("/toggle", {"port": "9"})
    assert r.status_code == 200
    assert r.json() == {"9": ["off", "on"]}
    assert legacy_switch == [("set", "9", "2"), ("set", "9", "1")]
    assert policy.ASKED == [(None, 9)]


def test_legacy_scheme_reports_a_port_the_site_offers(legacy_switch):
    r = post("/status", {"port": "9"})
    assert (r.status_code, r.json()) == (200, {"state": "on"})
    assert legacy_switch == [("get", "9")]


@pytest.mark.parametrize("path", ["/status", "/toggle"])
def test_legacy_scheme_refuses_a_port_the_site_does_not_offer(legacy_switch, path):
    r = post(path, {"port": "10"})
    assert r.status_code == 403
    assert "port 10 is not a board this site offers" in r.json()["error"]
    assert legacy_switch == []


def test_legacy_scheme_has_no_switch_to_name(legacy_switch):
    r = post("/toggle", {"port": "9", "switch": 1})
    assert r.status_code == 400
    assert "'switch' is not accepted" in r.json()["error"]
    assert legacy_switch == []


# --- one power cycle per port per interval --------------------------------


def test_a_second_toggle_inside_the_interval_is_a_429_and_the_switch_is_left_alone(switch_2, monkeypatch):
    sets = []
    real_set = switches.LibraryPort.set
    monkeypatch.setattr(switches.LibraryPort, "set", lambda self, on: sets.append(on) or real_set(self, on))
    assert post("/toggle", {"port": str(PORT), "switch": 2}).status_code == 200
    assert sets == [False, True]
    r = post("/toggle", {"port": str(PORT), "switch": 2})
    assert r.status_code == 429
    assert 1 <= int(r["Retry-After"]) <= 60
    assert f"switch 2 port {PORT} was power-cycled a moment ago; try again in {r['Retry-After']} s" in r.json()["error"]
    assert sets == [False, True]
    assert switch_2.state.poe[PORT].admin is True


def test_the_interval_is_per_port(legacy_switch):
    policy.OFFERED.add((None, 8))
    assert post("/toggle", {"port": "9"}).status_code == 200
    assert post("/toggle", {"port": "8"}).status_code == 200
    assert post("/toggle", {"port": "9"}).status_code == 429


def test_a_port_can_be_cycled_again_after_the_interval(legacy_switch):
    with override_settings(SNMP_SWITCH_TOGGLE_INTERVAL=1):
        assert post("/toggle", {"port": "9"}).status_code == 200
        assert post("/toggle", {"port": "9"}).status_code == 429
        time.sleep(1.1)
        assert post("/toggle", {"port": "9"}).status_code == 200


def test_status_is_not_rate_limited(legacy_switch):
    assert post("/toggle", {"port": "9"}).status_code == 200
    assert post("/status", {"port": "9"}).status_code == 200
    assert post("/status", {"port": "9"}).status_code == 200


def test_a_refused_port_takes_no_ones_turn(legacy_switch):
    """The policy comes first: asking for a port that is not offered neither
    reaches the limit store nor delays the offered port."""
    assert post("/toggle", {"port": "10"}).status_code == 403
    assert post("/toggle", {"port": "10"}).status_code == 403
    assert post("/toggle", {"port": "9"}).status_code == 200


@pytest.mark.parametrize("broken, reason", [
    ({"SNMP_SWITCH_RATE_LIMIT_CACHE": None}, "SNMP_SWITCH_RATE_LIMIT_CACHE is not set"),
    ({"SNMP_SWITCH_RATE_LIMIT_CACHE": "no-such-cache"}, "rate limit store is not answering"),
    ({"SNMP_SWITCH_TOGGLE_INTERVAL": 0}, "SNMP_SWITCH_TOGGLE_INTERVAL must be"),
    ({"SNMP_SWITCH_TOGGLE_INTERVAL": None}, "SNMP_SWITCH_TOGGLE_INTERVAL must be"),
])
def test_toggle_without_a_working_rate_limit_is_refused(legacy_switch, broken, reason):
    """Fail closed: no limit store, or one that cannot be asked, is not
    permission to power-cycle without a limit."""
    with override_settings(**broken):
        r = post("/toggle", {"port": "9"})
    assert r.status_code == 503
    assert reason in r.json()["error"]
    assert legacy_switch == []


# --- the legacy bulk routes are gone --------------------------------------


@pytest.mark.parametrize("path", ["/toggle_all", "/off_all"])
def test_the_bulk_routes_are_gone(legacy_switch, path):
    assert post(path, {}).status_code == 404
    assert Client().get(path).status_code == 404
    assert legacy_switch == []


# --- malformed requests ---------------------------------------------------

HUGE = "9" * 5000  # longer than int() will convert


@pytest.mark.parametrize("path", ["/status", "/toggle"])
@pytest.mark.parametrize("body", [
    {}, {"switch": 2},  # no port
    {"port": None, "switch": 2}, {"port": "", "switch": 2}, {"port": "three", "switch": 2},
    {"port": -3, "switch": 2}, {"port": "-3", "switch": 2}, {"port": 0, "switch": 2},
    {"port": 1000, "switch": 2}, {"port": 10 ** 30, "switch": 2}, {"port": HUGE, "switch": 2},
    {"port": 3.0, "switch": 2}, {"port": True, "switch": 2}, {"port": [3], "switch": 2},
    # one spelling per port: nothing that only looks like a board's port
    {"port": "03", "switch": 2}, {"port": " 3", "switch": 2}, {"port": "3\n", "switch": 2},
    {"port": "\uff13", "switch": 2},
    # the switch
    {"port": "3", "switch": 7}, {"port": "3", "switch": "two"}, {"port": "3", "switch": -2},
    {"port": "3", "switch": HUGE}, {"port": "3", "switch": 2.0}, {"port": "3", "switch": [2]},
    # not an object at all
    [3], "3", 3, None,
])
def test_a_malformed_request_is_a_clean_400(switch_2_untouchable, path, body):
    r = post(path, body)
    assert r.status_code == 400
    assert r.json()["error"]


@pytest.mark.parametrize("path", ["/status", "/toggle"])
def test_a_body_that_is_not_json_is_a_clean_400(switch_2_untouchable, path):
    r = Client().post(path, data="port=3&switch=2", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 400
    assert "expected a JSON body" in r.json()["error"]


@pytest.mark.parametrize("path", ["/status", "/toggle"])
def test_only_post_is_answered(switch_2_untouchable, path):
    assert Client().get(path).status_code == 405
