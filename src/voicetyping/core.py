"""Platform-neutral audio preparation and Gemini transcription primitives."""

from __future__ import annotations

import io
import math
from typing import Callable, Sequence

import numpy as np
import soundfile as sf
from google import genai
from google.genai import types

from .recording_recovery import RecordingSpool


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
DEFAULT_LANGUAGE_CODES = ("az-AZ", "en-US")
DEFAULT_CUSTOM_VOCABULARY = (
    "fallback",
    "backend",
    "frontend",
    "API",
    "GitHub",
    "Docker",
    "Python",
    "Linux",
    "Windows",
    "AppImage",
    "Fedora",
    "Ubuntu",
    "Gemini",
)


def create_client(api_key: str):
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def create_live_transcriber(
    api_key: str,
    *,
    language_codes: Sequence[str] | None = None,
    custom_vocabulary: Sequence[str] | None = None,
):
    from .live_transcription import LiveTranscriber

    return LiveTranscriber(
        api_key,
        language_codes=language_codes,
        custom_vocabulary=custom_vocabulary,
    )


def _valid_sample_rate(value) -> float:
    try:
        sample_rate = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Input device has no usable sample rate") from error
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("Input device has no usable sample rate")
    return sample_rate


def _native_input_sample_rate(query_devices=None) -> float:
    if query_devices is None:
        import sounddevice as sounddevice_module

        query_devices = sounddevice_module.query_devices
    device = query_devices(kind="input")
    default_sample_rate = (
        device.get("default_samplerate")
        if hasattr(device, "get")
        else getattr(device, "default_samplerate", None)
    )
    return _valid_sample_rate(default_sample_rate)


def select_input_sample_rate(check_input_settings=None, query_devices=None) -> float:
    """Prefer Gemini Live's 16 kHz rate, then use the device's native rate."""
    if check_input_settings is None:
        import sounddevice as sounddevice_module

        check_input_settings = sounddevice_module.check_input_settings
    try:
        check_input_settings(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
        )
    except Exception:
        return _native_input_sample_rate(query_devices)
    return float(SAMPLE_RATE)


def transcription_timeout_ms(
    sample_count: int, sample_rate: float = SAMPLE_RATE
) -> int:
    audio_seconds = sample_count / _valid_sample_rate(sample_rate)
    return min(180_000, REQUEST_TIMEOUT_MS + int(audio_seconds * 250))


def normalize_audio_signal(
    indata, bins: int = 12
) -> tuple[float, float, tuple[float, ...]]:
    """Return a bounded, perceptually compressed microphone snapshot."""
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
        segment = magnitudes[edges[index] : edges[index + 1]]
        value = (
            float(np.sqrt(np.mean(segment * segment))) if segment.size else 0.0
        )
        envelope.append(compress(value))
    return rms, peak, tuple(envelope)


def validate_audio(
    chunks: list[np.ndarray], sample_rate: float = SAMPLE_RATE
) -> tuple[np.ndarray | None, str | None]:
    if not chunks:
        return None, "empty"
    try:
        audio_data = np.concatenate(chunks, axis=0)
    except (TypeError, ValueError):
        return None, "invalid"
    if audio_data.size < int(_valid_sample_rate(sample_rate) * MIN_RECORDING_SECONDS):
        return None, "too_short"
    samples = audio_data.astype(np.int32, copy=False)
    if int(np.max(np.abs(samples), initial=0)) < SILENCE_PEAK_THRESHOLD:
        return None, "silence"
    return audio_data, None


def encode_wav(audio_data: np.ndarray, sample_rate: float = SAMPLE_RATE) -> bytes:
    wav_io = io.BytesIO()
    sf.write(
        wav_io,
        audio_data,
        int(round(_valid_sample_rate(sample_rate))),
        format="WAV",
        subtype="PCM_16",
    )
    return wav_io.getvalue()


