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

## fpgas-switch-setup

Converges a Netgear PoE switch's VLAN config (VLAN-per-port isolation for the
FPGA network) to the desired state derived from a YAML `switches:` config —
the same schema as the infra repo's host_vars `switches:` list (keys:
`index, model, mgmt_host, access_ports, gateway_trunk_port,
downstream_trunk_ports, house_uplink_port`).

```
fpgas-switch-setup --config FILE --switch N [--apply] [--community STR] [--host HOST]
```

- `--config FILE` -- path to the YAML config (required)
- `--switch N` -- the `index` of the switch to converge, from `switches:` (required)
- `--apply` -- execute the pending actions; omitted (check mode) only reports them
- `--community STR` -- SNMP community (read and write); defaults to env `FPGAS_SWITCH_COMMUNITY`
- `--host HOST` -- override the config's `mgmt_host` for this run

Check mode (the default) prints each pending action and never writes to the
switch. Only VLANs 2101-2348 and the owned ports' VLAN 1 membership are ever
touched.

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | In sync (check mode, nothing pending) or applied cleanly |
| `2` | Drift found in check mode (nothing was written) -- pairs with Ansible's `changed_when: rc == 2` |
| `1` | Error (bad config, missing community, etc.) |

Example, against a local `ngsw serve` mock:

```bash
ngsw serve --model gsm7252ps --port 1610 &
FPGAS_SWITCH_COMMUNITY=public fpgas-switch-setup \
  --config switches.yml --switch 2 --host 127.0.0.1:1610
```

## Linting

- **ruff**: blocking
- **shellcheck**: blocking

## Related Repos

- [fpgas.online-site](https://github.com/fpgas-online/fpgas.online-site) -- Django web app (depends on this package)
- [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) -- Ansible deployment and SNMP config

## License

Apache 2.0
