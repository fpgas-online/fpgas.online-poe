"""Long-running switch port poller (snmp_switch.poller): run under systemd, ``Type=notify`` with a watchdog.

Each port change is published as JSON on MQTT topic ``fpgas/<site>/gw/port`` (the broker the fleet consumer reads),
and the site turns it into the board page's status line, the switch dashboard's update and the registry record.

Settings the site supplies:

- ``SWITCH_POLLER_SITE``: the site name in the topic ("welland"); required.
- ``FLEET_MQTT``: ``{"host": ..., "port": ...}``, the broker (the fleet consumer's).
- ``SWITCH_POLL_SECONDS``: the period, default 2.
- ``SNMP_SWITCH_LEASE_RELEASE``: whether releases run (snmp_switch.release).
"""

import json
import logging
import os
import socket
import time

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from netgear_switch.transport.sync.snmp_netsnmp_cli import NetsnmpCliClient

from snmp_switch import gateway
from snmp_switch.poller import Poller, request_oids
from snmp_switch.release import release_port_lease
from snmp_switch.switches import configured_specs, switch_community

log = logging.getLogger(__name__)

DEFAULT_PERIOD = 2
SNMP_TIMEOUT = 2
# one summary line in the journal every this many polls
SUMMARY_POLLS = 300


def notify(state):
    """sd_notify(3) without a dependency: one datagram to $NOTIFY_SOCKET, when systemd gave one."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(path)
        s.sendall(state.encode())


class Command(BaseCommand):
    help = "Poll the switches' board ports; publish link changes and keep the DHCP leases honest"

    def handle(self, *args, **options):
        site = getattr(settings, "SWITCH_POLLER_SITE", "")
        if not site:
            raise CommandError("SWITCH_POLLER_SITE is not set")
        specs = configured_specs()
        if not specs:
            raise CommandError("no per-port-VLAN switches configured (FPGAS_SWITCHES_CONFIG)")
        period = float(getattr(settings, "SWITCH_POLL_SECONDS", DEFAULT_PERIOD))
        clients = {s.index: NetsnmpCliClient(s.mgmt_host, switch_community(s), timeout=SNMP_TIMEOUT, retries=0)
                   for s in specs}
        # read once here so a host with a broken ports.conf stops at start; the poller reads it again for each check
        gateway.read_with_retry(gateway.PORTS_CONF, gateway.parse_ports_conf)
        topic = f"fpgas/{site}/gw/port"

        broker = settings.FLEET_MQTT
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        # connect in the background and keep reconnecting: a broker that is not up yet (a fresh gateway's converge
        # starts this before the broker exists) or restarts must not stop the polling; qos 1 messages wait in paho's
        # queue until it is back
        client.connect_async(broker["host"], broker["port"])
        client.loop_start()

        def publish(record):
            client.publish(topic, json.dumps(record), qos=1)
            log.info("switch %(switch)s port %(port)s: link %(link)s", record)

        poller = Poller(
            specs=specs,
            read=lambda spec: clients[spec.index].get(request_oids(spec)),
            port_table=lambda: gateway.read_with_retry(gateway.PORTS_CONF, gateway.parse_ports_conf),
            leases=gateway.read_leases,
            release=lambda spec, port, cause: release_port_lease(spec.index, port, cause),
            publish=publish,
            keepalive=lambda: notify("WATCHDOG=1"),
        )
        log.info("polling %d switches every %s s; topic %s", len(specs), period, topic)
        notify("READY=1")
        run(poller, period)


def run(poller, period, polls=None, sleep=time.sleep, clock=time.monotonic):
    """Poll every `period` seconds (forever, or `polls` times): WATCHDOG=1 after each complete poll, and a summary
    line every SUMMARY_POLLS. An exception from a poll ends the process: systemd restarts it (Restart=always)."""
    done = 0
    while polls is None or done < polls:
        started = clock()
        poller.poll_once()
        notify("WATCHDOG=1")
        done += 1
        if poller.polls % SUMMARY_POLLS == 0:
            log.info("%d polls: %s", poller.polls, poller.totals)
        sleep(max(0.0, period - (clock() - started)))