def _numeric_status(error: Exception) -> int | None:
    value = getattr(error, "code", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def friendly_api_error(error: Exception) -> str:
    code = _numeric_status(error)
    if code in (401, 403):
        return "API açarı qəbul edilmədi — Parametrləri yoxlayın"
    if code == 429:
        return "Sorğu limiti dolub — bir az sonra yenidən sınayın"
    if code is not None and code >= 500:
        return "Transkripsiya xidməti müvəqqəti əlçatan deyil"
    if "timeout" in type(error).__name__.lower():
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
    if (
        result.startswith(('"', "'", "“", "«"))
        and result.endswith(('"', "'", "”", "»"))
        and len(result) >= 2
    ):
        result = result[1:-1].strip()
    return result


def transcribe_audio(
    client,
    audio_data: np.ndarray,
    sample_rate: float = SAMPLE_RATE,
    *,
    model: str = TRANSCRIPTION_MODEL,
    fallback_models: Sequence[str] | None = None,
    language_codes: Sequence[str] = DEFAULT_LANGUAGE_CODES,
    custom_vocabulary: Sequence[str] = DEFAULT_CUSTOM_VOCABULARY,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Transcribe one complete recording with bounded model fallback."""
    audio_part = types.Part.from_bytes(
        data=encode_wav(audio_data, sample_rate), mime_type="audio/wav"
    )
    timeout_ms = transcription_timeout_ms(len(audio_data), sample_rate)
    fallbacks = FALLBACK_MODELS if fallback_models is None else tuple(fallback_models)
    selected_models = (model, *fallbacks)
    last_error = None
    for model_name in selected_models:
        if should_stop is not None and should_stop():
            return ""
        try:
            if model_name == TRANSCRIPTION_MODEL:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[audio_part],
                    config=types.GenerateContentConfig(
                        audio_transcription_config=types.AudioTranscriptionConfig(
                            language_codes=list(language_codes),
                            custom_vocabulary=list(custom_vocabulary),
                        ),
                        http_options=types.HttpOptions(timeout=timeout_ms),
                    ),
                )
            else:
                prompt = (
                    "Accurately transcribe the spoken audio verbatim. "
                    "Return ONLY the transcribed speech. Do not add commentary, "
                    "conversational replies, explanations, formatting, or quotation marks. "
                    "Preserve code-switching and English technical terms exactly as spoken. "
                    "If no speech is heard, return nothing."
                )
                response = client.models.generate_content(
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
            code = _numeric_status(error)
            print(
                f"Model {model_name} failed "
                f"({type(error).__name__}, status={code})."
            )
            if code in (401, 403):
                raise
            if code not in (404, 429, 500, 502, 503, 504):
                description = str(error).lower()
                if "quota" not in description and "resource_exhausted" not in description:
                    raise
    if last_error is not None:
        raise last_error
    return ""


class _CaptureContext:
    """Audio owned by exactly one recording stream."""

    def __init__(
        self,
        live_session,
        max_frames: int,
        sample_rate: float = SAMPLE_RATE,
        live_skip_reason: str | None = None,
        spool: RecordingSpool | None = None,
    ):
        self.live_session = live_session
        self.max_frames = max(1, max_frames)
        self.sample_rate = _valid_sample_rate(sample_rate)
        self.live_skip_reason = live_skip_reason
        self.spool = spool
        self.live_resampler = (
            _StatefulLinearResampler(self.sample_rate, SAMPLE_RATE)
            if live_session is not None and self.sample_rate != SAMPLE_RATE
            else None
        )
        self.chunks: list[np.ndarray] = []
        self.frames = 0
        self.sealed = False
        self.live_failed = False
        self.status_messages: set[str] = set()

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
        if self.spool is not None:
            self.spool.offer(chunk.astype("<i2", copy=False).tobytes())
        if self.live_session is not None and not self.live_failed:
            try:
                live_chunk = (
                    self.live_resampler.process(chunk)
                    if self.live_resampler is not None
                    else chunk.astype("<i2", copy=False).tobytes()
                )
                if live_chunk and not self.live_session.feed_audio(live_chunk):
                    self.live_failed = True
            except Exception:
                self.live_failed = True
        return chunk

    def seal_and_snapshot(self) -> list[np.ndarray]:
        self.sealed = True
        return list(self.chunks)


class _StatefulLinearResampler:
    """Resample mono int16 chunks while preserving the output sample grid."""

    def __init__(self, input_rate: float, output_rate: float):
        self.input_rate = float(input_rate)
        self.output_rate = float(output_rate)
        self.input_frames = 0
        self.next_output_index = 0
        self.previous_sample: np.int16 | None = None

    def process(self, audio: np.ndarray) -> bytes:
        samples = np.asarray(audio, dtype=np.int16).reshape(-1)
        if samples.size == 0:
            return b""
        chunk_start = self.input_frames
        chunk_end = chunk_start + samples.size
        if self.previous_sample is None:
            source = samples
            source_start = chunk_start
        else:
            source = np.concatenate((np.asarray([self.previous_sample]), samples))
            source_start = chunk_start - 1
        last_available = chunk_end - 1
        last_output_index = int(
            math.floor(last_available * self.output_rate / self.input_rate)
        )
        if last_output_index < self.next_output_index:
            self.input_frames = chunk_end
            self.previous_sample = samples[-1]
            return b""
        output_indices = np.arange(
            self.next_output_index, last_output_index + 1, dtype=np.float64
        )
        positions = output_indices * (self.input_rate / self.output_rate)
        local_positions = positions - source_start
        lower = np.floor(local_positions).astype(np.int64)
        upper = np.minimum(lower + 1, source.size - 1)
        fraction = local_positions - lower
        interpolated = source[lower].astype(np.float64) + (
            source[upper].astype(np.float64) - source[lower]
        ) * fraction
        output = np.clip(np.rint(interpolated), -32768, 32767).astype("<i2")
        self.input_frames = chunk_end
        self.previous_sample = samples[-1]
        self.next_output_index = last_output_index + 1
        return output.tobytes()
