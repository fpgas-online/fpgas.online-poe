"""The SSH health check, driven by a fake `ssh` on PATH."""

import dataclasses
import os
import stat
import textwrap

from fleet_watchdog.config import WatchdogConfig
from fleet_watchdog.probe import REMOTE_COMMAND, SshProbe, probe_all
from fleet_watchdog.switches import make_board

BOARD = make_board(2, 42, "10.21")


def cfg(**overrides):
    base = WatchdogConfig(
        switches_config="/s",
        pib_network="10.21",
        ssh_key="/var/lib/fleet-watchdog/id_ed25519",
        known_hosts="/var/lib/fleet-watchdog/known_hosts",
        ssh_timeout=2,
    )
    return dataclasses.replace(base, **overrides)


def fake_ssh(tmp_path, monkeypatch, body):
    """Put an `ssh` on PATH that behaves as `body` says."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    script = d / "ssh"
    script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")


def test_the_command_pins_the_key_the_known_hosts_and_batch_mode():
    argv = SshProbe(cfg()).command(BOARD)
    assert argv[0] == "ssh"
    assert "-o" in argv and "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "UserKnownHostsFile=/var/lib/fleet-watchdog/known_hosts" in argv
    assert "ConnectTimeout=2" in argv
    assert "-i" in argv
    assert "/var/lib/fleet-watchdog/id_ed25519" in argv
    assert argv[-2] == "pi@10.21.2.42"
    assert argv[-1] == REMOTE_COMMAND


def test_the_command_never_allocates_a_pty():
    """A pty would write utmp and make the watchdog look like a logged-in user."""
    argv = SshProbe(cfg()).command(BOARD)
    assert "-n" in argv
    assert "-t" not in argv


def test_a_healthy_idle_board_reports_its_uptime(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        print("12345.67 98765.43")
    """)
    o = SshProbe(cfg())(BOARD)
    assert o.ok is True
    assert o.uptime_s == 12345.67
    assert o.in_use is False
    assert o.error is None


def test_a_board_with_a_login_is_in_use(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        print("500.0 900.0")
        print("pi       pts/0        2026-09-15 10:04 (10.21.0.1)")
    """)
    o = SshProbe(cfg())(BOARD)
    assert o.ok is True
    assert o.in_use is True


def test_a_nonzero_exit_is_a_failure(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        import sys
        print("Permission denied (publickey).", file=sys.stderr)
        sys.exit(255)
    """)
    o = SshProbe(cfg())(BOARD)
    assert o.ok is False
    assert o.uptime_s is None
    assert "publickey" in o.error


def test_unparseable_output_is_a_failure(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        print("cat: /proc/uptime: Input/output error")
    """)
    o = SshProbe(cfg())(BOARD)
    assert o.ok is False
    assert "uptime" in o.error


def test_empty_output_is_a_failure(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        pass
    """)
    o = SshProbe(cfg())(BOARD)
    assert o.ok is False


def test_a_hanging_ssh_is_killed_and_reported(tmp_path, monkeypatch):
    """A board on stale NFS handles accepts the TCP connection and then never
    finishes the banner, so ConnectTimeout never fires. The hard timeout must."""
    fake_ssh(tmp_path, monkeypatch, """
        import time
        time.sleep(30)
    """)
    o = SshProbe(cfg(ssh_timeout=1))(BOARD)
    assert o.ok is False
    assert "timed out" in o.error


def test_a_host_key_mismatch_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        import sys
        print("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", file=sys.stderr)
        sys.exit(255)
    """)
    o = SshProbe(cfg())(BOARD)
    assert o.ok is False
    assert "IDENTIFICATION HAS CHANGED" in o.error


def test_probe_all_returns_one_observation_per_board(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, """
        print("100.0 200.0")
    """)
    boards = [make_board(2, p, "10.21") for p in range(1, 6)]
    results = probe_all(SshProbe(cfg()), boards, concurrency=4)
    assert len(results) == 5
    assert {o.board for o in results} == set(boards)
    assert all(o.ok for o in results)


def test_probe_all_survives_a_probe_that_raises():
    def exploding(board):
        raise RuntimeError("boom")

    boards = [make_board(2, p, "10.21") for p in range(1, 4)]
    results = probe_all(exploding, boards, concurrency=2)
    assert len(results) == 3
    assert all(not o.ok for o in results)
    assert all("boom" in o.error for o in results)
