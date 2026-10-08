"""A read-only picture of every switch, for the site's switch dashboard.

``read_all()`` returns one :class:`SwitchView` per configured switch: plain
dataclasses (``dataclasses.asdict`` makes them JSON) so the page renders them
without knowing SNMP. ``cached_read_all()`` is what a page calls: one read
per switch per interval, whatever the number of viewers, with traffic worked
out as a rate between two reads.

Nothing here writes to a switch, and no SNMP community reaches a view, an
error text or a log line: any text that comes back from the library is
scrubbed of the community before it is kept.

Two deployment shapes exist (see :mod:`snmp_switch.switches`). The
per-port-VLAN switches (welland) are read through ``netgear_switch``. The
legacy SNMPv3 switch (ps1) is not read yet: it gets one view that says so.
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime

from netgear_switch.models import PoEDetect
from netgear_switch.transport.sync.snmp_netsnmp_cli import NetsnmpCliClient

from snmp_switch.switches import (
    PoeConfigError,
    PoeRequestError,
    configured_specs,
    legacy_configured,
    library_switch,
    switch_community,
)

log = logging.getLogger(__name__)

# A switch that does not answer within this many seconds (one try, no retry:
# the page asks again at its next refresh) is shown as not answering.
SNMP_TIMEOUT = 5
SNMP_RETRIES = 0

# How long a switch's last read is good for, in seconds.
DEFAULT_TTL = 15
# How long the cache keeps a read after it went stale, so the previous
# counters (for rates) and the last good ports (for a dead switch) survive.
KEEP_SECONDS = 24 * 60 * 60
# The longest one reader may hold a switch's lock; a crashed worker's lock
# then expires on its own.
LOCK_SECONDS = 60

# The most switches read at once.
MAX_THREADS = 8

LEGACY_NOT_WRITTEN = "dashboard reads for this switch are not written yet"

_POE_STATES = {
    PoEDetect.DISABLED: "disabled",
    PoEDetect.SEARCHING: "searching",
    PoEDetect.DELIVERING: "delivering",
    PoEDetect.FAULT: "fault",
}


@dataclass
class PortView:
    port: int
    label: str | None = None  # ifAlias
    link_up: bool = False
    speed_mbps: int | None = None
    # "disabled", "searching", "delivering", "fault" or "other"; None when the
    # port has no PoE
    poe_state: str | None = None
    poe_watts: float | None = None
    lldp_name: str | None = None
    lldp_port: str | None = None
    lldp_chassis: str | None = None
    macs: list[str] = field(default_factory=list)
    # the raw octet counters, so a caller can work out rates between two reads
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_bps: float | None = None
    tx_bps: float | None = None
    rx_errors: int | None = None
    tx_errors: int | None = None


@dataclass
class SwitchView:
    index: int | None  # None: the legacy single switch, which has no index
    name: str
    model: str
    reachable: bool
    error: str  # "" when all is well; never carries a secret
    read_at: str  # ISO 8601, with the offset
    ports: list[PortView] = field(default_factory=list)
    # when the ports were last read successfully (== read_at while reachable)
    good_at: str | None = None


def _now():
    return datetime.now().astimezone()


def rates(previous, current, seconds):
    """Bits per second from two reads of (rx_bytes, tx_bytes).

    Returns (rx_bps, tx_bps). A direction is None when there is no previous
    read, a counter is missing, a counter went backwards (a reset), or the
    interval is not positive: a rate is never guessed."""
    if previous is None or current is None or seconds is None or seconds <= 0:
        return (None, None)
    return tuple(
        None if before is None or after is None or after < before
        else (after - before) * 8 / seconds
        for before, after in zip(previous, current, strict=True))


def _scrub(text, community):
    return str(text).replace(community, "[community]") if community else str(text)


def _read_or_none(read, what, notes, spec, community):
    """One of the secondary reads: a switch that cannot give it (a model that
    does not support it, or a failed read) gives None for those columns, and
    the view says so, rather than failing the whole switch."""
    try:
        return read()
    except Exception as exc:  # an odd answer from the agent empties that column only
        notes.append(f"{what} not read")
        log.warning("switch %s (%s): %s not read: %s: %s", spec.index, spec.mgmt_host, what,
                    type(exc).__name__, _scrub(exc, community))
        return None


def _detail(exc, community):
    """The full, scrubbed text of an exception: for the log, never for a view."""
    return f"{type(exc).__name__}: {_scrub(exc, community)}"


def _lldp_by_port(neighbors):
    out = {}
    for n in neighbors:
        out.setdefault(n.local_port, n)  # the first neighbour on a port
    return out


def _poe_watts(status):
    return None if status.power_mw is None else status.power_mw / 1000


def _build_ports(ports, poe, stats, lldp, macs):
    poe = {p.port: p for p in poe or []}
    stats = {s.port: s for s in stats or []}
    lldp = _lldp_by_port(lldp or [])
    mac_lists = {}
    for m in macs or []:
        mac_lists.setdefault(m.port, set()).add(m.mac)
    views = []
    for p in sorted(ports, key=lambda p: p.port):
        v = PortView(port=p.port, label=p.description, link_up=p.link_up,
                     speed_mbps=p.speed_mbps if p.link_up else None)
        if p.port in poe:
            v.poe_state = _POE_STATES.get(poe[p.port].detect, "other")
            v.poe_watts = _poe_watts(poe[p.port])
        if p.port in stats:
            s = stats[p.port]
            v.rx_bytes, v.tx_bytes = s.rx_bytes, s.tx_bytes
            v.rx_errors, v.tx_errors = s.rx_errors, s.tx_errors
        if p.link_up and p.port in lldp:
            n = lldp[p.port]
            v.lldp_name = n.remote_sys_name
            v.lldp_port = n.remote_port_id or n.remote_port_desc
            v.lldp_chassis = n.remote_chassis_id
        if p.link_up:
            v.macs = sorted(mac_lists.get(p.port, ()))
        views.append(v)
    return views


def _spec_for(index):
    specs = configured_specs()
    spec = next((s for s in specs if s.index == index), None)
    if spec is None:
        raise PoeRequestError(f"switch {index} is not configured")
    return spec


def _read_spec(spec):
    now = _now().isoformat()
    view = SwitchView(index=spec.index, name=f"switch {spec.index}", model=spec.model,
                      reachable=False, error="", read_at=now)
    try:
        community = switch_community(spec)
    except PoeConfigError as exc:  # this switch is not fully configured; the others still are read
        view.error = "community not configured"
        log.warning("switch %s (%s): %s", spec.index, spec.mgmt_host, exc)
        return view
    try:
        client = NetsnmpCliClient(spec.mgmt_host, community,
                                  timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)
        sw = library_switch(spec, snmp_client=client)
        ports = sw.get_ports()
    except Exception as exc:  # a timeout or any library error: the switch is not answering
        view.error = "not answering"
        log.warning("switch %s (%s): not answering: %s", spec.index, spec.mgmt_host, _detail(exc, community))
        return view
    notes = []
    name = _read_or_none(sw.get_hostname, "name", notes, spec, community)
    poe = _read_or_none(sw.get_poe, "PoE", notes, spec, community)
    stats = _read_or_none(sw.get_stats, "counters", notes, spec, community)
    # the counters were sampled by now: this is the time their rates are worked out from
    view.good_at = _now().isoformat()
    lldp = _read_or_none(sw.get_lldp, "LLDP", notes, spec, community)
    macs = _read_or_none(sw.get_macs, "MAC table", notes, spec, community)
    if name:
        view.name = _scrub(name, community)
    view.ports = _build_ports(ports, poe, stats, lldp, macs)
    view.reachable = True
    view.error = "; ".join(notes)
    return view


def _legacy_view():
    return SwitchView(index=None, name="switch", model="legacy", reachable=False,
                      error=LEGACY_NOT_WRITTEN, read_at=_now().isoformat())


def read_switch(index):
    """Read the switch `index` of the per-port-VLAN configuration, now.

    Never raises for a switch that does not answer: that is a view with
    reachable=False. Raises PoeRequestError for an index that is not
    configured. A switch with no community is a view with that error."""
    return _read_spec(_spec_for(index))


def _concurrently(read, specs):
    """read(spec) for every spec, at the same time (a read is a few seconds of
    waiting on net-snmp processes), results in the order of specs. The first
    exception, if any, is raised once all are done."""
    with ThreadPoolExecutor(max_workers=min(len(specs), MAX_THREADS)) as pool:
        return list(pool.map(read, specs))


def read_all():
    """Every configured switch, in index order."""
    specs = sorted(configured_specs(), key=lambda s: s.index)
    if specs:
        return _concurrently(_read_spec, specs)
    if legacy_configured():
        return [_legacy_view()]
    raise PoeConfigError("no switches are configured: set FPGAS_SWITCHES_CONFIG")


# --- caching ---------------------------------------------------------------


def _from_dict(d):
    """A cached view, or None for an entry this code cannot read (written by
    another version): it is treated as a miss and read afresh."""
    try:
        return SwitchView(**{**d, "ports": [PortView(**p) for p in d["ports"]]})
    except (TypeError, KeyError, AttributeError):
        return None


def _seconds_between(earlier, later):
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


def _with_rates(new, old):
    """Fill in new's rx_bps/tx_bps from the counters of the previous good read."""
    if old is None or not old.good_at or not new.good_at:
        return
    seconds = _seconds_between(old.good_at, new.good_at)
    before = {p.port: p for p in old.ports}
    for p in new.ports:
        o = before.get(p.port)
        if o is not None:
            p.rx_bps, p.tx_bps = rates((o.rx_bytes, o.tx_bytes), (p.rx_bytes, p.tx_bytes), seconds)


