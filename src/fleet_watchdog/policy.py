"""What to cycle, what to clear, and why. Pure: no I/O, no clock, no sleeping.

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
from fleet_watchdog.switches import Board, PortSnapshot


@dataclass(frozen=True)
class Observation:
    board: Board
    ok: bool
    uptime_s: float | None
    in_use: bool
    error: str | None
    # True when the probe itself raised rather than the board failing to
    # answer. Counts as a failure (it is evidence the watchdog is broken, so
    # it should feed the breaker) but never justifies cutting power: a bug in
    # this process is not a reason to reboot somebody's hardware.
    internal: bool = False


@dataclass
class BoardState:
    consecutive_failures: int = 0
    last_cycle: float | None = None
    # Consecutive failed attempts to bring this port back into service, by
    # either route. Reset the moment the port is seen delivering again.
    recovery_attempts: int = 0


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
    internal_errors: int = 0
    in_use: int = 0
    # Why this sweep counts as unhealthy, or None. The service exits after
    # cfg.unhealthy_exit_after consecutive unhealthy sweeps so that systemd
    # restarts it and, if the condition persists, gives up and marks the unit
    # failed -- which is the only state anything outside the journal can see.
    unhealthy: str | None = None


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


class Recovery(str, enum.Enum):
    """Why a port needs putting back into service."""

    FAULT = "PoE fault"
    POWERED_OFF = "admin-disabled"


def ports_to_recover(
    snapshots: Iterable[PortSnapshot],
    states: MutableMapping[Board, BoardState],
    cfg: WatchdogConfig,
    now: float,
) -> tuple[list[tuple[Board, Recovery]], list[tuple[Board, str]]]:
    """Split unusable ports into (recover now, report but leave alone).

    Two states need putting right, and neither is ever seen again once it
    happens, because a port that is not DELIVERING is not a board to probe:

    * FAULT -- the switch cut the port to protect itself from an over-current
      or a short. Never a deliberate operator state, so clearing it needs no
      permission.
    * admin-disabled -- somebody, very possibly this service dying mid-cycle,
      told the switch to stop supplying the port. Left alone it stays dark
      forever.

    Excluded ports never reach here: scan_ports drops them, so the exclusion
    list remains the way to tell the watchdog to keep its hands off a port.

    A port that will not stay recovered is a hardware problem, and re-arming
    it every five minutes is both useless and unkind to the hardware, so give
    up after cfg.max_recovery_attempts and keep saying so.
    """
    recover: list[tuple[Board, Recovery]] = []
    give_up: list[tuple[Board, str]] = []
    for snap in snapshots:
        if snap.unreadable:
            # We do not know what is wrong, so we must not guess at a fix --
            # but saying nothing would drop the port out of every rule here.
            # Report it every sweep and leave the recovery budget untouched.
            give_up.append((
                snap.board,
                "PoE state unreadable (detect=unknown); not acting on it",
            ))
            continue
        if snap.faulted:
            why = Recovery.FAULT
        elif snap.powered_off:
            why = Recovery.POWERED_OFF
        else:
            # Delivering, or SEARCHING: a live port with nothing drawing on
            # it, which is what an empty socket and a successfully cleared
            # fault both look like. Either way the port is in service, so
            # forget the attempts -- a fault months from now deserves a full
            # budget rather than inheriting an exhausted one.
            states.setdefault(snap.board, BoardState()).recovery_attempts = 0
            continue
        state = states.setdefault(snap.board, BoardState())
        if state.recovery_attempts >= cfg.max_recovery_attempts:
            give_up.append((
                snap.board,
                f"{why.value}, still not delivering after "
                f"{state.recovery_attempts} recovery attempts; needs on-site "
                f"attention",
            ))
            continue
        if _in_boot_grace(state, cfg, now):
            continue
        recover.append((snap.board, why))
    return recover, give_up


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
        "internal_errors": sum(1 for o in observations if o.internal),
        "in_use": sum(1 for o in observations if o.ok and o.in_use),
    }

    # Finding nothing is not the same as finding nothing wrong. Every switch
    # unreachable, a wrong community, an empty switch list and a healthy but
    # unpopulated site all produce occupied=0, and only the last is benign --
    # and that one does not happen at a site that exists to host boards.
    if not observations:
        return Decision(unhealthy="no occupied ports found on any switch", **counts)

    # The watchdog is far likelier to be broken than most of the fleet is. A
    # wrong key, a wrong user, a routing fault or a changed NFS root host key
    # all look exactly like a dead fleet from here.
    if len(failed) >= cfg.breaker_min_failures and len(failed) > cfg.breaker_fraction * len(
        observations
    ):
        return Decision(
            breaker_tripped=True,
            unhealthy=f"circuit breaker: {len(failed)} of {len(observations)} boards failed",
            **counts,
        )

    if first_sweep:
        return Decision(**counts)

    cycles: list[tuple[Board, Reason]] = []
    deferred: list[tuple[Board, str]] = []

    for obs in failed:
        state = states[obs.board]
        if obs.internal:
            # The watchdog broke, not the board. Reported loudly by the probe
            # layer; acting on it would cut power over our own bug.
            continue
        if state.consecutive_failures < cfg.fail_threshold:
            continue
        # Without this, a board taking three minutes to boot would be cut again
        # mid-boot every sweep and never come up.
        if _in_boot_grace(state, cfg, now):
            continue
        cycles.append((obs.board, Reason.UNREACHABLE))

    cycles.extend(_scheduled(observations, states, cfg, now, deferred))
    return Decision(cycles=tuple(cycles), deferred=tuple(deferred), **counts)


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
