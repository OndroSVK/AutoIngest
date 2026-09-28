from __future__ import annotations

import asyncio
import logging
import re
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .security import redact_url
from .storage import storage

log = logging.getLogger("ingest.recorder")
BITRATE_RE = re.compile(r"bitrate=\s*([^ ]+)")


@dataclass
class ChannelRuntime:
    state: str = "DISABLED"
    pid: int | None = None
    current_file: str = ""
    input_info: str = ""
    bitrate: str = ""
    last_error: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    stopping: bool = False


class RecorderManager:
    def __init__(self, db) -> None:
        self.db = db
        self.runtimes: dict[int, ChannelRuntime] = {}
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        for channel in self.db.list_channels():
            self.ensure_channel(channel)

    async def stop(self) -> None:
        self._stop_event.set()
        await asyncio.gather(*(self.stop_channel(cid) for cid in list(self.runtimes)), return_exceptions=True)

    def ensure_channel(self, channel: dict) -> None:
        channel_id = int(channel["id"])
        runtime = self.runtimes.setdefault(channel_id, ChannelRuntime())
        if channel["enabled"] and channel["automatic_recording"]:
            if not runtime.task or runtime.task.done():
                runtime.stopping = False
                runtime.task = asyncio.create_task(self._run_channel(channel_id), name=f"recorder-{channel_id}")
        else:
            runtime.state = "DISABLED"

    async def refresh(self) -> None:
        configured = {int(ch["id"]): ch for ch in self.db.list_channels()}
        for channel in configured.values():
            self.ensure_channel(channel)
        for channel_id in set(self.runtimes) - set(configured):
            await self.stop_channel(channel_id)
            self.runtimes.pop(channel_id, None)

    async def restart_channel(self, channel_id: int) -> None:
        await self.stop_channel(channel_id)
        channel = self.db.get_channel(channel_id)
        if channel:
            runtime = self.runtimes.setdefault(channel_id, ChannelRuntime())
            runtime.stopping = False
            self.ensure_channel(channel)

    async def stop_channel(self, channel_id: int) -> None:
        runtime = self.runtimes.setdefault(channel_id, ChannelRuntime())
        runtime.stopping = True
        if runtime.process and runtime.process.returncode is None:
            await self._terminate_process(runtime)
        if runtime.task and not runtime.task.done():
            runtime.task.cancel()
            try:
                await runtime.task
            except asyncio.CancelledError:
                pass
        runtime.pid = None
        runtime.process = None
        runtime.state = "DISABLED"

    def snapshot(self, channel_id: int) -> dict:
        runtime = self.runtimes.setdefault(channel_id, ChannelRuntime())
        return {
            "state": runtime.state,
            "pid": runtime.pid,
            "current_file": runtime.current_file,
            "input_info": runtime.input_info,
            "bitrate": runtime.bitrate,
            "last_error": runtime.last_error,
            "logs": list(runtime.logs),
        }

    def all_snapshots(self) -> dict[int, dict]:
        return {channel_id: self.snapshot(channel_id) for channel_id in self.runtimes}

    def is_recording(self, channel_id: int) -> bool:
        return self.runtimes.get(channel_id, ChannelRuntime()).state == "RECORDING"

    def active_spool_file(self, channel_id: int) -> Path | None:
        value = self.runtimes.get(channel_id, ChannelRuntime()).current_file
        return Path(value) if value else None

    async def _run_channel(self, channel_id: int) -> None:
        runtime = self.runtimes.setdefault(channel_id, ChannelRuntime())
        while not self._stop_event.is_set() and not runtime.stopping:
            channel = self.db.get_channel(channel_id)
            if not channel or not channel["enabled"] or not channel["automatic_recording"]:
                runtime.state = "DISABLED"
                return
            runtime.state = "CONNECTING"
            runtime.last_error = ""
            log.info("channel_state", extra={"channel_id": channel_id, "state": runtime.state})
            try:
                await self._start_ffmpeg(channel, runtime)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.state = "ERROR"
                runtime.last_error = str(exc)
                runtime.logs.append(f"recorder error: {exc}")
                log.exception("channel_recorder_error", extra={"channel_id": channel_id})
            if not runtime.stopping:
                runtime.state = "OFFLINE"
                log.info("channel_state", extra={"channel_id": channel_id, "state": runtime.state})
                await asyncio.sleep(settings.recorder_retry_seconds)

    async def _start_ffmpeg(self, channel: dict, runtime: ChannelRuntime) -> None:
        channel_id = int(channel["id"])
        pattern = storage.segment_pattern(channel)
        command = [
            settings.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-i",
            channel["input_url"],
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(int(channel["segment_duration"])),
            "-segment_atclocktime",
            "1",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            pattern,
        ]
        redacted = [redact_url(part) if idx == 6 else part for idx, part in enumerate(command)]
        log.info("ffmpeg_start", extra={"channel_id": channel_id, "command": redacted})
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        runtime.process = proc
        runtime.pid = proc.pid
        runtime.state = "RECORDING"
        log.info("channel_state", extra={"channel_id": channel_id, "state": runtime.state, "pid": proc.pid})
        stderr_task = asyncio.create_task(self._read_stderr(channel_id, runtime, proc))
        try:
            while True:
                runtime.current_file = self._newest_file(storage.channel_spool_dir(channel))
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=2)
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            runtime.pid = None
            runtime.process = None
        if code != 0 and not runtime.stopping:
            runtime.last_error = f"FFmpeg exited with code {code}"
            raise RuntimeError(runtime.last_error)
        log.info("ffmpeg_stop", extra={"channel_id": channel_id, "returncode": code})

    async def _read_stderr(self, channel_id: int, runtime: ChannelRuntime, proc) -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            line = redact_url(line)
            if not line:
                continue
            runtime.logs.append(line)
            if "Input #" in line or "Video:" in line or "Audio:" in line:
                runtime.input_info = line[:240]
            if "error" in line.lower() or "failed" in line.lower():
                runtime.last_error = line[:500]
            bitrate = BITRATE_RE.search(line)
            if bitrate:
                runtime.bitrate = bitrate.group(1)
            log.info("ffmpeg_stderr", extra={"channel_id": channel_id, "ffmpeg_line": line})

    async def _terminate_process(self, runtime: ChannelRuntime) -> None:
        proc = runtime.process
        if not proc or proc.returncode is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    def _newest_file(self, directory: Path) -> str:
        files = [p for p in directory.glob("*.mkv") if p.is_file()]
        if not files:
            return ""
        return str(max(files, key=lambda p: p.stat().st_mtime))
