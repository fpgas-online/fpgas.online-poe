"""Which requests the PoE views act on.

The views are reachable by anyone who can reach the site, so a request is
acted on only when two things the Django project supplies say so. Both fail
closed: a project that has not configured them gets every request refused
(503), never an open endpoint.

``SNMP_SWITCH_PORT_POLICY``
    Dotted path of a callable ``policy(request, switch, port) -> bool``:
    whether that port is a board the site offers. ``switch`` is the switch's
    index, or None on the legacy single switch; ``port`` is an int. The
    project answers from the data its pages are built from; this package has
    no list of its own. Uplinks, trunks, service ports and empty ports are
    refused because the project does not name them.

``SNMP_SWITCH_RATE_LIMIT_CACHE``
    Alias (in ``CACHES``) of the cache that remembers when each port was last
    power-cycled. It must be shared by every process that serves the views
    (redis, memcached, the database): a per-process cache would let each
    worker allow its own cycle.

``SNMP_SWITCH_TOGGLE_INTERVAL``
    Seconds between two power cycles of one port. Optional; 60 when unset.
"""

import logging
import math
import time

from django.conf import settings
from django.core.cache import caches
from django.utils.module_loading import import_string

from snmp_switch.switches import PoeConfigError

log = logging.getLogger(__name__)

PORT_POLICY_SETTING = "SNMP_SWITCH_PORT_POLICY"
RATE_LIMIT_CACHE_SETTING = "SNMP_SWITCH_RATE_LIMIT_CACHE"
TOGGLE_INTERVAL_SETTING = "SNMP_SWITCH_TOGGLE_INTERVAL"
DEFAULT_TOGGLE_INTERVAL = 60


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


def toggle_interval():
    interval = getattr(settings, TOGGLE_INTERVAL_SETTING, DEFAULT_TOGGLE_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise PoeConfigError(f"PoE control is refused: {TOGGLE_INTERVAL_SETTING} must be a whole number of seconds, 1 or more")
    return interval


def seconds_until_toggle_allowed(ref):
    """Claim this port's one power cycle per interval. 0 when the claim was
    taken (go ahead), otherwise the seconds until the port may be cycled
    again. The claim is taken in one atomic step (cache.add), so of two
    requests that arrive together only one goes ahead. It is kept whatever
    the switch then answers: the limit is on what is sent to the switch."""
    interval = toggle_interval()
    alias = getattr(settings, RATE_LIMIT_CACHE_SETTING, None)
    if not alias:
        raise PoeConfigError(
            f"PoE control is refused: this site has no shared store for the power-cycle rate limit "
            f"({RATE_LIMIT_CACHE_SETTING} is not set)")
    key = f"snmp_switch:toggle:{'legacy' if ref.switch is None else ref.switch}:{ref.port}"
    now = time.time()
    try:
        cache = caches[alias]
        if cache.add(key, now + interval, timeout=interval):
            return 0
        until = cache.get(key)
    except Exception as e:
        # a store that cannot be asked is not permission to go ahead
        log.exception("the power-cycle rate limit store (cache %r) failed", alias)
        raise PoeConfigError("PoE control is refused: the power-cycle rate limit store is not answering") from e
    # the claim that beat this request may expire between add() and get()
    return max(1, math.ceil(until - now)) if isinstance(until, (int, float)) else 1
