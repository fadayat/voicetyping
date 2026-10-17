"""Thread-safe Google Live API transcription for one push-to-talk recording.

The class in this module is deliberately one-shot.  Each recording owns its
Google client, asyncio loop, and websocket so a failed live session cannot
contaminate either the next recording or the application's batch client.
"""

from __future__ import annotations

import asyncio
from collections import deque
import re
import threading
import time
from typing import Any, Callable, Deque, Dict, Optional, Sequence

from google import genai
from google.genai import types


LIVE_MODEL = "gemini-3.5-transcribe-live"
PCM_MIME_TYPE = "audio/pcm;rate=16000"


class _LiveSessionError(Exception):
    """An expected condition that makes the whole live result unusable."""


class LiveTranscriber:
    """Stream one PCM16 recording and return only its completed transcript.

    ``start`` and ``feed_audio`` are suitable for the UI and audio callback
    threads. ``finish`` waits, so callers should invoke it from a worker after
    stopping the microphone.  A ``None`` result means the caller must use its
    complete batch recording as the source of truth.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client_factory: Optional[Callable[[str], Any]] = None,
        connect_timeout: float = 3.0,
        max_buffer_bytes: int = 320_000,
        send_timeout: float = 2.0,
        receive_timeout: float = 4.0,
        poll_interval: float = 0.005,
        language_codes: Optional[Sequence[str]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if connect_timeout <= 0 or send_timeout <= 0 or receive_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_buffer_bytes <= 0:
            raise ValueError("max_buffer_bytes must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if isinstance(language_codes, (str, bytes)):
            raise TypeError("language_codes must be a sequence of language tags")
        if language_codes is not None and not all(
            isinstance(code, str) and code for code in language_codes
        ):
            raise ValueError("language_codes must contain non-empty strings")

        self._api_key = api_key
        self._client_factory = client_factory or (
            lambda key: genai.Client(api_key=key)
        )
        self._connect_timeout = connect_timeout
        self._max_buffer_bytes = max_buffer_bytes
        self._send_timeout = send_timeout
        self._receive_timeout = receive_timeout
        self._poll_interval = poll_interval
        self._language_codes = (
            tuple(language_codes) if language_codes is not None else None
        )

        self._lock = threading.Lock()
        self._chunks: Deque[bytes] = deque()
        self._buffer_bytes = 0
        self._started = False
        self._finishing = False
        self._cancelled = False
        self._overflowed = False
        self._activity_end_sent = False
        self._result: Optional[str] = None
        self._failure_reason: Optional[str] = None
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner_task: Optional[asyncio.Task[Any]] = None
        self._started_at: Optional[float] = None
        self._timings: Dict[str, float | int] = {
            "audio_bytes_sent": 0,
            "chunks_sent": 0,
        }

    def start(self) -> None:
        """Start the dedicated live connection thread without blocking."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._started_at = time.monotonic()
            thread = threading.Thread(
                target=self._thread_main,
                name="voice-live-transcriber",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def feed_audio(self, pcm: bytes) -> bool:
        """Offer raw 16 kHz mono little-endian PCM16 without blocking.

        Returning ``False`` means the live transcript is unusable.  In
        particular, overflow invalidates the *entire* live result; a prefix is
        never presented as a successful transcript.
        """
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise TypeError("pcm must be bytes-like")
        chunk = pcm if isinstance(pcm, bytes) else bytes(pcm)
        if not chunk:
            return True

        should_abort = False
        with self._lock:
            if (
                not self._started
                or self._finishing
                or self._cancelled
                or self._done.is_set()
                or self._overflowed
            ):
                return False
            if self._buffer_bytes + len(chunk) > self._max_buffer_bytes:
                self._overflowed = True
                self._chunks.clear()
                self._buffer_bytes = 0
                self._set_failure_locked("buffer_overflow")
                should_abort = True
            else:
                self._chunks.append(chunk)
                self._buffer_bytes += len(chunk)

        if should_abort:
            self._request_task_cancel()
            return False
        return True

    def finish(self, timeout: float = 4.0) -> Optional[str]:
        """Drain all offered audio, end the turn, and return final text only."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        with self._lock:
            if not self._started:
                self._set_failure_locked("not_started")
                return None
            self._finishing = True
            overflowed = self._overflowed

        if overflowed:
            self._request_task_cancel()
            return None

        waited = self._done.wait(timeout)
        if not waited:
            with self._lock:
                self._set_failure_locked("finish_timeout")
            self._request_task_cancel()
            return None

        with self._lock:
            return self._result if self._failure_reason is None else None

    def cancel(self) -> None:
        """Abort connection, sending, and receiving without waiting for them."""
        with self._lock:
            if self._done.is_set():
                return
            self._cancelled = True
            self._chunks.clear()
            self._buffer_bytes = 0
            self._set_failure_locked("cancelled")
        self._request_task_cancel()

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failure_reason is not None

    @property
    def failure_reason(self) -> Optional[str]:
        with self._lock:
            return self._failure_reason

    @property
    def alive(self) -> bool:
        with self._lock:
            thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def timings(self) -> Dict[str, float | int]:
        with self._lock:
            return dict(self._timings)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            task = loop.create_task(self._async_main())
            with self._lock:
                self._runner_task = task
                cancelled = self._cancelled or self._overflowed
            if cancelled:
                task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            with self._lock:
                self._runner_task = None
                self._loop = None
                if self._result is None and self._failure_reason is None:
                    self._set_failure_locked("incomplete_turn")
            self._done.set()

    async def _async_main(self) -> None:
        client: Any = None
        context: Any = None
        entered = False
        try:
            self._raise_if_aborted()
            client = self._client_factory(self._api_key)
            self._raise_if_aborted()
            config = types.LiveConnectConfig(
                response_modalities=["TEXT"],
                input_audio_transcription=types.AudioTranscriptionConfig(
                    language_codes=(
                        list(self._language_codes)
                        if self._language_codes is not None
                        else []
                    )
                ),
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        disabled=True
                    )
                ),
            )
            context = client.aio.live.connect(model=LIVE_MODEL, config=config)
            connect_started = time.monotonic()
            session = await asyncio.wait_for(
                context.__aenter__(), timeout=self._connect_timeout
            )
            entered = True
            self._record_timing(
                "connect_ms", (time.monotonic() - connect_started) * 1000.0
            )

            transcript = await self._run_session(session)
            self._raise_if_aborted()
            transcript = transcript.strip()
            if not transcript:
                raise _LiveSessionError("empty_transcript")
            with self._lock:
                self._result = transcript
                started_at = self._started_at
                if started_at is not None:
                    self._timings["total_ms"] = (
                        time.monotonic() - started_at
                    ) * 1000.0
        except asyncio.CancelledError:
            with self._lock:
                if self._failure_reason is None:
                    self._set_failure_locked("cancelled")
            raise
        except _LiveSessionError as exc:
            with self._lock:
                self._set_failure_locked(str(exc))
        except asyncio.TimeoutError:
            with self._lock:
                self._set_failure_locked("operation_timeout")
        except Exception as exc:
            with self._lock:
                self._set_failure_locked(self._sanitized_exception_reason(exc))
        finally:
            if entered and context is not None:
                try:
                    await asyncio.wait_for(
                        context.__aexit__(None, None, None),
                        timeout=self._send_timeout,
                    )
                except (Exception, asyncio.CancelledError):
                    pass
            if client is not None:
                await self._close_client(client)

    async def _run_session(self, session: Any) -> str:
        await self._send(
            session,
            activity_start=types.ActivityStart(),
        )
        receiver = asyncio.create_task(self._receive_completed_turn(session))
        stream_started = time.monotonic()
        try:
            while True:
                self._raise_if_aborted()
                self._raise_if_receiver_ended(receiver)
                chunk = self._pop_chunk()
                if chunk is not None:
                    await self._send(
                        session,
                        audio=types.Blob(data=chunk, mime_type=PCM_MIME_TYPE),
                    )
                    with self._lock:
                        self._timings["audio_bytes_sent"] += len(chunk)
                        self._timings["chunks_sent"] += 1
                    continue

                with self._lock:
                    finishing = self._finishing
                if finishing:
                    break
                await asyncio.sleep(self._poll_interval)

            self._raise_if_receiver_ended(receiver)
            # Set this immediately before the send.  A turn completed before
            # this point is an early, therefore incomplete, server turn.
            with self._lock:
                self._activity_end_sent = True
            await self._send(session, activity_end=types.ActivityEnd())
            final_wait_started = time.monotonic()
            transcript = await asyncio.wait_for(
                receiver, timeout=self._receive_timeout
            )
            self._record_timing(
                "finalize_ms", (time.monotonic() - final_wait_started) * 1000.0
            )
            self._record_timing(
                "stream_ms", (time.monotonic() - stream_started) * 1000.0
            )
            return transcript
        finally:
            if not receiver.done():
                receiver.cancel()
            try:
                await receiver
            except (Exception, asyncio.CancelledError):
                pass

    async def _receive_completed_turn(self, session: Any) -> str:
        chunks = []
        async for message in session.receive():
            server_content = getattr(message, "server_content", None)
            if server_content is None:
                continue
            final_chunk = getattr(server_content, "input_transcription", None)
            if final_chunk is not None:
                text = getattr(final_chunk, "text", None)
                if text:
                    chunks.append(text)
            completed = bool(
                getattr(server_content, "turn_complete", False)
                or getattr(server_content, "generation_complete", False)
            )
            if completed:
                with self._lock:
                    activity_end_sent = self._activity_end_sent
                if not activity_end_sent:
                    raise _LiveSessionError("early_turn_complete")
                return "".join(chunks)
        raise _LiveSessionError("incomplete_turn")

    async def _send(self, session: Any, **kwargs: Any) -> None:
        self._raise_if_aborted()
        await asyncio.wait_for(
            session.send_realtime_input(**kwargs), timeout=self._send_timeout
        )
        self._raise_if_aborted()

    def _pop_chunk(self) -> Optional[bytes]:
        with self._lock:
            if not self._chunks:
                return None
            chunk = self._chunks.popleft()
            self._buffer_bytes -= len(chunk)
            return chunk

    def _raise_if_receiver_ended(self, receiver: asyncio.Task[str]) -> None:
        if receiver.done():
            # result() deliberately re-raises the receiver's precise semantic
            # failure (early completion, closed socket, etc.).
            receiver.result()
            raise _LiveSessionError("early_turn_complete")

    def _raise_if_aborted(self) -> None:
        with self._lock:
            if self._overflowed:
                raise _LiveSessionError("buffer_overflow")
            if self._cancelled:
                raise asyncio.CancelledError()

    def _request_task_cancel(self) -> None:
        with self._lock:
            loop = self._loop
            task = self._runner_task
        if loop is None or task is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass

    async def _close_client(self, client: Any) -> None:
        aio_client = getattr(client, "aio", None)
        aclose = getattr(aio_client, "aclose", None)
        if aclose is not None:
            try:
                await asyncio.wait_for(aclose(), timeout=self._send_timeout)
            except (Exception, asyncio.CancelledError):
                pass
        close = getattr(client, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def _record_timing(self, name: str, value: float | int) -> None:
        with self._lock:
            self._timings[name] = value

    def _set_failure_locked(self, reason: str) -> None:
        if self._failure_reason is None:
            self._failure_reason = reason

    @staticmethod
    def _sanitized_exception_reason(exc: Exception) -> str:
        error_type = re.sub(r"[^A-Za-z0-9_]", "_", type(exc).__name__)[:64]
        status = None
        for name in ("status_code", "code", "status"):
            candidate = getattr(exc, name, None)
            if candidate is not None and not callable(candidate):
                status = re.sub(r"[^A-Za-z0-9_]", "_", str(candidate))[:32]
                break
        return f"{error_type}_{status}" if status else error_type
