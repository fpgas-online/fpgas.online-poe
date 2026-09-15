"""Board identity and which ports count as occupied, against the virtual S3300.

The gsm7228ps seed is a transcription of the real sw2 capture: ports 44 and 48
deliver PoE, port 46 is in FAULT, everything else is SEARCHING.
"""

import os
import textwrap

import pytest
from netgear_switch.errors import ProtectedPortError
from netgear_switch.virtual.server import VirtualSwitch

from fleet_watchdog.switches import (
    community_for,
    make_board,
    occupied_boards,
    open_switch,
    protected_ports,
)
from switch_setup.cli import load_specs

DELIVERING = {44, 48}


@pytest.fixture()
def virtual_switch():
    vs = VirtualSwitch("gsm7228ps")
    vs.start()
    yield vs
    vs.stop()


@pytest.fixture()
def spec(virtual_switch, tmp_path):
    cfg = tmp_path / "switches.yml"
    cfg.write_text(textwrap.dedent(f"""
        switches:
          - index: 2
            model: s3300
            mgmt_host: {virtual_switch.host}:{virtual_switch.port}
            access_ports: 48
            gateway_trunk_port: 51
            downstream_trunk_ports: []
            house_uplink_port: 52
    """))
    return load_specs(str(cfg))[0]


def test_board_identity_follows_the_port_vlan_map_formulas():
    b = make_board(2, 42, "10.21")
    assert b.switch == 2
    assert b.port == 42
    assert b.ip == "10.21.2.42"
    assert b.hostname == "pi-sw2-p42"


def test_protected_ports_are_the_trunks_and_the_uplink(spec):
    assert protected_ports(spec) == frozenset({51, 52})


def test_protected_ports_include_every_downstream_trunk():
    class Spec:
        gateway_trunk_port = 47
        downstream_trunk_ports = (50, 51)
        house_uplink_port = 48

    assert protected_ports(Spec()) == frozenset({47, 48, 50, 51})


def test_community_prefers_the_per_switch_variable(monkeypatch):
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY", "shared")
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", "specific")
    assert community_for(2) == "specific"


def test_community_falls_back_to_the_shared_variable(monkeypatch):
    monkeypatch.delenv("FPGAS_SWITCH_COMMUNITY_2", raising=False)
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY", "shared")
    assert community_for(2) == "shared"


def test_a_missing_community_is_a_clear_error(monkeypatch):
    for k in list(os.environ):
        if k.startswith("FPGAS_SWITCH_COMMUNITY"):
            monkeypatch.delenv(k)
    with pytest.raises(KeyError, match="FPGAS_SWITCH_COMMUNITY_2"):
        community_for(2)


def test_only_delivering_ports_are_occupied(spec, virtual_switch):
    sw = open_switch(spec, virtual_switch.community)
    boards = occupied_boards(sw, spec, frozenset(), "10.21")
    assert {b.port for b in boards} == DELIVERING


def test_excluded_ports_are_dropped(spec, virtual_switch):
    sw = open_switch(spec, virtual_switch.community)
    boards = occupied_boards(sw, spec, frozenset({44}), "10.21")
    assert {b.port for b in boards} == {48}


def test_boards_carry_their_ip_and_hostname(spec, virtual_switch):
    sw = open_switch(spec, virtual_switch.community)
    boards = {b.port: b for b in occupied_boards(sw, spec, frozenset(), "10.21")}
    assert boards[48].ip == "10.21.2.48"
    assert boards[48].hostname == "pi-sw2-p48"


def test_the_switch_refuses_to_touch_a_protected_port(spec, virtual_switch):
    sw = open_switch(spec, virtual_switch.community)
    with pytest.raises(ProtectedPortError):
        sw.set_poe(spec.house_uplink_port, False)
