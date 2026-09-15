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
        wd.sweep(dry_run=args.dry_run)
        return 0
    if args.dry_run:
        raise SystemExit("--dry-run needs --once")
    wd.run()
    return 0
