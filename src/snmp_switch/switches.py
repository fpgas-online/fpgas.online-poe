"""Which switch a PoE request goes to, and how it is spoken to.

Two deployment shapes exist and both must keep working:

* Per-port-VLAN hosts (welland): several switches, listed in the file
  infra renders for fpgas-switch-setup (``/etc/fpgas/switches.yml``) and
  managed over SNMP v2c through ``netgear_switch``. Configure with
  ``FPGAS_SWITCHES_CONFIG=<path>`` and a write community per switch in
  ``FPGAS_SWITCH_COMMUNITY_<index>`` (or one for all in
  ``FPGAS_SWITCH_COMMUNITY``). A request names the switch with
  ``switch``; when only one is configured it is implied.

* The legacy flat scheme (PS1): one switch over SNMPv3, configured by the
  ``SNMP_SWITCH_*`` variables read by :func:`snmp_switch.utils.mk_params`.
  ``netgear_switch`` has no SNMPv3 transport, so this path is kept as is.

Neither configured is a service error (503), not a traceback.
"""

import os
import re
from dataclasses import dataclass

from asgiref.sync import async_to_sync
from netgear_switch import SyncSwitch, get_model

from snmp_switch.utils import mk_params, snmp_get_state, snmp_set_state
from switch_setup.cli import load_specs
from switch_setup.plan import SwitchSpec

CONFIG_ENV = "FPGAS_SWITCHES_CONFIG"
COMMUNITY_ENV = "FPGAS_SWITCH_COMMUNITY"
LEGACY_HOST_ENV = "SNMP_SWITCH_HOST"


class PoeConfigError(Exception):
    """The service is not (fully) configured for PoE control."""


class PoeRequestError(Exception):
    """The request does not identify a configured switch port."""


@dataclass
class LibraryPort:
    """One switch port, driven through netgear_switch."""

    switch: SyncSwitch
    port: int

    def state(self):
        status = next((p for p in self.switch.get_poe() if p.port == self.port), None)
        if status is None:
            return None
        return "on" if status.admin_enabled else "off"

    def set(self, on):
        self.switch.set_poe(self.port, on)
        return self.state()


@dataclass
class LegacyPort:
    """One port on the legacy single SNMPv3 switch (utils.py, unchanged)."""

    params: dict

    def state(self):
        return async_to_sync(snmp_get_state)(**self.params)["state"]

    def set(self, on):
        return async_to_sync(snmp_set_state)(state="1" if on else "2", **self.params)["state"]


# A switch index or a port number in a request: a whole number, sent as a JSON
# number or as its one decimal spelling (the pages send the port as a string).
# Bounded, so nothing downstream ever sees a number it has to think about.
MAX_NUMBER = 999
_DIGITS = re.compile(r"0|[1-9][0-9]{0,2}")


def _number(value, what, lowest):
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not lowest <= value <= MAX_NUMBER:
        raise PoeRequestError(f"'{what}' must be a whole number from {lowest} to {MAX_NUMBER}")
    return value


@dataclass(frozen=True)
class PortRef:
    """The switch port a request names. Working this out reads only the
    request and this host's configuration: nothing is sent to a switch."""

    switch: int | None  # None: the legacy single switch, which has no index
    port: int
    spec: SwitchSpec | None = None

    def __str__(self):
        return f"port {self.port}" if self.switch is None else f"switch {self.switch} port {self.port}"


def _library_ref(body, port):
    specs = load_specs(os.environ[CONFIG_ENV])
    index = body.get("switch")
    if index is None:
        if len(specs) != 1:
            raise PoeRequestError(
                f"'switch' is required: {len(specs)} switches are configured "
                f"({', '.join(str(s.index) for s in specs)})")
        spec = specs[0]
    else:
        index = _number(index, "switch", 0)
        spec = next((s for s in specs if s.index == index), None)
        if spec is None:
            raise PoeRequestError(f"switch {index} is not configured")
    return PortRef(spec.index, port, spec)


def requested_port(body):
    """The port a request body ``{"port": ..., "switch": ...}`` names."""
    if not isinstance(body, dict) or "port" not in body:
        raise PoeRequestError('expected a JSON body {"port": ..., "switch": ...}')
    port = _number(body["port"], "port", 1)
    if CONFIG_ENV in os.environ:
        return _library_ref(body, port)
    if os.environ.get(LEGACY_HOST_ENV):
        if body.get("switch") is not None:
            raise PoeRequestError("'switch' is not accepted: this site has one switch, which has no index")
        return PortRef(None, port)
    raise PoeConfigError(
        f"PoE control is not configured: set {CONFIG_ENV} (per-port-VLAN switches) "
        f"or {LEGACY_HOST_ENV} (legacy SNMPv3 switch)")


def open_port(ref):
    """The object that speaks to the switch for a PortRef. Only called once
    the request has been allowed (snmp_switch.policy)."""
    if ref.spec is None:
        params = mk_params()
        params["port"] = str(ref.port)
        return LegacyPort(params)
    spec = ref.spec
    community = (os.environ.get(f"{COMMUNITY_ENV}_{spec.index}")
                 or os.environ.get(COMMUNITY_ENV))
    if not community:
        raise PoeConfigError(
            f"no SNMP community for switch {spec.index}: "
            f"set {COMMUNITY_ENV}_{spec.index} or {COMMUNITY_ENV}")
    # one community serves both read and write on these switches; SyncSwitch
    # wants it under both names or refuses to write (see switch_setup.cli)
    sw = SyncSwitch(get_model(spec.model), spec.mgmt_host,
                    snmp_community=community, snmp_write_community=community)
    return LibraryPort(sw, ref.port)
