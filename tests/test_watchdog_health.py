"""Failing loud: what the watchdog says, and when it gives up and exits.

A watchdog's failure mode is doing nothing, which is indistinguishable from
having nothing to do. Everything here exists so that a watchdog which has
stopped watching cannot be mistaken for a quiet one.
"""

import logging
import textwrap

import pytest

from fleet_watchdog.cli import main
from fleet_watchdog.config import WatchdogConfig
from fleet_watchdog.policy import (
    BoardState,
    Decision,
    Observation,
    Reason,
    decide,
)
from fleet_watchdog.service import Watchdog
from fleet_watchdog.switches import make_board


def cfg(**kw):
    base = dict(
        switches_config="/s", pib_network="10.21", ssh_key="/k", known_hosts="/kh",
    )
    base.update(kw)
    return WatchdogConfig(**base)


def obs(port, ok=True, uptime_s=60.0, in_use=False, error=None, internal=False):
    return Observation(
        board=make_board(2, port, "10.21"), ok=ok, uptime_s=uptime_s,
        in_use=in_use, error=error, internal=internal,
    )


# -- a sweep that finds nothing is not a sweep that found nothing wrong -----


def test_an_empty_sweep_is_unhealthy():
    """Every switch unreachable, a wrong community, an empty switch list and a
    healthy-but-unpopulated site all produce occupied=0. Only the last is
    benign, and it does not happen at a site that exists to host boards."""
    d = decide([], {}, cfg(), now=0.0, first_sweep=False)
    assert d.unhealthy is not None
    assert "no occupied ports" in d.unhealthy


def test_a_tripped_breaker_is_unhealthy():
    observations = [obs(p, ok=False, error="timed out") for p in range(1, 6)]
    d = decide(observations, {}, cfg(), now=0.0, first_sweep=False)
    assert d.breaker_tripped
    assert d.unhealthy is not None
    assert "5 of 5" in d.unhealthy


def test_a_normal_sweep_is_healthy():
    d = decide([obs(44), obs(48)], {}, cfg(), now=0.0, first_sweep=False)
    assert d.unhealthy is None


# -- a bug in this process is not a reason to cut power ---------------------


def test_an_internal_probe_error_never_cycles_a_board():
    """probe_all turns any exception into a board failure so one board cannot
    kill the sweep. Without this rule, a TypeError in our own code would cut
    mains power to real hardware two sweeps later."""
    board = make_board(2, 44, "10.21")
    states = {board: BoardState(consecutive_failures=5)}
    d = decide(
        [obs(44, ok=False, error="probe raised: TypeError()", internal=True)],
        states, cfg(fail_threshold=1), now=10_000.0, first_sweep=False,
    )
    assert d.cycles == ()


def test_an_internal_probe_error_still_counts_toward_the_breaker():
    """It is evidence the watchdog is broken, which is exactly what the
    breaker exists to notice."""
    observations = [
        obs(p, ok=False, error="probe raised: TypeError()", internal=True)
        for p in range(1, 6)
    ]
    d = decide(observations, {}, cfg(), now=0.0, first_sweep=False)
    assert d.breaker_tripped
    assert d.internal_errors == 5


def test_a_genuine_failure_still_cycles():
    board = make_board(2, 44, "10.21")
    states = {board: BoardState(consecutive_failures=1)}
    d = decide(
        [obs(44, ok=False, error="timed out"), obs(48)],
        states, cfg(fail_threshold=2), now=10_000.0, first_sweep=False,
    )
    assert [(b.port, r) for b, r in d.cycles] == [(44, Reason.UNREACHABLE)]


# -- what the journal actually says -----------------------------------------


def build(**kw):
    return Watchdog(cfg(**kw), probe=lambda b: None, sleep=lambda s: None,
                    clock=lambda: 0.0)


def test_a_failing_board_is_named_on_every_sweep_not_just_the_first(caplog):
    """The old code logged the board and the error only on the first failed
    probe. A board stuck failing for a week then showed up as a bare
    `failed=1` with no name and no reason, and `journalctl | grep pi-sw2-p44`
    -- the documented way to read one board's story -- returned nothing."""
    wd = build()
    failing = obs(44, ok=False, error="connection refused")
    seen = []
    for sweep in range(1, 4):
        wd.states[failing.board] = BoardState(consecutive_failures=sweep)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            wd.log_sweep(Decision(occupied=1, failed=1), [failing], [])
        seen.append([r.message for r in caplog.records])

    for sweep, messages in enumerate(seen, start=1):
        named = [m for m in messages if "pi-sw2-p44" in m]
        assert named, f"sweep {sweep} did not name the failing board"
        assert "connection refused" in named[0], f"sweep {sweep} dropped the reason"


def test_the_failure_line_shows_progress_towards_a_cycle(caplog):
    wd = build(fail_threshold=2)
    failing = obs(44, ok=False, error="timed out")
    wd.states[failing.board] = BoardState(consecutive_failures=1)
    with caplog.at_level(logging.WARNING):
        wd.log_sweep(Decision(occupied=1, failed=1), [failing], [])
    assert any("1 consecutive, cycles at 2" in r.message for r in caplog.records)


