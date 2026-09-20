"""The sweep loop: scan, recover, probe, decide, act, log.

Reporting is the journal and nothing else, by design, so all state lives in
this object's memory. Nothing here needs to survive a restart: board uptime is
read from the board itself, a port left in a bad state is found again by the
next scan, and the first sweep after any start only observes -- so a restart
costs at most one sweep of latency.

That matters because this process is meant to exit when the world stops making
sense, rather than logging into the void forever. systemd restarts it; if the
condition persists, StartLimitBurst puts the unit in `failed`, which is the
only signal anything outside the journal can actually read.
"""

from __future__ import annotations

import dataclasses
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from netgear_switch.snmp_write import PoeCycleTimeouts

from switch_setup.cli import load_specs

from .config import WatchdogConfig
from .cycle import cycle_port
from .policy import (
    BoardState,
    Decision,
    Observation,
    Reason,
    Recovery,
    decide,
    ports_to_recover,
)
from .probe import SshProbe, probe_all
from .switches import Board, PortSnapshot, community_for, open_switch, scan_ports

log = logging.getLogger("fleet_watchdog")

#: How many boards to name before truncating a summary line.
_NAME_LIMIT = 12


def _summarise(boards: list[Board]) -> str:
    names = [f"sw{b.switch}/p{b.port}" for b in sorted(boards, key=lambda b: (b.switch, b.port))]
    if len(names) <= _NAME_LIMIT:
        return ", ".join(names)
    return f"{', '.join(names[:_NAME_LIMIT])} and {len(names) - _NAME_LIMIT} more"


