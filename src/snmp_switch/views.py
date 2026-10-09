import functools
import json
import logging
import time
import unicodedata

from asgiref.sync import async_to_sync

# so we can send the browser a message when the power goes off and on:
# tangle up this code with the django-connect web socket code :(
from channels.layers import get_channel_layer
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from netgear_switch.errors import NetgearSwitchError

from snmp_switch.policy import port_policy, release_toggle_claim, seconds_until_toggle_allowed
from snmp_switch.switches import PoeConfigError, PoeNotABoardPort, PoeRequestError, open_port, requested_port

log = logging.getLogger(__name__)

# Switching a port back on is tried this many times, this long apart, before
# the view gives up and says the port may be off.
ON_ATTEMPTS = 3
ON_RETRY_SECONDS = 1


def not_a_board(ref):
    return JsonResponse(
        {'error': f'{ref} is not a board this site offers; nothing was sent to the switch'}, status=403)


def cross_site(request):
    """Why `request` may not switch a port, or "" when it may. The views are csrf_exempt and the pages have no login,
    so a page on another site could otherwise POST here from a visitor's browser and leave a board powered off. A
    browser's own fetch from our pages sends JSON and Sec-Fetch-Site: same-origin; a direct call from a tool on the
    host sends no Sec-Fetch-Site. A cross-site form or text/plain POST has neither JSON nor a same-origin fetch
    site, so it is refused before anything is read."""
    if request.content_type != "application/json":
        return "expected Content-Type: application/json"
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site != "same-origin":
        return f"refused a {site} request: PoE is switched only from this site's own pages"
    return ""


def poe_view(fn=None, *, with_request=False):
    """Decode the JSON body, work out the switch port it names, refuse a
    port that cannot be a board's (a trunk, an uplink, a port outside the
    switch's access ports), and ask the site whether that port is a board it
    offers (snmp_switch.policy) before the view does anything. The ways that can go wrong are JSON errors, not a
    bare 500: a bad request is a 400, a port the site does not offer a 403,
    an unconfigured service a 503, a switch that will not answer a 502.

    A cross-site request (cross_site) is refused first, with a 403, before
    anything is read. Then the site's policy is looked up, so a project that
    has none refuses every request, whatever it says.

    A view is called as ``fn(ref)``. One that also needs the request (its
    client address and user agent) and the decoded body (more fields than the
    port) is declared ``@poe_view(with_request=True)`` and called as
    ``fn(ref, request, body)``; the views declared plain are unchanged."""
    if fn is None:
        return functools.partial(poe_view, with_request=with_request)

    @csrf_exempt
    @require_POST
    @functools.wraps(fn)
    def wrapper(request):
        refused = cross_site(request)
        if refused:
            return JsonResponse({'error': f'{refused}; nothing was sent to the switch'}, status=403)
        try:
            allowed = port_policy()
            try:
                body = json.loads(request.body)
            except (ValueError, RecursionError):  # RecursionError: nested too deeply to read
                raise PoeRequestError('expected a JSON body {"port": ..., "switch": ...}') from None
            ref = requested_port(body)
            if not allowed(request, ref.switch, ref.port):
                return not_a_board(ref)
            return fn(ref, request, body) if with_request else fn(ref)
        except PoeNotABoardPort as e:
            return not_a_board(e.args[0])
        except PoeRequestError as e:
            return JsonResponse({'error': str(e)}, status=400)
        except PoeConfigError as e:
            return JsonResponse({'error': str(e)}, status=503)
        except NetgearSwitchError as e:
            return JsonResponse({'error': f'switch: {e}'}, status=502)

    return wrapper


def notify_dcws(port, gs, state):

    # send message to browser via web socket

    pi_name=f"pi{port}"
    group = f"pistat_{pi_name}"
    message_type="stat.message"
    message_text=f"snmp: {gs} power {state}"
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)( group, {"type": message_type, "message": message_text} )


@poe_view
def status(ref):
    # get_state (it's a getter yo.)

    state = open_port(ref).state()
    notify_dcws(ref.port, "get", state)

    return JsonResponse({'state': state})


