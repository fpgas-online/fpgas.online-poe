"""Pure derivation of desired switch state from the fpgas.online formulas.

Spec: fpgas.online-infra docs/superpowers/specs/
2026-08-14-vlan-per-port-network-design.md
"""

from dataclasses import dataclass

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
    vlans = {vlan_id(index, p): hostname(index, p)
             for p in range(1, spec.access_ports + 1)}
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
