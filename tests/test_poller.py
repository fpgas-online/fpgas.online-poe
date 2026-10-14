"""snmp_switch.poller: link changes, bounces, and which leases it releases, from scripted SNMP answers."""

import pytest
from netgear_switch.protocols.snmp.client import SnmpRow

from snmp_switch.poller import (
    IF_LAST_CHANGE,
    IF_OPER_STATUS,
    SYS_UPTIME,
    Lease,
    Poller,
    PollError,
    board_ports,
    parse_read,
    request_oids,
)
from switch_setup.plan import SwitchSpec

# a small switch: ports 1-6 are access ports, 6 is also the gateway trunk (so not a board port), 8 the uplink
SPEC = SwitchSpec(index=2, model="s3300", mgmt_host="192.0.2.1", access_ports=6,
                  gateway_trunk_port=6, downstream_trunk_ports=(), house_uplink_port=8)
OTHER = SwitchSpec(index=1, model="gsm7252ps", mgmt_host="192.0.2.2", access_ports=3,
                   gateway_trunk_port=7, downstream_trunk_ports=(), house_uplink_port=8)
LEASE_SECONDS = 300


def rows(spec, uptime, oper, last_change=None, drop=(), extra=()):
    """The rows one GET of request_oids(spec) would give. oper/last_change: {port: value}, default up / 0."""
    last_change = last_change or {}
    out = [SnmpRow(SYS_UPTIME, uptime, "Timeticks")]
    for p in board_ports(spec):
        out.append(SnmpRow(f"{IF_OPER_STATUS}.{p}", oper.get(p, 1), "INTEGER"))
        out.append(SnmpRow(f"{IF_LAST_CHANGE}.{p}", last_change.get(p, 0), "Timeticks"))
    out = [r for r in out if r.oid not in {f"{IF_OPER_STATUS}.{p}" for p in drop}]
    return out + list(extra)


class Script:
    """The poller's surroundings, scripted per poll."""

    def __init__(self, specs=(SPEC,)):
        self.answers = {s.index: [] for s in specs}
        self.leases = []
        self.addresses = {(s.index, p): {f"10.21.{s.index}.{p}", f"2404:e80:a137:21{s.index:02d}::{p}"}
                          for s in specs for p in board_ports(s)}
        self.released = []
        self.release_ok = True
        self.published = []
        self.now = 1_000_000.0
        self.poller = Poller(
            specs=list(specs), read=self.read, port_addresses=lambda: self.addresses,
            leases=lambda: list(self.leases), release=self.release, publish=self.published.append,
            lease_seconds=LEASE_SECONDS, clock=lambda: self.now)

    def read(self, spec):
        answer = self.answers[spec.index].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def release(self, spec, port, cause):
        self.released.append((spec.index, port, cause))
        return self.release_ok

    def poll(self, *answers, seconds=2):
        for spec, answer in zip(self.poller.specs, answers, strict=True):
            self.answers[spec.index].append(answer)
        self.now += seconds
        return [(c.switch, c.port, c.link) for c in self.poller.poll_once()]


def test_board_ports_leave_out_trunks_and_uplinks():
    assert board_ports(SPEC) == [1, 2, 3, 4, 5]


def test_one_get_asks_for_uptime_and_both_columns_of_every_board_port():
    oids = request_oids(SPEC)
    assert oids[0] == SYS_UPTIME and len(oids) == 1 + 2 * 5
    assert f"{IF_OPER_STATUS}.5" in oids and f"{IF_LAST_CHANGE}.5" in oids and f"{IF_OPER_STATUS}.6" not in oids


@pytest.mark.parametrize("bad", [
    lambda: rows(SPEC, 100, {}, drop=(3,)),  # a port missing
    lambda: rows(SPEC, 100, {}, extra=[SnmpRow(f"{IF_OPER_STATUS}.9", 1, "INTEGER")]),  # one not asked for
    lambda: rows(SPEC, 100, {}) + rows(SPEC, 100, {})[1:2],  # one twice
    lambda: [SnmpRow(r.oid, "up", "STRING") if r.oid.endswith(".8.2") else r for r in rows(SPEC, 100, {})],
])
def test_anything_but_a_complete_reading_is_refused(bad):
    with pytest.raises(PollError):
        parse_read(SPEC, bad())


def test_the_first_reading_reports_nothing():
    s = Script()
    assert s.poll(rows(SPEC, 100, {3: 2})) == []
    assert s.published == [] and s.released == []


def test_a_link_going_down_is_reported_and_its_lease_released_at_once():
    s = Script()
    s.poll(rows(SPEC, 100, {}))
    assert s.poll(rows(SPEC, 300, {3: 2}, {3: 250})) == [(2, 3, "down")]
    assert s.released == [(2, 3, "link-down")]
    assert s.published == [{"schema": 1, "switch": 2, "port": 3, "hostname": "pi-sw2-p3", "link": "down",
                            "at": s.published[0]["at"]}]
    assert s.published[0]["at"].endswith("+00:00")


def test_a_link_coming_up_is_reported_and_nothing_is_released():
    s = Script()
    s.poll(rows(SPEC, 100, {3: 2}))
    assert s.poll(rows(SPEC, 300, {}, {3: 250})) == [(2, 3, "up")]
    assert s.released == []


def test_a_flap_between_two_polls_is_a_bounce_and_releases():
    s = Script()
    s.poll(rows(SPEC, 100, {}, {3: 50}))
    assert s.poll(rows(SPEC, 300, {}, {3: 260})) == [(2, 3, "bounced")]
    assert s.released == [(2, 3, "link-down")]


