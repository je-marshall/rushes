"""
Brings up a GoPro USB CDC-NCM interface inside the container and configures
per-interface policy routing so multiple simultaneous cameras don't collide.

Over USB each GoPro sits on its own serial-derived /24 (172.2x.1xx.0/24): the
camera is .51, the host gets .52 by DHCP. We:
  1. DHCP the interface (WITHOUT letting it touch the container's default route
     or DNS — the GoPro is not a gateway to anywhere).
  2. Derive the camera IP as .51 of the assigned /24.
  3. Give the interface its own routing table + a policy rule
     "from <local_ip> lookup <table>", so httpx bound to that local IP reaches
     the right camera even with several plugged in at once.
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path

_ROUTE_TABLE_BASE = 200
_DHCP_TIMEOUT = 25  # seconds — dhclient must not hang forever when the camera is asleep

# Minimal dhclient config: ask only for the address details, never routers or
# DNS, so dhclient can't overwrite the container's default route / resolv.conf.
_DHCLIENT_CONF = "/run/rushes-dhclient.conf"
_DHCLIENT_CONF_BODY = "request subnet-mask, broadcast-address;\n"


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


def _camera_ip(local_ip: str) -> str:
    """The camera is always .51 on the same /24 as the DHCP-assigned host IP."""
    a, b, c, _ = local_ip.split(".")
    return f"{a}.{b}.{c}.51"


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


def bring_up(interface: str) -> tuple[str, str, int]:
    """
    Bring up interface, DHCP it, add policy routing.
    Returns (local_ip, camera_ip, routing_table_id) for teardown.
    Raises RuntimeError if DHCP fails.
    """
    print(f"netsetup: link up {interface}", flush=True)
    _run(["ip", "link", "set", interface, "up"])
    # Idempotent: kill any stale dhclient on this interface and clear its lease so
    # a retry doesn't hit "already assigned" or fight another client.
    _run(["pkill", "-f", f"dhclient.*{interface}"], check=False)
    _run(["ip", "addr", "flush", "dev", interface], check=False)

    Path(_DHCLIENT_CONF).write_text(_DHCLIENT_CONF_BODY)

    # Bounded: dhclient -1 gives up after one lease attempt, but we still cap it
    # with a hard timeout so an asleep camera (no DHCP server) can't hang ingest.
    print(f"netsetup: DHCP on {interface} (<= {_DHCP_TIMEOUT}s)", flush=True)
    try:
        subprocess.run(
            ["dhclient", "-1", "-cf", _DHCLIENT_CONF, interface],
            capture_output=True, timeout=_DHCP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pass

    # dhclient daemonizes and keeps renewing after it gets a lease. Kill it and
    # keep the leased address statically for the session (we don't `-r`, so the
    # address stays configured) — otherwise a stray/renewing dhclient can flap
    # the link mid-transfer and drop every download.
    _run(["pkill", "-f", f"dhclient.*{interface}"], check=False)

    # Belt-and-suspenders: if dhclient still installed a default route via the
    # camera, remove it — the GoPro must never become the container's gateway.
    for line in _run(["ip", "route", "show", "default"], check=False).splitlines():
        if f"dev {interface}" in line:
            _run(["ip", "route", "del"] + line.split(), check=False)

    local_ip = _local_ip(interface)
    if not local_ip:
        raise RuntimeError(f"DHCP on {interface} did not produce an IP (camera asleep?)")
    camera_ip = _camera_ip(local_ip)

    table = _next_table()
    _run(["ip", "route", "add", camera_ip, "dev", interface, "table", str(table)], check=False)
    _run(["ip", "rule",  "add", "from", local_ip, "lookup", str(table)], check=False)

    print(f"netsetup: {interface} → host {local_ip}, camera {camera_ip}, table {table}", flush=True)
    return local_ip, camera_ip, table


def tear_down(interface: str, local_ip: str, camera_ip: str, table: int) -> None:
    _run(["ip", "rule",  "del", "from",  local_ip, "lookup", str(table)], check=False)
    _run(["ip", "route", "del", camera_ip, "dev", interface, "table", str(table)], check=False)
    _run(["dhclient", "-r", interface], check=False)
    _run(["pkill", "-f", f"dhclient.*{interface}"], check=False)
    _run(["ip", "link", "set", interface, "down"], check=False)
    print(f"netsetup: cleaned up {interface}", flush=True)


@contextmanager
def managed_interface(interface: str):
    """Context manager: sets up networking on entry, tears it down on exit.
    Yields (local_ip, camera_ip)."""
    local_ip, camera_ip, table = bring_up(interface)
    try:
        yield local_ip, camera_ip
    finally:
        tear_down(interface, local_ip, camera_ip, table)
