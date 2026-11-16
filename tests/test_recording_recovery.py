import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from voicetyping import app as vt
from voicetyping.recording_recovery import RecoveryStore


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

    def test_legacy_spool_timestamp_is_not_treated_as_sample_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            store.ensure_directory()
            raw = store.root / "1794420446284008947.recording.pcm"
            raw.write_bytes(b"\x01\x00" * 800)

            pending = store.recover_incomplete_spools()

            self.assertEqual(len(pending), 1)
            self.assertFalse(raw.exists())
            with wave.open(str(pending[0]), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnframes(), 800)

    def test_out_of_range_spool_sample_rate_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            store.ensure_directory()
            raw = store.root / "123.999999999999.recording.pcm"
            raw.write_bytes(b"\x01\x00" * 80)

            pending = store.recover_incomplete_spools()

            with wave.open(str(pending[0]), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)

    def test_save_and_manual_discard(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            path = store.save_audio(b"wav bytes")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(store.pending_files(), [path])
            store.discard(path)
            self.assertEqual(store.pending_files(), [])

    def test_complete_staging_wav_is_recovered_after_process_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            store.ensure_directory()
            destination = store.root / "123.pending.wav"
            temporary = store.root / f".{destination.name}.{os.getpid() + 1}.tmp"
            temporary.write_bytes(vt.encode_wav(np.ones(800, dtype=np.int16)))
            temporary.chmod(0o644)

            with mock.patch.object(store, "_pid_is_alive", return_value=False):
                recovered = store.recover_incomplete_spools()

            self.assertEqual(recovered, [destination])
            self.assertFalse(temporary.exists())
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
            with wave.open(str(destination), "rb") as audio:
                self.assertEqual(audio.getnframes(), 800)

    def test_invalid_staging_wav_is_removed_without_touching_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            store.ensure_directory()
            temporary = store.root / f".broken.pending.wav.{os.getpid() + 1}.tmp"
            unrelated = store.root / ".notes.tmp"
            matching_symlink = (
                store.root / f".linked.pending.wav.{os.getpid() + 2}.tmp"
            )
            temporary.write_bytes(b"partial wav")
            unrelated.write_text("keep", encoding="utf-8")
            matching_symlink.symlink_to(unrelated)

            with mock.patch.object(store, "_pid_is_alive", return_value=False):
                self.assertEqual(store.recover_incomplete_spools(), [])

            self.assertFalse(temporary.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
            self.assertTrue(matching_symlink.is_symlink())

    def test_live_process_staging_wav_is_not_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            store.ensure_directory()
            temporary = store.root / f".active.pending.wav.{os.getpid()}.tmp"
            temporary.write_bytes(vt.encode_wav(np.ones(80, dtype=np.int16)))

            with mock.patch.object(store, "_pid_is_alive", return_value=True):
                self.assertEqual(store.recover_incomplete_spools(), [])

            self.assertTrue(temporary.exists())

    def test_impossible_pid_is_not_alive(self):
        self.assertFalse(RecoveryStore._pid_is_alive(10**100))


class RecoveryAppTests(unittest.TestCase):
    def test_transcription_failure_preserves_pending_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            app = vt.VoiceTypingApp(FakeClient(), FakePanel(), recovery_store=store)
            app.state = "processing"
            app._batch_transcribe = mock.Mock(side_effect=RuntimeError("offline"))
            with mock.patch("voicetyping.app.copy_to_clipboard"):
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
            with mock.patch("voicetyping.app.sf.read", return_value=(np.ones(4000, dtype=np.int16), 16000)):
                app._recovery_event.set()
                app._recovery_thread = threading.Thread(target=app._recovery_loop, daemon=True)
                app._recovery_thread.start()
                deadline = time.monotonic() + 2
                while pending.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(pending.exists())
            self.assertEqual(app.last_recovered_text, "Recovered words")
            with mock.patch("voicetyping.app.copy_to_clipboard") as copy:
                self.assertTrue(app.copy_last_recovered_transcription())
            copy.assert_called_once_with("Recovered words")
            self.assertEqual(app._recovered_texts, [])
            self.assertIsNone(app.last_recovered_text)
            app.shutdown()

    def test_multiple_recoveries_remain_accessible_until_successful_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery")
            pending = [store.save_audio(value) for value in (b"first", b"second")]
            app = vt.VoiceTypingApp(FakeClient(), FakePanel(), recovery_store=store)
            app._batch_transcribe = mock.Mock(side_effect=["First text", "Second text"])
            app._recovery_client_factory = lambda: FakeClient()
            app._recovery_event.set()
            with mock.patch(
                "voicetyping.app.sf.read",
                return_value=(np.ones(4000, dtype=np.int16), 16000),
            ):
                app._recovery_thread = threading.Thread(
                    target=app._recovery_loop, daemon=True
                )
                app._recovery_thread.start()
                deadline = time.monotonic() + 2
                while any(path.exists() for path in pending) and time.monotonic() < deadline:
                    time.sleep(0.01)

            self.assertFalse(any(path.exists() for path in pending))
            self.assertEqual(app._recovered_texts, ["First text", "Second text"])
            self.assertEqual(app.last_recovered_text, "Second text")
            with mock.patch("voicetyping.app.copy_to_clipboard") as copy:
                self.assertTrue(app.copy_last_recovered_transcription())
            copy.assert_called_once_with("First text\n\nSecond text")
            self.assertEqual(app._recovered_texts, [])
            self.assertIsNone(app.last_recovered_text)
            app.shutdown()

    def test_clipboard_failure_keeps_recovered_text_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            app = vt.VoiceTypingApp(
                FakeClient(),
                FakePanel(),
                recovery_store=RecoveryStore(Path(directory) / "recovery"),
            )
            app._recovered_texts = ["Keep this text"]
            app.last_recovered_text = "Keep this text"

            with mock.patch(
                "voicetyping.app.copy_to_clipboard",
                side_effect=RuntimeError("clipboard unavailable"),
            ):
                self.assertFalse(app.copy_last_recovered_transcription())

            self.assertEqual(app._recovered_texts, ["Keep this text"])
            self.assertEqual(app.last_recovered_text, "Keep this text")
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
