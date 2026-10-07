"""Ask one board whether it is alive, and whether anyone is using it.

/proc/uptime rather than `uptime -s`: netbooted Pis have no RTC, so a boot
timestamp is only as trustworthy as NTP, while seconds-since-boot needs no
clock at all. Neither command needs sudo.

`who` covers both ways in: the web terminal is webssh sshing into the board as
pi, and direct ssh arrives through the gateway DNAT. Both land as sshd sessions
with a pty, so both appear in utmp. The Django site knows about neither, so the
board itself is the only place this information exists.
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from .config import WatchdogConfig
from .policy import Observation
from .switches import Board

log = logging.getLogger("fleet_watchdog")

REMOTE_COMMAND = "cat /proc/uptime; who"


class SshProbe:
    def __init__(self, cfg: WatchdogConfig) -> None:
        self.cfg = cfg

    def command(self, board: Board) -> list[str]:
        cfg = self.cfg
        return [
            "ssh",
            # -n redirects stdin from /dev/null and, with a remote command,
            # allocates no pty. A pty would write a utmp record and the
            # watchdog would see itself as a logged-in user on every board.
            "-n",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(cfg.ssh_timeout)}",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={cfg.known_hosts}",
            "-o", "IdentitiesOnly=yes",
            "-i", cfg.ssh_key,
            f"{cfg.ssh_user}@{board.ip}",
            REMOTE_COMMAND,
        ]

    def __call__(self, board: Board) -> Observation:
        # The hard timeout is the one that matters: a board on stale NFS
        # handles completes the TCP handshake and then never finishes the SSH
        # banner, so ConnectTimeout alone never fires.
        try:
            proc = subprocess.run(
                self.command(board),
                capture_output=True,
                text=True,
                timeout=self.cfg.ssh_timeout + 10,
            )
        except subprocess.TimeoutExpired:
            return _failed(board, f"timed out after {self.cfg.ssh_timeout + 10:.0f}s")
        except OSError as exc:
            return _failed(board, f"could not run ssh: {exc}")

        if proc.returncode != 0:
            return _failed(board, _tidy(proc.stderr or proc.stdout) or f"exit {proc.returncode}")
        return parse_output(board, proc.stdout)


def parse_output(board: Board, stdout: str) -> Observation:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return _failed(board, "no output")
    try:
        uptime_s = float(lines[0].split()[0])
    except (ValueError, IndexError):
        return _failed(board, f"could not read uptime from {lines[0]!r}")
    return Observation(
        board=board,
        ok=True,
        uptime_s=uptime_s,
        # Anything `who` printed is a login session.
        in_use=len(lines) > 1,
        error=None,
    )


def _failed(board: Board, error: str, internal: bool = False) -> Observation:
    return Observation(
        board=board, ok=False, uptime_s=None, in_use=False, error=error,
        internal=internal,
    )


def _tidy(text: str) -> str:
    return " ".join(text.split())[:200]


def probe_all(
    probe: Callable[[Board], Observation], boards: Iterable[Board], concurrency: int
) -> list[Observation]:
    """Probe every board in parallel. One board's explosion is that board's
    failure, never the sweep's."""
    boards = list(boards)
    if not boards:
        return []

    def guarded(board: Board) -> Observation:
        try:
            return probe(board)
        except Exception as exc:  # noqa: BLE001 - a probe must never kill the sweep
            # A bug in this process must not read as "the board is dead", which
            # two sweeps later cuts mains power to real hardware. Log the
            # traceback -- the whole point is that this is OUR fault -- and
            # mark it internal so policy.decide refuses to cycle on it.
            log.exception("%s probe raised internally", board)
            return _failed(board, f"probe raised: {exc!r}", internal=True)

    with ThreadPoolExecutor(max(1, min(concurrency, len(boards)))) as pool:
        return list(pool.map(guarded, boards))
