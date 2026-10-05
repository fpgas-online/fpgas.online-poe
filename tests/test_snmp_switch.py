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
from netgear_switch.errors import NetgearSwitchError
from netgear_switch.virtual.server import VirtualSwitch

from snmp_switch import switches
from switch_setup.plan import SwitchSpec
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
@pytest.mark.parametrize("port", [OTHER_PORT, 1, 48])
def test_a_port_the_site_does_not_offer_is_refused_and_the_switch_never_asked(switch_2_untouchable, path, port):
    r = post(path, {"port": str(port), "switch": 2})
    assert r.status_code == 403
    assert r.json() == {"error": f"switch 2 port {port} is not a board this site offers; "
                                 "nothing was sent to the switch"}
    assert policy.ASKED == [(2, port)]


# --- never a trunk, an uplink or a port outside the access ports ----------
#
# The site's policy answers from what boards registered, and a registration
# can be wrong or forged. Whatever the policy would say, a port the switches
# file does not make an access port is refused before the policy is asked.

# A head switch as a site describes one: 40 access ports, with the ports above
# them given other jobs, and (to show the range alone is not the test) a
# downstream trunk that lies inside the access range.
HEAD = SwitchSpec(index=1, model="gsm7252ps", mgmt_host="192.0.2.1", access_ports=40,
                  gateway_trunk_port=47, downstream_trunk_ports=(50, 12), house_uplink_port=48)


@pytest.mark.parametrize("port, board", [
    (1, True), (11, True), (13, True), (40, True),
    (12, False),  # a downstream trunk inside the access range
    (0, False), (-1, False), (41, False), (46, False),  # outside the access ports
    (47, False), (48, False), (50, False),  # gateway trunk, uplink, downstream trunk
    (52, False), (60, False), (999, False),
])
def test_only_an_access_port_with_no_other_job_can_be_a_boards(port, board):
    assert switches.is_access_port(HEAD, port) is board


@pytest.fixture()
def head_switch_untouchable(tmp_path, monkeypatch, no_switch_traffic):
    """HEAD configured as switch 1, with no switch behind it, and a site
    policy that says yes to every port (as after forged registrations)."""
    cfg = tmp_path / "switches.yml"
    cfg.write_text(textwrap.dedent("""
        switches:
          - index: 1
            model: gsm7252ps
            mgmt_host: 192.0.2.1
            access_ports: 40
            gateway_trunk_port: 47
            downstream_trunk_ports: [50, 12]
            house_uplink_port: 48
        """))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_1", "not-used")
    monkeypatch.setattr(policy, "OFFERED", type("Everything", (), {"__contains__": lambda self, item: True})())


@pytest.mark.parametrize("path", ["/status", "/toggle"])
@pytest.mark.parametrize("port", [12, 41, 46, 47, 48, 50, 52, 60, 999])
def test_a_trunk_uplink_or_out_of_range_port_is_refused_even_if_the_site_would_offer_it(
        head_switch_untouchable, path, port):
    r = post(path, {"port": port, "switch": 1})
    assert r.status_code == 403
    assert r.json() == {"error": f"switch 1 port {port} is not a board this site offers; "
                                 "nothing was sent to the switch"}
    assert policy.ASKED == []  # refused on the switch's own description, before the site is asked
    # and it took no one's turn at the rate limit
    assert caches["poe-rate-limit"]._cache == {}


@pytest.mark.parametrize("port", [TRUNK_PORT, UPLINK_PORT])
def test_the_trunk_and_uplink_of_the_downstream_switch_are_refused_too(switch_2_untouchable, port):
    policy.OFFERED.add((2, port))
    assert post("/toggle", {"port": port, "switch": 2}).status_code == 403
    assert policy.ASKED == []


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
    assert (f"switch 2 port {PORT} was power-cycled a moment ago; try again in {r['Retry-After']} seconds"
            in r.json()["error"])
    assert sets == [False, True]
    assert switch_2.state.poe[PORT].admin is True


def test_retry_after_is_never_longer_than_the_interval(legacy_switch):
    """Whatever the store holds (an interval since shortened, a clock that
    moved), nobody is told to wait longer than the interval."""
    caches["poe-rate-limit"].add("snmp_switch:toggle:legacy:9", time.time() + 86400, timeout=60)
    r = post("/toggle", {"port": "9"})
    assert r.status_code == 429
    assert r["Retry-After"] == "60"
    assert legacy_switch == []