def test_ports_that_stay_up_cause_nothing():
    s = Script()
    s.poll(rows(SPEC, 100, {}, {1: 5}))
    assert s.poll(rows(SPEC, 300, {}, {1: 5})) == []
    assert s.released == [] and s.published == []


@pytest.mark.parametrize("value", [3, 5, 6, 7])
def test_an_oper_status_that_is_neither_up_nor_down_is_no_reading(value):
    s = Script()
    s.poll(rows(SPEC, 100, {}))
    assert s.poll(rows(SPEC, 300, {3: value}, {3: 250})) == []
    assert s.released == []


@pytest.mark.parametrize("failure", [TimeoutError("no answer"), "partial"])
def test_a_switch_that_does_not_answer_whole_changes_nothing(failure):
    s = Script()
    s.leases = [Lease("10.21.2.3", 1_000_400)]
    s.poll(rows(SPEC, 100, {}))
    broken = rows(SPEC, 300, {3: 2}, drop=(1,)) if failure == "partial" else failure
    for _ in range(3):
        assert s.poll(broken) == []
    assert s.released == [] and s.published == []
    # and when it answers again, its state is compared with the last complete reading
    assert s.poll(rows(SPEC, 400, {3: 2}, {3: 350})) == [(2, 3, "down")]


def test_a_switch_reboot_is_a_new_baseline():
    s = Script()
    s.poll(rows(SPEC, 100_000, {}))
    assert s.poll(rows(SPEC, 500, {3: 2}, {3: 400})) == []
    assert s.released == []


def test_a_down_port_holding_an_old_lease_is_released_on_the_second_down_reading():
    s = Script()
    s.poll(rows(SPEC, 100, {3: 2}))  # down from the start: the first reading is a baseline
    # granted (expiry - 300 s) well before the port was first read down
    s.leases = [Lease("10.21.2.3", int(s.now) - 1000 + LEASE_SECONDS)]
    s.poll(rows(SPEC, 300, {3: 2}))
    assert s.released == [(2, 3, "down-port")]


def test_the_v6_lease_of_a_down_port_counts_too():
    s = Script()
    s.leases = [Lease("2404:e80:a137:2102::3", 0)]  # a lease that never expires
    s.poll(rows(SPEC, 100, {3: 2}))
    s.poll(rows(SPEC, 300, {3: 2}))
    assert s.released == [(2, 3, "down-port")]


def test_one_down_reading_is_not_enough_to_release():
    s = Script()
    s.leases = [Lease("10.21.2.3", 0)]
    s.poll(rows(SPEC, 100, {3: 2}))
    assert s.released == []


def test_a_lease_granted_after_the_port_was_first_read_down_is_left_alone():
    s = Script()
    s.poll(rows(SPEC, 100, {3: 2}))
    first_down = s.now
    s.leases = [Lease("10.21.2.3", int(first_down) + 1 + LEASE_SECONDS)]  # granted a second after that reading
    s.poll(rows(SPEC, 300, {3: 2}))
    s.poll(rows(SPEC, 500, {3: 2}))
    assert s.released == []


def test_an_up_port_with_a_lease_is_left_alone():
    s = Script()
    s.leases = [Lease("10.21.2.3", 0)]
    for uptime in (100, 300, 500):
        s.poll(rows(SPEC, uptime, {}))
    assert s.released == []


def test_a_lease_of_another_port_is_not_this_ports():
    s = Script()
    s.leases = [Lease("10.21.2.4", 0)]  # port 4, which is up
    s.poll(rows(SPEC, 100, {3: 2}))
    s.poll(rows(SPEC, 300, {3: 2}))
    assert s.released == []


def test_an_unreadable_lease_table_skips_the_reconciliation_and_says_so_once(caplog):
    s = Script()

    def broken():
        raise ValueError("half written")

    s.poller.leases = broken
    for uptime in (100, 300, 500, 700):
        s.poll(rows(SPEC, uptime, {3: 2}))
    assert s.released == []
    assert sum("reconciliation skipped" in r.message for r in caplog.records) == 1


def test_a_failing_release_backs_off():
    s = Script()
    s.release_ok = False
    s.leases = [Lease("10.21.2.3", 0)]
    uptime = 100
    for _ in range(12):
        s.poll(rows(SPEC, uptime, {3: 2}))
        uptime += 200
    tries = len(s.released)
    # polls 2, 3, 5, 9 (1, 2, 4 polls apart), not every one of the 11 down readings after the first
    assert tries == 4


def test_a_release_that_works_again_resets_the_backoff():
    s = Script()
    s.release_ok = False
    s.leases = [Lease("10.21.2.3", 0)]
    s.poll(rows(SPEC, 100, {3: 2}))
    s.poll(rows(SPEC, 300, {3: 2}))  # tried, fails: the next try is one poll on
    s.poll(rows(SPEC, 500, {3: 2}))  # tried, fails twice in a row: the next try is two polls on
    s.release_ok = True
    s.poll(rows(SPEC, 700, {3: 2}))  # skipped
    s.poll(rows(SPEC, 900, {3: 2}))  # tried, works
    s.poll(rows(SPEC, 1100, {3: 2}))  # the scripted lease is still there: tried again at once, no backoff left
    assert len(s.released) == 4


def test_two_switches_are_kept_apart():
    s = Script(specs=(SPEC, OTHER))
    s.poll(rows(SPEC, 100, {}), rows(OTHER, 100, {}))
    assert s.poll(rows(SPEC, 300, {}), rows(OTHER, 300, {3: 2}, {3: 250})) == [(1, 3, "down")]
    assert s.released == [(1, 3, "link-down")]
    assert s.published[0]["hostname"] == "pi-sw1-p3"
