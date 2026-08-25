"""
Minimal Open GoPro HTTP client (USB / wired mode).

Addressing note: 10.5.5.9 is the GoPro's *WiFi* access-point address. Over USB
the camera uses a per-serial subnet of the form 172.2x.1xx.51 and runs a DHCP
server that hands the host .52 on the same /24. So the camera IP is discovered
at connect time (see netsetup.bring_up) and passed in here — never hardcoded.

Endpoints verified against the Open GoPro OpenAPI spec (HTTP API 2.0) for
Hero 10 and Hero 13:
  - GET /gopro/camera/control/wired_usb?p=1   enable wired control
  - GET /gopro/camera/keep_alive              prevent sleep (send every ~3s)
  - GET /gopro/camera/setting?setting=59&option=0   Auto Power Down = Never
  - GET /gopro/camera/state                   settings + statuses
  - GET /gopro/media/list                     media directories + files
  - GET /videos/DCIM/<dir>/<file>             download a clip

/gopro/camera/info returns {} on Hero 10, so identification uses camera/state:
status 30 = serial number, status 8 = busy, status 10 = encoding.
"""

from dataclasses import dataclass
from typing import Any

import httpx

# camera/state status IDs (Open GoPro statuses spec)
STATUS_BUSY     = "8"
STATUS_ENCODING = "10"
STATUS_SERIAL   = "30"

SETTING_AUTO_POWER_DOWN = 59
AUTO_POWER_DOWN_NEVER   = 0

KEEP_ALIVE_SECS = 3.0

# Fallback when no USB interface is given (e.g. talking to a camera over WiFi,
# where the AP address is fixed). USB always discovers the real IP via DHCP.
DEFAULT_CAMERA_IP = "10.5.5.9"


@dataclass
class MediaFile:
    filename:  str
    directory: str
    size:      int
    created:   int | None = None   # 'cre' from the media list — Unix seconds (camera clock)
    kind:      str = "video"       # "video" or "photo"

    def _path(self, name: str) -> str:
        return f"/videos/DCIM/{self.directory}/{name}"

    @property
    def _stem(self) -> str:
        return self.filename.rsplit(".", 1)[0]

    @property
    def download_path(self) -> str:
        # Relative to the client base_url (the camera).
        return self._path(self.filename)

    @property
    def gpr_path(self) -> str | None:
        # A RAW photo has a same-stem .GPR sibling (not listed; fetched by path).
        return self._path(self._stem + ".GPR") if self.kind == "photo" else None

    @property
    def thm_path(self) -> str | None:
        # The .THM shares the clip's stem (GX010001.MP4 -> GX010001.THM). These
        # sidecars aren't in /media/list but the camera serves them by path, so
        # we construct the URL and let the download 404 if it doesn't exist.
        return self._path(self._stem + ".THM")

    @property
    def lrv_path(self) -> str | None:
        # The .LRV proxy uses a 'GL' prefix (GX010001.MP4 -> GL010001.LRV).
        stem = self._stem
        if len(stem) < 3:
            return None
        return self._path("GL" + stem[2:] + ".LRV")


def _tail(name: str) -> str:
    """GoPro's file-identity key: the AABBBB tail shared by a clip and its
    sibling .LRV/.THM (e.g. GX010001.MP4 / GL010001.LRV / GX010001.THM)."""
    stem = name.rsplit(".", 1)[0]
    return stem[-6:]


def make_client(camera_ip: str, local_address: str | None = None,
                timeout: int = 60) -> httpx.AsyncClient:
    """
    httpx client pointed at a specific camera, optionally bound to the local IP
    of that camera's USB interface. Binding local_address + the policy routing
    from netsetup.py is what keeps multiple simultaneous cameras from colliding
    (they'd otherwise all look identical at the transport layer).
    """
    base = f"http://{camera_ip}:8080"
    transport = httpx.AsyncHTTPTransport(local_address=local_address) if local_address else None
    return httpx.AsyncClient(base_url=base, timeout=timeout, transport=transport)


async def enable_wired_usb(client: httpx.AsyncClient) -> None:
    """Enable wired control. Hero 10 needs this before it will serve the API."""
    resp = await client.get("/gopro/camera/control/wired_usb", params={"p": 1})
    resp.raise_for_status()


async def keep_alive(client: httpx.AsyncClient) -> None:
    resp = await client.get("/gopro/camera/keep_alive")
    resp.raise_for_status()


async def set_auto_power_off_never(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/gopro/camera/setting",
        params={"setting": SETTING_AUTO_POWER_DOWN, "option": AUTO_POWER_DOWN_NEVER},
    )
    resp.raise_for_status()


async def get_state(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.get("/gopro/camera/state")
    resp.raise_for_status()
    return resp.json()


def identify(state: dict[str, Any]) -> tuple[str, str]:
    """
    Return (serial, model). Serial comes from state status 30. Model name is not
    reliably exposed over the wired API on Hero 10 (camera/info returns {}), so
    we default to "GoPro"; the user renames cameras in the UI anyway.
    """
    status = state.get("status", {})
    serial = status.get(STATUS_SERIAL) or "unknown"
    return serial, "GoPro"


def is_ready(state: dict[str, Any]) -> bool:
    status = state.get("status", {})
    return status.get(STATUS_BUSY, 0) == 0 and status.get(STATUS_ENCODING, 0) == 0


async def get_media_list(client: httpx.AsyncClient) -> list[MediaFile]:
    resp = await client.get("/gopro/media/list")
    resp.raise_for_status()
    files: list[MediaFile] = []
    for media_dir in resp.json().get("media", []):
        d = media_dir["d"]
        for f in media_dir.get("fs", []):
            name = f["n"]
            up = name.upper()
            if up.endswith(".MP4"):
                kind = "video"
            elif up.endswith(".JPG") or up.endswith(".JPEG"):
                kind = "photo"
            else:
                continue  # skip .LRV/.THM/.GPR — handled as siblings, not top-level
            created = f.get("cre")
            files.append(MediaFile(
                filename=name, directory=d, size=int(f.get("s", 0)),
                created=int(created) if created else None, kind=kind,
            ))
    return files
