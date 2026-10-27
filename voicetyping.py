#!/usr/bin/env python3
"""Linux voice-to-text utility: F8 records, Gemini transcribes, clipboard receives text."""

from __future__ import annotations

import io
import json
import math
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import simpledialog
from typing import Callable

import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
from google import genai
from google.genai import types

from live_transcription import LiveTranscriber


APP_NAME = "VoiceTyping"
APP_VERSION = "2.1.1"
SAMPLE_RATE = 16_000
CHANNELS = 1
MIN_RECORDING_SECONDS = 0.25
MAX_RECORDING_SECONDS = 7 * 60
SILENCE_PEAK_THRESHOLD = 64
REQUEST_TIMEOUT_MS = 60_000
TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"
FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
)
TOGGLE_DEBOUNCE_SECONDS = 0.3
LIVE_FINALIZE_TIMEOUT_SECONDS = 3.0
LIVE_FAILURE_COOLDOWN_SECONDS = 60.0

CONFIG_DIR = Path.home() / ".config" / "voicetyping"
CONFIG_FILE = CONFIG_DIR / "config.json"
_runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
PID_FILE = _runtime_dir / f"voicetyping-{os.getuid()}.pid"


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        if "State:\tZ" in status or "State:\tX" in status:
            return False
    except OSError:
        pass
    return True


def is_voicetyping_process(pid: int) -> bool:
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(Path(os.fsdecode(argument)).name == "voicetyping.py" for argument in arguments if argument)


