"""The port policy the tests' Django settings name (SNMP_SWITCH_PORT_POLICY):
a site offers exactly the (switch, port) pairs a test put in OFFERED. The
real one lives in the Django project (fpgas.online-site)."""

OFFERED = set()
ASKED = []


def offered(request, switch, port):
    ASKED.append((switch, port))
    return (switch, port) in OFFERED
