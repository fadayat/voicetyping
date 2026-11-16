import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voicetyping import voicetyping_toggle as toggle


class ToggleHelperTests(unittest.TestCase):
    def test_process_validation_accepts_package_module_and_console_script(self):
        launch_forms = (
            b"/usr/bin/python3\0/home/user/voicetyping.py\0",
            b"/home/user/.venv/bin/voicetyping\0",
            b"/usr/bin/python3\0-m\0voicetyping.app\0",
        )
        for command_line in launch_forms:
            with self.subTest(command_line=command_line), mock.patch.object(
                toggle.Path, "read_bytes", return_value=command_line
            ):
                self.assertTrue(toggle.is_voicetyping_process(123))

        with mock.patch.object(
            toggle.Path, "read_bytes", return_value=b"/usr/bin/python3\0/tmp/unrelated.py\0"
        ):
            self.assertFalse(toggle.is_voicetyping_process(123))
        with mock.patch.object(
            toggle.Path,
            "read_bytes",
            return_value=b"/usr/bin/python3\0/tmp/unrelated.py\0/home/user/voicetyping.py\0",
        ):
            self.assertFalse(toggle.is_voicetyping_process(123))

    def test_invalid_pid_file_is_not_signalled(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "voice.pid"
            pid_file.write_text("not-a-pid")
            self.assertIsNone(toggle.read_pid(pid_file))

    def test_valid_process_receives_usr1(self):
        with mock.patch.object(toggle, "read_pid", return_value=123), mock.patch.object(
            toggle, "is_voicetyping_process", return_value=True
        ), mock.patch.object(toggle.os, "kill") as kill, mock.patch.object(
            toggle.subprocess, "run"
        ) as run:
            self.assertEqual(toggle.main([]), 0)
        kill.assert_called_once_with(123, signal.SIGUSR1)
        run.assert_not_called()

    def test_stale_pid_recovers_and_receives_usr1(self):
        with mock.patch.object(toggle, "read_pid", side_effect=[321, 654]), mock.patch.object(
            toggle, "is_voicetyping_process", side_effect=[False, True]
        ), mock.patch.object(toggle.os, "kill") as kill, mock.patch.object(
            toggle.subprocess, "run"
        ) as run:
            self.assertEqual(toggle.main([]), 0)
        run.assert_called_once_with(
            ["systemctl", "--user", "start", toggle.AUTOSTART_UNIT],
            check=True,
            stdout=toggle.subprocess.DEVNULL,
            stderr=toggle.subprocess.DEVNULL,
            timeout=5,
        )
        kill.assert_called_once_with(654, signal.SIGUSR1)

    def test_failed_recovery_does_not_signal_stale_pid(self):
        with mock.patch.object(toggle, "read_pid", return_value=None), mock.patch.object(
            toggle, "is_voicetyping_process", return_value=False
        ), mock.patch.object(toggle, "START_WAIT_SECONDS", 0), mock.patch.object(
            toggle.os, "kill"
        ) as kill, mock.patch.object(toggle.subprocess, "run") as run:
            self.assertEqual(toggle.main([]), 1)
        run.assert_called_once()
        kill.assert_not_called()

    def test_help_and_invalid_arguments_never_signal_or_start(self):
        for arguments, expected in ((["--help"], 0), (["unexpected"], 2)):
            with self.subTest(arguments=arguments), mock.patch.object(
                toggle.os, "kill"
            ) as kill, mock.patch.object(toggle.subprocess, "run") as run:
                self.assertEqual(toggle.main(arguments), expected)
            kill.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
