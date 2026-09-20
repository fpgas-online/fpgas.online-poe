"""Which boards exist, what their ports are doing, and how to reach the switch.

Board identity reproduces the formulas in the infra repo's
ansible/filter_plugins/port_vlans.py (registers the port_vlan_map filter),
which is the source of truth for the VLAN-per-port scheme: IPv4
<pib_network>.<switch>.<port>, hostname pi-sw<switch>-p<port>.
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


@dataclass(frozen=True)
class PortSnapshot:
    """One access port's PoE state, as read in a single sweep.

    The watchdog used to see only DELIVERING ports, which made two different
    problems invisible in the same way: a port the switch had faulted off, and
    a port this service turned off and then failed to turn back on. Neither is
    ever enumerated again if you only look at DELIVERING, so neither could be
    recovered or even reported. Carry the whole state instead.
    """

    board: Board
    detect: PoEDetect
    admin_enabled: bool
    power_mw: int | None

    @property
    def delivering(self) -> bool:
        return self.detect is PoEDetect.DELIVERING

    @property
    def faulted(self) -> bool:
        return self.detect is PoEDetect.FAULT

    @property
    def unreadable(self) -> bool:
        """The switch gave a PoE state this library could not interpret.

        Not the same as a healthy port. Treating it as one would quietly drop
        the port out of every rule below.
        """
        return self.detect is PoEDetect.UNKNOWN

    @property
    def powered_off(self) -> bool:
        """Admin-disabled: the switch was told to stop supplying this port.

        Distinct from SEARCHING, which is a live port with nothing drawing on
        it -- an empty socket, or one whose board has not started drawing yet.
        """
        return not self.admin_enabled


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
    underneath the soft filter in scan_ports.
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


def scan_ports(
    sw: SyncSwitch, spec, excluded: frozenset[int], pib_network: str
) -> list[PortSnapshot]:
    """Every access port the watchdog is allowed to touch, whatever its state.

    One SNMP read per switch per sweep; the callers filter this rather than
    asking the switch again.
    """
    skip = excluded | protected_ports(spec)
    return [
        PortSnapshot(
            board=make_board(spec.index, status.port, pib_network),
            detect=status.detect,
            admin_enabled=status.admin_enabled,
            power_mw=status.power_mw,
        )
        for status in sw.get_poe()
        if 1 <= status.port <= spec.access_ports and status.port not in skip
    ]


def occupied_boards(
    sw: SyncSwitch, spec, excluded: frozenset[int], pib_network: str
) -> list[Board]:
    """Every access port that is delivering PoE, minus exclusions and trunks.

    Delivering alone means occupied: a board hung hard enough to stop
    transmitting ages out of the MAC table, and that is exactly the board this
    service exists to rescue.
    """
    return [
        snap.board
        for snap in scan_ports(sw, spec, excluded, pib_network)
        if snap.delivering
    ]
