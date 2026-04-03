# fpgas.online-poe

SNMP PoE switch management for the [fpgas.online](https://fpgas.online) FPGA-as-a-Service platform.

## Overview

Controls Netgear PoE switches to power-cycle Raspberry Pi boards connected to FPGA devices. Provides a Python library, Django web UI, and shell scripts for managing switch ports.

## Features

- SNMP-based PoE port control (on/off/toggle)
- Django views for web-based switch status and control
- CLI tools for scripted port management
- Per-port and bulk operations (toggle all, power off all)

## Installation

Library only:

```bash
pip install git+https://github.com/fpgas-online/fpgas.online-poe.git
```

With CLI extras:

```bash
pip install "fpgas-online-poe[cli] @ git+https://github.com/fpgas-online/fpgas.online-poe.git"
```

## Configuration

The switch connection is configured via environment variables:

| Variable | Description |
|----------|-------------|
| `SNMP_SWITCH_HOST` | Switch IP address |
| `SNMP_SWITCH_OID` | SNMP OID for PoE control |
| `SNMP_SWITCH_USERNAME` | SNMPv3 username |
| `SNMP_SWITCH_SECURITY_LEVEL` | SNMPv3 security level |
| `SNMP_SWITCH_AUTH_PROTOCOL` | Authentication protocol |
| `SNMP_SWITCH_AUTHKEY` | Authentication key |
| `SNMP_SWITCH_PRIV_PROTOCOL` | Privacy protocol |
| `SNMP_SWITCH_PRIVKEY` | Privacy key |

These are set by the [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) Ansible `snmp` role via `/etc/environment`.

## Directory Structure

```
src/snmp_switch/    Python library and Django app (views, urls, utils)
scripts/            Shell scripts for PoE control (poe.sh, allpoe.sh)
nginx/              nginx location config for the web UI
pyproject.toml      Package configuration (hatchling build)
```

## Linting

- **ruff**: blocking
- **shellcheck**: blocking

## Related Repos

- [fpgas.online-site](https://github.com/fpgas-online/fpgas.online-site) -- Django web app (depends on this package)
- [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) -- Ansible deployment and SNMP config

## License

Apache 2.0