def _stale(new, old):
    """The view for a switch that did not answer: its last good ports, marked."""
    if old is None or not old.good_at:
        return new
    since = datetime.fromisoformat(old.good_at).strftime("%H:%M:%S")
    for p in old.ports:  # the old rates describe a moment that has passed
        p.rx_bps = p.tx_bps = None
    return SwitchView(index=new.index, name=old.name, model=old.model, reachable=False,
                      error=f"not answering since {since}",
                      read_at=new.read_at, ports=old.ports, good_at=old.good_at)


def _cached_switch(cache, ttl, spec):
    key = f"dashboard:v1:switch:{spec.index}"
    lock = f"dashboard:v1:lock:{spec.index}"
    entry = cache.get(key)
    old = _from_dict(entry) if entry else None
    if old is not None:
        age = (_now() - datetime.fromisoformat(old.read_at)).total_seconds()
        if 0 <= age < ttl:
            return old
    token = uuid.uuid4().hex
    if not cache.add(lock, token, LOCK_SECONDS):
        # another caller is reading this switch: give the last read
        return old or SwitchView(index=spec.index, name=f"switch {spec.index}", model=spec.model,
                                 reachable=False, error="first read in progress",
                                 read_at=_now().isoformat())
    try:
        new = _read_spec(spec)
        if new.reachable:
            _with_rates(new, old)
        else:
            new = _stale(new, old)
        cache.set(key, asdict(new), KEEP_SECONDS)
        return new
    finally:
        if cache.get(lock) == token:  # not one a later reader took after ours expired
            cache.delete(lock)


def cached_read_all(cache, ttl=DEFAULT_TTL):
    """read_all() through `cache` (a Django cache: the site passes caches["poe"]).

    One entry per switch, read at most once per `ttl` seconds, the switches
    that need a read at the same time; a caller that finds another caller
    reading a switch gets that switch's last read. Rates are worked out from
    the previous read of the same switch, and a switch that stops answering
    keeps its last good ports."""
    specs = sorted(configured_specs(), key=lambda s: s.index)
    if not specs:
        return read_all()
    return _concurrently(lambda spec: _cached_switch(cache, ttl, spec), specs)
