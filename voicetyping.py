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


APP_NAME = "VoiceTyping"
SAMPLE_RATE = 16_000
CHANNELS = 1
MIN_RECORDING_SECONDS = 0.25
MAX_RECORDING_SECONDS = 7 * 60
SILENCE_PEAK_THRESHOLD = 64
REQUEST_TIMEOUT_MS = 60_000
TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-3.5-flash")
TOGGLE_DEBOUNCE_SECONDS = 0.3

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


def transcription_timeout_ms(sample_count: int) -> int:
    audio_seconds = sample_count / SAMPLE_RATE
    return min(180_000, REQUEST_TIMEOUT_MS + int(audio_seconds * 250))


class NotificationPanel:
    """Modern animated floating island overlay with dynamic voice waveforms and AI status."""

    def __init__(self):
        self.root: tk.Tk | None = None
        self.canvas: tk.Canvas | None = None
        self.label: tk.Label | None = None  # Preserved for backward compatibility
        self._commands: queue.Queue[tuple] = queue.Queue()
        self._hide_job = None
        self._ready = threading.Event()
        self._is_running = True
        self._is_visible = False

        # Visual layout dimensions (compact pure-animation island)
        self.width = 190
        self.height = 44
        self.radius = 22

        # Dynamic state & animation tracking
        self.state = "idle"
        self.display_text = "Ready — Press F8"
        self.record_start_time = 0.0
        self.state_start_time = time.monotonic()
        self.bg_color = "#181825"
        self.text_color = "#cdd6f4"

        self._thread = threading.Thread(target=self._create_panel, daemon=True, name="status-panel")
        self._thread.start()
        self._ready.wait(timeout=3)

    def _create_panel(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", 0.96)
            self.root.configure(bg="#0f0f17")

            self.canvas = tk.Canvas(
                self.root,
                width=self.width,
                height=self.height,
                bg="#0f0f17",
                highlightthickness=0,
            )
            self.canvas.pack()

            screen_width = self.root.winfo_screenwidth()
            x_right = screen_width - self.width - 24
            self.root.geometry(f"{self.width}x{self.height}+{x_right}+18")
            self.root.withdraw()
            self._is_visible = False

            self.root.after(30, self._drain_commands)
            self.root.after(30, self._render_loop)
            self._ready.set()
            self.root.mainloop()
        except tk.TclError as error:
            print(f"Status panel unavailable ({type(error).__name__}).")
            self.root = None
            self._ready.set()

    def _draw_capsule(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _determine_state(self, text: str) -> str:
        low = text.lower()
        if "recording" in low or "qeyd" in low:
            return "recording"
        if "transcrib" in low or "çevrilir" in low or "gözləyin" in low:
            return "transcribing"
        if "copi" in low or "kopyalandı" in low:
            return "copied"
        if "ready" in low or "hazır" in low:
            return "ready"
        if any(w in low for w in ("unavailable", "tapılmadı", "error", "xəta", "failed", "rejected")):
            return "error"
        if any(w in low for w in ("no audio", "səs yazılmadı", "too short", "qısadır", "silence", "səssiz", "not understood")):
            return "warning"
        return "info"

    def _drain_commands(self) -> None:
        if self.root is None:
            return
        try:
            while True:
                command = self._commands.get_nowait()
                action = command[0]
                if action == "show":
                    _, text, color, bg_color, hide_after = command
                    if self._hide_job is not None:
                        self.root.after_cancel(self._hide_job)
                        self._hide_job = None

                    old_state = self.state
                    self.state = self._determine_state(text)
                    self.display_text = text
                    self.text_color = color
                    self.bg_color = bg_color
                    self.state_start_time = time.monotonic()
                    if self.state == "recording" and old_state != "recording":
                        self.record_start_time = time.monotonic()

                    self._is_visible = True
                    self.root.deiconify()
                    if hide_after > 0:
                        self._hide_job = self.root.after(hide_after, self._auto_hide)
                elif action == "hide":
                    self._auto_hide()
                elif action == "close":
                    self._is_running = False
                    if self._hide_job is not None:
                        self.root.after_cancel(self._hide_job)
                        self._hide_job = None
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.root.after(30, self._drain_commands)

    def _auto_hide(self) -> None:
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        self._is_visible = False
        if self.root is not None:
            self.root.withdraw()

    def _render_loop(self) -> None:
        if not self._is_running or self.root is None:
            return
        if self._is_visible:
            self._render_frame()
            self.root.after(20, self._render_loop)  # ~50 FPS smooth animation when visible
        else:
            self.root.after(200, self._render_loop)  # Zero CPU idle polling when hidden

    def _render_frame(self) -> None:
        if self.canvas is None:
            return
        t = time.monotonic()
        dt = t - self.state_start_time
        self.canvas.delete("all")

        # 1. Dynamic border and container background based on state
        border_color = "#2a2b3d"
        fill_color = "#181825"
        if self.state == "recording":
            glow = (math.sin(t * 5.0) + 1.0) / 2.0
            r_val = int(243 * 0.7 + (243 * 0.3) * glow)
            g_val = int(139 * 0.4 + (139 * 0.2) * glow)
            b_val = int(168 * 0.4 + (168 * 0.2) * glow)
            border_color = f"#{r_val:02x}{g_val:02x}{b_val:02x}"
            fill_color = "#1e1622"
        elif self.state == "transcribing":
            glow = (math.sin(t * 4.0) + 1.0) / 2.0
            r_val = int(137 * 0.6 + 50 * glow)
            g_val = int(180 * 0.6 + 60 * glow)
            b_val = int(250 * 0.6 + 5 * glow)
            border_color = f"#{r_val:02x}{g_val:02x}{b_val:02x}"
            fill_color = "#161b26"
        elif self.state == "copied":
            border_color = "#a6e3a1"
            fill_color = "#15241b"
        elif self.state in ("warning", "error"):
            border_color = "#f38ba8" if self.state == "error" else "#fab387"
            fill_color = "#24181c"

        # Outer capsule container
        self._draw_capsule(2, 2, self.width - 2, self.height - 2, self.radius, fill=fill_color, outline=border_color, width=1.5)

        cy = self.height // 2

        if self.state == "recording":
            # 1. Left: Glowing Pulsing Neon Red Record Dot
            orb_x = 24
            pulse = (math.sin(t * 6.0) + 1.0) / 2.0
            r_outer = 8 + int(4 * pulse)
            self.canvas.create_oval(orb_x - r_outer, cy - r_outer, orb_x + r_outer, cy + r_outer, outline="#f38ba8", width=1)
            self.canvas.create_oval(orb_x - 4.5, cy - 4.5, orb_x + 4.5, cy + 4.5, fill="#ff4d6d", outline="")

            # 2. Continuous fluid Siri-style waves (3 multi-frequency spline curves)
            wave_x1 = 44
            wave_x2 = self.width - 16
            wave_w = wave_x2 - wave_x1
            steps = 28

            wave_configs = [
                {"freq": 3.0, "speed": 6.5, "amp": 12.0, "phase": 0.0, "color": "#89dceb", "width": 2.2},
                {"freq": 2.5, "speed": -5.5, "amp": 10.0, "phase": 1.4, "color": "#cba6f7", "width": 2.0},
                {"freq": 3.5, "speed": 7.5, "amp": 8.5, "phase": 2.8, "color": "#f38ba8", "width": 1.8},
            ]

            for cfg in wave_configs:
                pts = []
                for i in range(steps + 1):
                    norm_x = i / steps
                    px = wave_x1 + norm_x * wave_w
                    envelope = math.sin(norm_x * math.pi) ** 1.5
                    wy = cy + math.sin(norm_x * cfg["freq"] * math.pi * 2 + t * cfg["speed"] + cfg["phase"]) * cfg["amp"] * envelope
                    pts.extend([px, wy])
                self.canvas.create_line(pts, smooth=True, fill=cfg["color"], width=cfg["width"], capstyle="round")

        elif self.state == "transcribing":
            # 5 AI glowing dots undulating in a smooth harmonic wave
            dot_count = 5
            spacing = 18
            start_x = (self.width - (dot_count - 1) * spacing) // 2
            colors = ["#89dceb", "#89b4fa", "#cba6f7", "#f5c2e7", "#fab387"]
            for i in range(dot_count):
                dx = start_x + i * spacing
                dy = cy + math.sin(t * 8.0 + i * 1.1) * 7.0
                r = 3.8 + 1.0 * math.cos(t * 8.0 + i * 1.1)
                self.canvas.create_oval(dx - r, dy - r, dx + r, dy + r, fill=colors[i], outline="")

        elif self.state == "copied":
            # Spring bounce checkmark in emerald green circle
            anim_t = min(1.0, dt / 0.3)
            scale = 1.0 + 0.25 * math.sin(anim_t * math.pi) if anim_t < 1.0 else 1.0
            r_pop = 13 * scale
            chk_x = self.width // 2
            self.canvas.create_oval(chk_x - r_pop, cy - r_pop, chk_x + r_pop, cy + r_pop, fill="#a6e3a1", outline="")
            self.canvas.create_text(chk_x, cy, text="✓", fill="#0f0f17", font=("sans-serif", 13, "bold"))

        elif self.state == "ready":
            # Centered soft breathing green dot
            dot_x = self.width // 2
            pulse = (math.sin(t * 3.5) + 1.0) / 2.0
            r_glow = 7 + int(4 * pulse)
            self.canvas.create_oval(dot_x - r_glow, cy - r_glow, dot_x + r_glow, cy + r_glow, outline="#a6e3a1", width=1)
            self.canvas.create_oval(dot_x - 4, cy - 4, dot_x + 4, cy + 4, fill="#a6e3a1", outline="")

        else:  # warning or error
            icon = "⚠" if self.state == "warning" else "✕"
            color = "#fab387" if self.state == "warning" else "#f38ba8"
            self.canvas.create_text(self.width // 2, cy, text=icon, fill=color, font=("sans-serif", 16, "bold"))

    def show(
        self,
        text: str,
        color: str = "#cdd6f4",
        bg_color: str = "#1e1e2e",
        hide_after: int = 0,
    ) -> None:
        if self.root is not None:
            self._commands.put(("show", text, color, bg_color, hide_after))

    def hide(self) -> None:
        if self.root is not None:
            self._commands.put(("hide",))

    def close(self) -> None:
        if self.root is not None:
            self._commands.put(("close",))


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
        return "API key rejected — open Settings"
    if code == 429:
        return "Gemini rate limit reached — try later"
    if code and code >= 500:
        return "Gemini is temporarily unavailable"
    name = type(error).__name__.lower()
    if "timeout" in name:
        return "Transcription timed out — try again"
    return "Transcription failed — check network/settings"


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


class VoiceTypingApp:
    def __init__(
        self,
        client,
        panel,
        stream_factory: Callable = sd.InputStream,
        max_recording_seconds: float = MAX_RECORDING_SECONDS,
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

    def microphone_callback(self, indata, frames, time_info, status) -> None:
        if status:
            print("Microphone reported an input warning.")
        with self._lock:
            if self.state == "recording":
                self.recordings.append(indata.copy())

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
            self.panel.show("Still transcribing…", color="#f9e2af", bg_color="#2d2a20", hide_after=1500)
        return False

    def start_recording(self) -> bool:
        with self._lock:
            if self.state != "idle" or self._shutdown.is_set():
                return False
            self.state = "starting"
            self.recordings = []
        new_stream = None
        try:
            new_stream = self.stream_factory(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=self.microphone_callback,
            )
            new_stream.start()
        except Exception as error:
            self._close_stream(new_stream)
            with self._lock:
                self.state = "idle"
            print(f"Could not open microphone ({type(error).__name__}).")
            self.panel.show("Microphone unavailable", color="#f38ba8", bg_color="#302020", hide_after=3000)
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
            return False
        self.panel.show("Recording…  (F8 — Stop)", color="#f38ba8", bg_color="#302030")
        print("Recording started. Press F8 to stop.")
        return True

    def _duration_limit_reached(self) -> None:
        self.panel.show("Maximum recording length reached", color="#fab387", bg_color="#2d2520")
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
            chunks = self.recordings
            self.recordings = []
            if timer:
                timer.cancel()
            self.worker = threading.Thread(
                target=self._stop_and_transcribe,
                args=(stream, chunks),
                daemon=True,
                name="transcription-worker",
            )
            self.panel.show("Transcribing…", color="#f9e2af", bg_color="#2d2a20")
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

    def _stop_and_transcribe(self, stream, chunks: list[np.ndarray]) -> None:
        try:
            self._close_stream(stream)
            audio_data, reason = validate_audio(chunks)
            if reason:
                messages = {
                    "empty": "No audio was recorded",
                    "too_short": "Recording was too short",
                    "silence": "Only silence was detected",
                    "invalid": "Recorded audio was invalid",
                }
                self.panel.show(messages[reason], color="#fab387", bg_color="#2d2520", hide_after=3000)
                return

            assert audio_data is not None
            audio_part = types.Part.from_bytes(data=encode_wav(audio_data), mime_type="audio/wav")
            if self._shutdown.is_set():
                return
            candidate_models = (TRANSCRIPTION_MODEL, *FALLBACK_MODELS)
            timeout_ms = transcription_timeout_ms(len(audio_data))
            response = None
            last_error = None

            for model_name in candidate_models:
                if self._shutdown.is_set():
                    return
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
                        break
                except Exception as model_err:
                    last_error = model_err
                    code = getattr(model_err, "code", None)
                    err_msg = str(model_err)
                    print(f"Model {model_name} failed (code={code}): {err_msg[:120]}")
                    if code in (401, 403):
                        raise
                    if code in (429, 404, 500, 502, 503, 504) or "quota" in err_msg.lower() or "resource_exhausted" in err_msg.lower():
                        continue

            if response is None:
                assert last_error is not None
                raise last_error
            text = extract_transcription_text(response)
            if not text:
                self.panel.show("Audio was not understood", color="#fab387", bg_color="#2d2520", hide_after=3000)
                return
            self.last_text = text
            if self._shutdown.is_set():
                return
            try:
                copy_to_clipboard(text)
            except Exception as error:
                print(f"Clipboard copy failed ({type(error).__name__}).")
                self.panel.show("Clipboard unavailable — text kept in memory", color="#f38ba8", bg_color="#302020", hide_after=5000)
                return
            self.panel.show("Copied to clipboard", color="#a6e3a1", bg_color="#1e2e1e", hide_after=3000)
            print("Transcription copied to clipboard.")
        except Exception as error:
            print(f"Transcription failed ({type(error).__name__}, status={getattr(error, 'code', 'unknown')}).")
            self.panel.show(friendly_api_error(error), color="#f38ba8", bg_color="#302020", hide_after=5000)
        finally:
            with self._lock:
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
            self.panel.show("No transcription is available", color="#fab387", bg_color="#2d2520", hide_after=2000)
            return False
        try:
            copy_to_clipboard(self.last_text)
        except Exception:
            self.panel.show("Clipboard unavailable", color="#f38ba8", bg_color="#302020", hide_after=3000)
            return False
        self.panel.show("Copied to clipboard", color="#a6e3a1", bg_color="#1e2e1e", hide_after=2000)
        return True

    def update_client(self, api_key: str) -> bool:
        with self._lock:
            if self.state == "processing":
                self.panel.show("Wait for transcription to finish", color="#fab387", bg_color="#2d2520", hide_after=2500)
                return False
            old_client = self.client
            self.client = create_client(api_key)
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
            timer = self.duration_timer
            self.duration_timer = None
            listener = self.listener
            worker = self.worker
        if timer:
            timer.cancel()
        self._close_stream(stream)
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
            try:
                subprocess.run(["notify-send", APP_NAME, "API key updated successfully"], check=False)
            except FileNotFoundError:
                pass

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
        app = VoiceTypingApp(create_client(api_key), panel)

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

        panel.show("Ready — Press F8", color="#a6e3a1", bg_color="#1e2e1e", hide_after=2500)
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
