"""
rushes-watch — the self-healing ingest daemon.

Runs continuously inside the container. The host udev rule only *moves* a GoPro
interface into the container's network namespace; this daemon does everything
else, with retries, so timing no longer matters:

  - detects GoPro interfaces appearing in the namespace (poll /sys/class/net)
  - brings each up with bounded DHCP, retrying while the camera boots/wakes
  - waits for the camera API to report ready (not busy / not encoding)
  - sets Auto Power Down = Never and sends keep-alives, so once connected the
    camera never sleeps again — killing the readiness race at the source
  - ingests, then keeps re-syncing new clips while the camera stays present
  - one worker per interface; multiple cameras run concurrently

Logging goes to stdout (journal) via StreamHandler, which flushes per record —
no buffering blackout like the old print()-under-systemd path.
"""

import asyncio
import logging
import sys
from pathlib import Path

import httpx

from . import db, gopro, ingest, netsetup

log = logging.getLogger("rushes.watch")

# Interfaces that are never a GoPro: loopback and the container's own uplink.
IGNORE_INTERFACES = {"lo", "eth0"}

POLL_SECS        = 3     # how often to scan for new interfaces
READY_TIMEOUT    = 30    # seconds to wait for the camera API to come up per cycle
RESCAN_SECS      = 120   # while connected, re-check for new clips this often
BACKOFF_START    = 3
BACKOFF_MAX      = 60


def _gopro_interfaces() -> set[str]:
    # Real interfaces are symlinks to device dirs and have an `address` file.
    # This filters out control files like `bonding_masters`.
    return {
        p.name for p in Path("/sys/class/net").iterdir()
        if p.name not in IGNORE_INTERFACES and (p / "address").exists()
    }


def _present(iface: str) -> bool:
    return (Path(f"/sys/class/net/{iface}") / "address").exists()


async def _wait_ready(client: httpx.AsyncClient, timeout: int) -> dict | None:
    """Poll camera/state until the camera is booted and idle, or timeout."""
    loop     = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            state = await gopro.get_state(client)
            if gopro.is_ready(state):
                return state
            log.info("camera reachable but busy (booting/encoding), waiting…")
        except httpx.HTTPError:
            pass
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(2)


async def _bring_up_with_retry(iface: str) -> tuple[str, str, int] | None:
    """Retry bounded DHCP until we get a lease, the interface vanishes, or we've
    clearly got no camera. Returns (local_ip, camera_ip, table) or None."""
    backoff = BACKOFF_START
    while _present(iface):
        try:
            return await asyncio.to_thread(netsetup.bring_up, iface)
        except Exception as exc:
            log.warning("%s: network setup failed (%s); retry in %ds", iface, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
    return None


async def _handle(iface: str) -> None:
    conn = db.connect()
    db.init_db(conn)
    log.info("interface %s appeared — bringing up", iface)

    netinfo = await _bring_up_with_retry(iface)
    if not netinfo:
        log.info("%s vanished before it could be brought up", iface)
        return
    local_ip, camera_ip, table = netinfo

    try:
        async with gopro.make_client(camera_ip, local_address=local_ip) as client:
            # best-effort wired-control enable, then wait for readiness
            try:
                await gopro.enable_wired_usb(client)
            except httpx.HTTPError:
                pass

            state = await _wait_ready(client, READY_TIMEOUT)
            if state is None:
                log.warning("%s: camera at %s never became ready; will retry", iface, camera_ip)
                return

            serial, model = gopro.identify(state)
            serial = serial or "unknown"
            log.info("connected: %s (%s) at %s", model, serial, camera_ip)

            try:
                await gopro.set_auto_power_off_never(client)
            except httpx.HTTPError:
                pass

            keeper = asyncio.create_task(ingest.keep_alive_loop(client))
            try:
                first = True
                while _present(iface):
                    try:
                        found, pulled = await ingest.pull_all(conn, client, serial, model)
                        if pulled or first:
                            log.info("%s: %d new clip(s), %d on camera", serial, pulled, found)
                        first = False
                    except httpx.HTTPError as exc:
                        log.warning("%s: media sync failed (%s) — ending cycle", serial, exc)
                        break  # camera dropped; rediscovery will bring it back
                    await asyncio.sleep(RESCAN_SECS)
            finally:
                keeper.cancel()
    finally:
        await asyncio.to_thread(netsetup.tear_down, iface, local_ip, camera_ip, table)
        log.info("%s: worker finished", iface)


async def _main_loop() -> None:
    log.info("rushes-watch started — watching for GoPro interfaces")
    workers: dict[str, asyncio.Task] = {}
    while True:
        for iface in _gopro_interfaces():
            if iface not in workers:
                workers[iface] = asyncio.create_task(_handle(iface))
        for iface, task in list(workers.items()):
            if task.done():
                if not task.cancelled() and task.exception():
                    log.error("%s worker crashed: %r", iface, task.exception())
                del workers[iface]
        await asyncio.sleep(POLL_SECS)


def main() -> None:
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # silence per-request spam
    asyncio.run(_main_loop())
