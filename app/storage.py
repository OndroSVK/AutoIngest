from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .security import safe_channel_slug, validate_output_folder

log = logging.getLogger("ingest.storage")


@dataclass
class StorageStatus:
    path: str
    exists: bool
    writable: bool
    mounted: bool
    filesystem: str
    available: bool
    free_bytes: int
    error: str = ""


class StorageManager:
    def __init__(self) -> None:
        self.spool_dir = settings.spool_dir
        self.recordings_dir = settings.recordings_dir
        self._last_available: bool | None = None

    def init(self) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def spool_status(self) -> StorageStatus:
        return self._status(self.spool_dir, require_mount=False)

    def recordings_status(self) -> StorageStatus:
        return self._status(self.recordings_dir, require_mount=settings.require_recordings_mount)

    def archive_available(self) -> bool:
        status = self.recordings_status()
        if self._last_available is not status.available:
            log.info("recordings_storage_state", extra={"available": status.available, "error": status.error})
            self._last_available = status.available
        return status.available

    def channel_spool_dir(self, channel: dict) -> Path:
        return self.spool_dir / f"{int(channel['id']):04d}_{safe_channel_slug(channel['display_name'])}"

    def output_dir(self, channel: dict) -> Path:
        folder = validate_output_folder(channel.get("output_folder") or "")
        return self.recordings_dir / folder if folder else self.recordings_dir

    def segment_pattern(self, channel: dict) -> str:
        spool = self.channel_spool_dir(channel)
        spool.mkdir(parents=True, exist_ok=True)
        return str(spool / f"{safe_channel_slug(channel['display_name'])}_%Y-%m-%d_%H-%M-%S.mkv")

    def queue_summary(self) -> dict:
        count = 0
        total = 0
        by_channel: dict[int, int] = {}
        for file in self.spool_dir.glob("*/*.mkv"):
            if not file.is_file():
                continue
            count += 1
            total += file.stat().st_size
            try:
                channel_id = int(file.parent.name.split("_", 1)[0])
                by_channel[channel_id] = by_channel.get(channel_id, 0) + 1
            except ValueError:
                pass
        return {"count": count, "bytes": total, "by_channel": by_channel}

    async def archive_loop(self, db, recorder_manager, stop_event: asyncio.Event) -> None:
        self.init()
        while not stop_event.is_set():
            try:
                await self.archive_once(db.list_channels(), recorder_manager)
            except Exception:
                log.exception("archive_worker_failure")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.archive_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def archive_once(self, channels: list[dict], recorder_manager) -> None:
        if not self.archive_available():
            return
        now = asyncio.get_running_loop().time()
        for channel in channels:
            spool_dir = self.channel_spool_dir(channel)
            files = sorted(spool_dir.glob("*.mkv"), key=lambda p: p.stat().st_mtime)
            if not files:
                continue
            active_skip = recorder_manager.active_spool_file(int(channel["id"]))
            newest = files[-1] if recorder_manager.is_recording(int(channel["id"])) else None
            for file in files:
                if active_skip and file == active_skip:
                    continue
                if newest and file == newest:
                    continue
                age = now - file.stat().st_mtime
                if age < settings.segment_close_grace_seconds:
                    continue
                await asyncio.to_thread(self.transfer_file, channel, file)

    def transfer_file(self, channel: dict, source: Path) -> None:
        dest_dir = self.output_dir(channel)
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = self._dedupe(dest_dir / source.name)
        temp = final.with_name(f".{final.name}.tmp-{uuid.uuid4().hex}")
        log.info("archive_transfer_start", extra={"source": str(source), "destination": str(final)})
        try:
            with source.open("rb") as src, temp.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temp, final)
            self._fsync_dir(dest_dir)
            source.unlink()
            log.info("archive_transfer_complete", extra={"destination": str(final)})
        except Exception:
            if temp.exists():
                temp.unlink(missing_ok=True)
            log.exception("archive_transfer_failed", extra={"source": str(source), "destination": str(final)})
            raise

    def _status(self, path: Path, require_mount: bool) -> StorageStatus:
        exists = path.exists()
        writable = False
        mounted = os.path.ismount(path)
        filesystem = self._filesystem_type(path)
        free = 0
        error = ""
        if exists:
            try:
                usage = shutil.disk_usage(path)
                free = usage.free
                probe = path / f".write-test-{uuid.uuid4().hex}"
                with probe.open("w") as fh:
                    fh.write("ok")
                    fh.flush()
                    os.fsync(fh.fileno())
                probe.unlink()
                writable = True
            except Exception as exc:
                error = str(exc)
        else:
            error = "path does not exist"
        if require_mount and not mounted:
            error = "path is not a mount point"
        if settings.recordings_expected_fstype and path == self.recordings_dir:
            expected = {item.strip() for item in settings.recordings_expected_fstype.split(",") if item.strip()}
            if filesystem not in expected:
                error = f"filesystem type '{filesystem or 'unknown'}' does not match expected {sorted(expected)}"
        return StorageStatus(
            path=str(path),
            exists=exists,
            writable=writable,
            mounted=mounted,
            filesystem=filesystem,
            available=exists and writable and (mounted or not require_mount) and not error,
            free_bytes=free,
            error=error,
        )

    def _dedupe(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for idx in range(1, 1000):
            candidate = path.with_name(f"{stem}_{idx}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Too many duplicate destination names for {path}")

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _filesystem_type(self, path: Path) -> str:
        try:
            best_mount = ""
            best_type = ""
            path_text = str(path)
            with Path("/proc/mounts").open() as mounts:
                for line in mounts:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mount_point = parts[1].replace("\\040", " ")
                    normalized = mount_point.rstrip("/") or "/"
                    if path_text == normalized or path_text.startswith(normalized.rstrip("/") + "/"):
                        if len(normalized) > len(best_mount):
                            best_mount = normalized
                            best_type = parts[2].lower()
            return best_type
        except OSError:
            return ""


storage = StorageManager()
