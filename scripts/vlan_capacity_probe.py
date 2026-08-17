#!/usr/bin/env python3
"""Probe how many VLANs a switch accepts. Uses IDs 3000+ (never the
production 2101-2348 block, never house VLANs) and deletes everything
it created, even on failure.

--community is used as both the SNMP read and write community, so it must
name a community with RW access on the target switch.

Usage: uv run scripts/vlan_capacity_probe.py --host 10.1.5.23 \
           --model gsm7252ps --community <rw-community> --count 150
"""

import argparse
import sys

from netgear_switch import SyncSwitch, get_model

BASE = 3000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--community", required=True)
    ap.add_argument("--count", type=int, default=150)
    args = ap.parse_args()

    # The current library separates read and write SNMP communities
    # (SyncSwitch.snmp_community vs snmp_write_community); a write raises
    # CredentialError if only the read community is set. This probe is meant
    # to be pointed at a single RW community (see --community help), so the
    # same value is used for both.
    sw = SyncSwitch(get_model(args.model), args.host,
                    snmp_community=args.community,
                    snmp_write_community=args.community)
    created: list[int] = []
    try:
        for i in range(args.count):
            vid = BASE + i
            try:
                sw.create_vlan(vid, f"probe{i}")
                created.append(vid)
            except Exception as e:  # noqa: BLE001 - report and stop at capacity
                print(f"create_vlan({vid}) failed after "
                      f"{len(created)} creations: {e}")
                break
        present = {v.vlan_id for v in sw.get_vlans()}
        verified = [v for v in created if v in present]
        print(f"created={len(created)} verified_present={len(verified)}")
    finally:
        for vid in created:
            try:
                sw.delete_vlan(vid, force=True)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup delete_vlan({vid}) failed: {e}")
    return 0 if len(created) >= args.count else 1


if __name__ == "__main__":
    sys.exit(main())
