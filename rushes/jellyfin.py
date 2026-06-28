"""
Jellyfin API calls. All functions are fire-and-forget — a Jellyfin outage
should never block ingest or the web UI.
"""

import httpx
from . import config


def trigger_rescan() -> None:
    if not config.JELLYFIN_URL or not config.JELLYFIN_TOKEN:
        return
    try:
        with httpx.Client(timeout=10) as client:
            client.post(
                f"{config.JELLYFIN_URL}/Library/Refresh",
                headers={"X-Emby-Token": config.JELLYFIN_TOKEN},
            )
    except Exception:
        pass  # Jellyfin being down must not affect anything else
