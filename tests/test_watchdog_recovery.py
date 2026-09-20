"""Putting ports back into service: PoE faults and ports left switched off.

A port that is not DELIVERING is not a board to probe, and the watchdog used
to enumerate only DELIVERING ports -- so a faulted port and a port stranded by
a half-finished cycle were both invisible forever. These are the rules and the
mechanism that fix that.
"""

import textwrap

import pytest
from netgear_switch import PoEDetect
from netgear_switch.snmp_write import PoeCycleTimeouts
from netgear_switch.virtual.server import VirtualSwitch

from fleet_watchdog.config import WatchdogConfig, load_config
from fleet_watchdog.policy import BoardState, Recovery, ports_to_recover
from fleet_watchdog.service import Watchdog
from fleet_watchdog.switches import PortSnapshot, make_board

# The gsm7228ps seed is a transcription of the real sw2 capture: 44 and 48
# deliver, 46 is in FAULT, the rest are searching.
FAULTED_PORT = 46
DELIVERING = {44, 48}

FAST = PoeCycleTimeouts(off_timeout=5.0, on_timeout=5.0, poll_interval=0.05)


def cfg(**kw):
    base = dict(
        switches_config="/s", pib_network="10.21", ssh_key="/k", known_hosts="/kh",
    )
    base.update(kw)
    return WatchdogConfig(**base)


def snap(port, detect, admin_enabled=True):
    return PortSnapshot(
        board=make_board(2, port, "10.21"),
        detect=detect,
        admin_enabled=admin_enabled,
        power_mw=None,
    )


# -- the rules --------------------------------------------------------------


def test_a_faulted_port_is_recovered():
    recover, give_up = ports_to_recover([snap(46, PoEDetect.FAULT)], {}, cfg(), 0.0)
    assert [(b.port, w) for b, w in recover] == [(46, Recovery.FAULT)]
    assert give_up == []


def test_an_admin_disabled_port_is_recovered():
    """This is the port a half-finished cycle strands. Nothing else in the
    system ever looks at it again."""
    ports = [snap(20, PoEDetect.DISABLED, admin_enabled=False)]
    recover, _ = ports_to_recover(ports, {}, cfg(), 0.0)
    assert [(b.port, w) for b, w in recover] == [(20, Recovery.POWERED_OFF)]


def test_a_searching_port_is_left_alone():
    """SEARCHING is an empty socket, or one whose board has not started
    drawing yet. Neither is a fault, and powering it is already the case."""
    recover, give_up = ports_to_recover([snap(7, PoEDetect.SEARCHING)], {}, cfg(), 0.0)
    assert recover == []
    assert give_up == []


def test_a_delivering_port_is_left_alone():
    recover, give_up = ports_to_recover([snap(44, PoEDetect.DELIVERING)], {}, cfg(), 0.0)
    assert recover == []
    assert give_up == []


def test_recovery_gives_up_after_the_configured_attempts():
    board = make_board(2, 46, "10.21")
    states = {board: BoardState(recovery_attempts=3)}
    recover, give_up = ports_to_recover(
        [snap(46, PoEDetect.FAULT)], states, cfg(max_recovery_attempts=3), 0.0
    )
    assert recover == []
    assert [b.port for b, _ in give_up] == [46]
    assert "on-site attention" in give_up[0][1]


def test_a_port_that_has_given_up_keeps_being_reported_every_sweep():
    """Giving up must not mean going quiet: that is the whole failure mode
    being fixed. It stops acting, not reporting."""
    board = make_board(2, 46, "10.21")
    states = {board: BoardState(recovery_attempts=9)}
    for _ in range(3):
        _, give_up = ports_to_recover(
            [snap(46, PoEDetect.FAULT)], states, cfg(max_recovery_attempts=3), 0.0
        )
        assert [b.port for b, _ in give_up] == [46]


def test_attempts_reset_once_the_port_is_back_in_service():
    board = make_board(2, 46, "10.21")
    states = {board: BoardState(recovery_attempts=2)}
    ports_to_recover([snap(46, PoEDetect.DELIVERING)], states, cfg(), 0.0)
    assert states[board].recovery_attempts == 0


def test_attempts_reset_when_a_cleared_fault_leaves_the_port_searching():
    """clear_poe_fault succeeds at DELIVERING *or* SEARCHING. An empty socket
    settles on SEARCHING and never reaches DELIVERING, so resetting only on
    DELIVERING would leak the budget away one fault at a time."""
    board = make_board(2, 46, "10.21")
    states = {board: BoardState(recovery_attempts=2)}
    ports_to_recover([snap(46, PoEDetect.SEARCHING)], states, cfg(), 0.0)
    assert states[board].recovery_attempts == 0


