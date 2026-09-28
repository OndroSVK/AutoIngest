from __future__ import annotations

import os
from pathlib import Path


class Settings:
    app_name = "Ingest Recorder"
    app_port = int(os.getenv("APP_PORT", "8080"))
    timezone = os.getenv("TZ", "Europe/Bratislava")
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    spool_dir = Path(os.getenv("SPOOL_DIR", "/spool"))
    recordings_dir = Path(os.getenv("RECORDINGS_DIR", "/recordings"))
    database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "recorder.db")))
    require_recordings_mount = os.getenv("REQUIRE_RECORDINGS_MOUNT", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    recordings_expected_fstype = os.getenv("RECORDINGS_EXPECTED_FSTYPE", "").strip().lower()
    archive_interval_seconds = int(os.getenv("ARCHIVE_INTERVAL_SECONDS", "10"))
    recorder_retry_seconds = int(os.getenv("RECORDER_RETRY_SECONDS", "10"))
    segment_close_grace_seconds = int(os.getenv("SEGMENT_CLOSE_GRACE_SECONDS", "15"))
    ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe_path = os.getenv("FFPROBE_PATH", "ffprobe")


settings = Settings()
