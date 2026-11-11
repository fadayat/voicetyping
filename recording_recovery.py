"""Private durable storage for recordings awaiting transcription recovery."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import wave
from pathlib import Path

DEFAULT_RECOVERY_DIR = Path(os.environ.get(
    "VOICETYPING_RECOVERY_DIR", Path.home() / "Documents" / "VoiceTyping Recovery"
))
SAMPLE_RATE = 16_000


class RecoveryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_RECOVERY_DIR

    def ensure_directory(self) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        return self.root

    def save_audio(self, wav_bytes: bytes) -> Path:
        if not wav_bytes:
            raise ValueError("wav_bytes must not be empty")
        root = self.ensure_directory()
        return self._atomic_write(root / f"{time.time_ns()}.pending.wav", wav_bytes)

    def start_spool(self, sample_rate: float = SAMPLE_RATE) -> "RecordingSpool":
        return RecordingSpool(
            self.ensure_directory()
            / f"{time.time_ns()}.{round(sample_rate)}.recording.pcm"
        )

    def recover_incomplete_spools(self) -> list[Path]:
        if not self.root.exists():
            return []
        recovered = []
        for raw_path in sorted(self.root.glob("*.recording.pcm")):
            try:
                rate_text = raw_path.name.removesuffix(".recording.pcm").rsplit(".", 1)[-1]
                sample_rate = float(rate_text) if rate_text.isdigit() else SAMPLE_RATE
                recovered.append(self._convert_raw_to_pending(raw_path, sample_rate))
            except OSError:
                continue
        return recovered

    def pending_files(self) -> list[Path]:
        return sorted(self.root.glob("*.pending.wav")) if self.root.exists() else []

    def discard(self, pending_audio: Path | None) -> None:
        if pending_audio is None:
            return
        try:
            Path(pending_audio).unlink(missing_ok=True)
            self._sync_directory(Path(pending_audio).parent)
        except OSError:
            pass

    def open_directory(self) -> bool:
        directory = self.ensure_directory()
        try:
            subprocess.Popen(["xdg-open", str(directory)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return False
        return True

    def _convert_raw_to_pending(self, raw_path: Path, sample_rate: float) -> Path:
        destination = raw_path.with_name(raw_path.name.removesuffix(".recording.pcm") + ".pending.wav")
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with wave.open(str(temporary), "wb") as output, raw_path.open("rb") as source:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(round(sample_rate))
                while block := source.read(256 * 1024):
                    output.writeframesraw(block)
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            self._sync_directory(destination.parent)
            raw_path.unlink(missing_ok=True)
            self._sync_directory(destination.parent)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_write(destination: Path, data: bytes) -> Path:
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            RecoveryStore._sync_directory(destination.parent)
            return destination
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class RecordingSpool:
    """Queue PCM from the audio callback; write it on a dedicated thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._error: Exception | None = None
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._thread = threading.Thread(target=self._write_loop, args=(descriptor,),
                                        daemon=True, name="recording-spool")
        self._thread.start()

    def offer(self, pcm_bytes: bytes) -> None:
        if pcm_bytes and not self._closed.is_set():
            self._queue.put(pcm_bytes)

    def finish(self) -> Path:
        if not self._closed.is_set():
            self._closed.set()
            self._queue.put(None)
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise TimeoutError("recording spool did not finish")
        if self._error:
            raise self._error
        return self.path

    def preserve(self, store: RecoveryStore, sample_rate: float = SAMPLE_RATE) -> Path:
        self.finish()
        return store._convert_raw_to_pending(self.path, sample_rate)

    def discard(self) -> None:
        try:
            self.finish()
        except Exception:
            pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_loop(self, descriptor: int) -> None:
        try:
            with os.fdopen(descriptor, "wb", buffering=0) as handle:
                while True:
                    chunk = self._queue.get()
                    if chunk is None:
                        break
                    handle.write(chunk)
                    os.fsync(handle.fileno())
        except Exception as error:
            self._error = error
