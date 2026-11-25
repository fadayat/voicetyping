import ctypes
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from voicetyping.windows import platform as windows_platform


class PlatformTests(unittest.TestCase):
    def test_hotkey_parser_supports_safe_keys_and_canonical_modifiers(self):
        modifiers, virtual_key, label = windows_platform.parse_hotkey('shift+ctrl+f9')
        self.assertEqual(
            modifiers,
            windows_platform.MOD_NOREPEAT
            | windows_platform.MOD_CONTROL
            | windows_platform.MOD_SHIFT,
        )
        self.assertEqual(virtual_key, 0x78)
        self.assertEqual(label, 'Ctrl+Shift+F9')
        self.assertEqual(
            windows_platform.parse_hotkey('F8'),
            (windows_platform.MOD_NOREPEAT, 0x77, 'F8'),
        )
        for invalid in (
            '', 'Ctrl', 'F12', 'Ctrl+F8+F9', 'Ctrl+Ctrl+F8', 'Win+F8', 'Tab'
        ):
            with self.subTest(invalid=invalid), self.assertRaises((TypeError, ValueError)):
                windows_platform.parse_hotkey(invalid)

    def test_paste_guard_requires_original_foreground_and_no_modifiers(self):
        self.assertTrue(windows_platform.paste_allowed(123, 123, False))
        self.assertFalse(windows_platform.paste_allowed(0, 0, False))
        self.assertFalse(windows_platform.paste_allowed(123, 456, False))
        self.assertFalse(windows_platform.paste_allowed(123, 123, True))

    def test_startup_command_quotes_paths_with_spaces(self):
        executable = r"C:\Program Files\Voice Typing\VoiceTyping.exe"
        with mock.patch.object(windows_platform.sys, "frozen", True, create=True), mock.patch.object(
            windows_platform.sys, "executable", executable
        ):
            command = windows_platform.startup_command()
        self.assertEqual(command, subprocess.list2cmdline([executable, "--hidden"]))
        self.assertIn('"', command)

    def test_partial_send_input_releases_only_keys_pressed_by_this_call(self):
        expected_releases = {
            0: [],
            1: [(0x11, 2)],
            2: [(0x56, 2), (0x11, 2)],
            3: [(0x11, 2)],
            4: [],
        }

        class Recorder:
            def __init__(self, first_result):
                self.first_result = first_result
                self.calls = []
                self.argtypes = None
                self.restype = None

            def __call__(self, count, events, _size):
                self.calls.append(
                    [
                        (int(events[index].ki.wVk), int(events[index].ki.dwFlags))
                        for index in range(count)
                    ]
                )
                return self.first_result if len(self.calls) == 1 else count

        class FakeUser:
            @staticmethod
            def GetAsyncKeyState(_key):
                return 0

        for sent, expected in expected_releases.items():
            with self.subTest(sent=sent):
                desktop = windows_platform.WindowsDesktop.__new__(
                    windows_platform.WindowsDesktop
                )
                user = FakeUser()
                user.SendInput = Recorder(sent)
                desktop.user = user
                desktop.external_foreground = lambda: 91
                self.assertEqual(desktop.paste(91), sent == 4)
                self.assertEqual(
                    user.SendInput.calls[0],
                    [(0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)],
                )
                actual_release = (
                    user.SendInput.calls[1] if len(user.SendInput.calls) > 1 else []
                )
                self.assertEqual(actual_release, expected)


@unittest.skipUnless(sys.platform == "win32", "Windows ABI smoke test")
class WindowsOnlySmokeTests(unittest.TestCase):
    def test_dedicated_hotkey_queue_receives_native_thread_message(self):
        desktop = windows_platform.WindowsDesktop()
        try:
            try:
                desktop.register_hotkey()
            except OSError:
                self.skipTest('F8 is occupied in this Windows session')
            post = desktop.user.PostThreadMessageW
            post.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.UINT,
                             ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
            post.restype = ctypes.wintypes.BOOL
            self.assertTrue(post(desktop._hotkey_thread.native_id, 0x312, desktop.HOTKEY_ID, 0))
            deadline = time.monotonic() + 1
            received = False
            while time.monotonic() < deadline and not received:
                received = desktop.poll_hotkey()
                if not received:
                    threading.Event().wait(0.01)
            self.assertTrue(received)
        finally:
            desktop.close()

    def test_native_single_instance_mutex_can_be_acquired_and_closed(self):
        desktop = windows_platform.WindowsDesktop()
        try:
            acquired = desktop.acquire()
            self.assertIsInstance(acquired, bool)
            if acquired:
                self.assertTrue(desktop.mutex)
        finally:
            desktop.close()
        self.assertIsNone(desktop.mutex)

    def test_input_structure_size_without_sending_real_input(self):
        desktop = windows_platform.WindowsDesktop.__new__(windows_platform.WindowsDesktop)

        class FakeUser:
            def __init__(self):
                self.sent_size = None
                self.SendInput = mock.Mock(side_effect=self.send_input)

            @staticmethod
            def GetAsyncKeyState(_key):
                return 0

            def send_input(self, count, _events, size):
                self.sent_size = size
                return count

        desktop.user = FakeUser()
        desktop.external_foreground = lambda: 91
        self.assertTrue(desktop.paste(91))
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(desktop.user.sent_size, expected_size)


if __name__ == "__main__":
    unittest.main()
