"""The 8-hour rule: jitter, the in-use deferral, the 12-hour hard cap, and
the per-sweep stagger."""

import dataclasses
import os
import subprocess
import sys

from fleet_watchdog.config import WatchdogConfig
from fleet_watchdog.policy import BoardState, Observation, Reason, decide, jitter_seconds
from fleet_watchdog.switches import make_board

NOW = 1_000_000.0
H = 3600.0


def cfg(**overrides):
    base = WatchdogConfig(
        switches_config="/s", pib_network="10.21", ssh_key="/k", known_hosts="/kh"
    )
    return dataclasses.replace(base, **overrides)


def obs(board, uptime_h, in_use=False):
    return Observation(
        board=board, ok=True, uptime_s=uptime_h * H, in_use=in_use, error=None
    )


def test_jitter_is_stable_within_a_process():
    assert jitter_seconds(2, 42, 60) == jitter_seconds(2, 42, 60)


def _jitter_in_subprocess(seed):
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fleet_watchdog.policy import jitter_seconds; print(jitter_seconds(2, 42, 60))",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return out.stdout.strip()


def test_jitter_survives_a_process_restart():
    """crc32, not hash(): Python salts string hashes per process, and the unit
    runs with Restart=always, so a restart must not re-slot every board."""
    assert _jitter_in_subprocess("1") == _jitter_in_subprocess("2")


def test_jitter_differs_between_boards():
    values = {jitter_seconds(2, p, 60) for p in range(1, 49)}
    assert len(values) > 40  # a handful of collisions is fine, a constant is not


def test_jitter_stays_inside_the_configured_window():
    for port in range(1, 49):
        assert 0 <= jitter_seconds(2, port, 60) < 3600


def test_jitter_of_zero_minutes_is_zero():
    assert jitter_seconds(2, 42, 0) == 0.0


def test_a_board_under_the_threshold_is_left_alone():
    b = make_board(2, 42, "10.21")
    d = decide([obs(b, 7.0)], {b: BoardState()}, cfg(uptime_jitter_minutes=0), NOW, False)
    assert d.cycles == ()


def test_a_board_past_the_threshold_is_cycled():
    b = make_board(2, 42, "10.21")
    d = decide([obs(b, 8.5)], {b: BoardState()}, cfg(uptime_jitter_minutes=0), NOW, False)
    assert d.cycles == ((b, Reason.UPTIME),)


def test_jitter_delays_a_board_past_the_bare_threshold():
    """With an hour of jitter, at least one board just past 8 h is still waiting."""
    bs = [make_board(2, p, "10.21") for p in range(1, 49)]
    st = {b: BoardState() for b in bs}
    d = decide([obs(b, 8.01) for b in bs], st, cfg(max_scheduled_cycles_per_sweep=99), NOW, False)
    assert len(d.cycles) < len(bs)


def test_an_in_use_board_is_deferred_not_cycled():
    b = make_board(2, 42, "10.21")
    d = decide(
        [obs(b, 9.0, in_use=True)], {b: BoardState()}, cfg(uptime_jitter_minutes=0), NOW, False
    )
    assert d.cycles == ()
    assert len(d.deferred) == 1
    assert d.deferred[0][0] == b
    assert "in use" in d.deferred[0][1]


def test_an_in_use_board_past_the_hard_cap_is_cycled_anyway():
    b = make_board(2, 42, "10.21")
    d = decide(
        [obs(b, 12.5, in_use=True)], {b: BoardState()}, cfg(uptime_jitter_minutes=0), NOW, False
    )
    assert d.cycles == ((b, Reason.UPTIME),)
    assert d.deferred == ()


def test_a_board_at_exactly_the_hard_cap_is_cycled_not_deferred():
    """Pins the < vs <= boundary in _scheduled: obs.uptime_s < hard_cap is the
    deferral test, so a board exactly AT the cap fails it and is cycled."""
    b = make_board(2, 42, "10.21")
    d = decide(
        [obs(b, 12.0, in_use=True)], {b: BoardState()}, cfg(uptime_jitter_minutes=0), NOW, False
    )
    assert d.cycles == ((b, Reason.UPTIME),)
    assert d.deferred == ()


def test_the_hard_cap_still_respects_the_per_sweep_cap():
    """Three in-use boards are all past the 12 h hard cap in one sweep, but
    the default per-sweep cap is 2. _scheduled deliberately staggers even
    hard-capped boards: exactly two go, the two with the longest uptime, and
    the third is neither cycled nor dropped from the sweep's accounting (it
    sorts to the front of the next sweep's candidates, so it waits only
    minutes against a 12 h cap). This documents that behaviour so it cannot
    change silently."""
    bs = [make_board(2, p, "10.21") for p in range(1, 4)]
    st = {b: BoardState() for b in bs}
    obs_list = [obs(bs[0], 13.0, in_use=True), obs(bs[1], 15.0, in_use=True),
                obs(bs[2], 14.0, in_use=True)]
    d = decide(obs_list, st, cfg(uptime_jitter_minutes=0), NOW, False)
    assert d.cycles == ((bs[1], Reason.UPTIME), (bs[2], Reason.UPTIME))
    assert d.deferred == ()
    assert d.in_use == 3
    assert d.occupied == 3


def test_scheduled_cycles_are_capped_per_sweep():
    bs = [make_board(2, p, "10.21") for p in range(1, 11)]
    st = {b: BoardState() for b in bs}
    d = decide([obs(b, 20.0) for b in bs], st, cfg(uptime_jitter_minutes=0), NOW, False)
    assert len(d.cycles) == 2


def test_the_longest_running_boards_go_first():
    bs = [make_board(2, p, "10.21") for p in range(1, 6)]
    st = {b: BoardState() for b in bs}
    obs_list = [obs(b, 9.0 + i) for i, b in enumerate(bs)]
    d = decide(obs_list, st, cfg(uptime_jitter_minutes=0), NOW, False)
    assert [b for b, _ in d.cycles] == [bs[4], bs[3]]


def test_a_board_inside_its_boot_grace_is_not_scheduled():
    b = make_board(2, 42, "10.21")
    st = {b: BoardState(last_cycle=NOW - 10)}
    d = decide([obs(b, 20.0)], st, cfg(uptime_jitter_minutes=0), NOW, False)
    assert d.cycles == ()


def test_the_first_sweep_schedules_nothing():
    b = make_board(2, 42, "10.21")
    d = decide([obs(b, 20.0)], {b: BoardState()}, cfg(uptime_jitter_minutes=0), NOW, True)
    assert d.cycles == ()


def test_the_breaker_suppresses_scheduled_cycles_too():
    bs = [make_board(2, p, "10.21") for p in range(1, 11)]
    st = {b: BoardState() for b in bs}
    observations = [
        Observation(board=b, ok=False, uptime_s=None, in_use=False, error="x")
        for b in bs[:6]
    ] + [obs(b, 20.0) for b in bs[6:]]
    d = decide(observations, st, cfg(uptime_jitter_minutes=0), NOW, False)
    assert d.breaker_tripped is True
    assert d.cycles == ()


def test_thresholds_are_configurable():
    b = make_board(2, 42, "10.21")
    d = decide(
        [obs(b, 2.5)],
        {b: BoardState()},
        cfg(uptime_jitter_minutes=0, max_uptime_hours=2),
        NOW,
        False,
    )
    assert d.cycles == ((b, Reason.UPTIME),)
