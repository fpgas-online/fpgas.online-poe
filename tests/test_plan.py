from switch_setup.plan import (
    OWN_RANGE, DesiredState, SwitchSpec, desired_state, hostname, vlan_id,
)

SPECS = [
    SwitchSpec(index=1, model="s3300", mgmt_host="10.1.5.11",
               access_ports=48, gateway_trunk_port=49,
               downstream_trunk_ports=(50,), house_uplink_port=52),
    SwitchSpec(index=2, model="gsm7252ps", mgmt_host="10.1.5.23",
               access_ports=48, gateway_trunk_port=49,
               downstream_trunk_ports=(50,), house_uplink_port=52),
]


def test_formulas():
    assert vlan_id(1, 7) == 2107
    assert vlan_id(3, 48) == 2348
    assert hostname(2, 7) == "pi-sw2-p7"
    assert OWN_RANGE == range(2101, 2349)


def test_desired_state_switch1():
    d = desired_state(SPECS, 1)
    assert d.vlans[2107] == "pi-sw1-p7"
    assert set(d.vlans) == set(range(2101, 2149))
    assert d.untagged[7] == 2107 and d.pvids[7] == 2107
    # gateway trunk carries own block AND switch 2's block
    assert d.tagged[49] == frozenset(range(2101, 2149)) | frozenset(range(2201, 2249))
    # downstream trunk carries only blocks BEHIND it
    assert d.tagged[50] == frozenset(range(2201, 2249))
    assert d.vlan1_excluded == frozenset(range(1, 49))
    # house uplink is never in any map
    assert 52 not in d.untagged and 52 not in d.tagged and 52 not in d.pvids


def test_desired_state_switch2_trunks():
    d = desired_state(SPECS, 2)
    assert d.tagged[49] == frozenset(range(2201, 2249))
    assert d.tagged[50] == frozenset()  # no switch 3 configured
