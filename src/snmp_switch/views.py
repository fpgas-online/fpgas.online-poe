import functools
import json
import time

from asgiref.sync import async_to_sync

# so we can send the browser a message when the power goes off and on:
# tangle up this code with the django-connect web socket code :(
from channels.layers import get_channel_layer
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from netgear_switch.errors import NetgearSwitchError

from snmp_switch.policy import port_policy, seconds_until_toggle_allowed
from snmp_switch.switches import PoeConfigError, PoeNotABoardPort, PoeRequestError, open_port, requested_port

def not_a_board(ref):
    return JsonResponse(
        {'error': f'{ref} is not a board this site offers; nothing was sent to the switch'}, status=403)


def poe_view(fn):
    """Decode the JSON body, work out the switch port it names, refuse a
    port that cannot be a board's (a trunk, an uplink, a port outside the
    switch's access ports), and ask the site whether that port is a board it
    offers (snmp_switch.policy) before the view does anything. The ways that can go wrong are JSON errors, not a
    bare 500: a bad request is a 400, a port the site does not offer a 403,
    an unconfigured service a 503, a switch that will not answer a 502.

    The site's policy is looked up first, so a project that has none refuses
    every request, whatever it says."""

    @csrf_exempt
    @require_POST
    @functools.wraps(fn)
    def wrapper(request):
        try:
            allowed = port_policy()
            try:
                body = json.loads(request.body)
            except ValueError:
                raise PoeRequestError('expected a JSON body {"port": ..., "switch": ...}') from None
            ref = requested_port(body)
            if not allowed(request, ref.switch, ref.port):
                return not_a_board(ref)
            return fn(ref)
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
            {'error': f'{ref} was power-cycled a moment ago; try again in {wait} s. '
                      f'Nothing was sent to the switch'},
            status=429)
        response['Retry-After'] = str(wait)
        return response

    poe = open_port(ref)
    port = str(ref.port)
    ret = {port: []}

    for on in (False, True):
        state = poe.set(on)
        notify_dcws(ref.port, "set", state)
        ret[port].append(state)
        if not on:
            time.sleep(.5)

    return JsonResponse(ret)
