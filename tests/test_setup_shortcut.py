import os
import unittest
from unittest.mock import patch

from setup_shortcut import (
    SHORTCUT_NOT_GNOME,
    build_shortcut_command,
    find_binding_collision,
    find_owned_binding,
    setup_gnome_shortcut,
)


class ShortcutOwnershipTests(unittest.TestCase):
    @patch.dict(os.environ, {"APPIMAGE": "/home/user/Voice Typing.AppImage"}, clear=True)
    def test_appimage_shortcut_uses_persistent_image_path(self):
        self.assertEqual(
            build_shortcut_command(),
            "'/home/user/Voice Typing.AppImage' --toggle",
        )

    @patch.dict(
        os.environ,
        {"VOICETYPING_LAUNCHER": "/usr/bin/voicetyping"},
        clear=True,
    )
    def test_packaged_shortcut_uses_launcher(self):
        self.assertEqual(build_shortcut_command(), "/usr/bin/voicetyping --toggle")

    @patch.dict(os.environ, {}, clear=True)
    @patch("setup_shortcut.sys.executable", "/usr/bin/python3")
    @patch("setup_shortcut.os.path.abspath", return_value="/opt/voicetyping/setup_shortcut.py")
    def test_source_shortcut_uses_python_and_helper(self, _abspath):
        self.assertEqual(
            build_shortcut_command(),
            "/usr/bin/python3 /opt/voicetyping/voicetyping_toggle.py",
        )

    def test_name_alone_does_not_claim_an_unrelated_binding(self):
        path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom8/"
        entries = {path: ("VoiceTyping", "/opt/voice.py --toggle", "<Alt>F8")}

        self.assertIsNone(find_owned_binding([path], entries))

    def test_existing_voice_typing_binding_is_found_by_command(self):
        path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom8/"
        entries = {path: ("My speech shortcut", "/opt/voicetyping.py --toggle", "F8")}

        self.assertEqual(find_owned_binding([path], entries), path)

    def test_unrelated_f8_binding_is_a_collision_not_owned(self):
        path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
        entries = {path: ("Launch browser", "firefox", "F8")}

        self.assertIsNone(find_owned_binding([path], entries))
        self.assertEqual(find_binding_collision([path], entries, "F8"), path)

    def test_command_merely_mentioning_name_is_not_owned(self):
        path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom2/"
        entries = {path: ("Reminder", "notify-send voicetyping", "F7")}

        self.assertIsNone(find_owned_binding([path], entries))

    def test_owned_path_is_excluded_from_collision_check(self):
        path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voicetyping/"
        entries = {path: ("VoiceTyping", "python3 voicetyping.py --toggle", "F8")}

        self.assertIsNone(find_binding_collision([path], entries, "F8", owned_path=path))

    @patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"})
    @patch("setup_shortcut._gsettings")
    def test_non_gnome_desktop_skips_shortcut_setup(self, gsettings):
        self.assertEqual(setup_gnome_shortcut(), SHORTCUT_NOT_GNOME)
        gsettings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
