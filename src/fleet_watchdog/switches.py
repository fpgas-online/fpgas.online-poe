"""Which boards exist, and how the watchdog talks to their switch.

Board identity reproduces the formulas in the infra repo's
ansible/filter_plugins/port_vlan_map.py, which is the source of truth for the
VLAN-per-port scheme: IPv4 <pib_network>.<switch>.<port>, hostname
pi-sw<switch>-p<port>.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from netgear_switch import PoEDetect, SyncSwitch, get_model

COMMUNITY_ENV = "FPGAS_SWITCH_COMMUNITY"


@dataclass(frozen=True)
class Board:
    switch: int
    port: int
    ip: str
    hostname: str

    def __str__(self) -> str:
        return f"sw{self.switch}/p{self.port} {self.hostname} {self.ip}"


def make_board(switch: int, port: int, pib_network: str) -> Board:
    return Board(
        switch=switch,
        port=port,
        ip=f"{pib_network}.{switch}.{port}",
        hostname=f"pi-sw{switch}-p{port}",
    )


def community_for(index: int) -> str:
    """The SNMP community for one switch, per-switch variable first."""
    community = os.environ.get(f"{COMMUNITY_ENV}_{index}") or os.environ.get(
        COMMUNITY_ENV
    )
    if not community:
        raise KeyError(
            f"no SNMP community for switch {index}: "
            f"set {COMMUNITY_ENV}_{index} or {COMMUNITY_ENV}"
        )
    return community


def protected_ports(spec) -> frozenset[int]:
    """Ports the watchdog must never cut: the trunks and the house uplink.

    Cutting the gateway trunk isolates the switch; cutting a downstream trunk
    takes the next switch (and every board on it) off the network. SyncSwitch
    raises ProtectedPortError on a write to any of these, which is a hard stop
    underneath the soft filter in occupied_boards.
    """
    return frozenset(
        {spec.gateway_trunk_port, spec.house_uplink_port, *spec.downstream_trunk_ports}
    )


def open_switch(spec, community: str) -> SyncSwitch:
    # One community serves both read and write on these switches, but
    # SyncSwitch raises CredentialError on any write unless the write
    # community is set explicitly, so pass it under both names.
    return SyncSwitch(
        get_model(spec.model),
        spec.mgmt_host,
        snmp_community=community,
        snmp_write_community=community,
        protected_ports=protected_ports(spec),
    )


def occupied_boards(
    sw: SyncSwitch, spec, excluded: frozenset[int], pib_network: str
) -> list[Board]:
    """Every access port that is delivering PoE, minus exclusions and trunks.

    Delivering alone means occupied: a board hung hard enough to stop
    transmitting ages out of the MAC table, and that is exactly the board this
    service exists to rescue.
    """
    skip = excluded | protected_ports(spec)
    return [
        make_board(spec.index, status.port, pib_network)
        for status in sw.get_poe()
        if status.detect is PoEDetect.DELIVERING
        and 1 <= status.port <= spec.access_ports
        and status.port not in skip
    ]
