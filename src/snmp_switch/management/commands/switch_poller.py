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
        addresses, lease_seconds = gateway.read_with_retry(gateway.PORTS_CONF, gateway.parse_ports_conf)
        topic = f"fpgas/{site}/gw/port"

        broker = settings.FLEET_MQTT
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect(broker["host"], broker["port"])
        client.loop_start()

        def publish(record):
            client.publish(topic, json.dumps(record), qos=1)
            log.info("switch %(switch)s port %(port)s: link %(link)s", record)

        poller = Poller(
            specs=specs,
            read=lambda spec: clients[spec.index].get(request_oids(spec)),
            port_addresses=lambda: addresses,
            leases=lambda: gateway.read_with_retry(gateway.LEASE_FILE, gateway.parse_leases),
            release=lambda spec, port, cause: release_port_lease(spec.index, port, cause),
            publish=publish,
            lease_seconds=lease_seconds,
        )
        log.info("polling %d switches every %s s; topic %s", len(specs), period, topic)
        notify("READY=1")
        while True:
            started = time.monotonic()
            poller.poll_once()
            notify("WATCHDOG=1")
            if poller.polls % SUMMARY_POLLS == 0:
                log.info("%d polls: %s", poller.polls, poller.totals)
            time.sleep(max(0.0, period - (time.monotonic() - started)))
