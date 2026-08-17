"""Integration tests for the fpgas-switch-setup console script.

Runs the CLI as a real subprocess against the library's in-process
VirtualSwitch. Requires the net-snmp CLI tools (apt: snmp).
"""

import subprocess
import textwrap

from netgear_switch.virtual.server import VirtualSwitch


def run_cli(*args, env_extra=None):
    import os
    env = os.environ | (env_extra or {})
    return subprocess.run(["uv", "run", "fpgas-switch-setup", *args],
                          capture_output=True, text=True, env=env)


def test_check_then_apply_then_clean(tmp_path):
    vs = VirtualSwitch("gsm7252ps")
    vs.start()
    try:
        cfg = tmp_path / "switches.yml"
        cfg.write_text(textwrap.dedent(f"""
            switches:
              - index: 2
                model: gsm7252ps
                mgmt_host: {vs.host}:{vs.port}
                access_ports: 4
                gateway_trunk_port: 49
                downstream_trunk_ports: [50]
                house_uplink_port: 52
        """))
        base = ["--config", str(cfg), "--switch", "2"]
        env = {"FPGAS_SWITCH_COMMUNITY": vs.community}
        r = run_cli(*base, env_extra=env)
        assert r.returncode == 2, r.stderr          # drift in check mode
        r = run_cli(*base, "--apply", env_extra=env)
        assert r.returncode == 0, r.stderr
        r = run_cli(*base, env_extra=env)
        assert r.returncode == 0, r.stderr          # now in sync
    finally:
        vs.stop()