@poe_view
def toggle(ref):
    # turn the port off and on again, at most once per interval

    wait = seconds_until_toggle_allowed(ref)
    if wait:
        response = JsonResponse(
            {'error': f'{ref} was power-cycled a moment ago; try again in {wait} seconds. '
                      f'Nothing was sent to the switch'},
            status=429)
        response['Retry-After'] = str(wait)
        return response

    poe = open_port(ref)
    port = str(ref.port)

    # Off. If the switch did not take it, the port is most likely still on,
    # but that is not known: either way the "on" below is still sent.
    try:
        off = poe.set(False)
    except Exception as e:
        log.exception("%s: switching off failed", ref)
        off, off_error = None, e
    else:
        off_error = None
        notify_dcws(ref.port, "set", off)
        time.sleep(.5)

    # On, and not left at one try: a port this view switched off must not
    # stay off because one answer from the switch went missing.
    on, on_error = None, None
    for attempt in range(ON_ATTEMPTS):
        if attempt:
            time.sleep(ON_RETRY_SECONDS)
        try:
            on = poe.set(True)
        except Exception as e:
            log.exception("%s: switching on failed (try %d of %d)", ref, attempt + 1, ON_ATTEMPTS)
            on, on_error = None, e
        else:
            on_error = None if on != "off" else RuntimeError("the switch still reports the port off")
        if on_error is None:
            break

    if on_error is not None:
        # no power cycle to protect, and someone has to be able to switch
        # the port back on: the next Reset must not be told to wait
        release_toggle_claim(ref)
        return JsonResponse(
            {'error': f'switch: {ref} was switched off and the switch did not confirm switching it '
                      f'back on ({on_error}). The port may be off: press Reset again, it can be '
                      f'tried again at once'},
            status=502)
    notify_dcws(ref.port, "set", on)
    if off_error is not None:
        release_toggle_claim(ref)
        return JsonResponse(
            {'error': f'switch: {ref} could not be switched off ({off_error}), so it was not '
                      f'power-cycled; the port is on. Reset can be pressed again at once'},
            status=502)

    return JsonResponse({port: [off, on]})


# The most characters of a client's user agent or address that are logged.
MAX_UA = 200
MAX_ADDRESS = 64


def _printable(text, limit):
    """`text` with control characters (and line or paragraph separators)
    removed, cut to `limit` characters: what a client sends is logged, so it
    must not be able to start a line of its own."""
    clean = ''.join(c for c in text if unicodedata.category(c) not in ('Cc', 'Cf', 'Zl', 'Zp'))
    return clean[:limit]


def client_address(request):
    """The address the request came from, for the log.

    The site's nginx (``include proxy_params``, the Debian file) sets
    ``X-Real-IP`` to the connecting peer ($remote_addr), overwriting any such
    header the client sent, and ``X-Forwarded-For`` to the client's own header
    *plus* the peer ($proxy_add_x_forwarded_for): so the first entry of
    X-Forwarded-For is whatever the client chose to write, and cannot be
    trusted. The gunicorn socket is a unix socket, so only nginx can set
    these. X-Real-IP is used; REMOTE_ADDR (which is no address at all behind a
    unix socket) is the fallback for a request that did not come through
    nginx, such as the test client."""
    address = request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR') or ''
    return _printable(address.strip(), MAX_ADDRESS) or '-'


def client_agent(request):
    return _printable(request.META.get('HTTP_USER_AGENT', ''), MAX_UA) or '-'


def log_power(ref, on, request, result):
    """The one INFO line for a power request: there is no login, so who is
    the client address and what it said it was."""
    log.info("poe %s switch=%s port=%s from=%s ua=%s result=%s",
             'on' if on else 'off', ref.switch if ref.switch is not None else '-', ref.port,
             client_address(request), client_agent(request), result)


@poe_view(with_request=True)
def power(ref, request, body):
    # switch the port on or off (not a cycle), at most once per interval,
    # sharing toggle's limit: the dashboard's PoE switch
    on = body.get('on')
    if not isinstance(on, bool):
        raise PoeRequestError("'on' must be true or false")

    wait = seconds_until_toggle_allowed(ref)
    if wait:
        log_power(ref, on, request, f'rate-limited {wait}s')
        response = JsonResponse(
            {'error': f'{ref} was switched a moment ago; try again in {wait} seconds. '
                      f'Nothing was sent to the switch'},
            status=429)
        response['Retry-After'] = str(wait)
        return response

    try:
        state = open_port(ref).set(on)
    except Exception as e:
        # nothing is known to have happened: someone may try again at once
        release_toggle_claim(ref)
        log_power(ref, on, request, f'error {type(e).__name__}')
        if isinstance(e, (NetgearSwitchError, PoeConfigError, PoeRequestError)):
            raise  # poe_view answers these (502, 503, 400)
        log.exception("%s: switching %s failed", ref, "on" if on else "off")
        return JsonResponse({'error': f'switch: the write failed ({type(e).__name__}); try again'}, status=502)
    log_power(ref, on, request, state)
    notify_dcws(ref.port, "set", state)
    return JsonResponse({'state': state})
