import io
import os
import stat
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from voicetyping import recording_recovery
from voicetyping.recording_recovery import RecoveryStore


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = RecoveryStore(Path(self.temporary.name) / "Recovery")

    def test_save_list_and_discard_pending_audio(self):
        path = self.store.save_audio(b"RIFF-test-only")
        self.assertEqual(self.store.pending_files(), [path])
        self.assertTrue(self.store.discard(path))
        self.assertEqual(self.store.pending_files(), [])

    def test_current_process_is_detected_as_active_spool_owner(self):
        self.assertTrue(RecoveryStore._pid_is_alive(os.getpid()))

    def test_spool_preserve_creates_valid_native_rate_wav(self):
        samples = np.arange(4_800, dtype="<i2")
        spool = self.store.start_spool(48_000)
        spool.offer(samples[:2_000].tobytes())
        spool.offer(samples[2_000:].tobytes())
        pending = spool.preserve(self.store, 48_000)

        self.assertTrue(pending.name.endswith(".pending.wav"))
        with wave.open(str(pending), "rb") as audio:
            self.assertEqual(audio.getframerate(), 48_000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            recovered = np.frombuffer(
                audio.readframes(audio.getnframes()), dtype="<i2"
            )
        np.testing.assert_array_equal(recovered, samples)
        self.assertFalse(list(self.store.root.glob("*.recording.pcm")))

    def test_spool_fsync_uses_a_writable_file_descriptor(self):
        def require_writable(descriptor):
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                os.write(descriptor, b"")

        with mock.patch(
            "voicetyping.recording_recovery.os.fsync",
            side_effect=require_writable,
        ):
            spool = self.store.start_spool(16_000)
            spool.offer(np.full(5_000, 500, dtype="<i2").tobytes())
            pending = spool.preserve(self.store, 16_000)

        self.assertTrue(pending.exists())

    def test_stale_raw_spool_is_recovered_after_restart(self):
        samples = np.full(5_000, 700, dtype="<i2")
        spool = self.store.start_spool(16_000)
        spool.offer(samples.tobytes())
        raw_path = spool.finish()
        self.assertTrue(raw_path.exists())

        with mock.patch.object(RecoveryStore, "_pid_is_alive", return_value=False):
            recovered = RecoveryStore(self.store.root).recover_incomplete_spools()
        self.assertEqual(len(recovered), 1)
        self.assertFalse(raw_path.exists())
        with wave.open(str(recovered[0]), "rb") as audio:
            self.assertEqual(audio.getframerate(), 16_000)
            self.assertEqual(audio.getnframes(), len(samples))

    def test_complete_stale_atomic_wav_is_promoted(self):
        payload = io.BytesIO()
        with wave.open(payload, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(np.full(5_000, 600, dtype="<i2").tobytes())
        pending = self.store.save_audio(payload.getvalue())
        temporary = pending.with_name(f".{pending.name}.999999.tmp")
        pending.replace(temporary)

        with mock.patch.object(RecoveryStore, "_pid_is_alive", return_value=False):
            recovered = self.store.recover_incomplete_spools()
        self.assertEqual(recovered, [pending])
        self.assertTrue(pending.exists())
        self.assertFalse(temporary.exists())

    def test_active_process_spool_is_not_treated_as_stale(self):
        spool = self.store.start_spool(16_000)
        spool.offer(np.full(5_000, 700, dtype="<i2").tobytes())
        raw_path = spool.finish()

        self.assertEqual(self.store.recover_incomplete_spools(), [])
        self.assertTrue(raw_path.exists())
        spool.discard()

    def test_discarded_spool_leaves_no_audio(self):
        spool = self.store.start_spool()
        spool.offer(np.full(5_000, 500, dtype="<i2").tobytes())
        spool.discard()
        self.assertFalse(list(self.store.root.glob("*")))

    @unittest.skipUnless(os.name == "nt", "Windows path check")
    def test_windows_default_recovery_path_uses_local_app_data(self):
        with mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"},
            clear=False,
        ):
            path = recording_recovery._default_recovery_dir()
        self.assertEqual(path.name, "Recovery")
        self.assertEqual(path.parent.name, "VoiceTyping")


if __name__ == "__main__":
    unittest.main()
