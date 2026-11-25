import contextlib
import io
from pathlib import Path
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest import mock

import numpy as np

from voicetyping.windows import engine as core
from voicetyping.recording_recovery import RecoveryStore


class Panel:
    def __init__(self):
        self.messages = []
        self.closed = False

    def show(self, message, **_kwargs):
        self.messages.append(message)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, response=None, error=None, on_generate=None):
        self.response = response or type("Response", (), {"text": "transcribed"})()
        self.error = error
        self.on_generate = on_generate
        self.calls = []
        self.closed = False
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_generate:
            self.on_generate()
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, callback, final_chunk=None):
        self.callback = callback
        self.final_chunk = final_chunk
        self.stopped = False
        self.closed = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True
        if self.final_chunk is not None:
            self.callback(self.final_chunk, len(self.final_chunk), None, None)

    def close(self):
        self.closed = True


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def make_app(self, client=None, **kwargs):
        self.panel = Panel()
        self.delivered = []
        return core.VoiceTypingApp(
            client or FakeClient(),
            self.panel,
            copy_action=self.delivered.append,
            recovery_store=RecoveryStore(Path(self.temporary.name) / "Recovery"),
            **kwargs,
        )

    def test_audio_validation_empty_short_silence_and_int16_minimum(self):
        self.assertEqual(core.validate_audio([]), (None, "empty"))
        short = np.full(int(core.SAMPLE_RATE * core.MIN_RECORDING_SECONDS) - 1, 1000, dtype=np.int16)
        self.assertEqual(core.validate_audio([short]), (None, "too_short"))
        silence = np.full(int(core.SAMPLE_RATE * core.MIN_RECORDING_SECONDS), 63, dtype=np.int16)
        self.assertEqual(core.validate_audio([silence]), (None, "silence"))
        at_threshold = np.full(int(core.SAMPLE_RATE * core.MIN_RECORDING_SECONDS), 64, dtype=np.int16)
        audio, reason = core.validate_audio([at_threshold])
        self.assertIsNone(reason)
        np.testing.assert_array_equal(audio, at_threshold)
        minimum_int16 = np.full(len(at_threshold), -32768, dtype=np.int16)
        audio, reason = core.validate_audio([minimum_int16])
        self.assertIsNone(reason)
        np.testing.assert_array_equal(audio, minimum_int16)

    def test_native_rate_controls_validation_wav_header_and_timeout(self):
        sample_rate = 48_000
        audio = np.full((int(sample_rate * 0.30), 1), 1000, dtype=np.int16)
        validated, reason = core.validate_audio([audio], sample_rate)
        self.assertIsNone(reason)
        with wave.open(io.BytesIO(core.encode_wav(validated, sample_rate)), "rb") as wav:
            self.assertEqual(wav.getframerate(), sample_rate)
            self.assertEqual(wav.getnframes(), len(audio))
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
        self.assertEqual(
            core.validate_audio([np.full((4_800, 1), 1000, dtype=np.int16)], sample_rate)[1],
            "too_short",
        )
        self.assertEqual(core.transcription_timeout_ms(sample_rate, sample_rate), 60_250)

    def test_sample_rate_selection_prefers_16khz_then_native_rate(self):
        accepted = mock.Mock()
        query = mock.Mock()
        self.assertEqual(core.select_input_sample_rate(accepted, query), 16_000)
        query.assert_not_called()

        rejected = mock.Mock(side_effect=RuntimeError("unsupported"))
        query = mock.Mock(return_value={"default_samplerate": 48_000.0})
        self.assertEqual(core.select_input_sample_rate(rejected, query), 48_000.0)
        query.assert_called_once_with(kind="input")
        for invalid in (None, "bad", 0, -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                core.select_input_sample_rate(
                    rejected,
                    mock.Mock(return_value={"default_samplerate": invalid}),
                )

    def test_native_rate_live_resampling_is_continuous_across_chunks(self):
        samples = np.arange(8_820, dtype=np.int16).reshape(-1, 1)
        whole = core._StatefulLinearResampler(44_100, 16_000)
        expected = whole.process(samples)
        split = core._StatefulLinearResampler(44_100, 16_000)
        actual = b"".join(
            (
                split.process(samples[:1_111]),
                split.process(samples[1_111:5_555]),
                split.process(samples[5_555:]),
            )
        )
        self.assertEqual(actual, expected)
        self.assertEqual(len(np.frombuffer(actual, dtype="<i2")), 3_200)

    def test_capture_is_bounded_to_maximum_frames(self):
        capture = core._CaptureContext(None, max_frames=5)
        first = capture.capture(np.arange(4, dtype=np.int16).reshape(-1, 1))
        second = capture.capture(np.arange(10, 20, dtype=np.int16).reshape(-1, 1))
        third = capture.capture(np.ones((2, 1), dtype=np.int16))
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 1)
        self.assertIsNone(third)
        self.assertEqual(capture.frames, 5)
        self.assertEqual(sum(len(chunk) for chunk in capture.seal_and_snapshot()), 5)

    def test_stream_stop_captures_final_callback_chunk_before_transcription(self):
        client = FakeClient()
        final_chunk = np.full((5000, 1), 1000, dtype=np.int16)
        stream_box = []

        def stream_factory(**kwargs):
            stream = FakeStream(kwargs["callback"], final_chunk=final_chunk)
            stream_box.append(stream)
            return stream

        app = self.make_app(client, stream_factory=stream_factory)
        self.assertTrue(app.start_recording())
        self.assertTrue(app.request_stop())
        app.worker.join(timeout=3)
        self.assertFalse(app.worker.is_alive())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(app.last_timing["audio_seconds"], 0.312)
        self.assertEqual(self.delivered, ["transcribed"])
        self.assertTrue(stream_box[0].stopped)
        self.assertTrue(stream_box[0].closed)
        self.assertEqual(app.pending_recovery_count(), 0)

    def test_batch_transcription_uses_configured_model(self):
        client = FakeClient()
        app = self.make_app(client, model="test-model")
        text = app._batch_transcribe(np.full(5000, 500, dtype=np.int16))
        self.assertEqual(text, "transcribed")
        self.assertEqual(client.calls[0]["model"], "test-model")

    def test_transcription_model_receives_language_and_vocabulary_hints(self):
        client = FakeClient()
        app = self.make_app(
            client,
            language_codes=("az-AZ", "en-US"),
            custom_vocabulary=("fallback", "GitHub"),
        )
        self.assertEqual(
            app._batch_transcribe(np.full(5000, 500, dtype=np.int16)),
            "transcribed",
        )
        call = client.calls[0]
        self.assertEqual(call["model"], core.TRANSCRIPTION_MODEL)
        config = call["config"].audio_transcription_config
        self.assertEqual(config.language_codes, ["az-AZ", "en-US"])
        self.assertEqual(config.custom_vocabulary, ["fallback", "GitHub"])

    def test_quota_error_uses_bounded_secondary_model(self):
        calls = []

        class QuotaClient(FakeClient):
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                if kwargs["model"] == core.TRANSCRIPTION_MODEL:
                    error = RuntimeError("RESOURCE_EXHAUSTED")
                    error.code = 429
                    raise error
                return SimpleNamespace(text="fallback text")

        app = self.make_app(QuotaClient(), fallback_models=("secondary-model",))
        text = app._batch_transcribe(np.full(5000, 500, dtype=np.int16))
        self.assertEqual(text, "fallback text")
        self.assertEqual([call["model"] for call in calls], [core.TRANSCRIPTION_MODEL, "secondary-model"])
        self.assertIn("code-switching", calls[1]["contents"][0])

    def test_native_rate_capture_streams_16khz_and_batches_original_rate(self):
        class Live:
            def __init__(self):
                self.audio = []

            def feed_audio(self, value):
                self.audio.append(value)
                return True

        live = Live()
        capture = core._CaptureContext(live, 48_000, sample_rate=48_000)
        original = np.full((14_400, 1), 1234, dtype=np.int16)
        capture.capture(original)
        streamed = np.frombuffer(live.audio[0], dtype="<i2")
        self.assertEqual(streamed.size, 4_800)
        np.testing.assert_array_equal(streamed, np.full(4_800, 1234, dtype=np.int16))
        np.testing.assert_array_equal(capture.seal_and_snapshot()[0], original)

    def test_injected_copy_action_delivers_transcription(self):
        app = self.make_app()
        app._stop_and_transcribe(None, [np.full(5000, 500, dtype=np.int16)])
        self.assertEqual(self.delivered, ["transcribed"])

    def test_guarded_paste_reports_clipboard_fallback(self):
        app = self.make_app(paste_action=lambda: False)
        app._stop_and_transcribe(None, [np.full(5000, 500, dtype=np.int16)])
        self.assertEqual(self.delivered, ["transcribed"])
        self.assertIn('Ctrl+V', self.panel.messages[-1])
        self.assertNotIn('stop_to_paste_ms', app.last_timing)

    def test_successful_paste_and_manual_copy_are_distinct(self):
        pasted = []
        app = self.make_app(paste_action=lambda: pasted.append(True))
        app._stop_and_transcribe(None, [np.full(5000, 500, dtype=np.int16)])
        self.assertEqual(pasted, [True])
        self.assertIn('yapışdırıldı', self.panel.messages[-1])
        app.copy_last_transcription()
        self.assertEqual(pasted, [True])
        app.shutdown()
        self.assertFalse(app.copy_last_transcription())

    def test_shutdown_during_api_call_prevents_copy(self):
        app_ref = []
        client = FakeClient(on_generate=lambda: app_ref[0].shutdown())
        app = self.make_app(client)
        app_ref.append(app)
        app._stop_and_transcribe(None, [np.full(5000, 500, dtype=np.int16)])
        self.assertEqual(self.delivered, [])
        self.assertTrue(self.panel.closed)

    def test_failed_request_preserves_audio_for_retry(self):
        error = RuntimeError("offline")
        error.code = 503
        app = self.make_app(FakeClient(error=error))
        app._stop_and_transcribe(
            None, [np.full(5000, 500, dtype=np.int16)]
        )
        self.assertEqual(app.pending_recovery_count(), 1)
        self.assertIn("səs saxlanıldı", self.panel.messages[-1])

    def test_saved_recording_recovers_and_copies_without_losing_text(self):
        recovery_client = FakeClient(response=SimpleNamespace(text="recovered text"))
        app = self.make_app(
            recovery_client_factory=lambda: recovery_client,
        )
        app.recovery_store.save_audio(
            core.encode_wav(np.full(5000, 800, dtype=np.int16))
        )
        self.assertTrue(app.retry_saved_recordings())
        app._recovery_thread.join(timeout=3)
        self.assertFalse(app._recovery_thread.is_alive())
        self.assertEqual(app.pending_recovery_count(), 1)
        self.assertEqual(app.recovered_text_count(), 1)
        self.assertTrue(app.copy_recovered_transcriptions())
        self.assertEqual(self.delivered, ["recovered text"])
        self.assertEqual(app.pending_recovery_count(), 0)
        self.assertEqual(app.recovered_text_count(), 0)

    def test_corrupt_saved_audio_does_not_starve_later_valid_recording(self):
        recovery_client = FakeClient(response=SimpleNamespace(text="valid recovered"))
        app = self.make_app(
            recovery_client_factory=lambda: recovery_client,
        )
        root = app.recovery_store.ensure_directory()
        (root / "000.pending.wav").write_bytes(b"not-a-wav")
        valid = app.recovery_store.save_audio(
            core.encode_wav(np.full(5000, 800, dtype=np.int16))
        )

        self.assertTrue(app.retry_saved_recordings())
        app._recovery_thread.join(timeout=3)
        self.assertEqual(app.recovered_text_count(), 1)
        self.assertTrue(valid.exists())
        self.assertTrue(app.copy_recovered_transcriptions())
        self.assertFalse(valid.exists())
        self.assertTrue((root / "000.pending.wav").exists())

    def test_shutdown_during_recording_preserves_valid_audio(self):
        chunk = np.full((5000, 1), 900, dtype=np.int16)

        def stream_factory(**kwargs):
            stream = FakeStream(kwargs["callback"])
            stream.start = lambda: kwargs["callback"](chunk, len(chunk), None, None)
            return stream

        app = self.make_app(stream_factory=stream_factory)
        self.assertTrue(app.start_recording())
        app.shutdown()
        self.assertEqual(app.pending_recovery_count(), 1)

    def test_api_error_message_does_not_echo_secret(self):
        secret = "sensitive-api-key-value"
        error = RuntimeError(secret)
        error.code = 401
        app = self.make_app(FakeClient(error=error))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app._stop_and_transcribe(None, [np.full(5000, 500, dtype=np.int16)])
        visible = " ".join(self.panel.messages) + output.getvalue()
        self.assertNotIn(secret, visible)
        self.assertIn("API açarı qəbul edilmədi", visible)


if __name__ == "__main__":
    unittest.main()
