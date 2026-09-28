# Ingest Recorder

Production-oriented FastAPI recorder for newsroom ingest/archive workflows.

The application manages multiple FFmpeg child processes inside one container. Each enabled channel records to `/spool` as wall-clock-aligned MKV segments and a background archive worker safely transfers completed files to `/recordings`.

## Features

- Python 3 + FastAPI backend with server-rendered HTML dashboard.
- SQLite configuration at `/data/recorder.db`.
- One FFmpeg process per enabled channel, supervised by the app.
- RTMP, SRT, HLS, and any other URL FFmpeg can open.
- Stream copy by default with `-c copy`.
- 5-minute default MKV segmenting with `-segment_atclocktime 1`.
- Local spool first, NAS archive second.
- NAS safety checks before archive writes.
- Atomic archive publish using copy to temporary file, fsync, then rename.
- REST API and OpenAPI docs at `/docs`.

## Storage Model

Mount storage from the host:

- `/data` stores SQLite.
- `/spool` stores active and queued local recordings.
- `/recordings` is the NAS archive bind mount.

The container does not mount SMB/CIFS. Mount the NAS on the Linux host first, then bind-mount that host path into Docker.

By default `REQUIRE_RECORDINGS_MOUNT=true`. If `/recordings` is not a mount point, the archive is marked unavailable and the worker will not write there. You can also set `RECORDINGS_EXPECTED_FSTYPE=cifs,smb3` to require a specific filesystem type when your runtime exposes that accurately.

## Quick Start

```bash
cp .env.example .env
docker compose up --build -d
```

Open:

```text
http://localhost:8080
```

To attach the recorder to an existing Restreamer network, create or identify that network:

```bash
docker network create restreamer_default
```

Then start with the optional override:

```bash
docker compose -f docker-compose.yml -f docker-compose.restreamer.yml up --build -d
```

Set `RESTREAMER_NETWORK` in `.env` if the external Docker network has a different name.

## Channel URLs

For MVP, add channel input URLs manually in the web UI, for example:

```text
rtmp://restreamer:1935/live/REMOTE1
srt://0.0.0.0:9000?mode=listener
https://example.com/live/playlist.m3u8
```

Credentials in URLs are redacted from UI and logs where practical.

## API

Common endpoints:

- `GET /api/channels`
- `POST /api/channels`
- `GET /api/channels/{id}`
- `PATCH /api/channels/{id}`
- `DELETE /api/channels/{id}`
- `POST /api/channels/{id}/restart`
- `GET /api/status`
- `GET /healthz`

OpenAPI documentation remains available at `/docs`.

## Notes

FFmpeg segment files appear in the spool while being written. The archiver avoids the newest active segment for a recording channel and only transfers closed-looking files after a grace period. If the NAS is offline, files remain in `/spool` and are retried automatically.

For deterministic long-term operations, monitor Docker logs and host disk free space:

```bash
docker logs -f ingest-recorder
```
