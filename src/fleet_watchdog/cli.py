"""fpgas-fleet-watchdog: sweep the switch ports and keep the fleet alive."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .policy import Observation
from .service import Watchdog


def _stub_probe(board):
    return Observation(board=board, ok=True, uptime_s=0.0, in_use=False, error=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to watchdog.yml")
    ap.add_argument("--once", action="store_true", help="run one sweep and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and log, but cycle nothing")
    ap.add_argument("--verbose", action="store_true", help="log every probe result")
    ap.add_argument("--probe-command", choices=["true"], default=None,
                    help="testing only: treat every board as reachable")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config(args.config)
    except OSError as exc:
        raise SystemExit(f"cannot read {args.config}: {exc}") from exc

    probe = _stub_probe if args.probe_command == "true" else None
    wd = Watchdog(cfg, probe=probe)
    if args.once:
        if args.dry_run:
            # policy.decide() returns early on first_sweep, before building any
            # cycle list, and a fresh Watchdog always starts first_sweep=True.
            # Left alone, `--once --dry-run` could never report a proposed
            # cycle, which is exactly what an operator is told to read before
            # enabling the service (see the role README). Cycling nothing is
            # safe by construction on a dry run, so pretend this is a steady
            # -state sweep for the purposes of the report.
            wd.first_sweep = False
        decision = wd.sweep(dry_run=args.dry_run)
        # A single sweep exits non-zero for the same reasons the daemon would
        # restart itself. Without this, `--once` reports success having found
        # nothing -- which is exactly what a wrong community, an unreachable
        # switch or a dead key produces, and it is the check the role's verify
        # step and the README's pre-enable gate both lean on.
        if decision.unhealthy:
            log = logging.getLogger("fleet_watchdog")
            log.error("sweep is unhealthy: %s", decision.unhealthy)
            return 1
        return 0
    if args.dry_run:
        raise SystemExit("--dry-run needs --once")
    return wd.run()
