"""What to cycle, and why. Pure: no I/O, no clock, no sleeping.

Every rule here is a decision the service then carries out. Keeping the rules
free of I/O is what lets them be tested exhaustively with a fake clock, which
matters because the failure mode of getting them wrong is rebooting a fleet.
"""

from __future__ import annotations

import enum
import zlib
from dataclasses import dataclass
from typing import Iterable, MutableMapping

from fleet_watchdog.config import WatchdogConfig
from fleet_watchdog.switches import Board


@dataclass(frozen=True)
class Observation:
    board: Board
    ok: bool
    uptime_s: float | None
    in_use: bool
    error: str | None


@dataclass
class BoardState:
    consecutive_failures: int = 0
    last_cycle: float | None = None


class Reason(str, enum.Enum):
    UNREACHABLE = "unreachable"
    UPTIME = "uptime"


@dataclass(frozen=True)
class Decision:
    cycles: tuple[tuple[Board, Reason], ...] = ()
    deferred: tuple[tuple[Board, str], ...] = ()
    breaker_tripped: bool = False
    occupied: int = 0
    failed: int = 0
    in_use: int = 0


def jitter_seconds(switch: int, port: int, jitter_minutes: float) -> float:
    """A stable per-board offset added to the uptime threshold.

    crc32, not hash(): Python salts string hashes per process, so hash() would
    move every board's slot on each restart and defeat the staggering.
    """
    if jitter_minutes <= 0:
        return 0.0
    span = int(jitter_minutes * 60)
    return float(zlib.crc32(f"{switch}:{port}".encode()) % span)


def _in_boot_grace(state: BoardState, cfg: WatchdogConfig, now: float) -> bool:
    return state.last_cycle is not None and (now - state.last_cycle) < cfg.boot_grace


def decide(
    observations: Iterable[Observation],
    states: MutableMapping[Board, BoardState],
    cfg: WatchdogConfig,
    now: float,
    first_sweep: bool,
) -> Decision:
    """Fold one sweep's observations into per-board state and an action list.

    Updates each board's consecutive_failures in place. Never touches
    last_cycle: only a cycle that actually ran may start a boot grace, and this
    function does not run cycles.
    """
    observations = list(observations)
    for obs in observations:
        state = states.setdefault(obs.board, BoardState())
        state.consecutive_failures = 0 if obs.ok else state.consecutive_failures + 1

    failed = [o for o in observations if not o.ok]
    counts = {
        "occupied": len(observations),
        "failed": len(failed),
        "in_use": sum(1 for o in observations if o.ok and o.in_use),
    }

    # The watchdog is far likelier to be broken than most of the fleet is. A
    # wrong key, a wrong user, a routing fault or a changed NFS root host key
    # all look exactly like a dead fleet from here.
    if len(failed) >= cfg.breaker_min_failures and len(failed) > cfg.breaker_fraction * len(
        observations
    ):
        return Decision(breaker_tripped=True, **counts)

    if first_sweep:
        return Decision(**counts)

    cycles: list[tuple[Board, Reason]] = []
    deferred: list[tuple[Board, str]] = []

    for obs in failed:
        state = states[obs.board]
        if state.consecutive_failures < cfg.fail_threshold:
            continue
        # Without this, a board taking three minutes to boot would be cut again
        # mid-boot every sweep and never come up.
        if _in_boot_grace(state, cfg, now):
            continue
        cycles.append((obs.board, Reason.UNREACHABLE))

    cycles.extend(_scheduled(observations, states, cfg, now, deferred))
    return Decision(
        cycles=tuple(cycles), deferred=tuple(deferred), **counts
    )


def _scheduled(
    observations: list[Observation],
    states: MutableMapping[Board, BoardState],
    cfg: WatchdogConfig,
    now: float,
    deferred: list[tuple[Board, str]],
) -> list[tuple[Board, Reason]]:
    """Healthy boards that have been up too long, oldest first, capped."""
    hard_cap = cfg.hard_cap_hours * 3600
    candidates: list[tuple[float, Board]] = []
    for obs in observations:
        if not obs.ok or obs.uptime_s is None:
            continue
        if _in_boot_grace(states[obs.board], cfg, now):
            continue
        threshold = cfg.max_uptime_hours * 3600 + jitter_seconds(
            obs.board.switch, obs.board.port, cfg.uptime_jitter_minutes
        )
        if obs.uptime_s < threshold:
            continue
        if obs.in_use and obs.uptime_s < hard_cap:
            deferred.append(
                (obs.board, f"in use, up {obs.uptime_s / 3600:.1f} h, cap {cfg.hard_cap_hours} h")
            )
            continue
        candidates.append((obs.uptime_s, obs.board))

    candidates.sort(key=lambda c: c[0], reverse=True)
    chosen = candidates[: cfg.max_scheduled_cycles_per_sweep]
    return [(board, Reason.UPTIME) for _, board in chosen]
