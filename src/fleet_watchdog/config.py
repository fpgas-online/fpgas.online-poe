"""The watchdog's on-disk configuration.

Rendered by the infra `fleet-watchdog` role to /etc/fpgas/watchdog.yml. Holds
no secrets: the SNMP write communities arrive in the environment instead (see
switches.community_for), exactly as the gunicorn PoE drop-in does it.

Every problem here is fatal. A watchdog that starts with a misread config is
worse than one that refuses to start: it power-cycles real hardware on rules
nobody wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import yaml


class ConfigError(ValueError):
    """The config file is missing a key, has an unknown one, or cannot be read
    as the type the setting needs."""


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
    # Consecutive unhealthy sweeps (breaker tripped, or no occupied port found
    # at all) before the process exits non-zero and lets systemd restart it.
    "unhealthy_exit_after": 3,
    # Consecutive failed attempts to bring one port back to a usable state
    # (clearing a fault, or re-enabling a port left off) before the watchdog
    # stops trying and just reports it. Reset the moment the port delivers.
    "max_recovery_attempts": 3,
}

_REQUIRED = ("switches_config", "pib_network", "ssh_key", "known_hosts")

_KNOWN = frozenset(_REQUIRED) | frozenset(_DEFAULTS) | {"exclude"}


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
    unhealthy_exit_after: int = 3
    max_recovery_attempts: int = 3
    exclude: Mapping[int, frozenset[int]] = field(default_factory=dict)


def load_config(path: str) -> WatchdogConfig:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(data).__name__}")

    for key in _REQUIRED:
        if key not in data:
            raise ConfigError(f"{path}: required key {key!r} is missing")

    # A typo must not load as a default. `max_uptime_hrs: 1` silently leaving
    # max_uptime_hours at 8 is how a fleet gets cycled on a rule nobody wrote.
    unknown = sorted(set(data) - _KNOWN)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            f"Known keys: {', '.join(sorted(_KNOWN))}"
        )

    kwargs: dict[str, object] = {k: data[k] for k in _REQUIRED}
    for key, default in _DEFAULTS.items():
        value = data.get(key, default)
        # YAML gives ints where the dataclass wants floats; normalise so
        # comparisons against timestamps never mix types surprisingly. Name the
        # key in the error: "could not convert string to float" alone leaves
        # the operator diffing the whole file.
        try:
            kwargs[key] = type(default)(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{path}: {key!r} must be a {type(default).__name__}, got {value!r}"
            ) from exc

    try:
        kwargs["exclude"] = {
            int(index): frozenset(int(p) for p in ports)
            for index, ports in (data.get("exclude") or {}).items()
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError(
            f"{path}: 'exclude' must map a switch index to a list of ports, "
            f"got {data.get('exclude')!r}"
        ) from exc

    return WatchdogConfig(**kwargs)  # type: ignore[arg-type]
