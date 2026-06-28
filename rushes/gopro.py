"""
Minimal Open GoPro HTTP client (USB mode).

When connected via USB-C, the GoPro exposes an HTTP API at 10.5.5.9:8080
over a CDC-ECM virtual ethernet interface. Each camera is always at the same
IP, so when multiple cameras are connected we pass local_address to bind the
httpx client to the IP of the specific interface for that camera.
"""

from dataclasses import dataclass
from typing import Any

import httpx

GOPRO_IP   = "10.5.5.9"
GOPRO_BASE = f"http://{GOPRO_IP}:8080"


@dataclass
class MediaFile:
    filename:  str
    directory: str
    size:      int

    @property
    def download_url(self) -> str:
        return f"{GOPRO_BASE}/videos/DCIM/{self.directory}/{self.filename}"


def make_client(local_address: str | None = None, timeout: int = 60) -> httpx.AsyncClient:
    """
    Create an httpx client optionally bound to a specific local IP.
    local_address should be the IP assigned to the USB interface for this camera.
    Combined with policy routing in netsetup.py, this ensures traffic for this
    client goes through the correct USB interface even when multiple cameras are
    connected simultaneously.
    """
    transport = httpx.AsyncHTTPTransport(local_address=local_address) if local_address else None
    return httpx.AsyncClient(base_url=GOPRO_BASE, timeout=timeout, transport=transport)


async def get_media_list(client: httpx.AsyncClient) -> list[MediaFile]:
    resp = await client.get("/gopro/media/list")
    resp.raise_for_status()
    files: list[MediaFile] = []
    for media_dir in resp.json().get("media", []):
        d = media_dir["d"]
        for f in media_dir.get("fs", []):
            name = f["n"]
            if not name.upper().endswith(".MP4"):
                continue  # skip .LRV proxy clips and .THM thumbnails
            files.append(MediaFile(filename=name, directory=d, size=int(f.get("s", 0))))
    return files


async def get_camera_info(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.get("/gopro/camera/info")
    resp.raise_for_status()
    return resp.json().get("info", {})