class Watchdog:
    def __init__(
        self,
        cfg: WatchdogConfig,
        probe: Callable[[Board], Observation] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poe_timeouts: PoeCycleTimeouts | None = None,
    ) -> None:
        self.cfg = cfg
        self.probe = probe or SshProbe(cfg)
        self.sleep = sleep
        self.clock = clock
        self.states: dict[Board, BoardState] = {}
        self.first_sweep = True
        self._switches: dict[int, object] = {}
        # Polling deadlines for clear_poe_fault. The library's defaults are
        # right for real hardware; tests inject tiny ones.
        self.poe_timeouts = poe_timeouts or PoeCycleTimeouts()
        # Set by _request_shutdown (installed on SIGTERM/SIGINT in run()) and
        # checked between sweeps. The role's own handler restarts this service
        # whenever the config, env file or unit changes, so a routine converge
        # can send SIGTERM mid-dwell; without this the loop would just die
        # there instead of finishing the sweep in progress and exiting clean.
        self._shutdown = False
        # run_cycles fans cycle() out across threads, and two boards on the
        # same switch in one sweep is the common case. Without this lock, two
        # threads can both pass the "not cached yet" check before either
        # assigns, each building its own SyncSwitch; one handle is then
        # silently discarded and its connection leaked. Held across
        # open_switch deliberately: handle creation is already effectively
        # serial, and the lock is uncontended once a handle exists.
        self._switch_lock = threading.Lock()

    # -- switch access ----------------------------------------------------

    def specs(self):
        return load_specs(self.cfg.switches_config)

    def switch(self, index: int):
        with self._switch_lock:
            if index not in self._switches:
                spec = next(s for s in self.specs() if s.index == index)
                self._switches[index] = open_switch(spec, community_for(index))
            return self._switches[index]

    def scan(self) -> tuple[list[PortSnapshot], int]:
        """Every watchable access port on every switch, with its PoE state.

        A switch that cannot be read costs its ports from this sweep and says
        so every time. It is deliberately NOT fatal on its own: exiting would
        stop watching the switches that ARE answering, and a restart cannot
        fix an unreachable switch. When no switch answers at all the sweep
        finds nothing, and that IS fatal -- see decide().

        self.specs() is outside the guard on purpose: an unreadable switch
        list is structural, and run() turns that into an exit.
        """
        snapshots: list[PortSnapshot] = []
        failures = 0
        for spec in self.specs():
            try:
                sw = self.switch(spec.index)
                snapshots.extend(
                    scan_ports(
                        sw,
                        spec,
                        self.cfg.exclude.get(spec.index, frozenset()),
                        self.cfg.pib_network,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one switch must not end the sweep
                failures += 1
                log.error("switch %s unreadable, skipping its ports: %s", spec.index, exc)
        return snapshots, failures

    # -- putting ports back into service ----------------------------------

    def recover(self, snapshots: list[PortSnapshot], dry_run: bool = False) -> None:
        """Clear PoE faults and re-enable ports left switched off.

        Runs before probing: a port that is not delivering has no board to
        probe, so recovery is the only thing that can ever bring it back.
        """
        to_recover, gave_up = ports_to_recover(
            snapshots, self.states, self.cfg, self.clock()
        )
        for board, why in gave_up:
            log.error("%s %s", board, why)
        if not to_recover:
            return
        if dry_run:
            for board, why in to_recover:
                log.warning("%s would recover: %s", board, why.value)
            return
        workers = max(1, min(self.cfg.cycle_concurrency, len(to_recover)))
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(lambda item: self.recover_one(*item), to_recover))

    def recover_one(self, board: Board, why: Recovery) -> None:
        state = self.states.setdefault(board, BoardState())
        state.recovery_attempts += 1
        log.warning(
            "%s recovering (%s), attempt %d of %d",
            board, why.value, state.recovery_attempts, self.cfg.max_recovery_attempts,
        )
        try:
            sw = self.switch(board.switch)
            if why is Recovery.FAULT:
                # Re-arms the port and polls until detect leaves FAULT for
                # DELIVERING or SEARCHING. Raises if it never does.
                sw.clear_poe_fault(board.port, timeouts=self.poe_timeouts)
            else:
                sw.set_poe(board.port, True)
            log.warning("%s recovered: %s cleared, port back in service", board, why.value)
        except Exception as exc:  # noqa: BLE001 - one port must not end the sweep
            log.error("%s recovery failed (%s): %s", board, why.value, exc)
        finally:
            # Whether or not it worked, give the port the boot grace before
            # anything else touches it.
            state.last_cycle = self.clock()

    # -- one sweep --------------------------------------------------------

    def sweep(self, dry_run: bool = False) -> Decision:
        snapshots, switch_errors = self.scan()
        self.recover(snapshots, dry_run=dry_run)

        boards = [s.board for s in snapshots if s.delivering]
        observations = probe_all(self.probe, boards, self.cfg.probe_concurrency)
        decision = decide(
            observations, self.states, self.cfg, self.clock(), self.first_sweep
        )
        self.first_sweep = False

        if decision.unhealthy and switch_errors:
            decision = dataclasses.replace(
                decision,
                unhealthy=f"{decision.unhealthy} ({switch_errors} switch(es) unreadable)",
            )

        self.log_sweep(decision, observations, snapshots)
        if not dry_run:
            self.run_cycles(decision)
        return decision

    def log_sweep(
        self,
        decision: Decision,
        observations: list[Observation],
        snapshots: list[PortSnapshot],
    ) -> None:
        """Say what happened, every sweep.

        A failing board is named on EVERY sweep it fails, not just the first.
        Logging it once meant a board stuck failing for a week showed up as a
        bare `failed=1` with no name and no reason, and the whole point of the
        per-board prefix is that `journalctl | grep pi-sw2-p20` tells that
        board's story without the service being restarted in verbose mode.
        """
        for obs in observations:
            if obs.ok:
                log.debug(
                    "%s ok uptime=%s in_use=%s",
                    obs.board,
                    f"{obs.uptime_s / 3600:.1f}h" if obs.uptime_s is not None else "-",
                    obs.in_use,
                )
            elif obs.internal:
                # probe_all already logged this one with its traceback; saying
                # it twice at two severities just buries the traceback.
                continue
            elif not decision.breaker_tripped:
                state = self.states.get(obs.board, BoardState())
                log.warning(
                    "%s probe failed (%d of %d before a cycle): %s",
                    obs.board, state.consecutive_failures, self.cfg.fail_threshold,
                    obs.error,
                )

        faulted = [s.board for s in snapshots if s.faulted]
        off = [s.board for s in snapshots if s.powered_off]
        log.info(
            "sweep: ports=%d occupied=%d ok=%d failed=%d internal=%d in_use=%d "
            "faulted=%d off=%d cycling=%d deferred=%d%s",
            len(snapshots),
            decision.occupied,
            decision.occupied - decision.failed,
            decision.failed,
            decision.internal_errors,
            decision.in_use,
            len(faulted),
            len(off),
            len(decision.cycles),
            len(decision.deferred),
            " BREAKER" if decision.breaker_tripped else "",
        )
        if decision.breaker_tripped:
            log.error(
                "circuit breaker: %d of %d boards failed; cycling nothing. "
                "Suspect the watchdog, not the fleet: key, user, routing, switch "
                "or the NFS root host key.",
                decision.failed, decision.occupied,
            )
            # One line rather than one per board: under a breaker every board
            # is failing, and 35 warnings a sweep buries the breaker itself.
            log.error(
                "circuit breaker: failing boards: %s",
                _summarise([o.board for o in observations if not o.ok]),
            )
        for board, why in decision.deferred:
            log.info("%s deferring scheduled cycle: %s", board, why)

    def run_cycles(self, decision: Decision) -> None:
        if not decision.cycles:
            return
        workers = max(1, min(self.cfg.cycle_concurrency, len(decision.cycles)))
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(lambda item: self.cycle(*item), decision.cycles))

    def cycle(self, board: Board, reason: Reason) -> None:
        log.warning("%s cycling PoE: %s", board, reason.value)
        try:
            cycle_port(
                self.switch(board.switch),
                board.port,
                self.cfg.poe_off_seconds,
                sleep=self.sleep,
            )
        except Exception as exc:  # noqa: BLE001 - includes CycleError
            log.error("%s cycle failed: %s", board, exc)
            # A cycle that dies between the off and the on leaves the port
            # dark. Try to undo it here for speed, but this is no longer the
            # last line of defence: the next sweep's scan sees an
            # admin-disabled port and recovers it (see ports_to_recover).
            try:
                self.switch(board.switch).set_poe(board.port, True)
                log.error("%s power restored after the failed cycle", board)
            except Exception as restore_exc:  # noqa: BLE001
                log.error(
                    "%s COULD NOT RESTORE POWER (%s); the next sweep will try "
                    "again. If it stays off, recover with the board page's "
                    "Reset button or fpgas-switch PoE control.",
                    board, restore_exc,
                )
        finally:
            # The grace starts whether or not the cycle completed. A port that
            # failed to come back must not be hammered every sweep either.
            self.states.setdefault(board, BoardState()).last_cycle = self.clock()

    # -- the loop ---------------------------------------------------------

    def _request_shutdown(self, signum, frame) -> None:  # noqa: ARG002 - signal handler signature
        log.info("received signal %d, stopping after the current sweep", signum)
        self._shutdown = True

    def run(self) -> int:
        """Sweep until told to stop, or until the world stops making sense.

        Returns the process exit code. Non-zero means "restart me": either a
        sweep raised, which is structural (a bad config, an unreadable switch
        list, a bug), or enough consecutive sweeps were unhealthy that
        continuing to log into the journal would be the only thing this
        service was achieving.
        """
        # Installed here, not __init__: signal.signal only works from the
        # main thread, and run() is where that assumption actually holds.
        signal.signal(signal.SIGTERM, self._request_shutdown)
        signal.signal(signal.SIGINT, self._request_shutdown)
        log.info(
            "fleet watchdog starting: interval=%.0fs threshold=%d off=%.0fs "
            "uptime=%.0fh cap=%.0fh exit-after=%d unhealthy sweeps; "
            "the first sweep only observes",
            self.cfg.interval, self.cfg.fail_threshold, self.cfg.poe_off_seconds,
            self.cfg.max_uptime_hours, self.cfg.hard_cap_hours,
            self.cfg.unhealthy_exit_after,
        )
        unhealthy_streak = 0
        while not self._shutdown:
            started = self.clock()
            try:
                decision = self.sweep()
            except Exception:
                log.exception(
                    "sweep failed; exiting so systemd restarts the service"
                )
                return 1
            if decision.unhealthy:
                unhealthy_streak += 1
                log.error(
                    "unhealthy sweep %d of %d: %s",
                    unhealthy_streak, self.cfg.unhealthy_exit_after, decision.unhealthy,
                )
                if unhealthy_streak >= self.cfg.unhealthy_exit_after:
                    log.error(
                        "%d consecutive unhealthy sweeps; exiting so systemd "
                        "restarts the service. If this repeats, systemd will "
                        "mark the unit failed, which is the intent: this "
                        "watchdog is not watching anything.",
                        unhealthy_streak,
                    )
                    return 1
            else:
                if unhealthy_streak:
                    log.warning(
                        "recovered after %d unhealthy sweep(s)", unhealthy_streak
                    )
                unhealthy_streak = 0
            if self._shutdown:
                break
            # Sweeps never overlap. A sweep that overruns simply starts the
            # next one immediately rather than stacking.
            self.sleep(max(0.0, self.cfg.interval - (self.clock() - started)))
        log.info("fleet watchdog stopping")
        return 0
