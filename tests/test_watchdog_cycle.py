"""Turning a port off, leaving it off, and bringing it back.

SyncSwitch.cycle_poe cannot be used: its off_timeout is a deadline for
CONFIRMING the port went off, not a dwell, so it turns the port straight back
on. The board needs 30 seconds without power.
"""

import pytest
from netgear_switch.errors import ProtectedPortError
from netgear_switch.virtual.server import VirtualSwitch

from fleet_watchdog.cycle import CycleError, cycle_port
from fleet_watchdog.switches import open_switch

DELIVERING_PORT = 44


class Spec:
    index = 2
    model = "s3300"
    access_ports = 48
    gateway_trunk_port = 51
    downstream_trunk_ports = ()
    house_uplink_port = 52

    def __init__(self, mgmt_host):
        self.mgmt_host = mgmt_host


@pytest.fixture()
def sw():
    vs = VirtualSwitch("gsm7228ps")
    vs.start()
    yield open_switch(Spec(f"{vs.host}:{vs.port}"), vs.community)
    vs.stop()


def poe_detect(sw, port):
    return next(p for p in sw.get_poe() if p.port == port).detect.value


def test_the_port_ends_up_delivering_again(sw):
    slept = []
    cycle_port(sw, DELIVERING_PORT, off_seconds=30, sleep=slept.append)
    assert poe_detect(sw, DELIVERING_PORT) == "delivering"


def test_the_dwell_is_the_configured_length(sw):
    slept = []
    cycle_port(sw, DELIVERING_PORT, off_seconds=30, sleep=slept.append)
    assert 30 in slept


def test_the_dwell_happens_while_the_port_is_off(sw):
    seen = []

    def sleep(seconds):
        if seconds == 30:
            seen.append(poe_detect(sw, DELIVERING_PORT))

    cycle_port(sw, DELIVERING_PORT, off_seconds=30, sleep=sleep)
    # The virtual switch mirrors real coherence: admin off sets detect to 1,
    # which parse.DETECT_MAP reads as "disabled".
    assert seen == ["disabled"]


def test_a_protected_port_is_refused(sw):
    with pytest.raises(ProtectedPortError):
        cycle_port(sw, 52, off_seconds=30, sleep=lambda s: None)


def test_a_port_that_never_goes_off_raises_before_the_dwell(sw, monkeypatch):
    monkeypatch.setattr(sw, "set_poe", lambda *a, **k: None)  # writes do nothing
    slept = []
    # clock() is called once for the deadline (0, so deadline is 30), then once
    # per loop check: 1 is inside, 31 is past it.
    with pytest.raises(CycleError, match="did not turn off"):
        cycle_port(
            sw,
            DELIVERING_PORT,
            off_seconds=30,
            sleep=slept.append,
            clock=iter([0, 1, 31]).__next__,
        )
    assert 30 not in slept  # never dwelled, never turned anything back on


def test_a_port_that_never_comes_back_raises(sw, monkeypatch):
    real_set = sw.set_poe

    def set_poe(port, on, **kwargs):
        if on:
            return None  # the turn-on silently does nothing
        return real_set(port, on, **kwargs)

    monkeypatch.setattr(sw, "set_poe", set_poe)
    # Phase 1 succeeds immediately, so it consumes one clock() for its
    # deadline and never loops. Phase 2 then gets 1 (deadline 61), 2 (inside)
    # and 100 (past it).
    with pytest.raises(CycleError, match="did not come back"):
        cycle_port(
            sw,
            DELIVERING_PORT,
            off_seconds=30,
            sleep=lambda s: None,
            clock=iter([0, 1, 2, 100]).__next__,
        )
