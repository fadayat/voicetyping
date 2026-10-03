import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import voicetyping as vt


class FakePanel:
    def __init__(self):
        self.messages = []
        self.closed = False

    def show(self, text, **kwargs):
        self.messages.append(text)

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, fail_start=False, fail_stop=False):
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        if self.fail_start:
            raise RuntimeError("device unavailable")
        self.started = True

    def stop(self):
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("stop failed")

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, text="hello", error=None, gate=None):
        self.text = text
        self.error = error
        self.gate = gate
        self.calls = []
        self.closed = False
        self.closed_when_called = None
        self.models = self

    def generate_content(self, **kwargs):
        self.closed_when_called = self.closed
        self.calls.append(kwargs)
        if self.gate:
            self.gate.wait(2)
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)

    def close(self):
        self.closed = True


def voiced_audio(seconds=0.3):
    return np.full((int(vt.SAMPLE_RATE * seconds), 1), 1000, dtype=np.int16)


class AudioValidationTests(unittest.TestCase):
    def test_rejects_empty_short_and_silent_audio(self):
        self.assertEqual(vt.validate_audio([])[1], "empty")
        self.assertEqual(vt.validate_audio([voiced_audio(0.1)])[1], "too_short")
        silent = np.zeros((int(vt.SAMPLE_RATE * 0.3), 1), dtype=np.int16)
        self.assertEqual(vt.validate_audio([silent])[1], "silence")

    def test_accepts_voice_and_encodes_wav(self):
        audio, reason = vt.validate_audio([voiced_audio()])
        self.assertIsNone(reason)
        wav = vt.encode_wav(audio)
        self.assertEqual(wav[:4], b"RIFF")

    def test_extracts_dedicated_audio_transcription_response(self):
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                audio_transcription=SimpleNamespace(text="  Voice typing works.  ")
                            )
                        ]
                    )
                )
            ],
            text=None,
        )
        self.assertEqual(vt.extract_transcription_text(response), "Voice typing works.")

    def test_long_audio_gets_bounded_extended_timeout(self):
        self.assertEqual(vt.transcription_timeout_ms(vt.SAMPLE_RATE), 60_250)
        self.assertLessEqual(
            vt.transcription_timeout_ms(vt.SAMPLE_RATE * vt.MAX_RECORDING_SECONDS), 180_000
        )


class ConfigTests(unittest.TestCase):
    def test_save_is_private_atomic_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "voicetyping"
            config_file = config_dir / "config.json"
            with mock.patch.object(vt, "CONFIG_DIR", config_dir), mock.patch.object(vt, "CONFIG_FILE", config_file):
                vt.save_config({"shortcut": "F9", "api_key": "secret"})
                self.assertEqual(os.stat(config_dir).st_mode & 0o777, 0o700)
                self.assertEqual(os.stat(config_file).st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(config_file.read_text())["shortcut"], "F9")

                with mock.patch.object(vt, "_prompt_api_key", return_value="new-secret"):
                    self.assertEqual(vt.get_api_key(force=True), "new-secret")
                self.assertEqual(json.loads(config_file.read_text())["shortcut"], "F9")

    def test_pid_validation_rejects_an_unrelated_process(self):
        with mock.patch.object(vt, "is_pid_running", return_value=True), mock.patch.object(
            vt, "is_voicetyping_process", return_value=False
        ), mock.patch.object(vt.os, "kill") as kill:
            with tempfile.TemporaryDirectory() as directory:
                pid_file = Path(directory) / "voice.pid"
                pid_file.write_text("123")
                self.assertFalse(vt.send_toggle(pid_file))
        kill.assert_not_called()

    def test_settings_mode_never_claims_pid_file(self):
        with mock.patch.object(vt, "get_api_key", return_value="key"), mock.patch.object(
            vt, "acquire_pid_file"
        ) as acquire:
            self.assertEqual(vt.main(["--settings"]), 0)
            acquire.assert_not_called()

    def test_startup_toggle_is_buffered_and_cleanup_stays_signal_safe(self):
        previous_handler = vt.signal.getsignal(vt.signal.SIGUSR1)
        holder = {}
        cleanup_handlers = []

        class DummyApp:
            def __init__(self, client, panel):
                self.toggle_event = threading.Event()
                self.listener = None
                holder["app"] = self

            def toggle(self):
                self.toggle_event.set()

            def shutdown(self):
                cleanup_handlers.append(vt.signal.getsignal(vt.signal.SIGUSR1))

        def key_during_startup(*args, **kwargs):
            os.kill(os.getpid(), vt.signal.SIGUSR1)
            return "key"

        def run_tray(app):
            self.assertTrue(app.toggle_event.wait(1))

        def release_pid():
            cleanup_handlers.append(vt.signal.getsignal(vt.signal.SIGUSR1))

        with mock.patch.object(vt, "acquire_pid_file", return_value=True), mock.patch.object(
            vt, "release_pid_file", side_effect=release_pid
        ), mock.patch("setup_shortcut.setup_gnome_shortcut", return_value="active"), mock.patch.object(
            vt, "get_api_key", side_effect=key_during_startup
        ), mock.patch.object(vt, "create_client", return_value=object()), mock.patch.object(
            vt, "NotificationPanel", return_value=FakePanel()
        ), mock.patch.object(vt, "VoiceTypingApp", DummyApp), mock.patch.object(
            vt, "run_tray", side_effect=run_tray
        ):
            self.assertEqual(vt.main([]), 0)

        self.assertTrue(holder["app"].toggle_event.is_set())
        self.assertTrue(all(handler != previous_handler for handler in cleanup_handlers))
        self.assertEqual(vt.signal.getsignal(vt.signal.SIGUSR1), previous_handler)


