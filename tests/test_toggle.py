import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voicetyping_toggle as toggle


class ToggleHelperTests(unittest.TestCase):
    def test_invalid_pid_file_is_not_signalled(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "voice.pid"
            pid_file.write_text("not-a-pid")
            self.assertIsNone(toggle.read_pid(pid_file))

    def test_valid_process_receives_usr1(self):
        with mock.patch.object(toggle, "read_pid", return_value=123), mock.patch.object(
            toggle, "is_voicetyping_process", return_value=True
        ), mock.patch.object(toggle.os, "kill") as kill:
            self.assertEqual(toggle.main(), 0)
        kill.assert_called_once_with(123, signal.SIGUSR1)

    def test_unrelated_process_is_not_signalled(self):
        with mock.patch.object(toggle, "read_pid", return_value=os.getpid()), mock.patch.object(
            toggle, "is_voicetyping_process", return_value=False
        ), mock.patch.object(toggle.os, "kill") as kill:
            self.assertEqual(toggle.main(), 1)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
