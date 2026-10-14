"""Watch every board port's link, tell the site about changes, and keep the gateway's DHCP leases honest.

What it is for (coord-56, 14 Oct 2026): a port's link changes reach the site as events; a port whose link goes
down (or bounces between two polls) has its DHCP lease released; and on every poll a port that is down but still
holds a lease has that lease released, so a lease means "a device on this port answered recently".

One poll reads, per switch, ``sysUpTime`` plus ``ifOperStatus`` and ``ifLastChange`` of each board port in one SNMP
GET (about 20-50 ms on welland's two switches, measured 14 Oct 2026). Nothing is written to a switch.

A port is *down* only when its ifOperStatus is present and equal to 2 on a read where every port answered; a
switch that does not answer, or answers part of the request, gives no state at all for that poll, so a stale or
partial read can never release a lease. ifLastChange (the sysUpTime of a port's last change) catches a flap shorter
than the poll: the same state on both reads but a different ifLastChange is reported as ``bounced``. A switch whose
sysUpTime went backwards rebooted: its ports are read again from scratch and nothing is reported for that poll.

The poll is pure apart from the four things passed in (the SNMP reader, the lease and port tables, the release and
the publish), so the tests drive it with recorded answers.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from snmp_switch.switches import is_access_port
from switch_setup.plan import hostname, vlan_id

log = logging.getLogger(__name__)

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
IF_LAST_CHANGE = "1.3.6.1.2.1.2.2.1.9"

# ifOperStatus values (RFC 2863). Anything else (testing, unknown, dormant, notPresent, lowerLayerDown) is not a
# reading of "unplugged" or "plugged in", so it is treated as no reading.
OPER_UP = 1
OPER_DOWN = 2

# A port must read down on this many consecutive complete polls before a lease found on it is released by the
# reconciliation (a link-down change releases at once).
DOWN_POLLS_BEFORE_RECONCILE = 2
# After a failed release a port waits 1, 2, 4 ... polls, at most this many, before the next try.
MAX_BACKOFF_POLLS = 60


class PollError(Exception):
    """A switch's answer is not a complete reading of its board ports."""


def board_ports(spec):
    """The ports of `spec`'s switch that can have a board on them (the access ports that are not trunks or uplinks)."""
    return [p for p in range(1, spec.access_ports + 1) if is_access_port(spec, p)]


def request_oids(spec):
    ports = board_ports(spec)
    return [SYS_UPTIME, *(f"{IF_OPER_STATUS}.{p}" for p in ports), *(f"{IF_LAST_CHANGE}.{p}" for p in ports)]


@dataclass(frozen=True)
class SwitchRead:
    """One complete reading of a switch's board ports."""

    uptime: int
    oper: dict
    last_change: dict


def parse_read(spec, rows):
    """A SwitchRead from the rows of one GET of request_oids(spec); PollError unless every OID answered once."""
    wanted = request_oids(spec)
    values = {}
    for row in rows:
        oid = row.oid.lstrip(".")
        if oid in values:
            raise PollError(f"{oid} answered twice")
        values[oid] = row.value
    missing = [oid for oid in wanted if oid not in values]
    extra = [oid for oid in values if oid not in set(wanted)]
    if missing or extra:
        raise PollError(f"{len(missing)} of {len(wanted)} values missing, {len(extra)} not asked for")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values.values()):
        raise PollError("a value is not a number")
    ports = board_ports(spec)
    return SwitchRead(
        uptime=values[SYS_UPTIME],
        oper={p: values[f"{IF_OPER_STATUS}.{p}"] for p in ports},
        last_change={p: values[f"{IF_LAST_CHANGE}.{p}"] for p in ports},
    )


def link(oper):
    """'up', 'down', or None for any other ifOperStatus value."""
    return {OPER_UP: "up", OPER_DOWN: "down"}.get(oper)


@dataclass(frozen=True)
class Change:
    switch: int
    port: int
    hostname: str
    link: str  # "up", "down" or "bounced"
    at: str  # UTC, ISO 8601

    def record(self):
        """The record published for this change (field names agreed with the Pi's network-state record)."""
        return {"schema": 1, "switch": self.switch, "port": self.port, "hostname": self.hostname,
                "link": self.link, "at": self.at}


@dataclass(frozen=True)
class Lease:
    address: str
    expires: int  # seconds since the epoch; 0 is a lease that never expires


@dataclass
class PortWatch:
    """What the poller remembers about one port between polls."""

    down_polls: int = 0
    down_since: float | None = None
    failures: int = 0
    retry_at_poll: int = 0


