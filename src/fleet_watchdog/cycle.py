"""One PoE cycle: off, confirmed, thirty seconds of nothing, on, confirmed.

SyncSwitch.cycle_poe is deliberately not used. Its PoeCycleTimeouts.off_timeout
is a deadline for confirming the port went off, not a dwell: _poe_rearm sets
the port off, polls until detect leaves DELIVERING and link drops, then sets it
straight back on. The board would lose power for a second or two. A board that
has hung needs longer than that to actually reset.
"""

from __future__ import annotations

import time
from typing import Callable

from netgear_switch import PoEDetect, SyncSwitch


class CycleError(Exception):
    """A cycle did not reach the state it was supposed to."""


def _detect(sw: SyncSwitch, port: int) -> PoEDetect | None:
    status = next((p for p in sw.get_poe() if p.port == port), None)
    return status.detect if status else None


def _wait_for(
    sw: SyncSwitch,
    port: int,
    predicate: Callable[[PoEDetect | None], bool],
    timeout: float,
    poll: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    message: str,
) -> None:
    deadline = clock() + timeout
    while not predicate(_detect(sw, port)):
        if clock() >= deadline:
            raise CycleError(f"{message} (detect={_detect(sw, port)})")
        sleep(poll)


def cycle_port(
    sw: SyncSwitch,
    port: int,
    off_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    off_timeout: float = 30.0,
    on_timeout: float = 60.0,
    poll: float = 2.0,
) -> None:
    """Power-cycle one port. Raises CycleError if either transition fails.

    A failure to go off raises BEFORE the dwell, so the function never leaves a
    port off believing it turned it back on.
    """
    sw.set_poe(port, False)
    _wait_for(
        sw, port, lambda d: d is not PoEDetect.DELIVERING, off_timeout, poll,
        sleep, clock, f"PoE port {port} did not turn off within {off_timeout:.0f}s",
    )
    sleep(off_seconds)
    sw.set_poe(port, True)
    _wait_for(
        sw, port, lambda d: d is PoEDetect.DELIVERING, on_timeout, poll,
        sleep, clock, f"PoE port {port} did not come back within {on_timeout:.0f}s",
    )
