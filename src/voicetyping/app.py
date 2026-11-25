#!/usr/bin/env python3
"""Linux voice-to-text utility: a shortcut records, Gemini transcribes, clipboard receives text."""

from __future__ import annotations

import json
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
from typing import Callable, Sequence

import numpy as np
import pyperclip
import soundfile as sf

from . import __version__
from .audio_routing import AudioRoutingError, SystemAudioRouter
from .recording_recovery import RecordingSpool, RecoveryStore


def _bundled_appimage_library(filename: str) -> str | None:
    """Locate a native library bundled inside the current AppImage."""
    appdir = os.environ.get("APPDIR", "").strip()
    if not appdir:
        return None
    root = Path(appdir)
    for relative_path in (
        Path("usr/lib/x86_64-linux-gnu") / filename,
        Path("lib/x86_64-linux-gnu") / filename,
        Path("usr/lib") / filename,
        Path("lib") / filename,
    ):
        candidate = root / relative_path
        if candidate.exists():
            return str(candidate)
    return None


def _import_sounddevice():
    bundled_portaudio = _bundled_appimage_library("libportaudio.so.2")
    if bundled_portaudio is None:
        import sounddevice

        return sounddevice

    import ctypes.util

    original_find_library = ctypes.util.find_library

    def find_library(name: str):
        if name in {"portaudio", "libportaudio.so.2"}:
            return bundled_portaudio
        return original_find_library(name)

    ctypes.util.find_library = find_library
    try:
        import sounddevice
    finally:
        ctypes.util.find_library = original_find_library
    return sounddevice


sd = _import_sounddevice()

from .core import (  # noqa: E402 - AppImage PortAudio must load first.
    CHANNELS,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_CODES,
    FALLBACK_MODELS,
    MAX_RECORDING_SECONDS,
    MIN_RECORDING_SECONDS,
    REQUEST_TIMEOUT_MS,
    SAMPLE_RATE,
    SILENCE_PEAK_THRESHOLD,
    TRANSCRIPTION_MODEL,
    _CaptureContext,
    _StatefulLinearResampler,
    _valid_sample_rate,
    create_client,
    create_live_transcriber,
    encode_wav,
    extract_transcription_text,
    friendly_api_error,
    normalize_audio_signal,
    select_input_sample_rate as _select_input_sample_rate,
    transcribe_audio,
    transcription_timeout_ms,
    validate_audio,
)


APP_NAME = "VoiceTyping"
APP_VERSION = __version__
TOGGLE_DEBOUNCE_SECONDS = 0.3
TOGGLE_SHUTDOWN_JOIN_SECONDS = 1.0
LIVE_FINALIZE_TIMEOUT_SECONDS = 3.0
LIVE_FAILURE_COOLDOWN_SECONDS = 60.0
MAX_CUSTOM_VOCABULARY_TERMS = 1000

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
    decoded = [os.fsdecode(argument) for argument in arguments if argument]
    if not decoded:
        return False
    executable = Path(decoded[0]).name
    if executable == "voicetyping":
        return True
    if not executable.startswith("python") or len(decoded) < 2:
        return False
    command = decoded[1]
    if Path(command).name in {"voicetyping.py", "voicetyping"}:
        return True
    return (
        command == "-m"
        and len(decoded) > 2
        and decoded[2] in {"voicetyping.app", "voicetyping"}
    )


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


