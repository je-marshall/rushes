# Rushes

Plug in a GoPro, footage gets ingested, catalogued, and browsable on the TV.

## What it does

- Server has USB-C ports exposed; plug in a GoPro and it auto-detects and backs up footage
- SQLite database tracks ingested files (filename, date, camera, duration, checksum)
- Clips can be favourited / flagged for review
- Jellyfin plugin for browsing clips on TV and marking them from the couch

## Hardware / cameras

- GoPro Hero 10 and Hero 13
- Both support the **Open GoPro API** over USB-C (BLE + HTTP over USB)
- Also support plain USB mass storage mode as a fallback
- Open GoPro is cleaner: can query media list, pull files, get metadata, check battery/status

## Key decisions to make

- Open GoPro API vs mass storage — API gives richer metadata and status, mass storage is simpler
- Jellyfin plugin (C# .NET) vs a separate web UI that Jellyfin links into
- Whether to transcode on ingest or serve originals (GoPro H.265 plays fine in Jellyfin)
- How to handle multi-camera (10 and 13 plugged in simultaneously)

## Tech stack (provisional)

- Python backend (Flask or FastAPI) for ingestion service + API
- SQLite for clip metadata
- udev rules for USB detection / auto-trigger on plug-in
- Jellyfin plugin in C# for TV browsing, or a simple web UI
