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