# --- a port this view switched off is not left off ------------------------


@pytest.fixture()
def flaky_on(legacy_switch, monkeypatch):
    """The legacy switch, whose next `failures["on"]` answers to "on" (and
    `failures["off"]` to "off") go missing after the switch acted on them."""
    failures = {"on": 0, "off": 0}
    real = switches.snmp_set_state

    async def set_state(state, **params):
        answer = await real(state, **params)
        step = {"1": "on", "2": "off"}[state]
        if failures[step]:
            failures[step] -= 1
            raise TimeoutError("no answer from the switch")
        return answer

    monkeypatch.setattr(switches, "snmp_set_state", set_state)
    return failures


def test_on_is_tried_again_when_its_first_answer_goes_missing(flaky_on, legacy_switch):
    flaky_on["on"] = 1
    r = post("/toggle", {"port": "9"})
    assert (r.status_code, r.json()) == (200, {"9": ["off", "on"]})
    assert legacy_switch == [("set", "9", "2"), ("set", "9", "1"), ("set", "9", "1")]
    # it was a power cycle: the limit holds
    assert post("/toggle", {"port": "9"}).status_code == 429


def test_a_port_that_may_be_left_off_can_be_reset_again_at_once(flaky_on, legacy_switch):
    flaky_on["on"] = 3  # every try
    r = post("/toggle", {"port": "9"})
    assert r.status_code == 502
    assert "The port may be off: press Reset again" in r.json()["error"]
    assert legacy_switch == [("set", "9", "2")] + [("set", "9", "1")] * 3
    # the claim was given back: the next Reset is not told to wait, and works
    legacy_switch.clear()
    r = post("/toggle", {"port": "9"})
    assert (r.status_code, r.json()) == (200, {"9": ["off", "on"]})
    assert legacy_switch == [("set", "9", "2"), ("set", "9", "1")]


def test_a_port_the_switch_still_reports_off_counts_as_left_off(legacy_switch, monkeypatch):
    async def stays_off(state, **params):
        legacy_switch.append(("set", params["port"], state))
        return {"state": "off"}

    monkeypatch.setattr(switches, "snmp_set_state", stays_off)
    r = post("/toggle", {"port": "9"})
    assert r.status_code == 502
    assert "The port may be off" in r.json()["error"]
    assert caches["poe-rate-limit"]._cache == {}


def test_when_off_fails_on_is_still_sent_and_reset_can_be_pressed_again(flaky_on, legacy_switch):
    """The answer to "off" went missing: whether the port went off is not
    known, so "on" is sent all the same, and no power cycle is counted."""
    flaky_on["off"] = 1
    r = post("/toggle", {"port": "9"})
    assert r.status_code == 502
    assert "could not be switched off" in r.json()["error"] and "the port is on" in r.json()["error"]
    assert legacy_switch == [("set", "9", "2"), ("set", "9", "1")]
    assert post("/toggle", {"port": "9"}).status_code == 200


def test_the_per_port_vlan_switch_is_switched_back_on_too(switch_2, monkeypatch):
    """The same, through netgear_switch: the first "on" never reaches the
    virtual switch; the second gets through."""
    real_set_poe = switches.SyncSwitch.set_poe
    failed = []

    def set_poe(self, port, on):
        if on and not failed:
            failed.append(port)
            raise NetgearSwitchError("timeout")
        return real_set_poe(self, port, on)

    monkeypatch.setattr(switches.SyncSwitch, "set_poe", set_poe)
    monkeypatch.setattr("snmp_switch.views.time", types.SimpleNamespace(sleep=lambda seconds: None))
    r = post("/toggle", {"port": str(PORT), "switch": 2})
    assert (r.status_code, r.json()) == (200, {str(PORT): ["off", "on"]})
    assert failed == [PORT]
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
@pytest.mark.parametrize("opener", ["[", '{"port":'])
def test_a_body_nested_too_deeply_to_read_is_a_clean_400(switch_2_untouchable, path, opener):
    r = Client().post(path, data=opener * 100000, content_type="application/json")
    assert r.status_code == 400
    assert "expected a JSON body" in r.json()["error"]


@pytest.mark.parametrize("path", ["/status", "/toggle"])
def test_only_post_is_answered(switch_2_untouchable, path):
    assert Client().get(path).status_code == 405