def test_a_port_inside_its_boot_grace_is_not_touched_again():
    board = make_board(2, 46, "10.21")
    states = {board: BoardState(last_cycle=100.0)}
    recover, _ = ports_to_recover(
        [snap(46, PoEDetect.FAULT)], states, cfg(boot_grace=300.0), now=200.0
    )
    assert recover == []


# -- the mechanism, against the virtual switch ------------------------------


@pytest.fixture()
def virtual_switch():
    vs = VirtualSwitch("gsm7228ps")
    vs.start()
    yield vs
    vs.stop()


@pytest.fixture()
def config_path(virtual_switch, tmp_path, monkeypatch):
    switches = tmp_path / "switches.yml"
    switches.write_text(textwrap.dedent(f"""
        switches:
          - index: 2
            model: s3300
            mgmt_host: {virtual_switch.host}:{virtual_switch.port}
            access_ports: 48
            gateway_trunk_port: 51
            downstream_trunk_ports: []
            house_uplink_port: 52
    """))
    watchdog = tmp_path / "watchdog.yml"
    watchdog.write_text(textwrap.dedent(f"""
        switches_config: {switches}
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
        uptime_jitter_minutes: 0
    """))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", virtual_switch.community)
    return str(watchdog)


def build(config_path):
    return Watchdog(
        load_config(config_path),
        probe=lambda b: None,
        sleep=lambda s: None,
        poe_timeouts=FAST,
    )


def detect_of(wd, port):
    return next(p for p in wd.switch(2).get_poe() if p.port == port).detect


def test_a_real_poe_fault_is_cleared_by_one_recovery_pass(config_path):
    wd = build(config_path)
    snapshots, _ = wd.scan()
    assert detect_of(wd, FAULTED_PORT) is PoEDetect.FAULT

    wd.recover(snapshots)

    assert detect_of(wd, FAULTED_PORT) in (PoEDetect.DELIVERING, PoEDetect.SEARCHING)


def test_a_port_left_switched_off_is_switched_back_on(config_path):
    """The stranded-port case: something turned a port off and did not turn it
    back on. Before this, nothing ever looked at that port again."""
    wd = build(config_path)
    wd.switch(2).set_poe(20, False)
    assert next(p for p in wd.switch(2).get_poe() if p.port == 20).admin_enabled is False

    snapshots, _ = wd.scan()
    wd.recover(snapshots)

    assert next(p for p in wd.switch(2).get_poe() if p.port == 20).admin_enabled is True


def test_a_dry_run_recovers_nothing(config_path):
    wd = build(config_path)
    snapshots, _ = wd.scan()
    wd.recover(snapshots, dry_run=True)
    assert detect_of(wd, FAULTED_PORT) is PoEDetect.FAULT


def test_recovery_never_touches_an_excluded_port(config_path, tmp_path, monkeypatch):
    """The exclusion list stays the way to tell the watchdog to keep its hands
    off a port, and it is applied before anything decides to act."""
    watchdog = tmp_path / "excluded.yml"
    watchdog.write_text(
        (tmp_path / "watchdog.yml").read_text()
        + f"\nexclude:\n  2: [{FAULTED_PORT}]\n"
    )
    wd = Watchdog(
        load_config(str(watchdog)), probe=lambda b: None, sleep=lambda s: None,
        poe_timeouts=FAST,
    )
    snapshots, _ = wd.scan()
    assert FAULTED_PORT not in {s.board.port for s in snapshots}

    wd.recover(snapshots)

    assert detect_of(wd, FAULTED_PORT) is PoEDetect.FAULT


def test_a_protected_port_is_never_recovered(config_path):
    """Trunks are filtered out of the scan, so a faulted trunk is reported by
    nothing here -- but the switch handle also refuses the write, which is the
    hard stop underneath."""
    wd = build(config_path)
    snapshots, _ = wd.scan()
    assert 51 not in {s.board.port for s in snapshots}
    assert 52 not in {s.board.port for s in snapshots}


def test_an_unreadable_port_state_is_reported_and_not_acted_on():
    """PoEDetect.UNKNOWN means the switch said something this library could
    not interpret. Treating it as healthy would silently drop the port out of
    every rule; guessing at a fix would act on a state we cannot read."""
    recover, give_up = ports_to_recover([snap(9, PoEDetect.UNKNOWN)], {}, cfg(), 0.0)
    assert recover == []
    assert [b.port for b, _ in give_up] == [9]
    assert "unreadable" in give_up[0][1]


def test_an_unreadable_port_does_not_refund_the_recovery_budget():
    """Otherwise a port flapping between FAULT and UNKNOWN would be re-armed
    forever, because each UNKNOWN sweep would reset the attempt count."""
    board = make_board(2, 9, "10.21")
    states = {board: BoardState(recovery_attempts=2)}
    ports_to_recover([snap(9, PoEDetect.UNKNOWN)], states, cfg(), 0.0)
    assert states[board].recovery_attempts == 2