def test_the_failure_line_still_reads_correctly_past_the_threshold(caplog):
    """A board held off by its boot grace keeps counting past fail_threshold,
    so the wording must not imply a countdown."""
    wd = build(fail_threshold=2)
    failing = obs(44, ok=False, error="timed out")
    wd.states[failing.board] = BoardState(consecutive_failures=7)
    with caplog.at_level(logging.WARNING):
        wd.log_sweep(Decision(occupied=1, failed=1), [failing], [])
    assert any("7 consecutive, cycles at 2" in r.message for r in caplog.records)


def test_a_tripped_breaker_names_the_failing_boards(caplog):
    """The breaker is the line the README tells the operator to worry about,
    and it used to report a count and nothing else -- so the one event worth
    diagnosing was the one that said least."""
    wd = build()
    observations = [obs(p, ok=False, error="timed out") for p in (20, 21, 22)]
    with caplog.at_level(logging.ERROR):
        wd.log_sweep(
            Decision(occupied=3, failed=3, breaker_tripped=True, unhealthy="x"),
            observations, [],
        )
    listed = [r.message for r in caplog.records if "failing boards" in r.message]
    assert listed
    for port in (20, 21, 22):
        assert f"sw2/p{port}" in listed[0]


def test_a_breaker_does_not_also_emit_one_warning_per_board(caplog):
    """Under a breaker every board is failing; 35 per-board warnings a sweep
    would bury the breaker line itself."""
    wd = build()
    observations = [obs(p, ok=False, error="timed out") for p in range(1, 36)]
    with caplog.at_level(logging.WARNING):
        wd.log_sweep(
            Decision(occupied=35, failed=35, breaker_tripped=True, unhealthy="x"),
            observations, [],
        )
    per_board = [r for r in caplog.records if "probe failed" in r.message]
    assert per_board == []


def test_an_internal_error_is_not_logged_twice(caplog):
    """probe_all already logged it with a traceback. Repeating it at WARNING
    just pushes the traceback off the screen."""
    wd = build()
    broken = obs(44, ok=False, error="probe raised: TypeError()", internal=True)
    with caplog.at_level(logging.WARNING):
        wd.log_sweep(Decision(occupied=1, failed=1, internal_errors=1), [broken], [])
    assert [r for r in caplog.records if "probe failed" in r.message] == []


# -- giving up, so systemd can see it ---------------------------------------


def run_with_sweeps(wd, verdicts):
    """Drive run() through a fixed list of per-sweep `unhealthy` values."""
    remaining = list(verdicts)

    def fake_sweep(dry_run=False):
        if not remaining:
            wd._shutdown = True
            return Decision(occupied=1)
        return Decision(occupied=1, failed=0, unhealthy=remaining.pop(0))

    wd.sweep = fake_sweep
    return wd.run()


def test_the_daemon_exits_after_enough_consecutive_unhealthy_sweeps(caplog):
    wd = build(unhealthy_exit_after=3)
    with caplog.at_level(logging.ERROR):
        rc = run_with_sweeps(wd, ["breaker"] * 5)
    assert rc == 1
    assert any("consecutive unhealthy sweeps" in r.message for r in caplog.records)


def test_the_daemon_keeps_going_below_the_threshold():
    wd = build(unhealthy_exit_after=3)
    assert run_with_sweeps(wd, ["breaker", "breaker"]) == 0


def test_one_healthy_sweep_resets_the_streak(caplog):
    """Otherwise a watchdog that hiccups once an hour eventually exits for no
    reason, and the exit stops meaning anything."""
    wd = build(unhealthy_exit_after=3)
    with caplog.at_level(logging.WARNING):
        rc = run_with_sweeps(wd, ["breaker", "breaker", None, "breaker", "breaker"])
    assert rc == 0
    assert any("recovered after 2 unhealthy" in r.message for r in caplog.records)


def test_a_clean_shutdown_exits_zero():
    wd = build()
    assert run_with_sweeps(wd, []) == 0


def test_sigterm_during_the_run_exits_zero():
    wd = build()

    def fake_sweep(dry_run=False):
        wd._request_shutdown(15, None)
        return Decision(occupied=1)

    wd.sweep = fake_sweep
    assert wd.run() == 0


# -- and the same verdict from a single sweep -------------------------------


def test_once_exits_non_zero_when_the_sweep_is_unhealthy(tmp_path, caplog):
    """`--once` is what the role's verify step and the README's pre-enable
    gate both run. Reporting success having found nothing is the exact shape
    of a wrong community or a dead key."""
    switches = tmp_path / "switches.yml"
    switches.write_text("switches: []\n")
    watchdog = tmp_path / "watchdog.yml"
    watchdog.write_text(textwrap.dedent(f"""
        switches_config: {switches}
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
    """))
    with caplog.at_level(logging.ERROR):
        rc = main(["--config", str(watchdog), "--once", "--dry-run"])
    assert rc == 1
    assert any("unhealthy" in r.message for r in caplog.records)


def test_a_bad_config_is_fatal_rather_than_defaulted(tmp_path):
    watchdog = tmp_path / "watchdog.yml"
    watchdog.write_text(textwrap.dedent("""
        switches_config: /s
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
        max_uptime_hrs: 1
    """))
    with pytest.raises(Exception, match="max_uptime_hrs"):
        main(["--config", str(watchdog), "--once", "--dry-run"])
