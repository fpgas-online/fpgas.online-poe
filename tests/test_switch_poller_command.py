"""The switch_poller management command: its systemd notifications, and that it refuses to start unconfigured."""

import socket

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from snmp_switch.management.commands import switch_poller


def test_notify_sends_one_datagram_to_the_socket_systemd_gave(tmp_path, monkeypatch):
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        switch_poller.notify("WATCHDOG=1")
        assert server.recv(100) == b"WATCHDOG=1"


def test_notify_without_systemd_does_nothing(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    switch_poller.notify("READY=1")


def test_it_will_not_start_without_a_site_name(monkeypatch):
    with pytest.raises(CommandError, match="SWITCH_POLLER_SITE"):
        call_command("switch_poller")


@override_settings(SWITCH_POLLER_SITE="welland")
def test_it_will_not_start_without_switches(monkeypatch):
    monkeypatch.delenv("FPGAS_SWITCHES_CONFIG", raising=False)
    with pytest.raises(CommandError, match="no per-port-VLAN switches"):
        call_command("switch_poller")


class FakePoller:
    def __init__(self):
        self.polls = 0
        self.totals = {}

    def poll_once(self):
        self.polls += 1


def test_the_loop_feeds_the_watchdog_after_each_poll_and_keeps_the_period(monkeypatch):
    told, slept = [], []
    monkeypatch.setattr(switch_poller, "notify", told.append)
    clock = iter([0.0, 0.5, 10.0, 12.5])  # the first poll takes 0.5 s, the second 2.5 s
    poller = FakePoller()
    switch_poller.run(poller, 2.0, polls=2, sleep=slept.append, clock=lambda: next(clock))
    assert poller.polls == 2 and told == ["WATCHDOG=1", "WATCHDOG=1"]
    assert slept == [1.5, 0.0]


def test_an_exception_from_a_poll_ends_the_loop_and_feeds_no_watchdog(monkeypatch):
    told = []
    monkeypatch.setattr(switch_poller, "notify", told.append)

    class Broken(FakePoller):
        def poll_once(self):
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        switch_poller.run(Broken(), 2.0, polls=1, sleep=lambda s: None, clock=lambda: 0.0)
    assert told == []
