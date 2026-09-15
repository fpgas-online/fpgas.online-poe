"""The sweep loop: enumerate, probe, decide, act, log.

Reporting is the journal and nothing else, by design, so all state lives in
this object's memory. Nothing here needs to survive a restart: board uptime is
read from the board itself, and the first sweep after any start only observes,
so a restart costs at most one sweep of latency.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from switch_setup.cli import load_specs

from .config import WatchdogConfig
from .cycle import cycle_port
from .policy import BoardState, Decision, Observation, Reason, decide
from .probe import SshProbe, probe_all
from .switches import Board, community_for, occupied_boards, open_switch

log = logging.getLogger("fleet_watchdog")


class Watchdog:
    def __init__(
        self,
        cfg: WatchdogConfig,
        probe: Callable[[Board], Observation] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.probe = probe or SshProbe(cfg)
        self.sleep = sleep
        self.clock = clock
        self.states: dict[Board, BoardState] = {}
        self.first_sweep = True
        self._switches: dict[int, object] = {}
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

    def enumerate(self) -> list[Board]:
        boards: list[Board] = []
        for spec in self.specs():
            try:
                sw = self.switch(spec.index)
                boards.extend(
                    occupied_boards(
                        sw,
                        spec,
                        self.cfg.exclude.get(spec.index, frozenset()),
                        self.cfg.pib_network,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one switch must not end the sweep
                # Dropping a switch's boards from the sweep is safe: an absent
                # board is never cycled. It also feeds the breaker, because the
                # remaining switch's failures are measured against a smaller
                # fleet.
                log.error("switch %s unreachable, skipping its ports: %s", spec.index, exc)
        return boards

    # -- one sweep --------------------------------------------------------

    def sweep(self, dry_run: bool = False) -> Decision:
        boards = self.enumerate()
        observations = probe_all(self.probe, boards, self.cfg.probe_concurrency)
        for obs in observations:
            log.debug(
                "%s ok=%s uptime=%s in_use=%s %s",
                obs.board, obs.ok,
                f"{obs.uptime_s / 3600:.1f}h" if obs.uptime_s is not None else "-",
                obs.in_use, obs.error or "",
            )
            if not obs.ok and self.states.get(obs.board, BoardState()).consecutive_failures == 0:
                log.warning("%s first failed probe: %s", obs.board, obs.error)

        decision = decide(
            observations, self.states, self.cfg, self.clock(), self.first_sweep
        )
        self.first_sweep = False

        log.info(
            "sweep: occupied=%d ok=%d failed=%d in_use=%d cycling=%d deferred=%d%s",
            decision.occupied,
            decision.occupied - decision.failed,
            decision.failed,
            decision.in_use,
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
        for board, why in decision.deferred:
            log.info("%s deferring scheduled cycle: %s", board, why)

        if not dry_run:
            self.run_cycles(decision)
        return decision

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
        finally:
            # The grace starts whether or not the cycle completed. A port that
            # failed to come back must not be hammered every sweep either.
            self.states.setdefault(board, BoardState()).last_cycle = self.clock()

    # -- the loop ---------------------------------------------------------

    def run(self) -> None:
        log.info(
            "fleet watchdog starting: interval=%.0fs threshold=%d off=%.0fs "
            "uptime=%.0fh cap=%.0fh; the first sweep only observes",
            self.cfg.interval, self.cfg.fail_threshold, self.cfg.poe_off_seconds,
            self.cfg.max_uptime_hours, self.cfg.hard_cap_hours,
        )
        while True:
            started = self.clock()
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - the loop outlives any one sweep
                log.exception("sweep failed")
            # Sweeps never overlap. A sweep that overruns simply starts the
            # next one immediately rather than stacking.
            self.sleep(max(0.0, self.cfg.interval - (self.clock() - started)))
