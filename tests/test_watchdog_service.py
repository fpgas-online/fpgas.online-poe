"""One whole sweep: enumerate, probe, decide, cycle, log."""

import logging
import textwrap
from concurrent.futures import ThreadPoolExecutor

import pytest
from netgear_switch.virtual.server import VirtualSwitch

from fleet_watchdog.cli import main
from fleet_watchdog.config import load_config
from fleet_watchdog.policy import Observation, Reason
from fleet_watchdog.service import Watchdog

DELIVERING = {44, 48}


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
        poe_off_seconds: 30
    """))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_2", virtual_switch.community)
    return str(watchdog)


def all_ok(uptime_s=3600.0, in_use=False):
    def probe(board):
        return Observation(board=board, ok=True, uptime_s=uptime_s, in_use=in_use, error=None)
    return probe


def all_dead(board_ports=None):
    def probe(board):
        if board_ports is None or board.port in board_ports:
            return Observation(board=board, ok=False, uptime_s=None, in_use=False, error="timed out")
        return Observation(board=board, ok=True, uptime_s=60.0, in_use=False, error=None)
    return probe


def watchdog(config_path, probe):
    return Watchdog(load_config(config_path), probe=probe, sleep=lambda s: None)


def poe_detect(wd, port):
    sw = wd.switch(2)
    return next(p for p in sw.get_poe() if p.port == port).detect.value


def test_concurrent_cycles_share_one_switch_handle(config_path):
    """run_cycles calls cycle() on several threads, and two boards on one
    switch is the common case. Without a lock each thread can build its own
    SyncSwitch and silently discard all but the last."""
    wd = watchdog(config_path, all_ok())
    with ThreadPoolExecutor(8) as pool:
        handles = list(pool.map(lambda _: wd.switch(2), range(8)))
    assert len({id(h) for h in handles}) == 1


def test_a_sweep_finds_the_delivering_ports(config_path):
    wd = watchdog(config_path, all_ok())
    d = wd.sweep()
    assert d.occupied == len(DELIVERING)
    assert d.failed == 0


def test_the_first_sweep_cycles_nothing(config_path):
    wd = watchdog(config_path, all_dead())
    wd.sweep()
    assert poe_detect(wd, 44) == "delivering"


def test_a_board_failing_twice_is_cycled(config_path):
    """Two boards occupied, one dead: one failure is under the breaker's count
    floor of 3, so the breaker stays out of the way and the board is cycled.

    Sweep 1 is the observe-only first sweep and counts failure 1. Sweep 2
    counts failure 2, reaches the threshold and cycles.
    """
    wd = watchdog(config_path, all_dead({44}))
    wd.sweep()
    d = wd.sweep()
    assert 44 in [b.port for b, _ in d.cycles]
    assert poe_detect(wd, 44) == "delivering"  # off, dwell, back on


def test_a_dry_run_decides_but_changes_nothing(config_path, monkeypatch):
    wd = watchdog(config_path, all_dead({44}))
    wd.sweep()
    calls = []
    monkeypatch.setattr(wd, "cycle", lambda board, reason: calls.append(board))
    d = wd.sweep(dry_run=True)
    assert 44 in [b.port for b, _ in d.cycles]
    assert calls == []


def test_a_cycled_board_starts_its_boot_grace(config_path):
    wd = watchdog(config_path, all_dead({44}))
    wd.sweep()
    wd.sweep()
    board = next(b for b in wd.states if b.port == 44)
    assert wd.states[board].last_cycle is not None


def test_an_uptime_cycle_happens_for_a_long_running_board(config_path):
    wd = watchdog(config_path, all_ok(uptime_s=20 * 3600))
    wd.sweep()
    d = wd.sweep()
    assert all(r.value == "uptime" for _, r in d.cycles)
    assert len(d.cycles) == 2  # both occupied boards, cap is 2


def test_a_sweep_logs_a_summary(config_path, caplog):
    wd = watchdog(config_path, all_ok())
    with caplog.at_level(logging.INFO):
        wd.sweep()
    assert any("occupied=2" in r.message for r in caplog.records)


def test_a_cycle_is_logged_with_the_board_and_the_reason(config_path, caplog):
    wd = watchdog(config_path, all_ok(uptime_s=20 * 3600))
    wd.sweep()
    with caplog.at_level(logging.WARNING):
        wd.sweep()
    assert any("pi-sw2-p44" in r.message and "uptime" in r.message for r in caplog.records)


def test_a_failed_cycle_attempts_to_restore_power(config_path, monkeypatch, caplog):
    """A cycle that dies between the off and the on leaves the port dark, and
    a port that is not DELIVERING is never enumerated again (occupied_boards
    only counts DELIVERING ports), so nothing would ever turn it back on. The
    service must make a best-effort attempt to restore power."""
    wd = watchdog(config_path, all_ok())
    monkeypatch.setattr(
        "fleet_watchdog.service.cycle_port",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dwell interrupted")),
    )
    board = next(b for b in wd.enumerate() if b.port == 44)
    with caplog.at_level(logging.ERROR):
        wd.cycle(board, Reason.UNREACHABLE)
    assert any("power restored after the failed cycle" in r.message for r in caplog.records)
    assert poe_detect(wd, 44) == "delivering"


def test_a_failed_cycle_that_cannot_restore_power_says_so(config_path, monkeypatch, caplog):
    """When even the restore attempt fails, the operator needs a message that
    says power may genuinely be off and how to recover it by hand."""
    wd = watchdog(config_path, all_ok())
    monkeypatch.setattr(
        "fleet_watchdog.service.cycle_port",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dwell interrupted")),
    )
    board = next(b for b in wd.enumerate() if b.port == 44)
    sw = wd.switch(2)
    monkeypatch.setattr(
        sw, "set_poe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snmp down"))
    )
    with caplog.at_level(logging.ERROR):
        wd.cycle(board, Reason.UNREACHABLE)
    assert any("COULD NOT RESTORE POWER" in r.message for r in caplog.records)


def test_the_signal_handler_sets_the_shutdown_flag(config_path):
    wd = watchdog(config_path, all_ok())
    assert wd._shutdown is False
    wd._request_shutdown(15, None)  # SIGTERM
    assert wd._shutdown is True


def test_run_exits_immediately_when_shutdown_is_already_set(config_path):
    """The role's own handler restarts this service on any config, env or
    unit change, which sends SIGTERM. run() must actually stop, not just
    record that it was asked to."""
    wd = watchdog(config_path, all_ok())
    wd._shutdown = True
    calls = []
    wd.sweep = lambda dry_run=False: calls.append(1)
    wd.run()
    assert calls == []


def test_a_switch_that_cannot_be_reached_does_not_kill_the_sweep(tmp_path, monkeypatch, caplog):
    """If pointing at a closed port makes this slow (the SNMP client may retry
    for tens of seconds), replace the unreachable host with
    `monkeypatch.setattr(Watchdog, "switch", raises)` where `raises` throws
    OSError. The behaviour under test is that one switch's failure is logged
    and skipped, not how the failure is produced."""
    switches = tmp_path / "switches.yml"
    switches.write_text(textwrap.dedent("""
        switches:
          - index: 9
            model: s3300
            mgmt_host: 127.0.0.1:1
            access_ports: 48
            gateway_trunk_port: 51
            downstream_trunk_ports: []
            house_uplink_port: 52
    """))
    watchdog_yml = tmp_path / "watchdog.yml"
    watchdog_yml.write_text(textwrap.dedent(f"""
        switches_config: {switches}
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
    """))
    monkeypatch.setenv("FPGAS_SWITCH_COMMUNITY_9", "public")
    wd = Watchdog(load_config(str(watchdog_yml)), probe=all_ok(), sleep=lambda s: None)
    with caplog.at_level(logging.ERROR):
        d = wd.sweep()
    assert d.occupied == 0
    assert any("switch 9" in r.message for r in caplog.records)


def test_dry_run_can_report_cycling_when_boards_warrant_it(config_path, monkeypatch, caplog):
    """cli.py must clear first_sweep before a dry-run sweep: policy.decide()
    returns early on first_sweep, before building any cycle list, and a fresh
    Watchdog always starts first_sweep=True. Left alone, `--once --dry-run`
    could never propose a cycle, even though the deployment procedure and the
    role README tell an operator to dry-run and read what it proposes.

    caplog, not capsys: main() calls logging.basicConfig(stream=sys.stdout),
    which is a process-wide no-op after the first call in this test session,
    so it can end up bound to an earlier test's capsys stream rather than
    this one's. caplog attaches to the logger itself and sees every record
    regardless.
    """
    monkeypatch.setattr(
        "fleet_watchdog.probe.SshProbe.__call__",
        lambda self, board: Observation(
            board=board, ok=True, uptime_s=20 * 3600, in_use=False, error=None
        ),
    )
    with caplog.at_level(logging.INFO):
        rc = main(["--config", config_path, "--once", "--dry-run"])
    assert rc == 0
    # both occupied boards are well past max_uptime_hours
    assert any("cycling=2" in r.message for r in caplog.records)


def test_the_cli_runs_one_sweep_and_exits_zero(config_path, capsys):
    rc = main([
        "--config", config_path, "--once", "--dry-run",
        "--probe-command", "true",
    ])
    assert rc == 0


def test_the_cli_rejects_a_missing_config(tmp_path):
    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "nope.yml"), "--once"])
