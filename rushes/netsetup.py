"""
Brings up a USB CDC-ECM interface inside the container and configures per-interface
policy routing. This is what makes multiple simultaneous GoPros (all advertising
themselves at 10.5.5.9) work without collision.

Each interface gets its own routing table (200, 201, ...). A policy rule
"from <local_ip> lookup <table>" means traffic bound to that local IP is sent
through the correct interface — which is how httpx.AsyncHTTPTransport(local_address=...)
ends up talking to the right camera.
"""

import subprocess
from contextlib import contextmanager

GOPRO_IP = "10.5.5.9"
_ROUTE_TABLE_BASE = 200


def _run(cmd: list[str], check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def _local_ip(interface: str) -> str | None:
    for line in _run(["ip", "addr", "show", interface], check=False).splitlines():
        line = line.strip()
        if line.startswith("inet ") and not line.startswith("inet6"):
            return line.split()[1].split("/")[0]
    return None


def _next_table() -> int:
    used = set()
    for line in _run(["ip", "rule", "show"], check=False).splitlines():
        parts = line.split()
        if "lookup" in parts:
            try:
                used.add(int(parts[parts.index("lookup") + 1]))
            except (ValueError, IndexError):
                pass
    for t in range(_ROUTE_TABLE_BASE, _ROUTE_TABLE_BASE + 32):
        if t not in used:
            return t
    return _ROUTE_TABLE_BASE + len(used)


def bring_up(interface: str) -> tuple[str, int]:
    """
    Bring up interface, run DHCP, add policy routing.
    Returns (local_ip, routing_table_id) needed for teardown.
    Raises RuntimeError if DHCP fails.
    """
    print(f"netsetup: link up {interface}")
    _run(["ip", "link", "set", interface, "up"])

    # GoPro runs its own DHCP server; -1 means exit after first lease
    print(f"netsetup: DHCP on {interface}")
    _run(["dhclient", "-1", "-v", interface])

    local_ip = _local_ip(interface)
    if not local_ip:
        raise RuntimeError(f"DHCP on {interface} did not produce an IP")

    table = _next_table()
    _run(["ip", "route", "add", GOPRO_IP, "dev", interface, "table", str(table)], check=False)
    _run(["ip", "rule",  "add", "from", local_ip, "lookup", str(table)], check=False)

    print(f"netsetup: {interface} → {local_ip}, routing table {table}")
    return local_ip, table


def tear_down(interface: str, local_ip: str, table: int) -> None:
    _run(["ip", "rule",  "del", "from",  local_ip, "lookup", str(table)], check=False)
    _run(["ip", "route", "del", GOPRO_IP, "dev", interface, "table", str(table)], check=False)
    _run(["dhclient", "-r", interface], check=False)
    _run(["ip", "link", "set", interface, "down"], check=False)
    print(f"netsetup: cleaned up {interface}")


@contextmanager
def managed_interface(interface: str):
    """Context manager: sets up networking on entry, tears it down on exit."""
    local_ip, table = bring_up(interface)
    try:
        yield local_ip
    finally:
        tear_down(interface, local_ip, table)
