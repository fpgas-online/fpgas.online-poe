"""snmp_switch.release: the one command it runs, and that it runs nothing unless the site turns it on."""

import subprocess
import types

import pytest
from django.test import override_settings

from snmp_switch import release


def test_the_command_names_only_the_ports_interface_and_the_cause():
    assert release.argv(2, 48, "link-down") == [
        "/usr/bin/sudo", "-n", "/usr/local/sbin/fpgas-release-port-lease", "v2248", "--cause", "link-down"]


def test_an_unknown_cause_is_refused():
    with pytest.raises(ValueError):
        release.argv(2, 48, "because")


def test_off_by_default_nothing_is_run(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("ran a command")
    monkeypatch.setattr(release.subprocess, "run", refuse)
    monkeypatch.setattr(release.subprocess, "Popen", refuse)
    assert release.release_port_lease(2, 48, "link-down") is True
    release.start_release(2, 48, "power-cycle")


@override_settings(SNMP_SWITCH_LEASE_RELEASE=True)
@pytest.mark.parametrize("returncode, ok", [(0, True), (1, False), (2, False)])
def test_the_answer_is_the_scripts_exit_status(monkeypatch, returncode, ok):
    ran = []

    def run(argv, **kwargs):
        ran.append((argv, kwargs["timeout"]))
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr="refused")

    monkeypatch.setattr(release.subprocess, "run", run)
    assert release.release_port_lease(1, 7, "down-port") is ok
    assert ran == [(release.argv(1, 7, "down-port"), release.TIMEOUT)]


@override_settings(SNMP_SWITCH_LEASE_RELEASE=True)
@pytest.mark.parametrize("failure", [OSError("no sudo"), subprocess.TimeoutExpired("sudo", 30)])
def test_a_script_that_cannot_run_or_hangs_is_a_failure(monkeypatch, failure):
    def run(argv, **kwargs):
        raise failure
    monkeypatch.setattr(release.subprocess, "run", run)
    assert release.release_port_lease(1, 7, "down-port") is False


@override_settings(SNMP_SWITCH_LEASE_RELEASE=True)
def test_start_release_does_not_wait(monkeypatch):
    started = []

    class Child:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(release.subprocess, "Popen", Child)
    release.start_release(2, 3, "power-cycle")
    assert started == [(release.argv(2, 3, "power-cycle"), {"start_new_session": True})]