def read_pid(path: Path = PID_FILE) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def acquire_pid_file(path: Path = PID_FILE) -> bool:
    """Atomically claim the per-user PID file, removing a stale one once."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(2):
        existing_pid = read_pid(path)
        if existing_pid and is_pid_running(existing_pid) and is_voicetyping_process(existing_pid):
            return False
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        except OSError:
            return False
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def release_pid_file(path: Path = PID_FILE) -> None:
    """Remove only a PID file owned by this process."""
    if read_pid(path) == os.getpid():
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def send_toggle(path: Path = PID_FILE) -> bool:
    pid = read_pid(path)
    if not pid or not is_pid_running(pid) or not is_voicetyping_process(pid):
        print("VoiceTyping is not running. Start it from the application menu first.")
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError) as error:
        print(f"Could not signal VoiceTyping ({type(error).__name__}).")
        return False
    return True


def load_config() -> dict:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            return {}
        try:
            CONFIG_DIR.chmod(0o700)
            CONFIG_FILE.chmod(0o600)
        except OSError:
            pass
        return config
    except (OSError, ValueError, TypeError):
        return {}


def save_config(config: dict) -> None:
    """Atomically save configuration with private permissions."""
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    fd, temporary_name = tempfile.mkstemp(prefix="config-", suffix=".json", dir=CONFIG_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, CONFIG_FILE)
        CONFIG_FILE.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _prompt_api_key() -> str | None:
    try:
        result = subprocess.run(
            [
                "zenity",
                "--entry",
                "--hide-text",
                f"--title={APP_NAME} - Setup",
                "--text=Enter your Gemini API key. It will be stored locally with private file permissions.",
                "--width=450",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except FileNotFoundError:
        try:
            root = tk.Tk()
            root.withdraw()
            api_key = simpledialog.askstring(
                f"{APP_NAME} - API Key", "Enter your Gemini API key:", show="*", parent=root
            )
            root.destroy()
            return api_key.strip() if api_key and api_key.strip() else None
        except tk.TclError:
            return None


def get_api_key(force: bool = False) -> str | None:
    config = load_config()
    if not force:
        environment_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if environment_key:
            return environment_key
        stored_key = config.get("api_key")
        if isinstance(stored_key, str) and stored_key.strip():
            return stored_key.strip()

    api_key = _prompt_api_key()
    if not api_key:
        return None
    config["api_key"] = api_key
    save_config(config)
    return api_key


def create_client(api_key: str):
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def create_live_transcriber(api_key: str):
    language_codes = load_config().get("language_codes", ["az-AZ"])
    if not isinstance(language_codes, list) or not all(
        isinstance(code, str) and code for code in language_codes
    ):
        language_codes = ["az-AZ"]
    return LiveTranscriber(api_key, language_codes=language_codes)


def transcription_timeout_ms(sample_count: int) -> int:
    audio_seconds = sample_count / SAMPLE_RATE
    return min(180_000, REQUEST_TIMEOUT_MS + int(audio_seconds * 250))


def normalize_audio_signal(indata, bins: int = 12) -> tuple[float, float, tuple[float, ...]]:
    """Return a small, perceptually compressed microphone snapshot.

    At most 192 samples are inspected so the sounddevice callback has predictable
    work regardless of the device block size.  Log compression keeps quiet speech
    visible while bounding every value to the range expected by the renderer.
    """
    samples = np.asarray(indata)
    if samples.size == 0 or bins <= 0:
        return 0.0, 0.0, tuple(0.0 for _ in range(max(0, bins)))
    flat = samples.reshape(-1)
    stride = max(1, flat.size // 192)
    magnitudes = np.abs(flat[::stride].astype(np.float32)) / 32768.0
    magnitudes = np.clip(magnitudes, 0.0, 1.0)

    def compress(value: float) -> float:
        return min(1.0, max(0.0, math.log1p(32.0 * value) / math.log(33.0)))

    rms = compress(float(np.sqrt(np.mean(magnitudes * magnitudes))))
    peak = compress(float(np.max(magnitudes, initial=0.0)))
    edges = np.linspace(0, len(magnitudes), bins + 1, dtype=int)
    envelope = []
    for index in range(bins):
        segment = magnitudes[edges[index]:edges[index + 1]]
        value = float(np.sqrt(np.mean(segment * segment))) if segment.size else 0.0
        envelope.append(compress(value))
    return rms, peak, tuple(envelope)


class NotificationPanel:
    """Send status and bounded audio levels to the isolated transparent overlay."""

    RETRY_INITIAL_SECONDS = 0.25
    RETRY_MAX_SECONDS = 2.0
    READY_TIMEOUT_SECONDS = 3.0
    FAILURE_LOG_INTERVAL_SECONDS = 30.0

    def __init__(self):
        self._commands: queue.Queue[tuple] = queue.Queue()
        self._audio_signals: queue.Queue[tuple] = queue.Queue(maxsize=1)
        self._ready = threading.Event()
        self._panel_ready = threading.Event()
        self._closing = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process = None
        self._desired_show: tuple[str, int] | None = None
        self._desired_deadline: float | None = None
        self._last_failure_log = 0.0
        self._suppressed_failures = 0
        self._thread = threading.Thread(target=self._run_overlay, daemon=True, name="voice-animation")
        self._thread.start()
        if not self._ready.wait(timeout=4):
            print("Voice animation initialization timed out.")

    def _spawn_overlay(self):
        environment = os.environ.copy()
        # Gtk positioning and click-through work through XWayland on GNOME.
        if environment.get("DISPLAY"):
            environment["GDK_BACKEND"] = "x11"
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("voice_overlay.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1, env=environment,
        )

    def _wait_until_ready(self, process) -> bool:
        import selectors

        deadline = time.monotonic() + self.READY_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not self._closing.is_set() and process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if selector.select(timeout=min(0.1, remaining)):
                    return process.stdout.readline().strip() == "ready"
        return False

    @staticmethod
    def _close_streams(process) -> None:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    @staticmethod
    def _stop_overlay(process) -> None:
        if process.poll() is None:
            try:
                process.stdin.write('["close"]\n')
                process.stdin.flush()
                process.wait(timeout=0.7)
            except (AttributeError, BrokenPipeError, OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                    process.wait(timeout=0.4)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=0.3)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        NotificationPanel._close_streams(process)

    def _log_failure(self, error: Exception) -> None:
        now = time.monotonic()
        if self._last_failure_log == 0.0 or now - self._last_failure_log >= self.FAILURE_LOG_INTERVAL_SECONDS:
            suffix = f"; {self._suppressed_failures} retries suppressed" if self._suppressed_failures else ""
            print(f"Voice animation unavailable ({type(error).__name__}{suffix}).")
            self._last_failure_log = now
            self._suppressed_failures = 0
        else:
            self._suppressed_failures += 1

    def _clear_commands(self) -> None:
        try:
            while True:
                self._commands.get_nowait()
        except queue.Empty:
            pass

    def _replay_command_locked(self, now: float) -> tuple | None:
        if self._desired_show is None:
            return None
        state, original_hide_ms = self._desired_show
        if self._desired_deadline is None:
            return ("show", state, original_hide_ms)
        remaining_ms = int((self._desired_deadline - now) * 1000)
        if remaining_ms <= 0:
            self._desired_show = None
            self._desired_deadline = None
            return None
        return ("show", state, remaining_ms)

    def _run_overlay(self) -> None:
        retry_delay = self.RETRY_INITIAL_SECONDS
        try:
            while not self._closing.is_set():
                process = None
                started_at = None
                try:
                    process = self._spawn_overlay()
                    with self._process_lock:
                        self._process = process
                    if not self._wait_until_ready(process):
                        if self._closing.is_set():
                            break
                        raise RuntimeError("The transparent animation did not start")

                    started_at = time.monotonic()
                    with self._state_lock:
                        # Commands for a dead child are stale. The desired state below
                        # is the single source of truth across process restarts.
                        self._clear_commands()
                        replay = self._replay_command_locked(started_at)
                        if replay is not None:
                            self._commands.put(replay)
                        self._panel_ready.set()
                    self._ready.set()
                    self._wake.set()

                    while not self._closing.is_set() and process.poll() is None:
                        self._wake.wait(timeout=0.1)
                        self._wake.clear()
                        commands = []
                        try:
                            while True:
                                commands.append(self._commands.get_nowait())
                        except queue.Empty:
                            pass
                        try:
                            rms, peak, envelope = self._audio_signals.get_nowait()
                            commands.append(("audio", rms, peak, envelope))
                        except queue.Empty:
                            pass
                        if commands:
                            process.stdin.write("".join(json.dumps(command) + "\n" for command in commands))
                            process.stdin.flush()

                    if not self._closing.is_set():
                        raise RuntimeError("The transparent animation stopped")
                except Exception as error:
                    if not self._closing.is_set():
                        self._log_failure(error)
                finally:
                    with self._state_lock:
                        self._panel_ready.clear()
                    self._ready.set()
                    if process is not None:
                        self._stop_overlay(process)
                    with self._process_lock:
                        if self._process is process:
                            self._process = None

                if self._closing.is_set():
                    break
                if started_at is not None and time.monotonic() - started_at >= 5.0:
                    retry_delay = self.RETRY_INITIAL_SECONDS
                if self._closing.wait(retry_delay):
                    break
                retry_delay = min(self.RETRY_MAX_SECONDS, retry_delay * 2)
        finally:
            self._panel_ready.clear()
            self._ready.set()

    @staticmethod
    def _determine_state(text: str) -> str:
        low = text.lower()
        if any(word in low for word in (
            "unavailable", "əlçatan deyil", "error", "xəta", "failed", "rejected",
            "uğursuz", "imtina", "qəbul edilmədi", "alınmadı", "vaxt bitdi",
        )):
            return "error"
        if any(word in low for word in (
            "no audio", "səs yazılmadı", "too short", "qısadır", "silence", "səssiz",
            "not understood", "başa düşülmədi", "mövcud deyil", "hazır mətn yoxdur",
            "çatdı", "hədd", "sorğu limiti",
        )):
            return "warning"
        if "recording" in low or "qeyd" in low or "yazılır" in low:
            return "recording"
        if "transcrib" in low or "çevril" in low or "gözləyin" in low or "wait for" in low:
            return "transcribing"
        if "copi" in low or "kopyalandı" in low or "yapışdırın" in low:
            return "copied"
        if "ready" in low or "hazır" in low:
            return "ready"
        return "info"

    def feed_audio(self, rms: float, peak: float, envelope: tuple[float, ...]) -> None:
        """Latest-only telemetry; the real-time microphone callback never waits."""
        snapshot = (
            max(0.0, min(1.0, float(rms))),
            max(0.0, min(1.0, float(peak))),
            tuple(max(0.0, min(1.0, float(value))) for value in envelope[:12]),
        )
        try:
            self._audio_signals.put_nowait(snapshot)
        except queue.Full:
            try:
                self._audio_signals.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_signals.put_nowait(snapshot)
            except queue.Full:
                pass
        self._wake.set()

    def show(
        self, text: str, color: str = "", bg_color: str = "", hide_after: int = 0,
    ) -> None:
        state = self._determine_state(text)
        now = time.monotonic()
        effective_hide_ms = hide_after
        if state in ("recording", "transcribing"):
            effective_hide_ms = 0
        elif state == "copied":
            effective_hide_ms = 300
        elif state == "ready":
            effective_hide_ms = 850
        with self._state_lock:
            if (
                state == "ready"
                and self._desired_show is not None
                and self._desired_show[0] in ("recording", "transcribing")
            ):
                return
            self._desired_show = (state, hide_after)
            self._desired_deadline = now + effective_hide_ms / 1000 if effective_hide_ms > 0 else None
            if self._panel_ready.is_set():
                self._commands.put(("show", state, hide_after))
        self._wake.set()

    def hide(self) -> None:
        with self._state_lock:
            self._desired_show = None
            self._desired_deadline = None
            if self._panel_ready.is_set():
                self._commands.put(("hide",))
        self._wake.set()

    def close(self) -> None:
        self._closing.set()
        self._wake.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                with self._process_lock:
                    process = self._process
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                self._thread.join(timeout=0.5)
            if self._thread.is_alive():
                with self._process_lock:
                    process = self._process
                if process is not None and process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                self._thread.join(timeout=0.4)


def validate_audio(chunks: list[np.ndarray]) -> tuple[np.ndarray | None, str | None]:
    if not chunks:
        return None, "empty"
    try:
        audio_data = np.concatenate(chunks, axis=0)
    except (TypeError, ValueError):
        return None, "invalid"
    if audio_data.size < int(SAMPLE_RATE * MIN_RECORDING_SECONDS):
        return None, "too_short"
    samples = audio_data.astype(np.int32, copy=False)
    if int(np.max(np.abs(samples), initial=0)) < SILENCE_PEAK_THRESHOLD:
        return None, "silence"
    return audio_data, None


def encode_wav(audio_data: np.ndarray) -> bytes:
    wav_io = io.BytesIO()
    sf.write(wav_io, audio_data, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return wav_io.getvalue()


def copy_to_clipboard(text: str, attempts: int = 2) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            pyperclip.copy(text)
            return
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.1)
    assert last_error is not None
    raise last_error


def friendly_api_error(error: Exception) -> str:
    code = getattr(error, "code", None)
    if code in (401, 403):
        return "API açarı qəbul edilmədi — Parametrləri yoxlayın"
    if code == 429:
        return "Sorğu limiti dolub — bir az sonra yenidən sınayın"
    if code and code >= 500:
        return "Transkripsiya xidməti müvəqqəti əlçatan deyil"
    name = type(error).__name__.lower()
    if "timeout" in name:
        return "Vaxt bitdi — yenidən sınayın"
    return "Transkripsiya alınmadı — interneti və parametrləri yoxlayın"


def extract_transcription_text(response) -> str:
    """Read dedicated Transcribe responses, with a text-response fallback."""
    transcriptions: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            transcription = getattr(part, "audio_transcription", None)
            text = getattr(transcription, "text", None)
            if isinstance(text, str) and text.strip():
                transcriptions.append(text.strip())
    if transcriptions:
        result = "\n".join(transcriptions).strip()
    else:
        raw_text = getattr(response, "text", None)
        result = raw_text.strip() if isinstance(raw_text, str) else ""

    # Strip surrounding quotation marks if returned by fallback generative models
    if (result.startswith(('"', "'", "“", "«")) and result.endswith(('"', "'", "”", "»")) and len(result) >= 2):
        result = result[1:-1].strip()
    return result


class _CaptureContext:
    """Audio owned by exactly one recording stream.

    SoundDevice serializes callbacks for a stream, and ``stream.stop()`` is
    the boundary that waits for the active callback.  The worker snapshots
    this context only after that boundary, so the callback needs no app-wide
    control lock and the last in-flight chunk cannot be lost.
    """

    def __init__(self, live_session, max_frames: int):
        self.live_session = live_session
        self.max_frames = max(1, max_frames)
        self.chunks: list[np.ndarray] = []
        self.frames = 0
        self.sealed = False
        self.live_failed = False

    def capture(self, indata) -> np.ndarray | None:
        if self.sealed:
            return None
        remaining = self.max_frames - self.frames
        if remaining <= 0:
            return None
        chunk = indata[:remaining].copy()
        if len(chunk) == 0:
            return None
        self.frames += len(chunk)
        self.chunks.append(chunk)
        if self.live_session is not None and not self.live_failed:
            try:
                if not self.live_session.feed_audio(
                    chunk.astype("<i2", copy=False).tobytes()
                ):
                    self.live_failed = True
            except Exception:
                self.live_failed = True
        return chunk

    def seal_and_snapshot(self) -> list[np.ndarray]:
        self.sealed = True
        return list(self.chunks)


class VoiceTypingApp:
    def __init__(
        self,
        client,
        panel,
        stream_factory: Callable = sd.InputStream,
        max_recording_seconds: float = MAX_RECORDING_SECONDS,
        live_factory: Callable | None = None,
        paste_action: Callable | None = None,
    ):
        self.client = client
        self.panel = panel
        self.stream_factory = stream_factory
        self.max_recording_seconds = max_recording_seconds
        self.state = "idle"
        self.recordings: list[np.ndarray] = []
        self.stream = None
        self.worker: threading.Thread | None = None
        self.duration_timer: threading.Timer | None = None
        self.last_text: str | None = None
        self.listener = None
        self._last_toggle = 0.0
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._live_factory = live_factory
        self._paste_action = paste_action
        self._live_session = None
        self._processing_live = None
        self._capture_context: _CaptureContext | None = None
        self._live_disabled_until = 0.0
        self.last_timing: dict = {}

    def microphone_callback(self, indata, frames, time_info, status) -> None:
        """Compatibility entry point used by tests and direct callers."""
        context = self._capture_context
        if context is not None:
            self._capture_microphone(context, indata)
            return
        # Older direct callers may set state/recordings without constructing a
        # stream.  This path also avoids the app control lock and therefore
        # retains audio while another thread is changing unrelated state.
        if self.state in ("starting", "recording"):
            chunk = indata.copy()
            self.recordings.append(chunk)
            if self._live_session is not None:
                try:
                    self._live_session.feed_audio(
                        chunk.astype("<i2", copy=False).tobytes()
                    )
                except Exception:
                    pass
            signal_sink = getattr(self.panel, "feed_audio", None)
            if callable(signal_sink):
                signal_sink(*normalize_audio_signal(indata))

    def _capture_microphone(self, context: _CaptureContext, indata) -> None:
        chunk = context.capture(indata)
        if chunk is None:
            return
        signal_sink = getattr(self.panel, "feed_audio", None)
        if callable(signal_sink):
            signal_sink(*normalize_audio_signal(chunk))

    def toggle(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._last_toggle < TOGGLE_DEBOUNCE_SECONDS:
                return False
            self._last_toggle = now
            state = self.state
        if state == "idle":
            return self.start_recording()
        if state == "recording":
            return self.request_stop()
        if state == "processing":
            self.panel.show("Mətnə çevrilir — zəhmət olmasa gözləyin", color="#f9e2af", bg_color="#2d2a20", hide_after=1500)
        return False

    def start_recording(self) -> bool:
        with self._lock:
            if self.state != "idle" or self._shutdown.is_set():
                return False
            self.state = "starting"
            self.recordings = []
            use_live = self._live_factory is not None and time.monotonic() >= self._live_disabled_until
        live_session = None
        if use_live:
            try:
                live_session = self._live_factory()
                live_session.start()
            except Exception as error:
                if live_session is not None:
                    live_session.cancel()
                live_session = None
                self._live_disabled_until = time.monotonic() + LIVE_FAILURE_COOLDOWN_SECONDS
                print(f"Live transcription setup failed ({type(error).__name__}); using batch.")
        with self._lock:
            if self._shutdown.is_set():
                if live_session is not None:
                    live_session.cancel()
                return False
            capture = _CaptureContext(
                live_session,
                max_frames=int(self.max_recording_seconds * SAMPLE_RATE),
            )
            self._capture_context = capture
            self.recordings = capture.chunks
            self._live_session = live_session
        new_stream = None
        try:
            new_stream = self.stream_factory(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=lambda indata, frames, time_info, status: self._capture_microphone(
                    capture, indata
                ),
            )
            new_stream.start()
        except Exception as error:
            self._close_stream(new_stream)
            if live_session is not None:
                live_session.cancel()
            with self._lock:
                if self._capture_context is capture:
                    self._capture_context = None
                self.recordings = []
                self._live_session = None
                if not self._shutdown.is_set():
                    self.state = "idle"
            print(f"Could not open microphone ({type(error).__name__}).")
            self.panel.show("Mikrofon əlçatan deyil", color="#f38ba8", bg_color="#302020", hide_after=3000)
            return False

        with self._lock:
            if self._shutdown.is_set():
                self.state = "shutting_down"
                close_immediately = True
            else:
                self.stream = new_stream
                self.state = "recording"
                close_immediately = False
                self.duration_timer = threading.Timer(self.max_recording_seconds, self._duration_limit_reached)
                self.duration_timer.daemon = True
                self.duration_timer.start()
        if close_immediately:
            self._close_stream(new_stream)
            if live_session is not None:
                live_session.cancel()
            return False
        self.panel.show("Səs yazılır — dayandırmaq üçün F8", color="#f38ba8", bg_color="#302030")
        print("Recording started. Press F8 to stop.")
        return True

    def _duration_limit_reached(self) -> None:
        self.panel.show("Yazı vaxt həddinə çatdı — dayandırılır", color="#fab387", bg_color="#2d2520")
        self.request_stop()

    def request_stop(self) -> bool:
        with self._lock:
            if self.state != "recording":
                return False
            self.state = "processing"
            stream = self.stream
            self.stream = None
            timer = self.duration_timer
            self.duration_timer = None
            capture = self._capture_context
            self._capture_context = None
            chunks = capture if capture is not None else self.recordings
            self.recordings = []
            live_session = (
                capture.live_session if capture is not None else self._live_session
            )
            self._live_session = None
            self._processing_live = live_session
            stopped_at = time.monotonic()
            if timer:
                timer.cancel()
            self.worker = threading.Thread(
                target=self._stop_and_transcribe,
                args=(stream, chunks, live_session, stopped_at),
                daemon=True,
                name="transcription-worker",
            )
            self.panel.show("Mətnə çevrilir…", color="#f9e2af", bg_color="#2d2a20")
            self.worker.start()
        return True

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _batch_transcribe(self, audio_data: np.ndarray) -> str:
        audio_part = types.Part.from_bytes(data=encode_wav(audio_data), mime_type="audio/wav")
        timeout_ms = transcription_timeout_ms(len(audio_data))
        last_error = None
        for model_name in (TRANSCRIPTION_MODEL, *FALLBACK_MODELS):
            if self._shutdown.is_set():
                return ""
            try:
                if model_name == TRANSCRIPTION_MODEL:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=[audio_part],
                        config=types.GenerateContentConfig(
                            audio_transcription_config=types.AudioTranscriptionConfig(language_codes=[]),
                            http_options=types.HttpOptions(timeout=timeout_ms),
                        ),
                    )
                else:
                    prompt = (
                        "Accurately transcribe the spoken audio verbatim. "
                        "Return ONLY the transcribed speech. Do not add commentary, "
                        "conversational replies, explanations, formatting, or quotation marks. "
                        "If no speech is heard, return nothing."
                    )
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=[prompt, audio_part],
                        config=types.GenerateContentConfig(
                            temperature=0.0,
                            http_options=types.HttpOptions(timeout=timeout_ms),
                        ),
                    )
                if response is not None:
                    return extract_transcription_text(response)
            except Exception as error:
                last_error = error
                code = getattr(error, "code", None)
                print(f"Model {model_name} failed ({type(error).__name__}, status={code}).")
                # Retry another model only for model-specific availability/quotas.
                if code in (401, 403):
                    raise
                if code not in (429, 404, 500, 502, 503, 504):
                    description = str(error).lower()
                    if "quota" not in description and "resource_exhausted" not in description:
                        raise
        if last_error is not None:
            raise last_error
        return ""

    def _stop_and_transcribe(
        self,
        stream,
        chunks: list[np.ndarray] | _CaptureContext,
        live_session=None,
        stopped_at: float | None = None,
    ) -> None:
        stopped_at = stopped_at if stopped_at is not None else time.monotonic()
        route = "batch"
        try:
            self._close_stream(stream)
            live_failed = False
            if isinstance(chunks, _CaptureContext):
                live_failed = chunks.live_failed
                chunks = chunks.seal_and_snapshot()
            if live_session is not None and live_failed:
                live_session.cancel()
                with self._lock:
                    self._live_disabled_until = (
                        time.monotonic() + LIVE_FAILURE_COOLDOWN_SECONDS
                    )
                print(
                    "Live audio streaming failed; using the full recording "
                    "with batch fallback."
                )
            audio_data, reason = validate_audio(chunks)
            if self._shutdown.is_set():
                return
            if reason:
                messages = {
                    "empty": "Səs yazılmadı — yenidən danışın",
                    "too_short": "Səs yazısı çox qısadır — yenidən danışın",
                    "silence": "Səssizlik aşkarlandı — mikrofona yaxın danışın",
                    "invalid": "Səs yazısı yararsızdır — yenidən sınayın",
                }
                self.panel.show(messages[reason], color="#fab387", bg_color="#2d2520", hide_after=3000)
                return

            assert audio_data is not None
            text = ""
            if live_session is not None and not live_failed:
                try:
                    text = (live_session.finish(timeout=LIVE_FINALIZE_TIMEOUT_SECONDS) or "").strip()
                except Exception as error:
                    print(f"Live finalization failed ({type(error).__name__}); using batch.")
                if text:
                    route = "live"
                else:
                    with self._lock:
                        self._live_disabled_until = time.monotonic() + LIVE_FAILURE_COOLDOWN_SECONDS
                    print("Live transcription incomplete; using the full recording with batch fallback.")
            if self._shutdown.is_set():
                return
            if not text:
                text = self._batch_transcribe(audio_data)
            if self._shutdown.is_set():
                return
            if not text:
                self.panel.show("Danışıq başa düşülmədi — yenidən sınayın", color="#fab387", bg_color="#2d2520", hide_after=3000)
                return
            self.last_text = text
            result_at = time.monotonic()
            self.last_timing = {
                "route": route,
                "audio_seconds": round(len(audio_data) / SAMPLE_RATE, 3),
                "stop_to_result_ms": round((result_at - stopped_at) * 1000),
            }
            if self._shutdown.is_set():
                return
            prepared_paste = self._paste_action
            prepare_paste = getattr(prepared_paste, "prepare", None)
            if callable(prepare_paste):
                try:
                    # Capture the destination before copying, because clipboard
                    # ownership may briefly move focus to the clipboard helper.
                    prepared_paste = prepare_paste()
                except Exception as error:
                    print(
                        f"Automatic paste preparation failed ({type(error).__name__}); "
                        "text remains on the clipboard."
                    )
                    prepared_paste = None
            try:
                copy_to_clipboard(text)
            except Exception as error:
                print(f"Clipboard copy failed ({type(error).__name__}).")
                self.panel.show("Clipboard əlçatan deyil — mətn yadda saxlanıldı", color="#f38ba8", bg_color="#302020", hide_after=5000)
                return
            self.last_timing["stop_to_copy_ms"] = round((time.monotonic() - stopped_at) * 1000)
            paste_succeeded = False
            if prepared_paste is not None and not self._shutdown.is_set():
                try:
                    prepared_paste()
                except Exception as error:
                    print(
                        f"Automatic paste failed ({type(error).__name__}); "
                        "text remains on the clipboard."
                    )
                else:
                    paste_succeeded = True
                    self.last_timing["stop_to_paste_ms"] = round(
                        (time.monotonic() - stopped_at) * 1000
                    )
            completion_message = (
                "Mətn kopyalandı"
                if paste_succeeded
                else "Hazırdır — Ctrl+V ilə yapışdırın"
            )
            self.panel.show(completion_message, color="#a6e3a1", bg_color="#1e2e1e", hide_after=3000)
            print(
                f"Transcription copied to clipboard. route={route}; "
                f"audio_seconds={self.last_timing['audio_seconds']}; "
                f"stop_to_copy_ms={self.last_timing['stop_to_copy_ms']}"
            )
        except Exception as error:
            print(f"Transcription failed ({type(error).__name__}, status={getattr(error, 'code', 'unknown')}).")
            self.panel.show(friendly_api_error(error), color="#f38ba8", bg_color="#302020", hide_after=5000)
        finally:
            if live_session is not None:
                live_session.cancel()
            with self._lock:
                if self._processing_live is live_session:
                    self._processing_live = None
                shutting_down = self.state == "shutting_down"
                if not shutting_down:
                    self.state = "idle"
            if shutting_down:
                try:
                    self.client.close()
                except Exception:
                    pass

    def copy_last_transcription(self) -> bool:
        if not self.last_text:
            self.panel.show("Yapışdırmaq üçün hazır mətn yoxdur", color="#fab387", bg_color="#2d2520", hide_after=2000)
            return False
        try:
            copy_to_clipboard(self.last_text)
        except Exception:
            self.panel.show("Clipboard əlçatan deyil", color="#f38ba8", bg_color="#302020", hide_after=3000)
            return False
        self.panel.show("Hazırdır — Ctrl+V ilə yapışdırın", color="#a6e3a1", bg_color="#1e2e1e", hide_after=2000)
        return True

    def update_client(self, api_key: str) -> bool:
        with self._lock:
            if self.state == "processing":
                self.panel.show("Mətnə çevrilmə bitsin deyə gözləyin", color="#fab387", bg_color="#2d2520", hide_after=2500)
                return False
            old_client = self.client
            self.client = create_client(api_key)
            if self._live_factory is not None:
                self._live_factory = lambda: create_live_transcriber(api_key)
                self._live_disabled_until = 0.0
        try:
            old_client.close()
        except Exception:
            pass
        return True

    def shutdown(self) -> None:
        with self._lock:
            if self.state == "shutting_down":
                return
            self.state = "shutting_down"
            self._shutdown.set()
            stream = self.stream
            self.stream = None
            capture = self._capture_context
            self._capture_context = None
            self.recordings = []
            timer = self.duration_timer
            self.duration_timer = None
            live_sessions = (self._live_session, self._processing_live)
            self._live_session = None
            listener = self.listener
            worker = self.worker
        if timer:
            timer.cancel()
        self._close_stream(stream)
        if capture is not None:
            capture.seal_and_snapshot()
        for live_session in live_sessions:
            if live_session is not None:
                live_session.cancel()
        if listener is not None:
            listener.stop()
        if worker is None or not worker.is_alive():
            try:
                self.client.close()
            except Exception:
                pass
        self.panel.close()


def create_tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=(137, 180, 250))
    return image


def run_tray(app: VoiceTypingApp) -> None:
    import pystray
    from pystray import MenuItem as item

    def on_change_api_key(icon, menu_item) -> None:
        new_key = get_api_key(force=True)
        if new_key and app.update_client(new_key):
            print("Gemini API key updated.")

    def on_quit(icon, menu_item) -> None:
        app.shutdown()
        icon.stop()

    menu = (
        item("Start/stop recording", lambda icon, menu_item: app.toggle()),
        item("Copy last transcription", lambda icon, menu_item: app.copy_last_transcription()),
        item("Settings (API Key)", on_change_api_key),
        item("Quit", on_quit),
    )
    pystray.Icon("voicetyping", create_tray_image(), APP_NAME, menu).run()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if argv and argv[0] == "--toggle":
        return 0 if send_toggle() else 1
    if argv and argv[0] == "--settings":
        if get_api_key(force=True):
            print("API key updated. Restart a running VoiceTyping instance to apply it.")
            return 0
        print("API key was not changed.")
        return 1
    if argv:
        print(f"Unknown argument: {argv[0]}")
        return 2

    previous_usr1_handler = signal.getsignal(signal.SIGUSR1)
    pending_toggles = 0

    def buffer_startup_toggle(sig, frame) -> None:
        nonlocal pending_toggles
        pending_toggles += 1

    signal.signal(signal.SIGUSR1, buffer_startup_toggle)
    if not acquire_pid_file():
        signal.signal(signal.SIGUSR1, previous_usr1_handler)
        print("VoiceTyping is already running.")
        return 0

    panel = None
    app = None
    try:
        try:
            from setup_shortcut import SHORTCUT_ACTIVE, SHORTCUT_NOT_GNOME, setup_gnome_shortcut

            shortcut_status = setup_gnome_shortcut()
        except Exception as error:
            print(f"Global shortcut setup unavailable ({type(error).__name__}).")
            shortcut_status = "error"
            SHORTCUT_ACTIVE = "active"
            SHORTCUT_NOT_GNOME = "not_gnome"

        api_key = get_api_key()
        if not api_key:
            print("No API key was provided; VoiceTyping did not start.")
            return 1

        panel = NotificationPanel()
        config = load_config()
        live_factory = None
        if config.get("live_transcription", True):
            live_factory = lambda: create_live_transcriber(api_key)
        paste_action = None
        if config.get("auto_paste", True):
            try:
                from auto_paste import AutomaticPaste

                paste_action = AutomaticPaste()
            except Exception as error:
                print(
                    f"Automatic paste unavailable ({type(error).__name__}); "
                    "text will remain on the clipboard."
                )
        app = VoiceTypingApp(
            create_client(api_key),
            panel,
            live_factory=live_factory,
            paste_action=paste_action,
        )

        def signal_handler(sig, frame) -> None:
            threading.Thread(target=app.toggle, daemon=True, name="toggle-signal").start()

        signal.signal(signal.SIGUSR1, signal_handler)
        if pending_toggles % 2:
            threading.Thread(target=app.toggle, daemon=True, name="startup-toggle").start()

        if shortcut_status == SHORTCUT_NOT_GNOME:
            try:
                from pynput import keyboard as pynput_keyboard
            except Exception as error:
                print(f"Global keyboard listener unavailable ({type(error).__name__}).")
                pynput_keyboard = None

            def on_press(key) -> None:
                if pynput_keyboard is not None and key == pynput_keyboard.Key.f8:
                    app.toggle()

            if pynput_keyboard is not None:
                try:
                    app.listener = pynput_keyboard.Listener(on_press=on_press)
                    app.listener.start()
                except Exception as error:
                    print(f"Global keyboard listener unavailable ({type(error).__name__}).")
        elif shortcut_status != SHORTCUT_ACTIVE:
            print("F8 shortcut is inactive; use the tray menu or resolve the GNOME shortcut warning.")

        panel.show("Hazırdır — başlamaq üçün F8 basın", color="#a6e3a1", bg_color="#1e2e1e", hide_after=2500)
        print("VoiceTyping is ready. Press F8 to start or stop recording.")
        try:
            run_tray(app)
        except KeyboardInterrupt:
            pass
        except Exception as error:
            print(f"System tray unavailable ({type(error).__name__}); press Ctrl+C to quit.")
            try:
                while not app._shutdown.wait(1):
                    pass
            except KeyboardInterrupt:
                pass
        return 0
    finally:
        if app is not None:
            app.shutdown()
        elif panel is not None:
            panel.close()
        release_pid_file()
        signal.signal(signal.SIGUSR1, previous_usr1_handler)


if __name__ == "__main__":
    raise SystemExit(main())
