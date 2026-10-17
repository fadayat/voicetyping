"""Integration checks for VoiceTypingApp's per-recording live route.

These tests inject microphone streams and live sessions, so they never access a
real microphone, make a network request, or need API credentials.
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import voicetyping as vt


def audio(seconds=0.30, amplitude=1000):
    return np.full((int(vt.SAMPLE_RATE * seconds), 1), amplitude, dtype=np.int16)


class FakePanel:
    def __init__(self):
        self.messages = []
        self.audio_signals = []
        self.closed = False

    def show(self, text, **kwargs):
        self.messages.append(text)

    def feed_audio(self, *signal):
        self.audio_signals.append(signal)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, text="batch result"):
        self.text = text
        self.closed = False
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)

    def close(self):
        self.closed = True


class FakeLiveSession:
    def __init__(
        self,
        result="live result",
        finish_error=None,
        finish_gate=None,
        start_error=None,
        feed_result=True,
    ):
        self.result = result
        self.finish_error = finish_error
        self.finish_gate = finish_gate
        self.start_error = start_error
        self.feed_result = feed_result
        self.started = False
        self.audio = []
        self.finish_calls = []
        self.cancel_calls = 0

    def start(self):
        self.started = True
        if self.start_error:
            raise self.start_error

    def feed_audio(self, pcm_bytes):
        self.audio.append(pcm_bytes)
        return self.feed_result

    def finish(self, timeout=3):
        self.finish_calls.append(timeout)
        if self.finish_gate is not None:
            self.finish_gate.wait(2)
        if self.finish_error:
            raise self.finish_error
        return self.result

    def cancel(self):
        self.cancel_calls += 1


class FakeStream:
    def __init__(
        self, callback, fail_start=False, during_start=None, during_stop=None
    ):
        self.callback = callback
        self.fail_start = fail_start
        self.during_start = during_start
        self.during_stop = during_stop
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        if self.during_start is not None:
            self.callback(self.during_start, len(self.during_start), None, None)
        if self.fail_start:
            raise RuntimeError("microphone unavailable")
        self.started = True

    def stop(self):
        if self.during_stop is not None:
            self.callback(self.during_stop, len(self.during_stop), None, None)
        self.stopped = True

    def close(self):
        self.closed = True


class StreamFactory:
    def __init__(self, fail_start=False, during_start=None, during_stop=None):
        self.fail_start = fail_start
        self.during_start = during_start
        self.during_stop = during_stop
        self.streams = []

    def __call__(self, **kwargs):
        stream = FakeStream(
            kwargs["callback"],
            fail_start=self.fail_start,
            during_start=self.during_start,
            during_stop=self.during_stop,
        )
        self.streams.append(stream)
        return stream


class StreamingAppTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = mock.patch("voicetyping.copy_to_clipboard")
        self.copy = self.clipboard.start()
        self.addCleanup(self.clipboard.stop)

    def make_app(self, live_factory=None, stream_factory=None):
        client = FakeClient()
        panel = FakePanel()
        streams = stream_factory or StreamFactory()
        app = vt.VoiceTypingApp(
            client,
            panel,
            stream_factory=streams,
            max_recording_seconds=120,
            live_factory=live_factory,
        )
        self.addCleanup(lambda: app.shutdown() if app.state != "shutting_down" else None)
        return app, client, panel, streams

    def finish_worker(self, app):
        worker = app.worker
        self.assertIsNotNone(worker, "stopping a recording should start finalization")
        worker.join(2)
        self.assertFalse(worker.is_alive(), "transcription worker did not finish promptly")

    @staticmethod
    def record(app, chunk=None):
        chunk = audio() if chunk is None else chunk
        app.microphone_callback(chunk, len(chunk), None, None)
        return chunk

    def test_live_session_gets_exact_pcm_and_supplies_single_clipboard_result(self):
        live = FakeLiveSession("recognized live text")
        factory = mock.Mock(return_value=live)
        streams = StreamFactory()
        app, client, _panel, _ = self.make_app(factory, streams)

        self.assertTrue(app.start_recording())
        chunk = self.record(app)
        expected_pcm = chunk.astype("<i2", copy=False).tobytes()
        self.assertTrue(app.request_stop())
        self.finish_worker(app)

        self.assertEqual(live.audio, [expected_pcm])
        self.assertEqual(live.finish_calls, [vt.LIVE_FINALIZE_TIMEOUT_SECONDS])
        self.assertEqual(self.copy.call_args_list, [mock.call("recognized live text")])
        self.assertEqual(client.calls, [])
        self.assertEqual(app.last_timing.get("route"), "live")
        self.assertEqual(app.state, "idle")

    def test_live_finalization_failure_batches_the_full_recording_and_sets_cooldown(self):
        live = FakeLiveSession(result=None)
        made = []

        def make_live():
            made.append(live)
            return live

        app, client, _panel, _streams = self.make_app(make_live)
        chunk = self.record_after_start(app)
        batch_audio = []
        app._batch_transcribe = lambda audio_data: batch_audio.append(audio_data.copy()) or "batch text"
        self.finish_worker_after_stop(app)

        np.testing.assert_array_equal(batch_audio[0], chunk)
        self.assertGreater(app._live_disabled_until, time.monotonic())
        self.assertEqual(self.copy.call_args_list, [mock.call("batch text")])
        self.assertEqual(client.calls, [])
        self.assertEqual(app.last_timing.get("route"), "batch")

    def record_after_start(self, app, chunk=None):
        self.assertTrue(app.start_recording())
        return self.record(app, chunk)

    def finish_worker_after_stop(self, app):
        self.assertTrue(app.request_stop())
        self.finish_worker(app)

    def test_cooldown_skips_new_live_session_after_failed_live_finalization(self):
        first = FakeLiveSession(result=None)
        sessions = [first]
        factory = mock.Mock(side_effect=lambda: sessions.pop(0))
        app, _client, _panel, _streams = self.make_app(factory)
        app._batch_transcribe = lambda _audio_data: "fallback"

        self.record_after_start(app)
        self.finish_worker_after_stop(app)
        self.assertEqual(factory.call_count, 1)

        self.record_after_start(app)
        self.finish_worker_after_stop(app)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.copy.call_args_list, [mock.call("fallback"), mock.call("fallback")])

    def test_microphone_start_failure_cancels_live_session_and_leaves_no_timer(self):
        live = FakeLiveSession()
        streams = StreamFactory(fail_start=True)
        app, _client, _panel, _ = self.make_app(lambda: live, streams)

        self.assertFalse(app.start_recording())

        self.assertEqual(live.cancel_calls, 1)
        self.assertEqual(app.state, "idle")
        self.assertIsNone(app.duration_timer)
        self.assertIsNone(app._live_session)
        self.assertTrue(streams.streams[0].closed)
        self.assertEqual(self.copy.call_count, 0)

    def test_shutdown_during_capture_cancels_session_without_copying(self):
        live = FakeLiveSession()
        app, client, panel, _streams = self.make_app(lambda: live)
        self.assertTrue(app.start_recording())
        self.record(app)

        app.shutdown()

        self.assertEqual(live.cancel_calls, 1)
        self.assertEqual(self.copy.call_count, 0)
        self.assertTrue(client.closed)
        self.assertTrue(panel.closed)
        self.assertEqual(app.state, "shutting_down")

    def test_shutdown_while_finish_is_blocked_cancels_without_closing_client_early(self):
        gate = threading.Event()
        live = FakeLiveSession("must not be copied", finish_gate=gate)
        app, client, _panel, _streams = self.make_app(lambda: live)
        self.record_after_start(app)
        self.assertTrue(app.request_stop())
        deadline = time.monotonic() + 1
        while not live.finish_calls and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(live.finish_calls, "worker never entered live finalization")

        app.shutdown()
        self.assertGreaterEqual(live.cancel_calls, 1)
        self.assertFalse(client.closed, "client closed while worker was still finalizing")
        self.assertEqual(self.copy.call_count, 0)

        gate.set()
        self.finish_worker(app)
        self.assertTrue(client.closed)
        self.assertEqual(self.copy.call_count, 0)

    def test_silent_and_too_short_recordings_cancel_live_without_batch_or_copy(self):
        cases = (
            (np.zeros((int(vt.SAMPLE_RATE * 0.30), 1), dtype=np.int16), "silence"),
            (audio(seconds=0.10), "too_short"),
        )
        for chunk, _reason in cases:
            with self.subTest(reason=_reason):
                self.copy.reset_mock()
                live = FakeLiveSession("should not be used")
                app, client, _panel, _streams = self.make_app(lambda: live)
                batch_calls = []
                app._batch_transcribe = lambda audio_data: batch_calls.append(audio_data) or "unexpected"

                self.record_after_start(app, chunk)
                self.finish_worker_after_stop(app)

                self.assertEqual(live.finish_calls, [])
                self.assertEqual(live.cancel_calls, 1)
                self.assertEqual(batch_calls, [])
                self.assertEqual(client.calls, [])
                self.assertEqual(self.copy.call_count, 0)

    def test_each_recording_gets_a_fresh_session_and_audio_does_not_mix(self):
        first = FakeLiveSession("first result")
        second = FakeLiveSession("second result")
        factory = mock.Mock(side_effect=[first, second])
        app, _client, _panel, _streams = self.make_app(factory)

        first_chunk = self.record_after_start(app, audio(amplitude=1111))
        self.finish_worker_after_stop(app)
        second_chunk = self.record_after_start(app, audio(amplitude=2222))
        self.finish_worker_after_stop(app)

        self.assertEqual(first.audio, [first_chunk.astype("<i2", copy=False).tobytes()])
        self.assertEqual(second.audio, [second_chunk.astype("<i2", copy=False).tobytes()])
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(self.copy.call_args_list, [mock.call("first result"), mock.call("second result")])

    def test_audio_arriving_during_stream_start_is_retained_by_live_session(self):
        early_chunk = audio(amplitude=4321)
        live = FakeLiveSession("early speech")
        streams = StreamFactory(during_start=early_chunk)
        app, _client, _panel, _ = self.make_app(lambda: live, streams)

        self.assertTrue(app.start_recording())
        self.assertEqual(len(app.recordings), 1)
        np.testing.assert_array_equal(app.recordings[0], early_chunk)
        self.assertEqual(live.audio, [early_chunk.astype("<i2", copy=False).tobytes()])

        self.finish_worker_after_stop(app)
        self.assertEqual(self.copy.call_args_list, [mock.call("early speech")])

    def test_app_lock_contention_does_not_drop_local_or_live_audio(self):
        live = FakeLiveSession("lock-safe result")
        streams = StreamFactory()
        app, _client, _panel, _ = self.make_app(lambda: live, streams)
        self.assertTrue(app.start_recording())

        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_control_lock():
            with app._lock:
                lock_held.set()
                release_lock.wait(1)

        holder = threading.Thread(target=hold_control_lock)
        holder.start()
        self.assertTrue(lock_held.wait(0.5))
        chunk = audio(amplitude=2468)
        try:
            started = time.monotonic()
            streams.streams[0].callback(chunk, len(chunk), None, None)
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            release_lock.set()
            holder.join(1)

        self.assertEqual(len(app.recordings), 1)
        np.testing.assert_array_equal(app.recordings[0], chunk)
        self.assertEqual(
            live.audio, [chunk.astype("<i2", copy=False).tobytes()]
        )
        self.finish_worker_after_stop(app)

    def test_callback_tail_during_stream_stop_is_in_full_batch_fallback(self):
        first = audio(seconds=0.15, amplitude=1111)
        tail = audio(seconds=0.15, amplitude=2222)
        live = FakeLiveSession(result=None)
        streams = StreamFactory(during_stop=tail)
        app, _client, _panel, _ = self.make_app(lambda: live, streams)
        batch_audio = []
        app._batch_transcribe = (
            lambda audio_data: batch_audio.append(audio_data.copy()) or "full batch"
        )

        self.assertTrue(app.start_recording())
        streams.streams[0].callback(first, len(first), None, None)
        self.finish_worker_after_stop(app)

        expected = np.concatenate([first, tail])
        np.testing.assert_array_equal(batch_audio[0], expected)
        self.assertEqual(
            live.audio,
            [
                first.astype("<i2", copy=False).tobytes(),
                tail.astype("<i2", copy=False).tobytes(),
            ],
        )
        self.assertEqual(self.copy.call_args_list, [mock.call("full batch")])

    def test_live_feed_failure_is_cancelled_before_full_batch_fallback(self):
        live = FakeLiveSession(feed_result=False)
        app, _client, _panel, _streams = self.make_app(lambda: live)
        cancel_counts_at_batch = []
        app._batch_transcribe = lambda _audio_data: (
            cancel_counts_at_batch.append(live.cancel_calls) or "batch result"
        )

        self.record_after_start(app)
        self.finish_worker_after_stop(app)

        self.assertGreaterEqual(cancel_counts_at_batch[0], 1)
        self.assertEqual(live.finish_calls, [])
        self.assertGreater(app._live_disabled_until, time.monotonic())
        self.assertEqual(self.copy.call_args_list, [mock.call("batch result")])


if __name__ == "__main__":
    unittest.main()
