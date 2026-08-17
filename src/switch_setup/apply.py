"""Read switch state, diff against desired, optionally apply."""

from netgear_switch import SyncSwitch

from .plan import Action, SwitchSpec, desired_state, diff


def converge(sw: SyncSwitch, specs: list[SwitchSpec], index: int,
             *, apply: bool) -> list[Action]:
    desired = desired_state(specs, index)
    actions = diff(sw.get_vlans(), dict(sw.get_pvids()), desired)
    if not apply:
        return actions
    for act in actions:
        match act:
            case ("create_vlan", vid, name):
                sw.create_vlan(vid, name)
            case ("membership", vid, port, mode):
                sw.set_vlan_membership(vid, port, mode, force=True)
            case ("pvid", port, vid):
                sw.set_pvid(port, vid, force=True)
            case ("delete_vlan", vid):
                sw.delete_vlan(vid, force=True)
    return actions
