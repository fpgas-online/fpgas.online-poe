"""The gateway's own files the switch poller reads: dnsmasq's per-port ranges and its lease file. Read-only.

``/etc/dnsmasq.d/ports.conf`` (rendered by fpgas.online-infra's ``pxe`` role) gives each per-port interface
``v<vlan>`` its one IPv4 and one IPv6 address and the lease time. The lease file is dnsmasq's
``/var/lib/misc/dnsmasq.leases``. Its content comes partly from clients (host names, client ids), and dnsmasq
rewrites it in place, so a reader can catch it half written: it is parsed strictly, and a read that does not parse
whole is retried and then reported as an error, never taken as "no leases".
"""

import ipaddress
import re
import time

from snmp_switch.poller import Lease

PORTS_CONF = "/etc/dnsmasq.d/ports.conf"
LEASE_FILE = "/var/lib/misc/dnsmasq.leases"

_IFACE = re.compile(r"v([0-9]{4})")
_RANGE = re.compile(r"dhcp-range=tag:(v[0-9]{4}),([^,]+),([^,]+),(.*)")
_LEASE_TIME = re.compile(r"([0-9]+)([smhd]?)")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_HEX_ID = re.compile(r"\*|[0-9a-f]{2}(:[0-9a-f]{2})*")
_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_NAME = re.compile(r"\*|[A-Za-z0-9][A-Za-z0-9.-]{0,252}")

READ_TRIES = 3
READ_PAUSE = 0.2


class GatewayFileError(Exception):
    """A gateway file is not in the shape this module knows."""


def port_of_vlan(vlan):
    """(switch, port) for a per-port VLAN id: the inverse of switch_setup.plan.vlan_id."""
    s, p = divmod(vlan - 2000, 100)
    if s < 1 or p < 1:
        raise GatewayFileError(f"VLAN {vlan} is not a per-port VLAN")
    return s, p


def _seconds(text):
    m = _LEASE_TIME.fullmatch(text.strip())
    if not m:
        raise GatewayFileError(f"lease time {text!r} not understood")
    return int(m[1]) * _UNITS[m[2]]


def parse_ports_conf(text):
    """({(switch, port): {address, ...}}, lease seconds) from ports.conf's per-port dhcp-range lines.

    Every per-port range must have the same lease time (the poller's lease-age guard depends on one value)."""
    addresses = {}
    times = set()
    for line in text.splitlines():
        m = _RANGE.fullmatch(line.strip())
        if not m:
            continue
        iface, start, end, rest = m.groups()
        if start != end:
            raise GatewayFileError(f"{iface}: a per-port range must be one address, not {start}-{end}")
        ip = ipaddress.ip_address(start)
        key = port_of_vlan(int(_IFACE.fullmatch(iface)[1]))
        addresses.setdefault(key, set()).add(str(ip))
        times.add(_seconds(rest.split(",")[-1]))
    if not addresses:
        raise GatewayFileError("no per-port dhcp-range lines")
    if len(times) != 1:
        raise GatewayFileError(f"per-port lease times differ: {sorted(times)}")
    return addresses, times.pop()


def parse_leases(text):
    """[Lease] from dnsmasq's lease file. GatewayFileError on any line that is not a lease, a `duid` line, or
    blank. v4 line: expiry mac address hostname client-id; v6 line: expiry iaid address hostname client-duid."""
    leases = []
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "duid":
            if len(fields) != 2 or not _HEX_ID.fullmatch(fields[1]):
                raise GatewayFileError(f"line {number}: bad duid line")
            continue
        if len(fields) != 5 or not fields[0].isdigit() or not _NAME.fullmatch(fields[3]) \
                or not _HEX_ID.fullmatch(fields[4]):
            raise GatewayFileError(f"line {number}: not a lease line")
        try:
            address = ipaddress.ip_address(fields[2])
        except ValueError:
            raise GatewayFileError(f"line {number}: bad address") from None
        if address.version == 4 and not _MAC.fullmatch(fields[1]):
            raise GatewayFileError(f"line {number}: bad MAC")
        if address.version == 6 and not fields[1].isdigit():
            raise GatewayFileError(f"line {number}: bad IAID")
        leases.append(Lease(str(address), int(fields[0])))
    return leases


def read_with_retry(path, parse, tries=READ_TRIES, pause=READ_PAUSE):
    """parse(text of path), retried when the file was caught mid-rewrite; the last error is raised."""
    for attempt in range(tries):
        try:
            with open(path, encoding="utf-8") as f:
                return parse(f.read())
        except (GatewayFileError, UnicodeDecodeError):
            if attempt == tries - 1:
                raise
            time.sleep(pause)
    raise AssertionError("unreachable")
