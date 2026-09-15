"""The watchdog's on-disk configuration.

Rendered by the infra `fleet-watchdog` role to /etc/fpgas/watchdog.yml. Holds
no secrets: the SNMP write communities arrive in the environment instead (see
switches.community_for), exactly as the gunicorn PoE drop-in does it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import yaml

# Required keys have no default; everything else falls back to the spec's
# table. Keeping the defaults HERE rather than only in the Ansible template
# means --once against a hand-written file behaves the same as the service.
_DEFAULTS: dict[str, object] = {
    "interval": 300.0,
    "fail_threshold": 2,
    "ssh_timeout": 20.0,
    "probe_concurrency": 8,
    "poe_off_seconds": 30.0,
    "boot_grace": 300.0,
    "max_uptime_hours": 8.0,
    "uptime_jitter_minutes": 60.0,
    "hard_cap_hours": 12.0,
    "max_scheduled_cycles_per_sweep": 2,
    "cycle_concurrency": 2,
    "breaker_fraction": 0.5,
    "breaker_min_failures": 3,
    "ssh_user": "pi",
}

_REQUIRED = ("switches_config", "pib_network", "ssh_key", "known_hosts")


@dataclass(frozen=True)
class WatchdogConfig:
    switches_config: str
    pib_network: str
    ssh_key: str
    known_hosts: str
    interval: float = 300.0
    fail_threshold: int = 2
    ssh_timeout: float = 20.0
    probe_concurrency: int = 8
    poe_off_seconds: float = 30.0
    boot_grace: float = 300.0
    max_uptime_hours: float = 8.0
    uptime_jitter_minutes: float = 60.0
    hard_cap_hours: float = 12.0
    max_scheduled_cycles_per_sweep: int = 2
    cycle_concurrency: int = 2
    breaker_fraction: float = 0.5
    breaker_min_failures: int = 3
    ssh_user: str = "pi"
    exclude: Mapping[int, frozenset[int]] = field(default_factory=dict)


def load_config(path: str) -> WatchdogConfig:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    for key in _REQUIRED:
        if key not in data:
            raise KeyError(f"{path}: required key {key!r} is missing")
    kwargs: dict[str, object] = {k: data[k] for k in _REQUIRED}
    for key, default in _DEFAULTS.items():
        value = data.get(key, default)
        # YAML gives ints where the dataclass wants floats; normalise so
        # comparisons against timestamps never mix types surprisingly.
        kwargs[key] = type(default)(value)
    kwargs["exclude"] = {
        int(index): frozenset(int(p) for p in ports)
        for index, ports in (data.get("exclude") or {}).items()
    }
    return WatchdogConfig(**kwargs)  # type: ignore[arg-type]
