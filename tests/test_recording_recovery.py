import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

import voicetyping as vt
from recording_recovery import RecoveryStore


class FakePanel:
    def __init__(self):
        self.messages = []

    def show(self, message, **_kwargs):
        self.messages.append(message)

    def close(self):
        pass


class FakeClient:
    def close(self):
        pass


class RecoveryStoreTests(unittest.TestCase):
    def test_permissions_and_interrupted_spool_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            spool = store.start_spool()
            spool.offer(b"\x01\x00" * 1600)
            raw = spool.finish()
            pending = store.recover_incomplete_spools()
            self.assertFalse(raw.exists())
            self.assertEqual(len(pending), 1)
            self.assertEqual(os.stat(store.root).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(pending[0]).st_mode & 0o777, 0o600)
            with wave.open(str(pending[0]), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnframes(), 1600)

    def test_crash_recovery_keeps_capture_sample_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            spool = store.start_spool(48000)
            spool.offer(b"\x01\x00" * 4800)
            spool.finish()
            pending = store.recover_incomplete_spools()[0]
            with wave.open(str(pending), "rb") as audio:
                self.assertEqual(audio.getframerate(), 48000)

    def test_save_and_manual_discard(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            path = store.save_audio(b"wav bytes")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(store.pending_files(), [path])
            store.discard(path)
            self.assertEqual(store.pending_files(), [])


class RecoveryAppTests(unittest.TestCase):
    def test_transcription_failure_preserves_pending_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            app = vt.VoiceTypingApp(FakeClient(), FakePanel(), recovery_store=store)
            app.state = "processing"
            app._batch_transcribe = mock.Mock(side_effect=RuntimeError("offline"))
            with mock.patch("voicetyping.copy_to_clipboard"):
                app._stop_and_transcribe(None, [np.full((4800, 1), 1000, dtype=np.int16)])
            self.assertEqual(len(store.pending_files()), 1)
            self.assertEqual(os.stat(store.pending_files()[0]).st_mode & 0o777, 0o600)
            app.shutdown()

    def test_failed_recording_preservation_and_copy_after_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            app = vt.VoiceTypingApp(FakeClient(), FakePanel(), recovery_store=store)
            pending = app._preserve_failed_recording(
                vt.encode_wav(np.ones(4000, dtype=np.int16)), None, vt.SAMPLE_RATE
            )
            self.assertTrue(pending.exists())
            self.assertEqual(store.pending_files(), [pending])
            app._batch_transcribe = mock.Mock(return_value="Recovered words")
            # Recovery execution uses the app client and closes it after one pass.
            client = mock.Mock()
            app._recovery_client_factory = lambda: client
            with mock.patch("voicetyping.sf.read", return_value=(np.ones(4000, dtype=np.int16), 16000)):
                app._recovery_event.set()
                app._recovery_thread = threading.Thread(target=app._recovery_loop, daemon=True)
                app._recovery_thread.start()
                deadline = time.monotonic() + 2
                while pending.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(pending.exists())
            self.assertEqual(app.last_recovered_text, "Recovered words")
            with mock.patch("voicetyping.copy_to_clipboard") as copy:
                self.assertTrue(app.copy_last_recovered_transcription())
            copy.assert_called_once_with("Recovered words")
            app.shutdown()

    def test_retry_pause_prevents_removing_pending_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            pending = store.save_audio(b"fake-wav")
            app = vt.VoiceTypingApp(FakeClient(), FakePanel(), recovery_store=store)
            app._recovery_pause.set()
            app._recovery_client_factory = lambda: FakeClient()
            app._recovery_event.set()
            app._recovery_thread = threading.Thread(target=app._recovery_loop, daemon=True)
            app._recovery_thread.start()
            time.sleep(0.05)
            self.assertTrue(pending.exists())
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
