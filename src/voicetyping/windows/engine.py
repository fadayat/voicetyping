"""Windows orchestration layered over shared VoiceTyping engine primitives."""
from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Callable, Sequence

import numpy as np
import sounddevice as sd
import soundfile as sf
from ..core import (
    CHANNELS,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_CODES,
    FALLBACK_MODELS,
    MAX_RECORDING_SECONDS,
    MIN_RECORDING_SECONDS,
    SAMPLE_RATE,
    SILENCE_PEAK_THRESHOLD,
    TRANSCRIPTION_MODEL,
    _CaptureContext,
    _StatefulLinearResampler,
    _native_input_sample_rate,
    _valid_sample_rate,
    create_client,
    create_live_transcriber,
    encode_wav,
    extract_transcription_text,
    friendly_api_error,
    normalize_audio_signal,
    select_input_sample_rate,
    transcribe_audio,
    transcription_timeout_ms,
    validate_audio,
)
from ..recording_recovery import RecordingSpool, RecoveryStore
from .settings import settings_path

TOGGLE_DEBOUNCE_SECONDS = 0.3
LIVE_FINALIZE_TIMEOUT_SECONDS = 3.0
LIVE_FAILURE_COOLDOWN_SECONDS = 60.0


def _missing_copy_action(_text: str) -> None:
    raise RuntimeError("Provide a platform copy_action to deliver transcribed text")