def _normalized_string_list(
    value,
    default: Sequence[str],
    *,
    limit: int | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return tuple(default)
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return tuple(default)
        item = item.strip()
        if item not in normalized:
            normalized.append(item)
    if limit is not None:
        normalized = normalized[:limit]
    return tuple(normalized)


def transcription_preferences(config: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return validated language hints and vocabulary without reading user state."""
    language_codes = _normalized_string_list(
        config.get("language_codes", DEFAULT_LANGUAGE_CODES),
        DEFAULT_LANGUAGE_CODES,
    )
    custom_vocabulary = _normalized_string_list(
        config.get("custom_vocabulary", DEFAULT_CUSTOM_VOCABULARY),
        DEFAULT_CUSTOM_VOCABULARY,
        limit=MAX_CUSTOM_VOCABULARY_TERMS,
    )
    return language_codes, custom_vocabulary


def select_input_sample_rate(check_input_settings=None, query_devices=None) -> float:
    """Use the patched AppImage PortAudio module for device probing."""
    return _select_input_sample_rate(
        check_input_settings or sd.check_input_settings,
        query_devices or sd.query_devices,
    )


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
        package_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (package_root, environment.get("PYTHONPATH", "")) if part
        )
        # Gtk positioning and click-through work through XWayland on GNOME.
        if environment.get("DISPLAY"):
            environment["GDK_BACKEND"] = "x11"
        return subprocess.Popen(
            [sys.executable, "-m", "voicetyping.voice_overlay"],
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
        if "preparing" in low or "hazırlanır" in low:
            return "starting"
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
        if state in ("starting", "recording", "transcribing"):
            effective_hide_ms = 0
        elif state == "copied":
            effective_hide_ms = 300
        elif state == "ready":
            effective_hide_ms = 850
        with self._state_lock:
            if (
                state == "ready"
                and self._desired_show is not None
                and self._desired_show[0] in ("starting", "recording", "transcribing")
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


class VoiceTypingApp:
    def __init__(
        self,
        client,
        panel,
        stream_factory: Callable = sd.InputStream,
        max_recording_seconds: float = MAX_RECORDING_SECONDS,
        live_factory: Callable | None = None,
        paste_action: Callable | None = None,
        shortcut_label: str = "F8",
        sample_rate_selector: Callable[[], float] | None = None,
        audio_router=None,
        recovery_store: RecoveryStore | None = None,
        language_codes: Sequence[str] = DEFAULT_LANGUAGE_CODES,
        custom_vocabulary: Sequence[str] = DEFAULT_CUSTOM_VOCABULARY,
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
        self.last_recovered_text: str | None = None
        self._recovered_texts: list[str] = []
        self.listener = None
        self._last_toggle = 0.0
        self._last_toggle_request = 0.0
        self._lock = threading.RLock()
        self._toggle_condition = threading.Condition(self._lock)
        self._toggle_pending_action: str | None = None
        self._toggle_active_action: str | None = None
        self._toggle_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._live_factory = live_factory
        self._language_codes = tuple(language_codes)
        self._custom_vocabulary = tuple(custom_vocabulary)
        self._paste_action = paste_action
        self.shortcut_label = shortcut_label
        if sample_rate_selector is not None:
            self._sample_rate_selector = sample_rate_selector
        elif stream_factory is sd.InputStream:
            self._sample_rate_selector = select_input_sample_rate
        else:
            # Injected streams used by tests and embedders historically receive
            # 16 kHz audio and must not probe the host's real microphone.
            self._sample_rate_selector = lambda: float(SAMPLE_RATE)
        if audio_router is not None:
            self._audio_router = audio_router
        elif stream_factory is sd.InputStream:
            self._audio_router = SystemAudioRouter()
        else:
            self._audio_router = None
        self._live_session = None
        self._processing_live = None
        self._capture_context: _CaptureContext | None = None
        self._live_disabled_until = 0.0
        self.last_timing: dict = {}
        self.recovery_store = recovery_store or RecoveryStore()
        self._active_route = None
        self._recovery_event = threading.Event()
        self._recovery_pause = threading.Event()
        self._recovery_thread: threading.Thread | None = None
        self._recovery_client_factory: Callable | None = None
        self._recovery_client = None
        self._spools_recovered = False

    def microphone_callback(self, indata, frames, time_info, status) -> None:
        """Compatibility entry point used by tests and direct callers."""
        context = self._capture_context
        if context is not None:
            self._capture_microphone(context, indata, status)
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

    def _capture_microphone(self, context: _CaptureContext, indata, status=None) -> None:
        if status:
            status_text = str(status).strip() or type(status).__name__
            if len(context.status_messages) < 8:
                context.status_messages.add(status_text)
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
        return self._toggle_once()

    def request_toggle(self) -> bool:
        """Queue a non-blocking toggle from a shortcut, signal, or tray action."""
        now = time.monotonic()
        with self._toggle_condition:
            if self._shutdown.is_set():
                return False
            if now - self._last_toggle_request < TOGGLE_DEBOUNCE_SECONDS:
                return False
            if self.state in ("processing", "shutting_down"):
                return False

            # Capture the requested transition now instead of reevaluating it
            # after a slow start.  A stop queued while startup is active must
            # not become a new start if device setup later fails back to idle.
            if self._toggle_active_action == "start" or self.state in (
                "starting",
                "recording",
            ):
                action = "stop"
            elif self._toggle_active_action == "stop":
                return False
            elif self.state == "idle":
                action = "start"
            else:
                return False

            self._last_toggle_request = now
            # Same-action parity bounds the queue: two pending presses cancel
            # instead of creating a delayed start/stop storm.
            if self._toggle_pending_action == action:
                self._toggle_pending_action = None
            else:
                self._toggle_pending_action = action
            thread = self._toggle_thread
            # The controller is lifetime-scoped.  Checking only for None also
            # closes the re-entrant signal window between assignment and
            # Thread.start(), when is_alive() is still false.
            if thread is None:
                thread = threading.Thread(
                    target=self._toggle_loop,
                    daemon=True,
                    name="toggle-controller",
                )
                self._toggle_thread = thread
                thread.start()
            self._toggle_condition.notify()
        return True

    def _toggle_loop(self) -> None:
        while not self._shutdown.is_set():
            with self._toggle_condition:
                self._toggle_condition.wait_for(
                    lambda: self._toggle_pending_action is not None
                    or self._shutdown.is_set()
                )
                if self._shutdown.is_set():
                    return
                action = self._toggle_pending_action
                self._toggle_pending_action = None
                self._toggle_active_action = action
            try:
                self._run_toggle_action(action)
            except Exception as error:
                print(f"Toggle failed ({type(error).__name__}).")
            finally:
                with self._toggle_condition:
                    self._toggle_active_action = None
                    self._toggle_condition.notify_all()

    def _run_toggle_action(self, action: str) -> bool:
        with self._lock:
            state = self.state
        if action == "start" and state == "idle":
            return self.start_recording()
        if action == "stop" and state == "recording":
            return self.request_stop()
        return False

    def _toggle_once(self) -> bool:
        with self._lock:
            state = self.state
        if state == "idle":
            return self.start_recording()
        if state == "recording":
            return self.request_stop()
        return False

    def _claim_active_route(self, route) -> bool:
        """Publish a provisional route before PipeWire is mutated."""
        with self._lock:
            if (
                self._shutdown.is_set()
                or self.state != "starting"
                or self._active_route is not None
            ):
                return False
            self._active_route = route
            return True

    def _restore_active_route(self, route=None) -> bool:
        with self._lock:
            owned_route = self._active_route
            if owned_route is None or (route is not None and owned_route is not route):
                return False
            self._active_route = None
        owned_route.restore()
        return True

    def start_recording(self) -> bool:
        with self._lock:
            if self.state != "idle" or self._shutdown.is_set():
                return False
            self.state = "starting"
            self.recordings = []
            recovery_client = self._recovery_client
        self.panel.show(
            "Mikrofon hazırlanır…",
            color="#89b4fa",
            bg_color="#202838",
        )
        self._recovery_pause.set()
        if recovery_client is not None:
            threading.Thread(target=self._close_recovery_client, args=(recovery_client,),
                             daemon=True, name="recovery-cancel").start()
        use_live = (
            self._live_factory is not None
            and time.monotonic() >= self._live_disabled_until
        )
        live_skip_reason = None
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

        active_route = None
        spool = None
        try:
            if self._audio_router is not None:
                if isinstance(self._audio_router, SystemAudioRouter):
                    active_route = self._audio_router.activate(
                        on_route=self._claim_active_route
                    )
                else:
                    active_route = self._audio_router.activate()
                    if not self._claim_active_route(active_route):
                        active_route.restore()
                        if live_session is not None:
                            live_session.cancel()
                        self._resume_recovery()
                        return False
                selection = active_route.selection
                print(
                    f"Microphone selected: {selection.source_description}; "
                    f"policy={selection.reason}; api={selection.device_api or 'system'}"
                )
            sample_rate = _valid_sample_rate(self._sample_rate_selector())
        except Exception as error:
            self._restore_active_route(active_route)
            if live_session is not None:
                live_session.cancel()
            with self._lock:
                notify_failure = not self._shutdown.is_set()
                if notify_failure:
                    self.state = "idle"
            if spool is not None:
                spool.discard()
            self._resume_recovery()
            if isinstance(error, AudioRoutingError):
                print(f"Could not route headset microphone ({type(error).__name__}).")
                message = "Qulaqlıq mikrofonu əlçatan deyil"
            else:
                print(f"Could not select microphone sample rate ({type(error).__name__}).")
                message = "Mikrofon əlçatan deyil"
            if notify_failure:
                self.panel.show(message, color="#f38ba8", bg_color="#302020", hide_after=3000)
            return False
        try:
            spool = self.recovery_store.start_spool(sample_rate)
        except Exception as error:
            spool = None
            print(f"Recording spool unavailable ({type(error).__name__}).")
        with self._lock:
            abort_start = self._shutdown.is_set()
            if not abort_start:
                capture = _CaptureContext(
                    live_session,
                    max_frames=int(self.max_recording_seconds * sample_rate),
                    sample_rate=sample_rate,
                    live_skip_reason=live_skip_reason,
                    spool=spool,
                )
                self._capture_context = capture
                self.recordings = capture.chunks
                self._live_session = live_session
        if abort_start:
            if live_session is not None:
                live_session.cancel()
            if spool is not None:
                spool.discard()
            self._resume_recovery()
            self._restore_active_route(active_route)
            return False
        new_stream = None
        try:
            new_stream = self.stream_factory(
                samplerate=sample_rate,
                channels=CHANNELS,
                dtype="int16",
                callback=lambda indata, frames, time_info, status: self._capture_microphone(
                    capture, indata, status
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
                notify_failure = not self._shutdown.is_set()
                if notify_failure:
                    self.state = "idle"
            self._restore_active_route(active_route)
            if spool is not None:
                spool.discard()
            self._resume_recovery()
            print(f"Could not open microphone ({type(error).__name__}).")
            if notify_failure:
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
            self._restore_active_route(active_route)
            if spool is not None:
                try:
                    spool.preserve(self.recovery_store, sample_rate)
                except Exception:
                    pass
            if live_session is not None:
                live_session.cancel()
            return False
        self.panel.show(
            f"Səs yazılır — dayandırmaq üçün {self.shortcut_label}",
            color="#f38ba8",
            bg_color="#302030",
        )
        print(f"Recording started. Press {self.shortcut_label} to stop.")
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
            active_route = self._active_route
            self._active_route = None
            self._live_session = None
            self._processing_live = live_session
            stopped_at = time.monotonic()
            if timer:
                timer.cancel()
            self.worker = threading.Thread(
                target=self._stop_and_transcribe,
                args=(stream, chunks, live_session, stopped_at, active_route),
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

    def _batch_transcribe(
        self, audio_data: np.ndarray, sample_rate: float = SAMPLE_RATE, client=None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        return transcribe_audio(
            client or self.client,
            audio_data,
            sample_rate,
            language_codes=self._language_codes,
            custom_vocabulary=self._custom_vocabulary,
            should_stop=lambda: self._shutdown.is_set()
            or (should_stop is not None and should_stop()),
        )

    def _stop_and_transcribe(
        self,
        stream,
        chunks: list[np.ndarray] | _CaptureContext,
        live_session=None,
        stopped_at: float | None = None,
        active_route=None,
    ) -> None:
        stopped_at = stopped_at if stopped_at is not None else time.monotonic()
        route = "batch"
        fallback_reason = None
        sample_rate = float(SAMPLE_RATE)
        recovery_path = None
        spool = chunks.spool if isinstance(chunks, _CaptureContext) else None
        audio_data = None
        route_restored = False
        try:
            self._close_stream(stream)
            if active_route is not None:
                active_route.restore()
                route_restored = True
            live_failed = False
            if isinstance(chunks, _CaptureContext):
                if chunks.status_messages:
                    print(
                        "Microphone stream status: "
                        + "; ".join(sorted(chunks.status_messages))
                        + "."
                    )
                live_failed = chunks.live_failed
                sample_rate = chunks.sample_rate
                fallback_reason = chunks.live_skip_reason
                chunks = chunks.seal_and_snapshot()
            if spool is not None:
                spool.finish()
            if live_session is not None and live_failed:
                fallback_reason = "feed_failed"
                live_session.cancel()
                with self._lock:
                    self._live_disabled_until = (
                        time.monotonic() + LIVE_FAILURE_COOLDOWN_SECONDS
                    )
                print(
                    "Live audio streaming failed; using the full recording "
                    "with batch fallback."
                )
            audio_data, reason = validate_audio(chunks, sample_rate)
            if self._shutdown.is_set():
                return
            if reason:
                if spool is not None:
                    spool.discard()
                messages = {
                    "empty": "Səs yazılmadı — yenidən danışın",
                    "too_short": "Səs yazısı çox qısadır — yenidən danışın",
                    "silence": "Səssizlik aşkarlandı — mikrofona yaxın danışın",
                    "invalid": "Səs yazısı yararsızdır — yenidən sınayın",
                }
                self.panel.show(messages[reason], color="#fab387", bg_color="#2d2520", hide_after=3000)
                return

            assert audio_data is not None
            wav_bytes = encode_wav(audio_data, sample_rate)
            text = ""
            # Snapshot the destination while transcription runs. Clipboard
            # helpers can briefly change focus, so this must finish before copy.
            paste_action = self._paste_action
            prepare_paste = getattr(paste_action, "prepare", None)
            prepared_result = {}
            prepared_event = threading.Event()
            preparation_thread = None
            if callable(prepare_paste):
                def safe_label(value):
                    label = "".join(
                        char if char.isascii() and (char.isalnum() or char == "_") else "_"
                        for char in str(value)
                    )
                    return label[:64] or "unknown"

                def start_preparation():
                    nonlocal preparation_thread

                    def prepare_in_background():
                        try:
                            prepared_result["action"] = prepare_paste()
                        except Exception as error:
                            prepared_result["error"] = safe_label(type(error).__name__)
                        finally:
                            prepared_event.set()

                    preparation_thread = threading.Thread(
                        target=prepare_in_background,
                        daemon=True,
                        name="paste-preparation",
                    )
                    preparation_thread.start()

                if live_session is not None and not live_failed:
                    start_preparation()
            else:
                prepared_result["action"] = paste_action

            if live_session is not None and not live_failed:
                try:
                    text = (live_session.finish(timeout=LIVE_FINALIZE_TIMEOUT_SECONDS) or "").strip()
                except Exception as error:
                    error_type = "".join(
                        char if char.isascii() and (char.isalnum() or char == "_") else "_"
                        for char in type(error).__name__
                    )[:64] or "unknown"
                    fallback_reason = f"finish_failed_{error_type}"
                    print(f"Live finalization failed ({type(error).__name__}); using batch.")
                if text:
                    route = "live"
                else:
                    if fallback_reason is None:
                        failure_reason = getattr(live_session, "failure_reason", None)
                        if isinstance(failure_reason, str) and failure_reason:
                            safe_reason = "".join(
                                char if char.isascii() and (char.isalnum() or char == "_") else "_"
                                for char in failure_reason
                            )[:64] or "unknown"
                            fallback_reason = f"live_{safe_reason}"
                        else:
                            fallback_reason = "live_incomplete"
                    with self._lock:
                        self._live_disabled_until = time.monotonic() + LIVE_FAILURE_COOLDOWN_SECONDS
                    print("Live transcription incomplete; using the full recording with batch fallback.")
            elif live_session is None and fallback_reason is None:
                fallback_reason = "live_unavailable"
            if self._shutdown.is_set():
                return
            if not text:
                text = self._batch_transcribe(audio_data, sample_rate)
            if self._shutdown.is_set():
                return
            if not text:
                recovery_path = self._preserve_failed_recording(wav_bytes, spool, sample_rate)
                self.panel.show("Danışıq başa düşülmədi — yenidən sınayın", color="#fab387", bg_color="#2d2520", hide_after=3000)
                if recovery_path is not None:
                    self.retry_saved_recordings()
                return
            if spool is not None:
                spool.discard()
            if preparation_thread is None and callable(prepare_paste):
                start_preparation()
            self.last_text = text
            result_at = time.monotonic()
            self.last_timing = {
                "route": route,
                "audio_seconds": round(len(audio_data) / sample_rate, 3),
                "stop_to_result_ms": round((result_at - stopped_at) * 1000),
            }
            if fallback_reason is not None:
                self.last_timing["fallback_reason"] = fallback_reason
            if live_session is not None:
                try:
                    live_timings = live_session.timings
                except Exception:
                    live_timings = {}
                if isinstance(live_timings, dict):
                    for name in ("connect_ms", "finalize_ms", "stream_ms"):
                        value = live_timings.get(name)
                        if isinstance(value, (int, float)):
                            self.last_timing[name] = round(value)
            if self._shutdown.is_set():
                return
            prepared_paste = None
            if preparation_thread is not None:
                if prepared_event.wait(0.25):
                    if "error" in prepared_result:
                        error_type = prepared_result["error"]
                        print(
                            f"Automatic paste preparation failed ({error_type}); "
                            "text remains on the clipboard."
                        )
                    else:
                        prepared_paste = prepared_result.get("action")
                else:
                    print(
                        "Automatic paste preparation timed out (TimeoutError); "
                        "text remains on the clipboard."
                    )
            else:
                prepared_paste = prepared_result.get("action")
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
            if spool is not None:
                spool.discard()
            print(
                f"Transcription copied to clipboard. route={route}; "
                f"audio_seconds={self.last_timing['audio_seconds']}; "
                f"stop_to_copy_ms={self.last_timing['stop_to_copy_ms']}; "
                f"fallback_reason={self.last_timing.get('fallback_reason', 'none')}; "
                f"connect_ms={self.last_timing.get('connect_ms', 'n/a')}; "
                f"finalize_ms={self.last_timing.get('finalize_ms', 'n/a')}; "
                f"stream_ms={self.last_timing.get('stream_ms', 'n/a')}"
            )
        except Exception as error:
            print(f"Transcription failed ({type(error).__name__}, status={getattr(error, 'code', 'unknown')}).")
            if audio_data is not None:
                try:
                    recovery_path = self._preserve_failed_recording(
                        encode_wav(audio_data, sample_rate), spool, sample_rate
                    )
                except Exception as save_error:
                    print(f"Recording recovery save failed ({type(save_error).__name__}).")
            if recovery_path is not None:
                self.panel.show("Transkripsiya alınmadı — səs saxlanıldı və yenidən sınanacaq", color="#f38ba8", bg_color="#302020", hide_after=5000)
                self.retry_saved_recordings()
            else:
                self.panel.show(friendly_api_error(error), color="#f38ba8", bg_color="#302020", hide_after=5000)
        finally:
            if active_route is not None and not route_restored:
                active_route.restore()
            if live_session is not None:
                live_session.cancel()
            with self._lock:
                if self._processing_live is live_session:
                    self._processing_live = None
                shutting_down = self.state == "shutting_down"
                if not shutting_down:
                    self.state = "idle"
            self._resume_recovery()
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

    def _preserve_failed_recording(self, wav_bytes, spool, sample_rate):
        try:
            path = self.recovery_store.save_audio(wav_bytes)
            if spool is not None:
                spool.discard()
            return path
        except Exception as error:
            print(f"Recording recovery save failed ({type(error).__name__}).")
            if spool is not None:
                try:
                    return spool.preserve(self.recovery_store, sample_rate)
                except Exception as spool_error:
                    print(f"Recording spool preservation failed ({type(spool_error).__name__}).")
            return None

    def start_recovery_worker(self, client_factory: Callable) -> None:
        with self._lock:
            self._recovery_client_factory = client_factory
            if self._recovery_thread is not None and self._recovery_thread.is_alive():
                return
            self._recovery_thread = threading.Thread(
                target=self._recovery_loop, daemon=True, name="recording-recovery"
            )
            thread = self._recovery_thread
        thread.start()
        self._recovery_event.set()

    def retry_saved_recordings(self, notify: bool = False) -> None:
        self._recovery_event.set()
        if notify:
            self.panel.show("Saxlanmış səslər yenidən yoxlanılır", color="#89b4fa", bg_color="#202838", hide_after=2500)

    def _resume_recovery(self) -> None:
        self._recovery_pause.clear()
        self._recovery_event.set()

    def _recovery_loop(self) -> None:
        while not self._shutdown.is_set():
            self._recovery_event.wait()
            self._recovery_event.clear()
            if self._shutdown.is_set():
                return
            if not self._spools_recovered:
                self.recovery_store.recover_incomplete_spools()
                self._spools_recovered = True
            if self._recovery_pause.is_set():
                continue
            with self._lock:
                if self.state != "idle":
                    continue
                factory = self._recovery_client_factory
            if factory is None:
                continue
            files = self.recovery_store.pending_files()
            if not files:
                continue
            client = None
            try:
                client = factory()
                with self._lock:
                    self._recovery_client = client
                for pending in files:
                    if self._shutdown.is_set() or self._recovery_pause.is_set():
                        break
                    audio_data, sample_rate = sf.read(
                        str(pending), dtype="int16", always_2d=True
                    )
                    if audio_data.ndim == 2 and audio_data.shape[1] == 1:
                        audio_data = audio_data[:, 0]
                    text = self._batch_transcribe(
                        audio_data,
                        sample_rate,
                        client,
                        should_stop=lambda: self._recovery_pause.is_set(),
                    ).strip()
                    if text and not self._recovery_pause.is_set() and not self._shutdown.is_set():
                        with self._lock:
                            self._recovered_texts.append(text)
                            self.last_recovered_text = text
                        self.recovery_store.discard(pending)
                        self.panel.show("Saxlanmış mətn bərpa edildi", color="#a6e3a1", bg_color="#1e2e1e", hide_after=5000)
            except Exception as error:
                print(f"Saved recording retry failed ({type(error).__name__}).")
            finally:
                with self._lock:
                    if self._recovery_client is client:
                        self._recovery_client = None
                if client is not None:
                    self._close_recovery_client(client)
            if self.recovery_store.pending_files() and not self._shutdown.is_set():
                self._shutdown.wait(5.0)
                if not self._recovery_pause.is_set() and not self._shutdown.is_set():
                    self._recovery_event.set()

    @staticmethod
    def _close_recovery_client(client) -> None:
        try:
            client.close()
        except Exception:
            pass

    def copy_last_recovered_transcription(self) -> bool:
        with self._lock:
            recovered_texts = tuple(self._recovered_texts)
        if not recovered_texts:
            self.panel.show("Yeni bərpa edilmiş mətn yoxdur", color="#fab387", bg_color="#2d2520", hide_after=2500)
            return False
        clipboard_text = "\n\n".join(recovered_texts)
        try:
            copy_to_clipboard(clipboard_text)
        except Exception:
            self.panel.show("Clipboard əlçatan deyil", color="#f38ba8", bg_color="#302020", hide_after=3000)
            return False
        with self._lock:
            if self._recovered_texts[:len(recovered_texts)] == list(recovered_texts):
                del self._recovered_texts[:len(recovered_texts)]
            self.last_recovered_text = (
                self._recovered_texts[-1] if self._recovered_texts else None
            )
        message = (
            "Bərpa edilmiş mətn kopyalandı"
            if len(recovered_texts) == 1
            else f"{len(recovered_texts)} bərpa edilmiş mətn kopyalandı"
        )
        self.panel.show(message, color="#a6e3a1", bg_color="#1e2e1e", hide_after=2000)
        return True

    def open_recovery_folder(self) -> bool:
        opened = self.recovery_store.open_directory()
        if not opened:
            self.panel.show("Bərpa qovluğu açıla bilmədi", color="#f38ba8", bg_color="#302020", hide_after=3000)
        return opened

    def discard_saved_recording(self, pending_audio) -> None:
        self.recovery_store.discard(pending_audio)

    def update_client(self, api_key: str) -> bool:
        with self._lock:
            if self.state == "processing":
                self.panel.show("Mətnə çevrilmə bitsin deyə gözləyin", color="#fab387", bg_color="#2d2520", hide_after=2500)
                return False
            old_client = self.client
            self.client = create_client(api_key)
            if self._live_factory is not None:
                self._live_factory = lambda: create_live_transcriber(
                    api_key,
                    language_codes=self._language_codes,
                    custom_vocabulary=self._custom_vocabulary,
                )
                self._live_disabled_until = 0.0
            if self._recovery_client_factory is not None:
                self._recovery_client_factory = lambda: create_client(api_key)
        try:
            old_client.close()
        except Exception:
            pass
        return True

    def install_listener_if_running(self, listener) -> bool:
        with self._lock:
            if self.state == "shutting_down":
                accepted = False
                previous = None
            else:
                accepted = True
                previous = self.listener
                self.listener = listener
        if not accepted:
            try:
                listener.stop()
            except Exception:
                pass
            return False
        if previous is not None and previous is not listener:
            try:
                previous.stop()
            except Exception:
                pass
        return True

    def install_paste_action_if_running(self, paste_action) -> bool:
        with self._lock:
            if self.state == "shutting_down":
                accepted = False
                previous = None
            else:
                accepted = True
                previous = self._paste_action
                self._paste_action = paste_action
        if not accepted:
            close = getattr(paste_action, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            return False
        if previous is not None and previous is not paste_action:
            close = getattr(previous, "close", None)
            if callable(close):
                try:
                    close()
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
            active_route = self._active_route
            self._active_route = None
            self.recordings = []
            timer = self.duration_timer
            self.duration_timer = None
            live_sessions = (self._live_session, self._processing_live)
            self._live_session = None
            listener = self.listener
            self.listener = None
            paste_action = self._paste_action
            self._paste_action = None
            worker = self.worker
            toggle_thread = self._toggle_thread
        with self._toggle_condition:
            self._toggle_pending_action = None
            self._toggle_condition.notify_all()
        if timer:
            timer.cancel()
        self._close_stream(stream)
        if active_route is not None:
            active_route.restore()
        if capture is not None:
            chunks = capture.seal_and_snapshot()
            audio_data, reason = validate_audio(chunks, capture.sample_rate)
            if reason is None and audio_data is not None:
                try:
                    self.recovery_store.save_audio(encode_wav(audio_data, capture.sample_rate))
                    if capture.spool is not None:
                        capture.spool.discard()
                except Exception:
                    if capture.spool is not None:
                        try:
                            capture.spool.preserve(self.recovery_store, capture.sample_rate)
                        except Exception:
                            pass
            elif capture.spool is not None:
                try:
                    capture.spool.discard()
                except Exception:
                    pass
        for live_session in live_sessions:
            if live_session is not None:
                live_session.cancel()
        if listener is not None:
            listener.stop()
        close_paste_backend = getattr(paste_action, "close", None)
        if callable(close_paste_backend):
            try:
                close_paste_backend()
            except Exception:
                pass
        recovery_client = self._recovery_client
        if recovery_client is not None:
            self._close_recovery_client(recovery_client)
        if worker is None or not worker.is_alive():
            try:
                self.client.close()
            except Exception:
                pass
        if self._recovery_thread is not None and self._recovery_thread.is_alive():
            self._recovery_event.set()
            self._recovery_thread.join(timeout=0.5)
        if (
            toggle_thread is not None
            and toggle_thread is not threading.current_thread()
            and toggle_thread.is_alive()
        ):
            # The route is already restored above.  Give startup a short
            # cleanup window without letting a stuck device backend block Quit.
            toggle_thread.join(timeout=TOGGLE_SHUTDOWN_JOIN_SECONDS)
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
        item("Start/stop recording", lambda icon, menu_item: app.request_toggle()),
        item("Copy last transcription", lambda icon, menu_item: app.copy_last_transcription()),
        item("Copy recovered transcriptions", lambda icon, menu_item: app.copy_last_recovered_transcription()),
        item("Retry saved recordings", lambda icon, menu_item: app.retry_saved_recordings(notify=True)),
        item("Open saved recordings", lambda icon, menu_item: app.open_recovery_folder()),
        item("Settings (API Key)", on_change_api_key),
        item("Quit", on_quit),
    )
    pystray.Icon("voicetyping", create_tray_image(), APP_NAME, menu).run()


def _start_wayland_portal_bootstrap(app, environment, shortcut, *, auto_paste: bool):
    """Request optional Wayland permissions without blocking tray startup."""
    def bootstrap():
        try:
            from .desktop_integration import build_wayland_shortcut_listener

            if not app._shutdown.is_set():
                listener = build_wayland_shortcut_listener(
                    shortcut, app.request_toggle, environment
                )
                if listener is not None:
                    if listener.start():
                        if app.install_listener_if_running(listener):
                            print(f"Wayland global shortcut ready ({shortcut}).")
                    else:
                        print("Global shortcut is unavailable; use the tray or bind the toggle command manually.")
        except Exception as error:
            print(
                f"Wayland global shortcut unavailable ({type(error).__name__}); "
                "use the tray or bind the toggle command manually."
            )

        if not auto_paste or app._shutdown.is_set():
            return
        keyboard = None
        try:
            from .desktop_integration import build_paste_action
            from .xdg_portal import RemoteDesktopKeyboard

            keyboard = RemoteDesktopKeyboard()
            if not keyboard.start():
                keyboard.close()
                print("Wayland keyboard permission was not granted; text will remain on the clipboard.")
                return
            paste_action = build_paste_action(True, environment, portal_keyboard=keyboard)
            if paste_action is None:
                keyboard.close()
                print("Automatic paste is unavailable; text will remain on the clipboard.")
                return
            if app.install_paste_action_if_running(paste_action):
                print("Wayland portal paste is ready.")
        except Exception as error:
            if keyboard is not None:
                keyboard.close()
            print(
                f"Automatic paste unavailable ({type(error).__name__}); "
                "text will remain on the clipboard."
            )

    thread = threading.Thread(target=bootstrap, daemon=True, name="voicetyping-portal-bootstrap")
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if argv == ["--audio-info"]:
        try:
            for line in SystemAudioRouter().audio_info():
                print(line)
            print(f"Capture sample rate: {select_input_sample_rate():g} Hz")
        except Exception as error:
            print(f"Audio diagnostics failed ({type(error).__name__}).")
            return 1
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
    portal_setup_pending = False
    try:
        try:
            from .desktop_environment import detect_desktop_environment
            from .desktop_integration import setup_global_shortcut
            from .setup_shortcut import SHORTCUT_ACTIVE, SHORTCUT_NOT_GNOME, get_shortcut
            from .shortcuts import parse_shortcut

            desktop_environment = detect_desktop_environment()
            shortcut_status = setup_global_shortcut(desktop_environment)
            shortcut_spec = parse_shortcut(get_shortcut())
            shortcut = shortcut_spec.to_xdg_trigger()
            shortcut_label = shortcut_spec.to_label()
        except Exception as error:
            print(f"Global shortcut setup unavailable ({type(error).__name__}).")
            shortcut_status = "error"
            SHORTCUT_ACTIVE = "active"
            SHORTCUT_NOT_GNOME = "not_gnome"
            desktop_environment = None
            shortcut = "F8"
            shortcut_label = "F8"

        api_key = get_api_key()
        if not api_key:
            print("No API key was provided; VoiceTyping did not start.")
            return 1

        panel = NotificationPanel()
        config = load_config()
        language_codes, custom_vocabulary = transcription_preferences(config)
        portal_setup_pending = bool(
            config.get("auto_paste", True)
            and desktop_environment is not None
            and desktop_environment.is_wayland
            and not desktop_environment.is_gnome
        )
        portal_shortcut_pending = bool(
            shortcut_status == SHORTCUT_NOT_GNOME
            and desktop_environment is not None
            and desktop_environment.is_wayland
        )
        live_factory = None
        if config.get("live_transcription", True):
            live_factory = lambda: create_live_transcriber(
                api_key,
                language_codes=language_codes,
                custom_vocabulary=custom_vocabulary,
            )
        paste_action = None
        try:
            from .desktop_integration import build_paste_action

            if not portal_setup_pending:
                paste_action = build_paste_action(
                    config.get("auto_paste", True), desktop_environment
                )
            if config.get("auto_paste", True) and paste_action is None and not portal_setup_pending:
                print("Automatic paste is unavailable; text will remain on the clipboard.")
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
            shortcut_label=shortcut_label,
            language_codes=language_codes,
            custom_vocabulary=custom_vocabulary,
        )
        start_recovery = getattr(app, "start_recovery_worker", None)
        if callable(start_recovery):
            start_recovery(lambda: create_client(api_key))

        def signal_handler(sig, frame) -> None:
            app.request_toggle()

        signal.signal(signal.SIGUSR1, signal_handler)
        if pending_toggles % 2:
            app.request_toggle()

        from .desktop_integration import should_start_x11_listener

        if should_start_x11_listener(shortcut_status, desktop_environment):
            try:
                from pynput import keyboard as pynput_keyboard
            except Exception as error:
                print(f"Global keyboard listener unavailable ({type(error).__name__}).")
                pynput_keyboard = None

            from .shortcuts import PynputShortcutState, parse_shortcut

            shortcut_spec = parse_shortcut(shortcut)
            shortcut_state = PynputShortcutState()

            def on_press(key) -> None:
                press = shortcut_state.press(key)
                if press is None:
                    return
                key_name, pressed_modifiers = press
                if shortcut_spec.matches(key_name, pressed_modifiers):
                    app.request_toggle()

            def on_release(key) -> None:
                shortcut_state.release(key)

            if pynput_keyboard is not None:
                try:
                    app.listener = pynput_keyboard.Listener(
                        on_press=on_press,
                        on_release=on_release,
                    )
                    app.listener.start()
                except Exception as error:
                    print(f"Global keyboard listener unavailable ({type(error).__name__}).")
        elif portal_shortcut_pending:
            print("Wayland shortcut permission will be requested in the background.")
        elif shortcut_status == SHORTCUT_NOT_GNOME:
            print("Global shortcut is not configured; use the tray or bind the toggle command manually.")
        elif shortcut_status != SHORTCUT_ACTIVE:
            print(f"{shortcut_label} shortcut is inactive; use the tray menu or resolve the GNOME shortcut warning.")

        panel.show(
            f"Hazırdır — başlamaq üçün {shortcut_label} basın",
            color="#a6e3a1",
            bg_color="#1e2e1e",
            hide_after=2500,
        )
        print(f"VoiceTyping is ready. Press {shortcut_label} to start or stop recording.")
        if portal_shortcut_pending or portal_setup_pending:
            _start_wayland_portal_bootstrap(
                app,
                desktop_environment,
                shortcut,
                auto_paste=config.get("auto_paste", True),
            )
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
        try:
            if app is not None:
                app.shutdown()
            elif panel is not None:
                panel.close()
        finally:
            release_pid_file()
            signal.signal(signal.SIGUSR1, previous_usr1_handler)


if __name__ == "__main__":
    raise SystemExit(main())
