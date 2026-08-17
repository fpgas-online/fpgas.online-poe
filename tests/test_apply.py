"""Integration tests against the library's in-process virtual switch.

Requires the net-snmp CLI tools (apt: snmp) for NetsnmpCliClient.
"""

import pytest
from netgear_switch import SyncSwitch, get_model
from netgear_switch.transport.sync.snmp_netsnmp_cli import NetsnmpCliClient
from netgear_switch.virtual.server import VirtualSwitch

from switch_setup.apply import converge
from switch_setup.plan import SwitchSpec

SPECS = [SwitchSpec(index=2, model="gsm7252ps", mgmt_host="unused",
                    access_ports=8, gateway_trunk_port=49,
                    downstream_trunk_ports=(50,), house_uplink_port=52)]


@pytest.fixture()
def mock_switch():
    vs = VirtualSwitch("gsm7252ps")
    vs.start()
    client = NetsnmpCliClient(f"{vs.host}:{vs.port}", vs.community)
    sw = SyncSwitch(get_model("gsm7252ps"), vs.host,
                    snmp_community=vs.community,
                    snmp_client=client, snmp_write_client=client)
    yield sw
    vs.stop()


def test_converge_then_idempotent(mock_switch):
    actions = converge(mock_switch, SPECS, 2, apply=True)
    assert actions  # first run had work to do
    vlans = {v.vlan_id: v for v in mock_switch.get_vlans()}
    assert vlans[2203].untagged_ports == frozenset({3})
    assert 49 in vlans[2203].tagged_ports
    assert dict(mock_switch.get_pvids())[3] == 2203
    assert 3 not in vlans[1].member_ports
    # second run: nothing left to do
    assert converge(mock_switch, SPECS, 2, apply=True) == []


def test_check_mode_writes_nothing(mock_switch):
    before = mock_switch.get_vlans()
    actions = converge(mock_switch, SPECS, 2, apply=False)
    assert actions
    assert mock_switch.get_vlans() == before