class VoiceTypingApp:
    def __init__(
        self,
        client,
        panel,
        stream_factory: Callable = sd.InputStream,
        max_recording_seconds: float = MAX_RECORDING_SECONDS,
        live_factory: Callable | None = None,
        copy_action: Callable | None = None,
        paste_action: Callable | None = None,
        model: str = TRANSCRIPTION_MODEL,
        fallback_models: Sequence[str] | None = None,
        sample_rate_selector: Callable[[], float] | None = None,
        language_codes: Sequence[str] = DEFAULT_LANGUAGE_CODES,
        custom_vocabulary: Sequence[str] = DEFAULT_CUSTOM_VOCABULARY,
        shortcut_label: str = "F8",
        client_factory: Callable = create_client,
        recovery_store: RecoveryStore | None = None,
        recovery_client_factory: Callable[[], object] | None = None,
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
        self._recovered_results: list[tuple[str, Path]] = []
        self.listener = None
        self._last_toggle = 0.0
        self._lock = threading.RLock()
        self._delivery_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._live_factory = live_factory
        self.copy_action = copy_action if copy_action is not None else _missing_copy_action
        self._paste_action = paste_action
        self.model = model
        self.fallback_models = tuple(
            FALLBACK_MODELS if fallback_models is None and model == TRANSCRIPTION_MODEL
            else (fallback_models or ())
        )
        self._sample_rate_selector = sample_rate_selector
        self._language_codes = tuple(language_codes)
        self._custom_vocabulary = tuple(custom_vocabulary)
        self.shortcut_label = shortcut_label
        self._client_factory = client_factory
        self.recovery_store = recovery_store or RecoveryStore(settings_path().parent / "Recovery")
        self._recovery_client_factory = recovery_client_factory
        self._recovery_lock = threading.Lock()
        self._recovery_thread: threading.Thread | None = None
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

    def _open_microphone(self, callback: Callable):
        sample_rate = float(SAMPLE_RATE)
        if self._sample_rate_selector is not None:
            sample_rate = _valid_sample_rate(self._sample_rate_selector())
        settings = dict(channels=CHANNELS, dtype="int16", callback=callback)
        try:
            stream = self.stream_factory(samplerate=sample_rate, **settings)
        except sd.PortAudioError:
            if self._sample_rate_selector is not None or self.stream_factory is not sd.InputStream:
                raise
            native_rate = _native_input_sample_rate()
            if native_rate == sample_rate:
                raise
            sample_rate = native_rate
            stream = self.stream_factory(samplerate=sample_rate, **settings)
        return sample_rate, stream

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
        new_stream = None
        capture = None
        spool = None

        def capture_audio(indata, frames, time_info, status):
            if capture is not None:
                self._capture_microphone(capture, indata)

        try:
            sample_rate, new_stream = self._open_microphone(capture_audio)
            try:
                spool = self.recovery_store.start_spool(sample_rate)
            except Exception as error:
                print(f"Recording spool unavailable ({type(error).__name__}).")
            with self._lock:
                if self._shutdown.is_set():
                    if live_session is not None:
                        live_session.cancel()
                    self._close_stream(new_stream)
                    if spool is not None:
                        spool.discard()
                    return False
                capture = _CaptureContext(
                    live_session,
                    max_frames=int(self.max_recording_seconds * sample_rate),
                    sample_rate=sample_rate,
                    spool=spool,
                )
                self._capture_context = capture
                self.recordings = capture.chunks
                self._live_session = live_session
            new_stream.start()
        except Exception as error:
            self._close_stream(new_stream)
            if spool is not None:
                spool.discard()
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
            if spool is not None:
                spool.discard()
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

    def _batch_transcribe(
        self,
        audio_data: np.ndarray,
        sample_rate: float = SAMPLE_RATE,
        client=None,
    ) -> str:
        client = client or self.client
        return transcribe_audio(
            client,
            audio_data,
            sample_rate,
            model=self.model,
            fallback_models=self.fallback_models,
            language_codes=self._language_codes,
            custom_vocabulary=self._custom_vocabulary,
            should_stop=self._shutdown.is_set,
        )

    def _run_delivery_action(self, action: Callable):
        """Serialize short clipboard/paste actions against shutdown."""
        with self._delivery_lock:
            if self._shutdown.is_set():
                return False, None
            return True, action()

    def _stop_and_transcribe(
        self,
        stream,
        chunks: list[np.ndarray] | _CaptureContext,
        live_session=None,
        stopped_at: float | None = None,
    ) -> None:
        stopped_at = stopped_at if stopped_at is not None else time.monotonic()
        route = "batch"
        sample_rate = float(SAMPLE_RATE)
        spool = chunks.spool if isinstance(chunks, _CaptureContext) else None
        audio_data = None
        recovery_path = None
        try:
            self._close_stream(stream)
            live_failed = False
            if isinstance(chunks, _CaptureContext):
                live_failed = chunks.live_failed
                sample_rate = chunks.sample_rate
                chunks = chunks.seal_and_snapshot()
            if spool is not None:
                try:
                    spool.finish()
                except Exception as error:
                    print(f"Recording spool unavailable ({type(error).__name__}).")
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
                text = self._batch_transcribe(audio_data, sample_rate)
            if self._shutdown.is_set():
                return
            if not text:
                recovery_path = self._preserve_failed_recording(
                    audio_data, sample_rate, spool
                )
                self.panel.show("Danışıq başa düşülmədi — yenidən sınayın", color="#fab387", bg_color="#2d2520", hide_after=3000)
                if recovery_path is not None:
                    self.retry_saved_recordings()
                return
            if spool is not None:
                spool.discard()
            self.last_text = text
            result_at = time.monotonic()
            self.last_timing = {
                "route": route,
                "audio_seconds": round(len(audio_data) / sample_rate, 3),
                "stop_to_result_ms": round((result_at - stopped_at) * 1000),
            }
            if self._shutdown.is_set():
                return
            prepared_paste = self._paste_action
            prepare_paste = getattr(prepared_paste, "prepare", None)
            if callable(prepare_paste):
                try:
                    active, prepared_paste = self._run_delivery_action(prepare_paste)
                    if not active:
                        return
                except Exception as error:
                    print(
                        f"Automatic paste preparation failed ({type(error).__name__}); "
                        "text will remain copied."
                    )
                    prepared_paste = None
            try:
                active, _ = self._run_delivery_action(lambda: self.copy_action(text))
                if not active:
                    return
            except Exception as error:
                print(f"Clipboard copy failed ({type(error).__name__}).")
                self.panel.show("Mətni kopyalamaq mümkün olmadı", color="#f38ba8", bg_color="#302020", hide_after=5000)
                return
            self.last_timing["stop_to_copy_ms"] = round((time.monotonic() - stopped_at) * 1000)
            paste_succeeded = False
            if callable(prepared_paste):
                try:
                    active, paste_result = self._run_delivery_action(prepared_paste)
                    if not active:
                        return
                except Exception as error:
                    print(
                        f"Automatic paste failed ({type(error).__name__}); "
                        "text remains copied."
                    )
                else:
                    paste_succeeded = paste_result is not False
                    if paste_succeeded:
                        self.last_timing["stop_to_paste_ms"] = round(
                            (time.monotonic() - stopped_at) * 1000
                        )
            completion_message = (
                "Mətn yapışdırıldı"
                if paste_succeeded
                else "Mətn kopyalandı — Ctrl+V ilə yapışdırın"
            )
            self.panel.show(completion_message, color="#a6e3a1", bg_color="#1e2e1e", hide_after=3000)
            print(
                f"Transcription {'pasted' if paste_succeeded else 'copied'}. route={route}; "
                f"audio_seconds={self.last_timing['audio_seconds']}; "
                f"stop_to_copy_ms={self.last_timing['stop_to_copy_ms']}"
            )
        except Exception as error:
            print(f"Transcription failed ({type(error).__name__}, status={getattr(error, 'code', 'unknown')}).")
            if audio_data is not None:
                recovery_path = self._preserve_failed_recording(
                    audio_data, sample_rate, spool
                )
            if recovery_path is not None:
                self.panel.show(
                    f"{friendly_api_error(error)} — səs saxlanıldı",
                    color="#f38ba8",
                    bg_color="#302020",
                    hide_after=5000,
                )
            else:
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
        if self._shutdown.is_set():
            return False
        if not self.last_text:
            self.panel.show("Kopyalamaq üçün hazır mətn yoxdur", color="#fab387", bg_color="#2d2520", hide_after=2000)
            return False
        try:
            active, _ = self._run_delivery_action(lambda: self.copy_action(self.last_text))
            if not active:
                return False
        except Exception:
            self.panel.show("Mətni kopyalamaq mümkün olmadı", color="#f38ba8", bg_color="#302020", hide_after=3000)
            return False
        self.panel.show("Mətn kopyalandı", color="#a6e3a1", bg_color="#1e2e1e", hide_after=2000)
        return True

    def _preserve_failed_recording(
        self,
        audio_data: np.ndarray,
        sample_rate: float,
        spool: RecordingSpool | None,
    ):
        if spool is not None:
            try:
                return spool.preserve(self.recovery_store, sample_rate)
            except Exception as error:
                print(
                    f"Recording spool preservation failed "
                    f"({type(error).__name__})."
                )
        try:
            return self.recovery_store.save_audio(
                encode_wav(audio_data, sample_rate)
            )
        except Exception as error:
            print(f"Recording recovery save failed ({type(error).__name__}).")
            return None

    def retry_saved_recordings(self, notify: bool = False) -> bool:
        with self._lock:
            if self.state != "idle" or self._shutdown.is_set():
                if notify:
                    self.panel.show(
                        "Cari əməliyyat bitsin deyə gözləyin",
                        color="#fab387",
                        bg_color="#2d2520",
                        hide_after=2500,
                    )
                return False
        with self._recovery_lock:
            if self._recovery_thread is not None and self._recovery_thread.is_alive():
                return False
            self._recovery_thread = threading.Thread(
                target=self._recovery_loop,
                daemon=True,
                name="recording-recovery",
            )
            self._recovery_thread.start()
        if notify:
            self.panel.show(
                "Saxlanmış səslər yenidən yoxlanılır",
                color="#89b4fa",
                bg_color="#202838",
                hide_after=2500,
            )
        return True

    def _recovery_loop(self) -> None:
        client = None
        try:
            with self._lock:
                if self.state != "idle" or self._shutdown.is_set():
                    return
                queued_paths = {
                    pending for _text, pending in self._recovered_results
                }
            self.recovery_store.recover_incomplete_spools()
            files = [
                pending
                for pending in self.recovery_store.pending_files()
                if pending not in queued_paths
            ]
            if not files or self._recovery_client_factory is None:
                return
            client = self._recovery_client_factory()
            for pending in files:
                try:
                    if self._shutdown.is_set():
                        return
                    with self._lock:
                        if self.state != "idle":
                            return
                    audio_data, sample_rate = sf.read(
                        str(pending), dtype="int16", always_2d=True
                    )
                    if audio_data.ndim == 2 and audio_data.shape[1] == 1:
                        audio_data = audio_data[:, 0]
                    text = self._batch_transcribe(
                        audio_data,
                        sample_rate,
                        client=client,
                    ).strip()
                except Exception as error:
                    print(
                        f"Saved recording retry skipped one file "
                        f"({type(error).__name__})."
                    )
                    continue
                if text and not self._shutdown.is_set():
                    with self._lock:
                        if any(
                            existing_path == pending
                            for _existing_text, existing_path
                            in self._recovered_results
                        ):
                            continue
                        self._recovered_results.append((text, pending))
                        self.last_recovered_text = text
                    self.panel.show(
                        "Saxlanmış mətn bərpa edildi",
                        color="#a6e3a1",
                        bg_color="#1e2e1e",
                        hide_after=5000,
                    )
        except Exception as error:
            print(f"Saved recording retry failed ({type(error).__name__}).")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def copy_recovered_transcriptions(self) -> bool:
        with self._lock:
            recovered = tuple(self._recovered_results)
        if not recovered or self._shutdown.is_set():
            self.panel.show(
                "Yeni bərpa edilmiş mətn yoxdur",
                color="#fab387",
                bg_color="#2d2520",
                hide_after=2500,
            )
            return False
        text = "\n\n".join(item[0] for item in recovered)
        try:
            active, _ = self._run_delivery_action(lambda: self.copy_action(text))
            if not active:
                return False
        except Exception:
            self.panel.show(
                "Bərpa edilmiş mətn kopyalanmadı",
                color="#f38ba8",
                bg_color="#302020",
                hide_after=3000,
            )
            return False
        removed_paths = {
            pending
            for _text, pending in recovered
            if self.recovery_store.discard(pending)
        }
        with self._lock:
            self._recovered_results = [
                item
                for item in self._recovered_results
                if not (item in recovered and item[1] in removed_paths)
            ]
            self.last_recovered_text = (
                self._recovered_results[-1][0]
                if self._recovered_results
                else None
            )
        self.panel.show(
            "Bərpa edilmiş mətn kopyalandı",
            color="#a6e3a1",
            bg_color="#1e2e1e",
            hide_after=2000,
        )
        return True

    def pending_recovery_count(self) -> int:
        try:
            return len(self.recovery_store.pending_files())
        except OSError:
            return 0

    def recovered_text_count(self) -> int:
        with self._lock:
            return len(self._recovered_results)

    def open_recovery_folder(self) -> bool:
        opened = self.recovery_store.open_directory()
        if not opened:
            self.panel.show(
                "Bərpa qovluğu açıla bilmədi",
                color="#f38ba8",
                bg_color="#302020",
                hide_after=3000,
            )
        return opened

    def update_client(
        self,
        api_key: str,
        live_factory: Callable | None = None,
        recovery_client_factory: Callable[[], object] | None = None,
    ) -> bool:
        with self._lock:
            if self.state != "idle":
                self.panel.show("Cari əməliyyat bitsin deyə gözləyin", color="#fab387", bg_color="#2d2520", hide_after=2500)
                return False
            old_client = self.client
            self.client = self._client_factory(api_key)
            self._live_factory = live_factory
            self._recovery_client_factory = recovery_client_factory
            self._live_disabled_until = 0.0
        try:
            old_client.close()
        except Exception:
            pass
        return True

    def update_preferences(
        self,
        *,
        live_factory: Callable | None,
        language_codes: Sequence[str],
        custom_vocabulary: Sequence[str],
        shortcut_label: str,
    ) -> None:
        """Apply settings to future recordings without disturbing active audio."""
        with self._lock:
            self._live_factory = live_factory
            self._language_codes = tuple(language_codes)
            self._custom_vocabulary = tuple(custom_vocabulary)
            self.shortcut_label = shortcut_label
            self._live_disabled_until = 0.0

    def shutdown(self) -> None:
        with self._delivery_lock:
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
            chunks = capture.seal_and_snapshot()
            audio_data, reason = validate_audio(chunks, capture.sample_rate)
            if reason or audio_data is None:
                if capture.spool is not None:
                    capture.spool.discard()
            else:
                self._preserve_failed_recording(
                    audio_data,
                    capture.sample_rate,
                    capture.spool,
                )
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


