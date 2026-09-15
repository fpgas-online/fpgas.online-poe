"""Health-driven cycles: the failure threshold, the boot grace, the breaker,
and the observe-only first sweep. No switch, no network, no real clock."""

import dataclasses

import pytest

from fleet_watchdog.config import WatchdogConfig
from fleet_watchdog.policy import BoardState, Observation, Reason, decide
from fleet_watchdog.switches import make_board

NOW = 1_000_000.0


def cfg(**overrides):
    base = WatchdogConfig(
        switches_config="/s", pib_network="10.21", ssh_key="/k", known_hosts="/kh"
    )
    return dataclasses.replace(base, **overrides)


def boards(n, switch=2):
    return [make_board(switch, port, "10.21") for port in range(1, n + 1)]


def ok(board, uptime_s=3600.0, in_use=False):
    return Observation(board=board, ok=True, uptime_s=uptime_s, in_use=in_use, error=None)


def dead(board, error="timed out"):
    return Observation(board=board, ok=False, uptime_s=None, in_use=False, error=error)


def states(bs):
    return {b: BoardState() for b in bs}


def test_one_failure_does_not_cycle():
    bs = boards(5)
    st = states(bs)
    d = decide([dead(bs[0])] + [ok(b) for b in bs[1:]], st, cfg(), NOW, first_sweep=False)
    assert d.cycles == ()
    assert st[bs[0]].consecutive_failures == 1


def test_two_consecutive_failures_cycle():
    bs = boards(5)
    st = states(bs)
    obs = [dead(bs[0])] + [ok(b) for b in bs[1:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW + 300, first_sweep=False)
    assert d.cycles == ((bs[0], Reason.UNREACHABLE),)
    assert st[bs[0]].consecutive_failures == 2


def test_a_success_resets_the_failure_count():
    bs = boards(5)
    st = states(bs)
    decide([dead(bs[0])] + [ok(b) for b in bs[1:]], st, cfg(), NOW, first_sweep=False)
    decide([ok(b) for b in bs], st, cfg(), NOW + 300, first_sweep=False)
    assert st[bs[0]].consecutive_failures == 0


def test_a_board_inside_its_boot_grace_is_not_cycled_again():
    bs = boards(5)
    st = states(bs)
    st[bs[0]].last_cycle = NOW - 100  # grace is 300 s
    obs = [dead(bs[0])] + [ok(b) for b in bs[1:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW, first_sweep=False)
    assert st[bs[0]].consecutive_failures == 2
    assert d.cycles == ()


def test_a_board_past_its_boot_grace_is_cycled_again():
    bs = boards(5)
    st = states(bs)
    st[bs[0]].last_cycle = NOW - 301
    obs = [dead(bs[0])] + [ok(b) for b in bs[1:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW, first_sweep=False)
    assert d.cycles == ((bs[0], Reason.UNREACHABLE),)


def test_the_first_sweep_cycles_nothing_but_still_counts():
    bs = boards(5)
    st = states(bs)
    obs = [dead(bs[0]), dead(bs[1])] + [ok(b) for b in bs[2:]]
    decide(obs, st, cfg(), NOW, first_sweep=True)
    d = decide(obs, st, cfg(), NOW + 300, first_sweep=True)
    assert d.cycles == ()
    assert st[bs[0]].consecutive_failures == 2


def test_health_cycles_are_not_capped_per_sweep():
    bs = boards(10)
    st = states(bs)
    obs = [dead(b) for b in bs[:4]] + [ok(b) for b in bs[4:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW + 300, first_sweep=False)
    assert len(d.cycles) == 4


def test_the_breaker_trips_when_most_of_the_fleet_fails():
    bs = boards(10)
    st = states(bs)
    obs = [dead(b) for b in bs[:6]] + [ok(b) for b in bs[6:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW + 300, first_sweep=False)
    assert d.breaker_tripped is True
    assert d.cycles == ()
    assert d.failed == 6
    assert d.occupied == 10


def test_the_breaker_does_not_trip_at_exactly_half():
    bs = boards(10)
    st = states(bs)
    obs = [dead(b) for b in bs[:5]] + [ok(b) for b in bs[5:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW + 300, first_sweep=False)
    assert d.breaker_tripped is False
    assert len(d.cycles) == 5


def test_the_count_floor_lets_a_small_site_recover_itself():
    """Two boards, both dead: the fraction test would trip, the floor of 3 saves it."""
    bs = boards(2)
    st = states(bs)
    obs = [dead(b) for b in bs]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    d = decide(obs, st, cfg(), NOW + 300, first_sweep=False)
    assert d.breaker_tripped is False
    assert len(d.cycles) == 2


def test_the_breaker_still_advances_failure_counts():
    bs = boards(10)
    st = states(bs)
    obs = [dead(b) for b in bs[:6]] + [ok(b) for b in bs[6:]]
    decide(obs, st, cfg(), NOW, first_sweep=False)
    assert st[bs[0]].consecutive_failures == 1


def test_a_board_with_no_prior_state_is_tracked():
    bs = boards(3)
    st = {}
    decide([dead(b) for b in bs], st, cfg(), NOW, first_sweep=False)
    assert set(st) == set(bs)


def test_the_decision_counts_reachable_and_in_use_boards():
    bs = boards(4)
    st = states(bs)
    obs = [ok(bs[0], in_use=True), ok(bs[1]), dead(bs[2]), dead(bs[3])]
    d = decide(obs, st, cfg(), NOW, first_sweep=False)
    assert (d.occupied, d.failed, d.in_use) == (4, 2, 1)


@pytest.mark.parametrize("threshold,expected", [(1, 1), (3, 0)])
def test_the_failure_threshold_is_configurable(threshold, expected):
    bs = boards(5)
    st = states(bs)
    obs = [dead(bs[0])] + [ok(b) for b in bs[1:]]
    d = decide(obs, st, cfg(fail_threshold=threshold), NOW, first_sweep=False)
    assert len(d.cycles) == expected
