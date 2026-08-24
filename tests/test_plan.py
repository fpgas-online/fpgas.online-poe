from netgear_switch.models import VLANInfo, VlanMode

from switch_setup.plan import (
    OWN_RANGE,
    SwitchSpec,
    desired_state,
    diff,
    hostname,
    vlan_id,
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
    # own block (2101-2148) PLUS switch 2's block (2201-2248) as transit VLANs
    assert set(d.vlans) == set(range(2101, 2149)) | set(range(2201, 2249))
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


def test_transit_vlans_are_created_before_being_tagged():
    # A switch with another switch "behind" it must CREATE the downstream
    # block as transit VLANs before tagging them on its trunk/downlink ports.
    # Without this, set_vlan_membership fails "VLAN 2201 does not exist" on the
    # first behind VLAN -- regression against the live gsm7252ps + s3300 chain.
    d = desired_state(SPECS, 1)
    for vid in range(2201, 2249):
        assert d.vlans[vid] == f"transit-{vid}"  # named as transit, not a Pi
    assert d.vlans[2107] == "pi-sw1-p7"           # own block keeps Pi names

    current = [vinfo(1, "default", member=range(1, 53), untagged=range(1, 53))]
    actions = diff(current, {p: 1 for p in range(1, 53)}, d)
    # the transit VLAN is created, and created BEFORE it is tagged on a trunk
    assert ("create_vlan", 2201, "transit-2201") in actions
    assert ("membership", 2201, 49, VlanMode.TAGGED) in actions
    assert (actions.index(("create_vlan", 2201, "transit-2201"))
            < actions.index(("membership", 2201, 49, VlanMode.TAGGED)))


def vinfo(vid, name="", member=(), tagged=(), untagged=()):
    return VLANInfo(vlan_id=vid, name=name, member_ports=frozenset(member),
                    tagged_ports=frozenset(tagged),
                    untagged_ports=frozenset(untagged))


def test_diff_from_factory_default():
    d = desired_state(SPECS, 2)
    # factory-ish: only VLAN 1, all ports untagged members, all PVID 1
    current = [vinfo(1, "default", member=range(1, 53), untagged=range(1, 53))]
    pvids = {p: 1 for p in range(1, 53)}
    actions = diff(current, pvids, d)
    assert ("create_vlan", 2207, "pi-sw2-p7") in actions
    assert ("membership", 2207, 7, VlanMode.UNTAGGED) in actions
    assert ("pvid", 7, 2207) in actions
    assert ("membership", 1, 7, VlanMode.EXCLUDED) in actions
    # order: create < trunk tag < access untag < pvid < vlan1 exclusion
    assert (actions.index(("membership", 2207, 7, VlanMode.UNTAGGED))
            < actions.index(("pvid", 7, 2207))
            < actions.index(("membership", 1, 7, VlanMode.EXCLUDED)))
    # house uplink (52) untouched: no action mentions port 52
    assert not [a for a in actions if a[0] == "membership" and a[2] == 52]


def test_diff_adds_egress_when_untagged_default_is_all_ports():
    # Some models (gsm7252ps) report a freshly created VLAN with EVERY port
    # already in the untagged bitmap but NONE in the egress/member set. Checking
    # untagged_ports alone wrongly concludes the access port is already a member
    # and never adds it to egress, so the port cannot receive frames (the DHCP
    # OFFER never egresses to it). The diff must still emit the untagged
    # membership -- regression: pi-sw1-p9 stuck at DHCPOFFER on the live GSM.
    d = desired_state(SPECS, 2)
    # VLAN 2207 exists; port 7 is in the untagged bitmap but is NOT a member
    current = [vinfo(2207, "pi-sw2-p7", member=(49,), untagged=range(1, 53))]
    actions = diff(current, {}, d)
    assert ("membership", 2207, 7, VlanMode.UNTAGGED) in actions


def test_diff_idempotent_and_prunes_stale():
    d = desired_state(SPECS, 2)
    current = [vinfo(1, "default", member=(49, 50, 52), untagged=(49, 50, 52))]
    # sorted(d.vlans) is [2201, 2202, ..., 2248], i.e. vlan_id(2, p) for
    # p = 1..48 in ascending order, so enumerate(..., start=1) pairs each
    # vlan back up with the very port number (p) it was derived from.
    current += [vinfo(v, d.vlans[v], member={p, 49}, untagged={p}, tagged={49})
                for p, v in enumerate(sorted(d.vlans), start=1)]
    current.append(vinfo(2199, "stale"))       # in OWN_RANGE -> delete
    current.append(vinfo(5, "net-house"))      # outside -> NEVER touched
    pvids = {p: 2200 + p for p in range(1, 49)} | {49: 1, 50: 1, 52: 1}
    actions = diff(current, pvids, d)
    assert ("delete_vlan", 2199) in actions
    assert not [a for a in actions if a[1] == 5]
    # the rest of `current` is already fully converged: the only action
    # against it is the stale-VLAN delete
    assert actions == [("delete_vlan", 2199)]
    # second run over the converged state (stale VLAN now gone) = no actions
    current_converged = [v for v in current if v.vlan_id != 2199]
    assert diff(current_converged, pvids, d) == []
