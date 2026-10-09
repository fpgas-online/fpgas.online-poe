"""The switch dashboard reader against the walks of welland's two real
switches (recorded read-only with snmpbulkwalk on 2026-10-09), replayed
through the same net-snmp client the reader uses on the gateway.

The seam is the reader's own client construction: ``dashboard.NetsnmpCliClient``
is replaced by the real class with ``runner=`` set to a WalkRunner
(tests/walk_runner.py), so everything above the process boundary (the client's
argv and parsing, netgear_switch, the reader's joins) is the real code.

The fixtures hold OID lines only: no community and no IPv4 address. Names are
the ones the public dashboard may show (fleet Pis, the switches' own sysNames)
or stand-ins: the gateway is "<role>.gateway" in its port labels and
gateway.invalid as an LLDP name, two private devices are "dev-device" and
"house-device". Two recorded strings were dropped: the management address inside
switch 1's sysDescr and a firmware version string on switch 2 that reads like an
address. MAC addresses: the Pis' are real; every other machine's is a 02:00:5e:00 stand-in.
"""

import re
import tempfile
from pathlib import Path

import pytest
from netgear_switch.transport.sync.snmp_netsnmp_cli import NetsnmpCliClient

from snmp_switch import dashboard
from tests.test_dashboard import COMMUNITY, no_switch_env, short_timeout  # noqa: F401  (autouse fixtures)
from tests.test_snmp_switch import write_switches
from tests.walk_runner import FIXTURES, WalkRunner

WALKS = sorted(FIXTURES.glob("welland-switch*.walk"))


@pytest.fixture(scope="module")
def recorded():
    """Both recorded switches read once: switch 1 is the GSM7252PS, switch 2 the S3300."""
    runners = {1: WalkRunner(FIXTURES / "welland-switch1.walk"),
               2: WalkRunner(FIXTURES / "welland-switch2.walk")}
    by_host = {"walk-1.invalid:161": runners[1], "walk-2.invalid:161": runners[2]}

    def client(host, community, **kwargs):
        return NetsnmpCliClient(host, community, runner=by_host[host], **kwargs)

    mp = pytest.MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg = write_switches(Path(d), (1, "gsm7252ps", "walk-1.invalid:161"), (2, "s3300", "walk-2.invalid:161"))
            mp.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
            mp.setenv("FPGAS_SWITCH_COMMUNITY", COMMUNITY)
            mp.setattr(dashboard, "NetsnmpCliClient", client)
            views = dashboard.read_all()
    finally:
        mp.undo()
    return {v.index: v for v in views}, runners


def port(recorded, switch, number):
    return next(p for p in recorded[0][switch].ports if p.port == number)


def test_both_switches_are_read_cleanly(recorded):
    views = recorded[0]
    assert sorted(views) == [1, 2]
    assert all(v.reachable and v.error == "" for v in views.values())


def test_the_sysnames(recorded):
    assert recorded[0][1].name == "sw-netgear-gsm7252ps-s2"
    assert recorded[0][2].name == "sw-netgear-s3300-1"


def test_exactly_the_52_front_panel_ports(recorded):
    # the CPU, LAG and VLAN interfaces of ifTable are dropped
    for view in recorded[0].values():
        assert [p.port for p in view.ports] == list(range(1, 53))


def test_switch_1_port_10_is_a_pi_delivering_power(recorded):
    p = port(recorded, 1, 10)
    assert p.link_up and p.speed_mbps == 1000
    assert p.poe_state == "delivering"
    assert p.poe_watts == pytest.approx(8.6, abs=0.05)
    assert p.lldp_name == "pi-sw1-p10"


def test_switch_2_port_7_is_a_pi_delivering_power(recorded):
    p = port(recorded, 2, 7)
    assert p.poe_state == "delivering"
    assert p.poe_watts == pytest.approx(3.6, abs=0.05)
    assert p.lldp_name == "pi-sw2-p7"


def test_the_runner_only_ever_read(recorded):
    for runner in recorded[1].values():
        assert runner.calls
        assert {binary for binary, _ in runner.calls} <= {"snmpbulkwalk", "snmpget"}


def test_the_gateway_is_named_by_the_placeholder(recorded):
    names = {p.lldp_name for v in recorded[0].values() for p in v.ports}
    assert "gateway.invalid" in names


# --- the fixtures themselves --------------------------------------------------

IPV4 = re.compile(r"(?<![\d.])\d{1,3}(\.\d{1,3}){3}(?![\d.])")


def values(path):
    """The value of each recorded line, the OID dropped (an OID is dotted digits too)."""
    return [line.split(" = ", 1)[1] for line in path.read_text().splitlines() if " = " in line]


def test_there_are_two_fixtures():
    assert [p.name for p in WALKS] == ["welland-switch1.walk", "welland-switch2.walk"]


