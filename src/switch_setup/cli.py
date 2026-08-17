"""fpgas-switch-setup: converge Netgear switch VLAN config for fpgas.online.

Check mode (default) prints the pending actions and exits 2 if there are
any; --apply executes them. Only VLANs 2101-2348 and the owned ports'
VLAN 1 membership are ever written (see plan.diff).
"""

import argparse
import os
import sys

import yaml
from netgear_switch import SyncSwitch, get_model

from .apply import converge
from .plan import SwitchSpec


def load_specs(path: str) -> list[SwitchSpec]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return [SwitchSpec(
        index=s["index"], model=s["model"], mgmt_host=s["mgmt_host"],
        access_ports=s["access_ports"],
        gateway_trunk_port=s["gateway_trunk_port"],
        downstream_trunk_ports=tuple(s["downstream_trunk_ports"]),
        house_uplink_port=s["house_uplink_port"],
    ) for s in data["switches"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--switch", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--host", help="override mgmt_host from config")
    ap.add_argument("--community",
                    default=os.environ.get("FPGAS_SWITCH_COMMUNITY"))
    args = ap.parse_args()
    if not args.community:
        print("no SNMP community (--community or FPGAS_SWITCH_COMMUNITY)",
              file=sys.stderr)
        return 1
    specs = load_specs(args.config)
    spec = next(s for s in specs if s.index == args.switch)
    # Both read AND write communities are required: SyncSwitch raises
    # CredentialError on any write op if snmp_write_community is unset, even
    # when snmp_community (read) is present. This deployment uses a single
    # community string for both read and write per-switch, so pass it twice.
    sw = SyncSwitch(get_model(spec.model), args.host or spec.mgmt_host,
                    snmp_community=args.community,
                    snmp_write_community=args.community)
    actions = converge(sw, specs, args.switch, apply=args.apply)
    for act in actions:
        print(" ".join(str(a) for a in act))
    if actions and not args.apply:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
