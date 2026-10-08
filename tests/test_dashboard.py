"""The switch dashboard reader against the library's in-process VirtualSwitch,
served over real SNMP and configured the way the per-port-VLAN hosts are
(a switches file plus a community in the environment).

Requires the net-snmp CLI tools (apt: snmp), like the switch_setup tests.
Nothing here talks to a real switch.
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from django.core.cache.backends.locmem import LocMemCache
from netgear_switch.errors import UnsupportedCapabilityError
from netgear_switch.virtual.server import VirtualSwitch

from snmp_switch import dashboard
from snmp_switch.switches import PoeConfigError, PoeRequestError
from tests.test_snmp_switch import write_switches

COMMUNITY = "dashboard-test-community-4f9a"


@pytest.fixture(autouse=True)
def no_switch_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("SNMP_SWITCH_", "FPGAS_SWITCH")):
            monkeypatch.delenv(k)


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch):
    monkeypatch.setattr(dashboard, "SNMP_TIMEOUT", 1)


def serve(model):
    vs = VirtualSwitch(model, community=COMMUNITY)
    vs.start()
    return vs


def configure(monkeypatch, tmp_path, *switches):
    """Configure (index, library model, vs) switches, as infra does."""
    cfg = write_switches(tmp_path, *[(i, model, f"{vs.host}:{vs.port}") for i, model, vs in switches])
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY", COMMUNITY)


@pytest.fixture(scope="module")
def welland():
    """Both welland switches read once: switch 1 is the GSM7252PS and switch 2
    the S3300/GSM7228PS. Reading is slow (a process per SNMP request), so
    the tests that only look at the result share one read."""
    one, two = serve("gsm7252ps"), serve("gsm7228ps")
    mp = pytest.MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as d:
            configure(mp, Path(d), (1, "gsm7252ps", one), (2, "s3300", two))
            views = dashboard.read_all()
    finally:
        mp.undo()
        one.stop()
        two.stop()
    return {v.index: {p.port: p for p in v.ports} for v in views}, views


def port(welland, switch, number):
    return welland[0][switch][number]


# --- read_all against both virtual switches ----------------------------------


def test_every_configured_switch_in_index_order(welland):
    views = welland[1]
    assert [v.index for v in views] == [1, 2]
    assert all(v.reachable and v.error == "" for v in views)
    assert [v.model for v in views] == ["gsm7252ps", "s3300"]
    assert len(views[0].ports) == 52 and len(views[1].ports) == 52


def test_a_delivering_port_has_link_poe_and_watts(welland):
    p = port(welland, 1, 1)  # the seed's eth0.rpi5-pmod
    assert p.label == "eth0.rpi5-pmod"
    assert p.link_up and p.speed_mbps == 1000
    assert p.poe_state == "delivering"
    assert p.poe_watts == 3.5
    assert port(welland, 2, 48).poe_state == "delivering"
    assert port(welland, 2, 48).poe_watts == 0.7


def test_poe_states_of_the_seed(welland):
    assert port(welland, 2, 46).poe_state == "fault"
    assert port(welland, 2, 47).poe_state == "searching"
    assert port(welland, 1, 16).poe_state == "searching"  # link up, nothing drawing
    assert port(welland, 1, 6).poe_state == "other"  # the seed's unknown detect state


def test_a_port_without_poe_has_no_poe_state(welland):
    for switch, number in [(1, 49), (1, 52), (2, 49), (2, 51)]:
        p = port(welland, switch, number)
        assert p.poe_state is None and p.poe_watts is None


def test_a_down_port_has_no_link_speed_lldp_or_macs(welland):
    p = port(welland, 1, 48)  # spare.ex-cisco, down
    assert p.label == "spare.ex-cisco"
    assert not p.link_up
    assert p.speed_mbps is None
    assert p.lldp_name is None and p.lldp_port is None and p.lldp_chassis is None
    assert p.macs == []
    # the seed gives port 10 a MAC, but its link is down: nothing is shown
    ten = port(welland, 1, 10)
    assert not ten.link_up and ten.macs == []


def test_lldp_neighbour_of_the_head_switch(welland):
    p = port(welland, 1, 49)
    assert p.link_up and p.speed_mbps == 10000
    assert p.lldp_name == "sw-cisco-shed"
    assert p.lldp_port == "1/xg51"
    assert p.lldp_chassis == "C8:00:84:89:71:70"


def test_lldp_neighbours_of_the_s3300(welland):
    p = port(welland, 2, 49)
    assert p.lldp_name == "sw-netgear-gsm7252ps-s1.welland.mithis.com"
    assert p.lldp_port == "1/0/48"
    p = port(welland, 2, 51)
    assert p.lldp_name == "sw-netgear-gsm7252ps-s2.welland.mithis.com"
    assert p.lldp_port == "1/0/50"


def test_macs_seen_on_a_port(welland):
    assert port(welland, 1, 11).macs == ["00:1B:21:3C:4D:5E"]
    macs = port(welland, 2, 51).macs
    assert len(macs) == 14 and macs == sorted(macs)
    assert "44:A5:6E:60:C5:B6" in macs  # on VLANs 5 and 121 in the seed: listed once
    assert macs.count("44:A5:6E:60:C5:B6") == 1


def test_counters_and_errors_are_kept_raw(welland):
    p = port(welland, 1, 9)
    assert (p.rx_bytes, p.tx_bytes) == (43952641, 2474560700)
    assert (p.rx_errors, p.tx_errors) == (188, 0)
    assert p.speed_mbps == 100
    # a first read has no rate
    assert all(p.rx_bps is None and p.tx_bps is None for p in welland[1][0].ports)


def test_views_are_json_able(welland):
    again = json.loads(json.dumps([asdict(v) for v in welland[1]]))
    assert again[0]["index"] == 1 and again[0]["ports"][0]["port"] == 1
    assert again[1]["ports"][0]["poe_state"] == "searching"


def test_the_community_is_in_no_view(welland):
    assert COMMUNITY not in json.dumps([asdict(v) for v in welland[1]])
    assert COMMUNITY not in repr(welland[1])


# --- a switch that does not answer --------------------------------------------


def test_an_unreachable_switch_is_a_view_not_an_exception(monkeypatch, tmp_path, caplog):
    cfg = write_switches(tmp_path, (1, "gsm7252ps", "127.0.0.1:1"))  # nothing listens
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_1", COMMUNITY)
    with caplog.at_level(logging.DEBUG):
        view = dashboard.read_switch(1)
    assert not view.reachable
    assert view.ports == []
    assert view.error == "not answering"
    assert "127.0.0.1" not in json.dumps(asdict(view))
    assert "127.0.0.1:1" in caplog.text  # the detail is for the journal
    assert COMMUNITY not in view.error
    assert COMMUNITY not in json.dumps(asdict(view))
    assert COMMUNITY not in caplog.text


def test_library_error_text_with_the_community_is_scrubbed(monkeypatch, tmp_path, caplog):
    cfg = write_switches(tmp_path, (1, "gsm7252ps", "192.0.2.1:161"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY", COMMUNITY)

    def leak(*args, **kwargs):
        raise RuntimeError(f"snmpget -c {COMMUNITY} 192.0.2.1 failed")

    monkeypatch.setattr(dashboard, "library_switch", leak)
    with caplog.at_level(logging.DEBUG):
        view = dashboard.read_switch(1)
    assert not view.reachable
    assert view.error == "not answering"
    assert "192.0.2.1" in caplog.text and "[community]" in caplog.text
    assert COMMUNITY not in view.error and COMMUNITY not in caplog.text


@pytest.mark.parametrize("method, error, what, check", [
    ("get_lldp", UnsupportedCapabilityError, "LLDP", lambda p: p.lldp_name is None and p.poe_state == "delivering"),
    # not a library error: an odd answer from the agent
    ("get_stats", ValueError, "counters", lambda p: p.rx_bytes is None and p.poe_state == "delivering"),
    ("get_poe", ValueError, "PoE", lambda p: p.poe_state is None and p.poe_watts is None and p.rx_bytes is not None),
    ("get_macs", KeyError, "MAC table", lambda p: p.macs == [] and p.poe_state == "delivering"),
])
def test_one_secondary_read_that_fails_leaves_the_rest_of_the_switch(monkeypatch, tmp_path, caplog,
                                                                      method, error, what, check):
    """A switch that cannot give one of the reads, for whatever reason, has
    None for those columns and says so, but the switch is still shown."""
    from netgear_switch.sync_api import SyncSwitch

    vs = serve("gsm7252ps")
    try:
        configure(monkeypatch, tmp_path, (1, "gsm7252ps", vs))

        def broken(self, **kwargs):
            raise error(f"odd answer ({COMMUNITY})")

        monkeypatch.setattr(SyncSwitch, method, broken)
        with caplog.at_level(logging.DEBUG):
            view = dashboard.read_switch(1)
    finally:
        vs.stop()
    assert view.reachable
    assert view.error == f"{what} not read"
    assert COMMUNITY not in json.dumps(asdict(view)) and COMMUNITY not in caplog.text
    assert "[community]" in caplog.text
    assert check(next(p for p in view.ports if p.port == 1))


def test_the_poe_disabled_state(monkeypatch, tmp_path):
    vs = serve("gsm7252ps")
    try:
        configure(monkeypatch, tmp_path, (1, "gsm7252ps", vs))
        vs.state.poe[1].detect = 1  # RFC 3621 disabled
        refresh(vs)
        view = dashboard.read_switch(1)
    finally:
        vs.stop()
    assert next(p for p in view.ports if p.port == 1).poe_state == "disabled"


def test_the_name_is_the_switchs_sysname(welland):
    assert [v.name for v in welland[1]] == ["sw-netgear-gsm7252ps-s1.welland.mithis.com", "sw-netgear-s3300-1"]


def test_the_name_falls_back_to_the_index(monkeypatch, tmp_path):
    from netgear_switch.sync_api import SyncSwitch

    vs = serve("gsm7252ps")
    try:
        configure(monkeypatch, tmp_path, (1, "gsm7252ps", vs))
        monkeypatch.setattr(SyncSwitch, "get_hostname", lambda self, **kw: (_ for _ in ()).throw(ValueError("x")))
        view = dashboard.read_switch(1)
    finally:
        vs.stop()
    assert view.reachable and view.name == "switch 1" and view.error == "name not read"


def test_an_unknown_switch_is_refused_and_a_missing_community_is_a_view(monkeypatch, tmp_path, caplog):
    cfg = write_switches(tmp_path, (1, "gsm7252ps", "192.0.2.1:161"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    with pytest.raises(PoeRequestError, match="switch 7 is not configured"):
        dashboard.read_switch(7)
    with caplog.at_level(logging.DEBUG):
        view = dashboard.read_switch(1)
    assert not view.reachable and view.error == "community not configured"
    assert "192.0.2.1" not in json.dumps(asdict(view))
    assert "no SNMP community for switch 1" in caplog.text


def test_one_switch_without_a_community_does_not_hide_the_others(monkeypatch, tmp_path):
    vs = serve("gsm7252ps")
    try:
        cfg = write_switches(tmp_path, (1, "gsm7252ps", "192.0.2.1:161"), (2, "gsm7252ps", f"{vs.host}:{vs.port}"))
        monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
        monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", COMMUNITY)
        one, two = dashboard.read_all()
        cached = dashboard.cached_read_all(LocMemCache("partial", {}), ttl=15)
    finally:
        vs.stop()
    assert (one.index, one.reachable, one.error) == (1, False, "community not configured")
    assert two.index == 2 and two.reachable and len(two.ports) == 52
    assert [v.index for v in cached] == [1, 2] and cached[1].reachable


def test_no_management_host_is_in_any_view(monkeypatch, tmp_path):
    """The page is public: not the address of a switch, whatever went wrong."""
    cfg = write_switches(tmp_path, (1, "gsm7252ps", "192.0.2.1:161"), (2, "gsm7252ps", "127.0.0.1:1"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", COMMUNITY)
    text = json.dumps([asdict(v) for v in dashboard.read_all()])
    assert "192.0.2.1" not in text and "127.0.0.1" not in text


def test_nothing_configured_is_a_config_error():
    with pytest.raises(PoeConfigError):
        dashboard.read_all()


def test_the_legacy_switch_is_not_read_yet(monkeypatch):
    monkeypatch.setenv("SNMP_SWITCH_HOST", "192.0.2.2")
    (view,) = dashboard.read_all()
    assert view.index is None
    assert not view.reachable
    assert view.error == "dashboard reads for this switch are not written yet"
    assert view.ports == []
    (again,) = dashboard.cached_read_all(LocMemCache("legacy", {}))
    assert again.error == view.error


# --- rates -------------------------------------------------------------------


def test_rates_are_bits_per_second():
    assert dashboard.rates((1000, 4000), (2500, 4000), 10) == (1200.0, 0.0)


@pytest.mark.parametrize("previous, current, seconds, expected", [
    (None, (10, 10), 5, (None, None)),  # the first read
    ((10, 10), None, 5, (None, None)),
    ((100, 100), (50, 200), 5, (None, 160.0)),  # one counter went backwards
    ((100, 100), (50, 40), 5, (None, None)),  # both did
    ((100, 100), (200, 200), 0, (None, None)),  # no time passed
    ((100, 100), (200, 200), -3, (None, None)),  # the clock stepped back
    ((100, 100), (200, 200), None, (None, None)),
    ((None, 100), (200, 200), 5, (None, 160.0)),  # a counter the port does not have
    ((100, 100), (100, 100), 5, (0.0, 0.0)),  # an idle port is 0, not unknown
])
def test_rates_edge_cases(previous, current, seconds, expected):
    assert dashboard.rates(previous, current, seconds) == expected


# --- the cache ---------------------------------------------------------------


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=9, minutes=30)))

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(dashboard, "_now", c)
    return c


@pytest.fixture()
def reads(monkeypatch):
    """The switches actually read, by index."""
    seen = []
    real = dashboard._read_spec

    def counting(spec):
        seen.append(spec.index)
        return real(spec)

    monkeypatch.setattr(dashboard, "_read_spec", counting)
    return seen


@pytest.fixture()
def cache():
    c = LocMemCache("dashboard-test", {})
    yield c
    c.clear()


@pytest.fixture()
def head(monkeypatch, tmp_path):
    """The virtual GSM7252PS, configured as switch 1."""
    vs = serve("gsm7252ps")
    configure(monkeypatch, tmp_path, (1, "gsm7252ps", vs))
    yield vs
    vs.stop()


def refresh(vs):
    """Make the virtual switch answer from its changed state (its SNMP face
    serves a view built when it started)."""
    vs._snmp_face._view.rebuild()


def test_a_second_call_within_the_ttl_does_not_read_the_switch(head, cache, clock, reads):
    first = dashboard.cached_read_all(cache, ttl=15)
    clock.advance(14)
    second = dashboard.cached_read_all(cache, ttl=15)
    assert reads == [1]
    assert first == second


def test_rates_come_from_the_previous_cached_read(head, cache, clock, reads):
    first = dashboard.cached_read_all(cache, ttl=15)[0]
    assert next(p for p in first.ports if p.port == 1).rx_bps is None
    before = head.state.ports[1]
    rx, tx = before.rx_octets, before.tx_octets
    before.rx_octets += 1500
    before.tx_octets += 3000
    refresh(head)
    clock.advance(20)
    second = dashboard.cached_read_all(cache, ttl=15)[0]
    assert reads == [1, 1]
    p = next(p for p in second.ports if p.port == 1)
    assert (p.rx_bytes, p.tx_bytes) == (rx + 1500, tx + 3000)
    assert p.rx_bps == 1500 * 8 / 20
    assert p.tx_bps == 3000 * 8 / 20
    idle = next(p for p in second.ports if p.port == 2)
    assert idle.rx_bps == 0.0 and idle.tx_bps == 0.0
    # and a counter that goes backwards (a switch reboot) has no rate again
    before.rx_octets = 5
    refresh(head)
    clock.advance(20)
    third = dashboard.cached_read_all(cache, ttl=15)[0]
    p = next(p for p in third.ports if p.port == 1)
    assert p.rx_bps is None and p.tx_bps == 0.0


def test_a_caller_that_loses_the_lock_gets_the_last_read(head, cache, clock, reads):
    first = dashboard.cached_read_all(cache, ttl=15)[0]
    clock.advance(30)  # stale, so a read is due
    assert cache.add("dashboard:v1:lock:1", 1, 30)  # another worker is reading
    got = dashboard.cached_read_all(cache, ttl=15)[0]
    assert reads == [1]  # this caller did not read
    assert got == first
    cache.delete("dashboard:v1:lock:1")
    dashboard.cached_read_all(cache, ttl=15)
    assert reads == [1, 1]
    assert cache.add("dashboard:v1:lock:1", 1, 30)  # and its own lock was let go


def test_the_lock_is_let_go_when_a_read_fails(head, cache, clock, monkeypatch):
    def broken(spec):
        raise RuntimeError("boom")

    monkeypatch.setattr(dashboard, "_read_spec", broken)
    with pytest.raises(RuntimeError):
        dashboard.cached_read_all(cache, ttl=15)
    assert cache.add("dashboard:v1:lock:1", 1, 30)


def test_losing_the_lock_with_nothing_cached_says_so(head, cache, clock, reads):
    assert cache.add("dashboard:v1:lock:1", 1, 30)
    (view,) = dashboard.cached_read_all(cache, ttl=15)
    assert reads == []
    assert not view.reachable and view.error == "first read in progress"


def test_a_dead_switch_keeps_its_last_good_ports(head, cache, clock, reads):
    good = dashboard.cached_read_all(cache, ttl=15)[0]
    assert good.reachable
    head.stop()  # nothing listens any more
    clock.advance(60)
    dead = dashboard.cached_read_all(cache, ttl=15)[0]
    assert not dead.reachable
    assert dead.error == "not answering since 12:00:00"
    assert COMMUNITY not in dead.error
    assert [asdict(p) for p in dead.ports] == [asdict(p) | {"rx_bps": None, "tx_bps": None} for p in good.ports]
    assert dead.good_at == good.good_at
    assert dead.read_at != good.read_at
    # still dead later: still since the last good read, and the page is not hammered inside the TTL
    clock.advance(5)
    again = dashboard.cached_read_all(cache, ttl=15)[0]
    assert reads == [1, 1]
    assert again.error == "not answering since 12:00:00"
    assert COMMUNITY not in json.dumps(asdict(again))


def test_a_dead_switch_never_seen_has_no_ports(monkeypatch, tmp_path, cache, clock):
    cfg = write_switches(tmp_path, (1, "gsm7252ps", "127.0.0.1:1"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY", COMMUNITY)
    (view,) = dashboard.cached_read_all(cache, ttl=15)
    assert not view.reachable and view.ports == []
    assert COMMUNITY not in view.error


def test_an_entry_this_code_cannot_read_is_a_miss(head, cache, clock, reads):
    key = "dashboard:v1:switch:1"
    cache.set(key, {"index": 1, "a_field_from_another_version": True, "ports": []}, 60)
    (view,) = dashboard.cached_read_all(cache, ttl=15)
    assert reads == [1] and view.reachable


def test_an_entry_from_the_future_is_not_fresh(head, cache, clock, reads):
    dashboard.cached_read_all(cache, ttl=15)
    clock.advance(-100)  # the clock stepped back
    dashboard.cached_read_all(cache, ttl=15)
    assert reads == [1, 1]


def test_a_lock_a_later_reader_took_is_not_deleted(head, cache, clock, monkeypatch):
    real = dashboard._read_spec

    def slow(spec):  # our lock expired while we read, and another worker took its own
        cache.set("dashboard:v1:lock:1", "someone else", 60)
        return real(spec)

    monkeypatch.setattr(dashboard, "_read_spec", slow)
    dashboard.cached_read_all(cache, ttl=15)
    assert cache.get("dashboard:v1:lock:1") == "someone else"


def test_switches_are_read_at_the_same_time(monkeypatch, tmp_path, cache, clock):
    import threading

    cfg = write_switches(tmp_path, (1, "gsm7252ps", "192.0.2.1:161"), (2, "gsm7252ps", "192.0.2.2:161"))
    monkeypatch.setenv("FPGAS_SWITCHES_CONFIG", str(cfg))
    barrier = threading.Barrier(2, timeout=10)  # passes only if both reads are running at once

    def meet(spec):
        barrier.wait()
        return dashboard.SwitchView(index=spec.index, name="x", model="m", reachable=True, error="",
                                    read_at=clock().isoformat(), good_at=clock().isoformat())

    monkeypatch.setattr(dashboard, "_read_spec", meet)
    assert [v.index for v in dashboard.cached_read_all(cache, ttl=15)] == [1, 2]