class AppStateTests(unittest.TestCase):
    def test_microphone_start_failure_returns_to_idle_and_closes_stream(self):
        panel = FakePanel()
        stream = FakeStream(fail_start=True)
        app = vt.VoiceTypingApp(FakeClient(), panel, stream_factory=lambda **kwargs: stream)
        self.assertFalse(app.start_recording())
        self.assertEqual(app.state, "idle")
        self.assertTrue(stream.closed)
        self.assertIn("Microphone unavailable", panel.messages)

    def test_processing_blocks_overlapping_recording(self):
        gate = threading.Event()
        panel = FakePanel()
        client = FakeClient(gate=gate)
        stream = FakeStream()
        app = vt.VoiceTypingApp(client, panel)
        app.state = "recording"
        app.stream = stream
        app.recordings = [voiced_audio()]

        with mock.patch.object(vt, "copy_to_clipboard"):
            self.assertTrue(app.request_stop())
            self.assertEqual(app.state, "processing")
            self.assertFalse(app.toggle())
            gate.set()
            app.worker.join(2)

        self.assertEqual(app.state, "idle")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(stream.closed)

    def test_success_uses_transcription_model_and_copies_once(self):
        panel = FakePanel()
        client = FakeClient(text="  Salam dünya.  ")
        app = vt.VoiceTypingApp(client, panel)
        app.state = "processing"

        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            app._stop_and_transcribe(FakeStream(fail_stop=True), [voiced_audio()])

        copy.assert_called_once_with("Salam dünya.")
        self.assertEqual(client.calls[0]["model"], vt.TRANSCRIPTION_MODEL)
        self.assertEqual(app.last_text, "Salam dünya.")
        self.assertEqual(app.state, "idle")

    def test_quota_exhausted_falls_back_to_secondary_model(self):
        panel = FakePanel()
        calls = []

        class QuotaFallbackClient:
            def __init__(self):
                self.models = self
                self.closed = False

            def generate_content(self, **kwargs):
                calls.append(kwargs)
                if kwargs["model"] == vt.TRANSCRIPTION_MODEL:
                    error = RuntimeError("RESOURCE_EXHAUSTED quota exceeded")
                    error.code = 429
                    raise error
                return SimpleNamespace(text="Salam fallback.")

            def close(self):
                self.closed = True

        app = vt.VoiceTypingApp(QuotaFallbackClient(), panel)
        app.state = "processing"
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            app._stop_and_transcribe(FakeStream(), [voiced_audio()])

        copy.assert_called_once_with("Salam fallback.")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["model"], vt.TRANSCRIPTION_MODEL)
        self.assertEqual(calls[1]["model"], "gemini-2.5-flash")
        self.assertEqual(app.last_text, "Salam fallback.")
        self.assertEqual(app.state, "idle")

    def test_empty_response_is_not_copied(self):
        panel = FakePanel()
        app = vt.VoiceTypingApp(FakeClient(text="  "), panel)
        app.state = "processing"
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            app._stop_and_transcribe(FakeStream(), [voiced_audio()])
        copy.assert_not_called()
        self.assertIn("Audio was not understood", panel.messages)

    def test_clipboard_failure_keeps_last_text_for_retry(self):
        panel = FakePanel()
        app = vt.VoiceTypingApp(FakeClient(text="kept text"), panel)
        app.state = "processing"
        with mock.patch.object(vt, "copy_to_clipboard", side_effect=RuntimeError("clipboard")):
            app._stop_and_transcribe(FakeStream(), [voiced_audio()])
        self.assertEqual(app.last_text, "kept text")
        self.assertTrue(any("kept in memory" in message for message in panel.messages))

    def test_shutdown_closes_active_resources(self):
        panel = FakePanel()
        client = FakeClient()
        stream = FakeStream()
        app = vt.VoiceTypingApp(client, panel)
        app.state = "recording"
        app.stream = stream
        app.shutdown()
        self.assertEqual(app.state, "shutting_down")
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)
        self.assertTrue(panel.closed)

    def test_shutdown_does_not_close_client_before_worker_uses_it(self):
        gate = threading.Event()
        panel = FakePanel()
        client = FakeClient(gate=gate)
        app = vt.VoiceTypingApp(client, panel)
        app.state = "recording"
        app.stream = FakeStream()
        app.recordings = [voiced_audio()]

        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            self.assertTrue(app.request_stop())
            app.shutdown()
            gate.set()
            app.worker.join(2)

        self.assertFalse(client.closed_when_called)
        self.assertTrue(client.closed)
        copy.assert_not_called()


class ClipboardTests(unittest.TestCase):
    def test_copy_retries_once(self):
        with mock.patch.object(vt.pyperclip, "copy", side_effect=[RuntimeError("busy"), None]) as copy:
            with mock.patch.object(vt.time, "sleep"):
                vt.copy_to_clipboard("text")
        self.assertEqual(copy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