@dataclass
class Poller:
    """See the module docstring. The callables:

    - ``read(spec) -> rows``: one SNMP GET of request_oids(spec); raises on any failure;
    - ``port_table() -> ({(switch, port): {address, ...}}, lease seconds)``: each board port's own v4 and v6
      addresses and the per-port lease time, read afresh for each reconciliation (ports.conf can change);
    - ``leases() -> [Lease]``: the gateway's current DHCP leases;
    - ``release(spec, port, cause) -> bool``: release that port's leases; True when the release was confirmed;
    - ``publish(record)``: send a port change record to the site;
    - ``keepalive()``: called before each release, which can take seconds (the systemd watchdog).

    A change is a port's link reading differing from the last *known* one: a reading that is neither up nor down
    (``link`` None) leaves the known state as it was, so up, then unknown, then down is still a change to down.
    """

    specs: list
    read: object
    port_table: object
    leases: object
    release: object
    publish: object
    keepalive: object = lambda: None
    clock: object = time.time
    last: dict = field(default_factory=dict)
    read_at: dict = field(default_factory=dict)
    known: dict = field(default_factory=dict)
    known_change: dict = field(default_factory=dict)
    watch: dict = field(default_factory=dict)
    silent: set = field(default_factory=set)
    polls: int = 0
    tables_failing: bool = False
    totals: dict = field(default_factory=lambda: {"changes": 0, "releases": 0, "failed": 0, "unread": 0})

    def poll_once(self):
        """One poll of every switch, then the reconciliation. Returns the changes seen."""
        self.polls += 1
        now = self.clock()
        at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
        seen = []
        check = []  # (spec, port, since, cause): release when the port holds a lease granted before `since`
        for spec in self.specs:
            current = self._read(spec)
            if current is None:
                continue
            found = self._changes(spec, self.last.get(spec.index), current, at)
            previous_read_at = self.read_at.get(spec.index)
            self.last[spec.index], self.read_at[spec.index] = current, now
            for change in found:
                self.totals["changes"] += 1
                seen.append(change)
                self.publish(change.record())
                if change.link == "down":
                    self._release(spec, change.port, "link-down")
                elif change.link == "bounced":
                    # the flap was after the last reading: a lease granted since is the board's new one, kept
                    check.append((spec, change.port, previous_read_at, "link-down"))
            for p in board_ports(spec):
                w = self.watch.setdefault((spec.index, p), PortWatch())
                if link(current.oper[p]) == "down":
                    if w.down_polls == 0:
                        w.down_since = now
                    w.down_polls += 1
                    if w.down_polls >= DOWN_POLLS_BEFORE_RECONCILE:
                        check.append((spec, p, w.down_since, "down-port"))
                else:
                    w.down_polls, w.down_since = 0, None
        if check:
            self._reconcile(check, now)
        return seen

    def _changes(self, spec, previous, current, at):
        """The port changes this reading shows. Nothing on the first reading of a switch, and nothing across a
        switch reboot (sysUpTime went backwards): both are a new baseline. A reboot cut every board's power, but
        each board's lease is then its own again as it boots, and a port left down is caught by the reconciliation."""
        rebooted = previous is not None and current.uptime < previous.uptime
        out = []
        for p in board_ports(spec):
            now = link(current.oper[p])
            if now is None:
                continue
            key = (spec.index, p)
            before, before_change = self.known.get(key), self.known_change.get(key)
            self.known[key], self.known_change[key] = now, current.last_change[p]
            if previous is None or rebooted or before is None:
                continue
            if before != now:
                out.append(Change(spec.index, p, hostname(spec.index, p), now, at))
            elif current.last_change[p] != before_change:
                # the same state as the last known reading, but the port changed since: it flapped
                out.append(Change(spec.index, p, hostname(spec.index, p), "bounced", at))
        return out

    def _read(self, spec):
        try:
            current = parse_read(spec, self.read(spec))
        except Exception as exc:  # any failure: no reading of this switch for this poll
            self.totals["unread"] += 1
            if spec.index not in self.silent:
                self.silent.add(spec.index)
                log.warning("switch %s: no complete reading (%s); its ports are left as they were",
                            spec.index, type(exc).__name__)
            return None
        if spec.index in self.silent:
            self.silent.discard(spec.index)
            log.info("switch %s: answering again", spec.index)
        return current

    def _reconcile(self, check, now):
        try:
            leases = self.leases()
            addresses, lease_seconds = self.port_table()
        except Exception as exc:  # reported once until a read works again, not every poll
            if not self.tables_failing:
                self.tables_failing = True
                log.error("reconciliation skipped until the lease and port tables read again: %s: %s",
                          type(exc).__name__, exc)
            return
        if self.tables_failing:
            self.tables_failing = False
            log.info("reconciliation running again: the lease and port tables read")
        by_address = {}
        for lease in leases:
            by_address.setdefault(lease.address, []).append(lease)
        for spec, p, since, cause in check:
            held = [lease for a in addresses.get((spec.index, p), ()) for lease in by_address.get(a, ())]
            if any(self._stale(lease, since, now, lease_seconds) for lease in held):
                self._release(spec, p, cause)

    @staticmethod
    def _stale(lease, down_since, now, lease_seconds):
        """Whether a lease on a down port was granted before the port was first read down. A lease's grant time is
        its expiry less the lease time. A lease that never expires, or runs longer than the current lease time
        (granted under an earlier, longer one), was granted before any reading this poller made."""
        if lease.expires == 0 or lease.expires - now > lease_seconds:
            return True
        return lease.expires - lease_seconds < down_since

    def _release(self, spec, port, cause):
        w = self.watch.setdefault((spec.index, port), PortWatch())
        if self.polls < w.retry_at_poll:
            return
        self.keepalive()
        if self.release(spec, port, cause):
            self.totals["releases"] += 1
            w.failures, w.retry_at_poll = 0, 0
            return
        self.totals["failed"] += 1
        w.failures += 1
        w.retry_at_poll = self.polls + min(2 ** (w.failures - 1), MAX_BACKOFF_POLLS)
        log.error("switch %s port %s (v%s): lease release for %s failed (%d in a row)",
                  spec.index, port, vlan_id(spec.index, port), cause, w.failures)
