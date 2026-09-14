
import functools
import json
import time

from asgiref.sync import async_to_sync

# so we can send the browser a message when the power goes off and on:
# tangle up this code with the django-connect web socket code :(
from channels.layers import get_channel_layer
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from netgear_switch.errors import NetgearSwitchError

from snmp_switch.switches import PoeConfigError, PoeRequestError, poe_port
from snmp_switch.utils import mk_params, snmp_set_state


def poe_view(fn):
    """Decode the JSON body, resolve the switch port it names, and turn the
    ways that can go wrong into JSON errors instead of a bare 500: a bad
    request is a 400, an unconfigured service a 503, a switch that will not
    answer a 502."""

    @csrf_exempt
    @functools.wraps(fn)
    def wrapper(request):
        try:
            body = json.loads(request.body)
            port = body['port']
        except (ValueError, KeyError, TypeError):
            return JsonResponse({'error': 'expected a JSON body {"port": ..., "switch": ...}'}, status=400)
        try:
            return fn(port, poe_port(body))
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
def status(port, poe):
    # get_state (it's a getter yo.)

    state = poe.state()
    notify_dcws(port, "get", state)

    return JsonResponse({'state': state})


@poe_view
def toggle(port, poe):
    # turn the port off and on again

    ret = {port: []}

    for on in (False, True):
        state = poe.set(on)
        notify_dcws(port, "set", state)
        ret[port].append(state)
        if not on:
            time.sleep(.5)

    return JsonResponse(ret)


@csrf_exempt
def toggle_all(request):

    params = mk_params()

    ret = { port:[] for port in range(48) }

    # all off:
    for port in range(1,48):
        params['port'] = str(port)
        d = snmp_set_state( state='2', **params )
        notify_dcws(port, "get", d['state'])
        ret[port].append(d['state'])

    time.sleep(1)

    # all on:
    for port in range(1,48):
        params['port'] = str(port)
        d = snmp_set_state( state='1', **params )
        notify_dcws(port, "set", d['state'])
        ret['was'][port] = d['state']

    response = HttpResponse(content_type="application/json")
    json.dump(ret, response, indent=2)

    return response

@csrf_exempt
def off_all(request):

    # 2=off
    # params['state']=2

    params = mk_params()
    ret = { port:[] for port in range(48) }

    # all off:
    for port in range(1,48):
        params['port'] = str(port)
        d = snmp_set_state( state='2', **params )
        notify_dcws(port, "set", d['state'])
        ret[port].append(d['state'])

    response = HttpResponse(content_type="application/json")
    json.dump(ret, response, indent=2)

    return response


