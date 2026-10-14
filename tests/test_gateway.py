"""snmp_switch.gateway: ports.conf and the dnsmasq lease file, parsed strictly."""

import pytest

from snmp_switch import gateway
from snmp_switch.gateway import (
    GatewayFileError,
    parse_leases,
    parse_ports_conf,
    port_of_vlan,
    read_leases,
    read_with_retry,
)
from snmp_switch.poller import Lease

# the shape fpgas.online-infra's ports.conf.j2 renders (two ports, then the quarantine range on the trunk)
PORTS_CONF = """\
# Ansible managed
interface=v2101
dhcp-range=tag:v2101,10.21.1.1,10.21.1.1,255.255.0.0,5m
dhcp-range=tag:v2101,2404:e80:a137:2101::1,2404:e80:a137:2101::1,off-link,64,5m
host-record=pi-sw1-p1,pi-sw1-p1.fpgas.example,10.21.1.1,2404:e80:a137:2101::1
interface=v2248
dhcp-range=tag:v2248,10.21.2.48,10.21.2.48,255.255.0.0,5m
dhcp-range=tag:v2248,2404:e80:a137:2102::48,2404:e80:a137:2102::48,off-link,64,5m
host-record=pi-sw2-p48,pi-sw2-p48.fpgas.example,10.21.2.48,2404:e80:a137:2102::48

interface=eth-local
dhcp-range=tag:eth-local,10.21.0.128,10.21.0.150,255.255.255.0,1h
enable-ra
"""

# the lines dnsmasq writes (lease.c lease_update_file): v4 leases, the server DUID, v6 leases
LEASES = """\
1791941144 b8:27:eb:e3:e7:e4 10.21.2.48 pi-sw2-p48 *
1791965048 98:fe:54:13:f5:9c 10.21.2.30 pi-sw2-p30 01:98:fe:54:13:f5:9c
duid 00:01:00:01:2e:4c:7a:10:52:54:00:12:34:56
1791959703 318764444 2404:e80:a137:2102::30 rpi5-new-13f59c 00:04:a1:b2:c3:d4:e5:f6:07:18:29:3a:4b:5c:6d:7e:8f:90
"""


def test_port_of_vlan_inverts_the_formula():
    assert port_of_vlan(2248) == (2, 48)
    assert port_of_vlan(2101) == (1, 1)
    with pytest.raises(GatewayFileError):
        port_of_vlan(2100)


def test_ports_conf_gives_each_ports_two_addresses_and_the_lease_time():
    addresses, seconds = parse_ports_conf(PORTS_CONF)
    assert addresses == {(1, 1): {"10.21.1.1", "2404:e80:a137:2101::1"},
                         (2, 48): {"10.21.2.48", "2404:e80:a137:2102::48"}}
    assert seconds == 300


def test_a_per_port_range_of_more_than_one_address_is_refused():
    with pytest.raises(GatewayFileError):
        parse_ports_conf("dhcp-range=tag:v2101,10.21.1.1,10.21.1.9,255.255.0.0,5m\n")


def test_per_port_lease_times_must_agree():
    with pytest.raises(GatewayFileError):
        parse_ports_conf(PORTS_CONF.replace("10.21.1.1,255.255.0.0,5m", "10.21.1.1,255.255.0.0,12h"))


def test_a_ports_conf_with_no_per_port_range_is_refused():
    with pytest.raises(GatewayFileError):
        parse_ports_conf("interface=eth-local\n")


def test_the_lease_file_gives_every_v4_and_v6_lease():
    assert parse_leases(LEASES) == [Lease("10.21.2.48", 1791941144), Lease("10.21.2.30", 1791965048),
                                    Lease("2404:e80:a137:2102::30", 1791959703)]


def test_an_empty_lease_file_has_no_leases():
    assert parse_leases("") == []


def test_lines_that_are_not_leases_are_passed_over():
    text = LEASES + "1791941144 b8:27:eb\n" + "garbage\n" + "1791941144 b8:27:eb:e3:e7:e4 not-an-address x *\n"
    assert parse_leases(text) == parse_leases(LEASES)


def test_real_dnsmasq_lines_with_an_underscore_name_or_a_temporary_v6_lease_are_read():
    # dnsmasq allows "_" in host names (util.c legal_hostname) and writes T<iaid> for a temporary address (lease.c)
    text = ("1791941144 b8:27:eb:e3:e7:e4 10.21.0.130 my_laptop 01:b8:27:eb:e3:e7:e4\n"
            "1791959703 T318764444 2404:e80:a137:2102::99 my_laptop 00:04:a1\n")
    assert parse_leases(text) == [Lease("10.21.0.130", 1791941144), Lease("2404:e80:a137:2102::99", 1791959703)]


def test_a_ports_conf_caught_mid_rewrite_is_read_again(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway.time, "sleep", lambda seconds: None)
    reads = iter(["interface=v2101\n", PORTS_CONF])
    path = tmp_path / "ports.conf"
    path.write_text("x")
    assert read_with_retry(path, lambda text: parse_ports_conf(next(reads)))[1] == 300


def test_a_ports_conf_that_never_reads_whole_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway.time, "sleep", lambda seconds: None)
    path = tmp_path / "ports.conf"
    path.write_text("interface=v2101\n")
    with pytest.raises(GatewayFileError):
        read_with_retry(path, parse_ports_conf)


def test_the_lease_file_counts_only_when_two_reads_agree(tmp_path):
    path = tmp_path / "leases"
    texts = iter(["", LEASES, LEASES])  # caught empty mid-rewrite, then whole twice
    path.write_text("1791941144 b8:27:eb")
    assert len(read_leases(path, sleep=lambda s: path.write_text(next(texts)))) == 3


def test_a_lease_file_that_never_reads_the_same_twice_is_an_error(tmp_path):
    path = tmp_path / "leases"
    count = iter(range(100))
    path.write_text(LEASES)
    with pytest.raises(GatewayFileError):
        read_leases(path, sleep=lambda s: path.write_text(LEASES * (next(count) + 2)))
