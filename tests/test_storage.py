from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import settings
from app.storage import StorageManager


class RecorderState:
    def __init__(self, *, active_file: Path | None = None, recording: bool = False) -> None:
        self.active_file = active_file
        self.recording = recording

    def active_spool_file(self, channel_id: int) -> Path | None:
        return self.active_file

    def is_recording(self, channel_id: int) -> bool:
        return self.recording


class StorageArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.manager = StorageManager()
        self.manager.spool_dir = root / "spool"
        self.manager.recordings_dir = root / "recordings"
        self.manager.init()
        self.manager.archive_available = Mock(return_value=True)
        self.channel = {
            "id": 1,
            "display_name": "REMOTE1",
            "output_folder": "",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_segment(self, name: str, mtime: float) -> Path:
        spool_dir = self.manager.channel_spool_dir(self.channel)
        spool_dir.mkdir(parents=True, exist_ok=True)
        segment = spool_dir / name
        segment.write_bytes(name.encode("ascii"))
        os.utime(segment, (mtime, mtime))
        return segment

    async def test_old_completed_segment_uses_wall_clock_and_is_archived(self) -> None:
        wall_now = time.time()
        segment = self.create_segment("completed.mkv", wall_now - 60)

        with (
            patch("app.storage.time.time", return_value=wall_now) as wall_clock,
            patch.object(settings, "segment_close_grace_seconds", 15),
        ):
            await self.manager.archive_once([self.channel], RecorderState())

        wall_clock.assert_called_once_with()
        self.assertFalse(segment.exists())
        archived = self.manager.recordings_dir / segment.name
        self.assertTrue(archived.exists())
        self.assertEqual(archived.read_bytes(), b"completed.mkv")

    async def test_active_and_newest_recording_segments_are_skipped(self) -> None:
        wall_now = time.time()
        completed = self.create_segment("completed.mkv", wall_now - 90)
        active = self.create_segment("active.mkv", wall_now - 60)
        newest = self.create_segment("newest.mkv", wall_now - 30)
        recorder = RecorderState(active_file=active, recording=True)

        with (
            patch("app.storage.time.time", return_value=wall_now),
            patch.object(settings, "segment_close_grace_seconds", 15),
        ):
            await self.manager.archive_once([self.channel], recorder)

        self.assertFalse(completed.exists())
        self.assertTrue((self.manager.recordings_dir / completed.name).exists())
        self.assertTrue(active.exists())
        self.assertTrue(newest.exists())
        self.assertFalse((self.manager.recordings_dir / active.name).exists())
        self.assertFalse((self.manager.recordings_dir / newest.name).exists())


if __name__ == "__main__":
    unittest.main()
