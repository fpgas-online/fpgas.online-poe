"""Pure derivation of desired switch state from the fpgas.online formulas.

Spec: fpgas.online-infra docs/superpowers/specs/
2026-08-14-vlan-per-port-network-design.md
"""

from dataclasses import dataclass

from netgear_switch.models import VLANInfo, VlanMode

OWN_RANGE = range(2101, 2349)  # the only VLAN IDs this tool may create/delete


def vlan_id(s: int, p: int) -> int:
    return 2000 + 100 * s + p


def hostname(s: int, p: int) -> str:
    return f"pi-sw{s}-p{p}"


@dataclass(frozen=True)
class SwitchSpec:
    index: int
    model: str
    mgmt_host: str
    access_ports: int
    gateway_trunk_port: int
    downstream_trunk_ports: tuple[int, ...]
    house_uplink_port: int


def _block(spec: SwitchSpec) -> frozenset[int]:
    return frozenset(vlan_id(spec.index, p) for p in range(1, spec.access_ports + 1))


@dataclass(frozen=True)
class DesiredState:
    vlans: dict[int, str]
    untagged: dict[int, int]          # access port -> its vlan
    tagged: dict[int, frozenset[int]]  # trunk port -> vlan set
    pvids: dict[int, int]
    vlan1_excluded: frozenset[int]     # access ports removed from VLAN 1


def desired_state(specs: list[SwitchSpec], index: int) -> DesiredState:
    spec = next(s for s in specs if s.index == index)
    behind = frozenset().union(
        *[_block(s) for s in specs if s.index > index] or [frozenset()])
    # This switch's own access VLANs (named for the Pi on each port) plus the
    # "behind" transit VLANs. Every VLAN tagged onto a trunk/downlink port must
    # exist on the switch first, so downstream switches' blocks are created here
    # as transit VLANs -- otherwise set_vlan_membership fails with
    # "VLAN <id> does not exist" on the first behind VLAN.
    vlans = {vid: f"transit-{vid}" for vid in behind}
    vlans.update({vlan_id(index, p): hostname(index, p)
                  for p in range(1, spec.access_ports + 1)})
    tagged = {spec.gateway_trunk_port: _block(spec) | behind}
    for port in spec.downstream_trunk_ports:
        tagged[port] = behind
    return DesiredState(
        vlans=vlans,
        untagged={p: vlan_id(index, p) for p in range(1, spec.access_ports + 1)},
        tagged=tagged,
        pvids={p: vlan_id(index, p) for p in range(1, spec.access_ports + 1)},
        vlan1_excluded=frozenset(range(1, spec.access_ports + 1)),
    )


Action = tuple


def diff(current_vlans: list[VLANInfo], current_pvids: dict[int, int],
         desired: DesiredState) -> list[Action]:
    """Actions to converge, in an order that never strands a port:
    1. create missing VLANs
    2. trunk tagged memberships (path to gateway exists first)
    3. access untagged memberships
    4. access PVIDs (port must already be a member)
    5. access-port VLAN 1 exclusions (only after PVID moved off 1)
    6. delete stale VLANs in OWN_RANGE
    Only VLANs in OWN_RANGE (plus owned ports' VLAN 1 membership) are
    ever written; everything else on the switch is invisible to us.
    """
    by_id = {v.vlan_id: v for v in current_vlans}
    creates: list[Action] = []
    trunk: list[Action] = []
    access: list[Action] = []
    pvids: list[Action] = []
    vlan1: list[Action] = []
    deletes: list[Action] = []

    for vid, name in sorted(desired.vlans.items()):
        if vid not in by_id:
            creates.append(("create_vlan", vid, name))

    for port, vids in sorted(desired.tagged.items()):
        for vid in sorted(vids):
            cur = by_id.get(vid)
            if cur is None or port not in cur.tagged_ports:
                trunk.append(("membership", vid, port, VlanMode.TAGGED))

    for port, vid in sorted(desired.untagged.items()):
        cur = by_id.get(vid)
        # A port is a real untagged member only when it is in BOTH the egress
        # (member) set AND the untagged set. Some models (gsm7252ps) default
        # every VLAN's untagged bitmap to all-ports while the egress bitmap is
        # empty, so checking untagged_ports alone wrongly concludes the port is
        # already a member and never adds it to egress -- leaving the access
        # port unable to receive frames (DHCP OFFER never egresses to it).
        if (cur is None or port not in cur.member_ports
                or port not in cur.untagged_ports):
            access.append(("membership", vid, port, VlanMode.UNTAGGED))

    for port, vid in sorted(desired.pvids.items()):
        if current_pvids.get(port) != vid:
            pvids.append(("pvid", port, vid))

    v1 = by_id.get(1)
    for port in sorted(desired.vlan1_excluded):
        if v1 is not None and port in v1.member_ports:
            vlan1.append(("membership", 1, port, VlanMode.EXCLUDED))

    for vid in sorted(by_id):
        if vid in OWN_RANGE and vid not in desired.vlans:
            deletes.append(("delete_vlan", vid))

    return creates + trunk + access + pvids + vlan1 + deletes