@pytest.mark.parametrize("path", WALKS, ids=lambda p: p.name)
def test_the_fixtures_hold_no_secret_or_address(path):
    text = path.read_text()
    # no real host name or domain: the gateway is named "gateway" (its labels) and "gateway.invalid" (its LLDP name)
    assert not re.search(r"\.(com|net|org|au|io|local|lan)\b", text.lower())
    if path.name == "welland-switch1.walk":
        assert '"eth-uplink.gateway"' in text and '"gateway.invalid"' in text
    assert COMMUNITY not in text
    assert "community" not in text.lower()
    for value in values(path):
        if not value.startswith("OID:"):  # an OID value is dotted digits, not an address
            assert not IPV4.search(value), value
    # OID lines only
    assert all(re.fullmatch(r"\.[0-9.]+ = .*", line) for line in text.splitlines())


# The strings in a walk that can name a machine: port labels (ifAlias), sysContact/sysName/sysLocation, LLDP system
# names. Each must be a name the public dashboard may show, the gateway's stand-in, or a stand-in for a private
# device; the real names of private devices and of our hosts are not kept.
NAME_BEARING = re.compile(r"^\.(1\.3\.6\.1\.2\.1\.31\.1\.1\.1\.18|1\.3\.6\.1\.2\.1\.1\.[4-6]\.0"
                          r"|1\.0\.8802\.1\.1\.2\.1\.4\.1\.1\.9)\b")
SHOWN_NAME = re.compile(r"^(|eth-uplink\.pi\d+(\.fpgas)?|pi-sw\d+-p\d+|sw-netgear-[a-z0-9-]+"
                        r"|[0-9a-z/]+\.sw-netgear-[a-z0-9-]+|[a-z-]+\.gateway|gateway\.invalid"
                        r"|eth0\.dev-device|dev-device|house-device)$")


@pytest.mark.parametrize("path", WALKS, ids=lambda p: p.name)
def test_every_name_in_the_fixtures_is_one_the_public_page_may_show_or_a_stand_in(path):
    for line in path.read_text().splitlines():
        if NAME_BEARING.match(line):
            value = line.split(" = ", 1)[1]
            value = value[len("STRING: "):].strip('"') if value.startswith("STRING: ") else value.strip('"')
            assert SHOWN_NAME.match(value), line


def test_the_runner_answers_what_a_real_agent_answers():
    runner = WalkRunner(FIXTURES / "welland-switch1.walk")
    absent = runner(["snmpbulkwalk", "-r", "0", "host", ".1.3.6.1.99"])
    assert absent.stdout == ".1.3.6.1.99 = No Such Object available on this agent at this OID\n"
    got = runner(["snmpget", "-r", "0", "host", "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.99.0"])
    assert got.stdout.splitlines()[0] == '.1.3.6.1.2.1.1.5.0 = STRING: "sw-netgear-gsm7252ps-s2"'
    assert "No Such Instance" in got.stdout.splitlines()[1]
    with pytest.raises(AssertionError):
        runner(["snmpset", "-r", "0", "host", "1.3.6.1.2.1.1.5.0"])


PI_OUIS = {"b8:27:eb", "dc:a6:32", "e4:5f:01", "d8:3a:dd", "2c:cf:67", "28:cd:c1"}  # Raspberry Pi
STAND_IN = "02:00:5e:00:"  # the scrubbed MACs of every other machine


def macs(path):
    """Every MAC in a walk: LLDP chassis and port IDs and the Q-BRIDGE MAC table's OID suffixes."""
    for line in path.read_text().splitlines():
        # LLDP chassis IDs (.5) and port IDs (.7) of the MAC subtype
        m = re.match(r"^\.1\.0\.8802\.1\.1\.2\.1\.4\.1\.1\.[57]\.[\d.]+ = Hex-STRING: ([0-9A-F ]+)$", line)
        if m and len(m.group(1).split()) == 6:
            yield ":".join(m.group(1).lower().split())
        m = re.match(r"^\.1\.3\.6\.1\.2\.1\.17\.7\.1\.2\.2\.1\.2\.\d+\.((?:\d+\.){5}\d+) = ", line)
        if m:
            yield ":".join(f"{int(b):02x}" for b in m.group(1).split("."))


def test_the_only_real_macs_kept_are_the_pis():
    """A Pi's MAC is kept (a Raspberry Pi OUI, or the locally administered MAC of a fleet Orange Pi, which LLDP
    names pi-sw<S>-p<P>); every other machine's MAC is a stand-in."""
    pi_lldp = set()
    for path in WALKS:
        lines = path.read_text().splitlines()
        names = {ln.split(" = ")[0].split("1.0.8802.1.1.2.1.4.1.1.9.")[1]: ln.split('"')[1]
                 for ln in lines if ln.startswith(".1.0.8802.1.1.2.1.4.1.1.9.")}
        for ln in lines:
            if ln.startswith(".1.0.8802.1.1.2.1.4.1.1.5."):
                key = ln.split(" = ")[0].split("1.0.8802.1.1.2.1.4.1.1.5.")[1]
                if re.fullmatch(r"pi-sw\d+-p\d+", names.get(key, "")):
                    pi_lldp.add(":".join(ln.split(": ", 1)[1].lower().split()))
    seen = [mac for path in WALKS for mac in macs(path)]
    assert seen and any(mac.startswith(STAND_IN) for mac in seen)
    for mac in seen:
        assert mac[:8] in PI_OUIS or mac in pi_lldp or mac.startswith(STAND_IN), mac
