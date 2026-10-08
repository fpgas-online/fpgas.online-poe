"""A fake ``runner`` for netgear_switch's NetsnmpCliClient that answers from a
recorded ``snmpbulkwalk -On`` file instead of running net-snmp against a switch.

The client calls ``runner(argv, capture_output=True, text=True, check=False)``
with argv ``[binary, -v2c, -c, <community>, <output flags>, -t, N, -r, N,
<host>, <oid>...]``. This answers ``snmpbulkwalk`` with the recorded lines
under the OID, in the recorded format, and ``snmpget`` with the lines of the
OIDs asked for. What a real agent says for an OID that holds nothing is said
too: the lone "No Such Object" line for a walk, "No Such Instance" for a get.
"""

import types
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_walk(path):
    """The recorded lines as (oid without the leading dot, whole line) pairs, in file order."""
    rows = []
    for line in Path(path).read_text().splitlines():
        if line.startswith(".") and " = " in line:
            rows.append((line.split(" = ", 1)[0][1:], line))
    return rows


def _under(oid, base):
    return oid == base or oid.startswith(base + ".")


class WalkRunner:
    """Callable with subprocess.run's signature, answering from a walk file."""

    def __init__(self, path):
        self.rows = load_walk(path)
        self.calls = []  # (binary name, [oids asked for])

    def __call__(self, argv, **kwargs):
        binary = Path(argv[0]).name
        # the oids follow the host, which follows "-r <retries>"
        rest = argv[argv.index("-r") + 2:]
        oids = [o.lstrip(".") for o in rest[1:]]
        self.calls.append((binary, oids))
        if binary == "snmpbulkwalk":
            return self._walk(oids[0])
        if binary == "snmpget":
            return self._get(oids)
        raise AssertionError(f"the recorded walk cannot answer {binary}: it is read-only")

    @staticmethod
    def _done(stdout):
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def _walk(self, base):
        lines = [line for oid, line in self.rows if _under(oid, base)]
        if not lines:
            lines = [f".{base} = No Such Object available on this agent at this OID"]
        return self._done("".join(f"{line}\n" for line in lines))

    def _get(self, oids):
        by_oid = dict(self.rows)
        lines = [by_oid.get(o, f".{o} = No Such Instance currently exists at this OID") for o in oids]
        return self._done("".join(f"{line}\n" for line in lines))
