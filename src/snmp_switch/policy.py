"""Which requests the PoE views act on.

The views are reachable by anyone who can reach the site, so a request is
acted on only when the Django project says the port is one of its boards.
This fails closed: a project that has not configured it gets every request
refused (503), never an open endpoint.

``SNMP_SWITCH_PORT_POLICY``
    Dotted path of a callable ``policy(request, switch, port) -> bool``:
    whether that port is a board the site offers. ``switch`` is the switch's
    index, or None on the legacy single switch; ``port`` is an int. The
    project answers from the data its pages are built from; this package has
    no list of its own. Uplinks, trunks, service ports and empty ports are
    refused because the project does not name them.
"""

import logging

from django.conf import settings
from django.utils.module_loading import import_string

from snmp_switch.switches import PoeConfigError

log = logging.getLogger(__name__)

PORT_POLICY_SETTING = "SNMP_SWITCH_PORT_POLICY"


def port_policy():
    """The project's policy callable. PoeConfigError when it has none."""
    path = getattr(settings, PORT_POLICY_SETTING, None)
    if not path:
        raise PoeConfigError(
            f"PoE control is refused: this site has not said which switch ports are boards "
            f"({PORT_POLICY_SETTING} is not set)")
    try:
        return import_string(path)
    except ImportError as e:
        log.exception("%s = %r cannot be imported", PORT_POLICY_SETTING, path)
        raise PoeConfigError(
            f"PoE control is refused: {PORT_POLICY_SETTING} names nothing that can be imported") from e

