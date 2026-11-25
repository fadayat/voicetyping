"""Private durable storage for recordings awaiting transcription recovery."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
MIN_SAMPLE_RATE = 1_000
MAX_SAMPLE_RATE = 768_000


def _default_recovery_dir() -> Path:
    configured = os.environ.get("VOICETYPING_RECOVERY_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        return base / "VoiceTyping" / "Recovery"
    return Path.home() / "Documents" / "VoiceTyping Recovery"


DEFAULT_RECOVERY_DIR = _default_recovery_dir()


class RecoveryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_RECOVERY_DIR

    def ensure_directory(self) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        return self.root

    def save_audio(self, wav_bytes: bytes) -> Path:
        if not wav_bytes:
            raise ValueError("wav_bytes must not be empty")
        root = self.ensure_directory()
        return self._atomic_write(root / f"{time.time_ns()}.pending.wav", wav_bytes)

    def start_spool(self, sample_rate: float = SAMPLE_RATE) -> "RecordingSpool":
        sample_rate = self._valid_sample_rate(sample_rate)
        return RecordingSpool(
            self.root
            / f"{time.time_ns()}.{os.getpid()}.{sample_rate}.recording.pcm",
            prepare_directory=self.ensure_directory,
        )

    def recover_incomplete_spools(self) -> list[Path]:
        if not self.root.exists():
            return []
        self.ensure_directory()
        recovered = self._recover_stale_temporaries()
        for raw_path in sorted(self.root.glob("*.recording.pcm")):
            try:
                stem = raw_path.name.removesuffix(".recording.pcm")
                name_parts = stem.rsplit(".", 2)
                if (
                    len(name_parts) == 3
                    and name_parts[-2].isdigit()
                    and self._pid_is_alive(int(name_parts[-2]))
                ):
                    continue
                sample_rate = (
                    self._valid_sample_rate(name_parts[-1])
                    if len(name_parts) >= 2 and name_parts[-1].isdigit()
                    else SAMPLE_RATE
                )
                recovered.append(self._convert_raw_to_pending(raw_path, sample_rate))
            except (OSError, OverflowError, ValueError, wave.Error):
                continue
        return recovered

    @staticmethod
    def _valid_sample_rate(value: float | str) -> int:
        try:
            sample_rate = round(float(value))
        except (OverflowError, TypeError, ValueError):
            return SAMPLE_RATE
        if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            return SAMPLE_RATE
        return sample_rate

    def _recover_stale_temporaries(self) -> list[Path]:
        """Promote complete staging WAVs left by a terminated app process."""
        recovered = []
        for temporary in sorted(self.root.glob(".*.pending.wav.*.tmp")):
            if temporary.is_symlink() or not temporary.is_file():
                continue
            parsed = self._parse_temporary_name(temporary.name)
            if parsed is None:
                continue
            destination_name, owner_pid = parsed
            # A temporary file owned by a live process may still be in flight.
            if self._pid_is_alive(owner_pid):
                continue
            destination = self.root / destination_name
            raw_path = self.root / (
                destination_name.removesuffix(".pending.wav") + ".recording.pcm"
            )
            try:
                if destination.exists() or raw_path.exists():
                    temporary.unlink()
                    self._sync_directory(self.root)
                    continue
                if not self._is_complete_wav(temporary):
                    temporary.unlink()
                    self._sync_directory(self.root)
                    continue
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                with temporary.open("r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                self._sync_directory(self.root)
                recovered.append(destination)
            except OSError:
                # Preserve unreadable or otherwise uncertain data for a later run.
                continue
        return recovered

    @staticmethod
    def _parse_temporary_name(name: str) -> tuple[str, int] | None:
        if not name.startswith(".") or not name.endswith(".tmp"):
            return None
        body = name[1:-4]
        try:
            destination_name, pid_text = body.rsplit(".", 1)
        except ValueError:
            return None
        if not destination_name.endswith(".pending.wav") or not pid_text.isdigit():
            return None
        owner_pid = int(pid_text)
        if owner_pid <= 0:
            return None
        return destination_name, owner_pid

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0 or (os.name == "nt" and pid > 0xFFFFFFFF):
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                return bool(
                    kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    and exit_code.value == still_active
                )
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError):
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @staticmethod
    def _is_complete_wav(path: Path) -> bool:
        try:
            with wave.open(str(path), "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                sample_rate = audio.getframerate()
                frame_count = audio.getnframes()
                if min(channels, sample_width, sample_rate, frame_count) <= 0:
                    return False
                audio.setpos(frame_count - 1)
                return len(audio.readframes(1)) == channels * sample_width
        except (EOFError, wave.Error):
            return False

    def pending_files(self) -> list[Path]:
        return sorted(self.root.glob("*.pending.wav")) if self.root.exists() else []

    def discard(self, pending_audio: Path | None) -> bool:
        if pending_audio is None:
            return True
        try:
            Path(pending_audio).unlink(missing_ok=True)
            self._sync_directory(Path(pending_audio).parent)
            return not Path(pending_audio).exists()
        except OSError:
            return False

    def open_directory(self) -> bool:
        directory = self.ensure_directory()
        try:
            if os.name == "nt":
                os.startfile(str(directory))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(
                    ["xdg-open", str(directory)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError:
            return False
        return True

    def _convert_raw_to_pending(self, raw_path: Path, sample_rate: float) -> Path:
        sample_rate = self._valid_sample_rate(sample_rate)
        destination = raw_path.with_name(raw_path.name.removesuffix(".recording.pcm") + ".pending.wav")
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            # Windows rejects fsync on a separately reopened read-only handle.
            # Keep the writable handle open through WAV header finalization.
            with temporary.open("w+b") as handle, raw_path.open("rb") as source:
                with wave.open(handle, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(sample_rate)
                    while block := source.read(256 * 1024):
                        output.writeframesraw(block)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
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
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class RecordingSpool:
    """Queue PCM from the audio callback; write it on a dedicated thread."""

    def __init__(self, path: Path, prepare_directory=None) -> None:
        self.path = path
        self._prepare_directory = prepare_directory
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._discard_requested = threading.Event()
        self._offer_lock = threading.Lock()
        self._error: Exception | None = None
        self._owns_file = False
        self._writer_closed = threading.Event()
        self._thread = threading.Thread(
            target=self._write_loop,
            daemon=True,
            name="recording-spool",
        )
        self._thread.start()

    def offer(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        with self._offer_lock:
            if not self._closed.is_set():
                self._queue.put(pcm_bytes)

    def finish(self) -> Path:
        with self._offer_lock:
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
        self._discard_requested.set()
        try:
            self.finish()
        except Exception:
            pass
        if self._writer_closed.is_set():
            self._unlink_owned_file()

    def _unlink_owned_file(self) -> None:
        with self._offer_lock:
            if not self._owns_file:
                return
            self._owns_file = False
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_loop(self) -> None:
        descriptor = -1
        try:
            if self._discard_requested.is_set():
                return
            if self._prepare_directory is not None:
                self._prepare_directory()
            if self._discard_requested.is_set():
                return
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            self._owns_file = True
            with os.fdopen(descriptor, "wb", buffering=0) as handle:
                descriptor = -1
                last_sync = time.monotonic()
                while True:
                    chunk = self._queue.get()
                    if chunk is None:
                        break
                    handle.write(chunk)
                    now = time.monotonic()
                    if now - last_sync >= 1.0:
                        os.fsync(handle.fileno())
                        last_sync = now
                os.fsync(handle.fileno())
        except Exception as error:
            with self._offer_lock:
                self._error = error
                self._closed.set()
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            self._writer_closed.set()
            if self._discard_requested.is_set():
                self._unlink_owned_file()
