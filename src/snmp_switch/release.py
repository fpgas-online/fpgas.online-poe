"""Release a board port's DHCP leases on the gateway.

The release itself is fpgas.online-infra's ``fpgas-release-port-lease``, run as root through one sudoers rule that
names that script. It takes nothing but the port's interface (``v<vlan>``) and a cause word; it finds the port's
addresses and leases itself, releases them with dnsmasq's ``dhcp_release``/``dhcp_release6``, checks the lease file,
and logs each release. Off unless the site sets ``SNMP_SWITCH_LEASE_RELEASE = True`` (the gateway converge does,
where it installs the script), so a host without the script never calls sudo.
"""

import logging
import subprocess
import threading

from django.conf import settings

from switch_setup.plan import vlan_id

log = logging.getLogger(__name__)

SUDO = "/usr/bin/sudo"
SCRIPT = "/usr/local/sbin/fpgas-release-port-lease"
CAUSES = ("link-down", "power-cycle", "power-off", "down-port")
# The script's own worst case: a 30 s wait for another release's lock, then 10 s per tool, 8 s for the lease file and
# a few seconds of reads: about 61 s. The poller's systemd watchdog (WatchdogSec=120, fpgas.online-infra) must stay
# above this; it is kept alive before each release.
TIMEOUT = 75


def enabled():
    return bool(getattr(settings, "SNMP_SWITCH_LEASE_RELEASE", False))


def argv(switch, port, cause):
    if cause not in CAUSES:
        raise ValueError(f"unknown cause {cause!r}")
    return [SUDO, "-n", SCRIPT, f"v{vlan_id(switch, port)}", "--cause", cause]


def release_port_lease(switch, port, cause):
    """Release the port's leases and wait for the answer: True when the script confirmed it (or there was nothing
    to release). The script's own log line says what it did."""
    if not enabled():
        return True
    try:
        done = subprocess.run(argv(switch, port, cause), capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("switch %s port %s: lease release (%s) did not run: %s", switch, port, cause, exc)
        return False
    if done.returncode != 0:
        log.error("switch %s port %s: lease release (%s) exited %d: %s", switch, port, cause, done.returncode,
                  (done.stderr or done.stdout).strip()[:300])
        return False
    return True


def start_release(switch, port, cause):
    """Start a release and return at once (the PoE views: a request must not wait for dhcp_release6). The outcome
    is in the script's log line; a failure to start is logged here."""
    if not enabled():
        return
    try:
        child = subprocess.Popen(argv(switch, port, cause), start_new_session=True)
    except OSError as exc:
        log.error("switch %s port %s: lease release (%s) did not start: %s", switch, port, cause, exc)
        return

    def reap():
        try:
            code = child.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            log.error("switch %s port %s: lease release (%s) still running after %d s", switch, port, cause, TIMEOUT)
            return
        if code != 0:
            log.error("switch %s port %s: lease release (%s) exited %d", switch, port, cause, code)

    threading.Thread(target=reap, daemon=True).start()
