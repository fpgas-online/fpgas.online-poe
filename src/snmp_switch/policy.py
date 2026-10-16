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
    switched. It must be shared by every process that serves the views
    (redis, memcached, the database): a per-process cache would let each
    worker allow its own switch.

``SNMP_SWITCH_ON_AFTER_OFF``
    Seconds after a port was switched off before it may be switched again
    (that is, back on). Optional; 10 when unset.

``SNMP_SWITCH_OFF_AFTER_ON``
    Seconds after a port was switched on, or power-cycled (which ends on),
    before it may be switched again (off, or cycled). Optional; 60 when unset.

Tim, 17 Oct 2026: "The time delay between power off to power on should be ~10
seconds. The time delay from power on -> power off should be ~60 seconds." So
the wait depends on what was done last, and there is no other limit. The one
interval for every request that came before (``SNMP_SWITCH_TOGGLE_INTERVAL``)
is refused by name, so a site still setting it hears about it.
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
ON_AFTER_OFF_SETTING = "SNMP_SWITCH_ON_AFTER_OFF"
OFF_AFTER_ON_SETTING = "SNMP_SWITCH_OFF_AFTER_ON"
DEFAULT_ON_AFTER_OFF = 10
DEFAULT_OFF_AFTER_ON = 60
RETIRED_INTERVAL_SETTING = "SNMP_SWITCH_TOGGLE_INTERVAL"


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


def _seconds(setting, default):
    value = getattr(settings, setting, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PoeConfigError(f"PoE control is refused: {setting} must be a whole number of seconds, 1 or more")
    return value


def hold_after(on):
    """Seconds a port is held after it was left `on` (True: switched on, or power-cycled) or off (False)."""
    if hasattr(settings, RETIRED_INTERVAL_SETTING):
        raise PoeConfigError(
            f"PoE control is refused: {RETIRED_INTERVAL_SETTING} is no longer read; the wait depends on what was "
            f"done last: set {ON_AFTER_OFF_SETTING} and {OFF_AFTER_ON_SETTING} instead")
    if on:
        return _seconds(OFF_AFTER_ON_SETTING, DEFAULT_OFF_AFTER_ON)
    return _seconds(ON_AFTER_OFF_SETTING, DEFAULT_ON_AFTER_OFF)


def _claim_key(ref):
    return f"snmp_switch:toggle:{'legacy' if ref.switch is None else ref.switch}:{ref.port}"


def seconds_until_toggle_allowed(ref, leaves_on):
    """Claim this port for a request that leaves it on (`leaves_on`: a switch
    on, or a power cycle) or off. 0 when the claim was taken (go ahead),
    otherwise the seconds until the port may be switched again. The claim
    holds the port for hold_after(leaves_on): what was done last decides how
    long the next request waits. It is taken in one atomic step (cache.add),
    so of two requests that arrive together only one goes ahead. The view
    gives it back (release_toggle_claim) when the switch did not carry the
    request out."""
    hold = hold_after(leaves_on)
    longest = max(hold_after(True), hold_after(False))
    alias = getattr(settings, RATE_LIMIT_CACHE_SETTING, None)
    if not alias:
        raise PoeConfigError(
            f"PoE control is refused: this site has no shared store for the power-cycle rate limit "
            f"({RATE_LIMIT_CACHE_SETTING} is not set)")
    key = _claim_key(ref)
    now = time.time()
    try:
        cache = caches[alias]
        if cache.add(key, now + hold, timeout=hold):
            return 0
        until = cache.get(key)
    except Exception as e:
        # a store that cannot be asked is not permission to go ahead
        log.exception("the power-cycle rate limit store (cache %r) failed", alias)
        raise PoeConfigError("PoE control is refused: the power-cycle rate limit store is not answering") from e
    # the claim that beat this request may expire between add() and get()
    # (and never tell anyone to wait longer than the longest hold, whatever is stored)
    return min(longest, max(1, math.ceil(until - now))) if isinstance(until, (int, float)) else 1


def release_toggle_claim(ref):
    """Give the claim back: the power cycle it was taken for did not happen
    (or did not finish), so the port may be tried again at once. A store that
    cannot be asked is logged and left: the claim then runs out by itself."""
    try:
        caches[getattr(settings, RATE_LIMIT_CACHE_SETTING)].delete(_claim_key(ref))
    except Exception:
        log.exception("could not release the power-cycle claim on %s", ref)
