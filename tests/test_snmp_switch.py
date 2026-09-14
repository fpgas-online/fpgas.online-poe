"""The /snmp/status and /snmp/toggle endpoints against the library's
in-process VirtualSwitch, configured the way the per-port-VLAN hosts are:
a switches file (the one infra renders to /etc/fpgas/switches.yml) plus a
per-switch SNMP write community in the environment.

Requires the net-snmp CLI tools (apt: snmp), like the switch_setup tests.
"""

import json
import os
import textwrap

import pytest
from django.test import Client
from netgear_switch.virtual.server import VirtualSwitch

PORT = 3  # PoE admin-enabled in the virtual S3300's seed


def post(path, body):
    return Client().post(path, data=json.dumps(body), content_type="application/json")


@pytest.fixture(autouse=True)
def no_switch_env(monkeypatch):
    """Start every test unconfigured: no legacy SNMP_SWITCH_* and no FPGAS_SWITCH*."""
    for k in list(os.environ):
        if k.startswith(("SNMP_SWITCH_", "FPGAS_SWITCH")):
            monkeypatch.delenv(k)


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
