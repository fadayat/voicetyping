"""Desktop integration selection keeps unsupported sessions clipboard-safe."""

import unittest
from unittest import mock

from desktop_environment import detect_desktop_environment
from desktop_integration import (
    build_paste_action,
    build_wayland_shortcut_listener,
    setup_global_shortcut,
    should_start_x11_listener,
)
from setup_shortcut import SHORTCUT_ACTIVE, SHORTCUT_NOT_GNOME


class DesktopIntegrationTests(unittest.TestCase):
    def test_gnome_wayland_keeps_existing_mutter_paste_backend(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "GNOME",
            "XDG_SESSION_TYPE": "wayland",
        })
        with mock.patch("auto_paste.AutomaticPaste") as backend:
            self.assertIs(build_paste_action(environment=environment), backend.return_value)

    def test_x11_uses_native_keyboard_paste_backend(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "x11",
            "DISPLAY": ":0",
        })
        with mock.patch("auto_paste.X11AutomaticPaste") as backend:
            self.assertIs(build_paste_action(environment=environment), backend.return_value)

    def test_unknown_wayland_desktop_keeps_transcript_on_clipboard(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
        })
        self.assertIsNone(build_paste_action(environment=environment))

    def test_wayland_keyboard_permission_selects_portal_paste_backend(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
        })
        keyboard = mock.Mock()
        with mock.patch("auto_paste.PortalAutomaticPaste") as backend:
            self.assertIs(
                build_paste_action(environment=environment, portal_keyboard=keyboard),
                backend.return_value,
            )
        backend.assert_called_once_with(keyboard)

    def test_disabled_auto_paste_does_not_select_backend(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "GNOME",
            "XDG_SESSION_TYPE": "wayland",
        })
        self.assertIsNone(build_paste_action(False, environment))

    def test_shortcut_setup_only_calls_gnome_configuration(self):
        gnome = detect_desktop_environment({"XDG_CURRENT_DESKTOP": "GNOME"})
        kde = detect_desktop_environment({"XDG_CURRENT_DESKTOP": "KDE"})
        with mock.patch("setup_shortcut.setup_gnome_shortcut", return_value=SHORTCUT_ACTIVE) as setup:
            self.assertEqual(setup_global_shortcut(gnome), SHORTCUT_ACTIVE)
            self.assertEqual(setup_global_shortcut(kde), SHORTCUT_NOT_GNOME)
        setup.assert_called_once_with()

    def test_keyboard_listener_is_restricted_to_x11_fallback(self):
        x11 = detect_desktop_environment({
            "XDG_SESSION_TYPE": "x11",
            "DISPLAY": ":0",
        })
        wayland = detect_desktop_environment({
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
        })
        self.assertTrue(should_start_x11_listener(SHORTCUT_NOT_GNOME, x11))
        self.assertFalse(should_start_x11_listener(SHORTCUT_NOT_GNOME, wayland))
        self.assertFalse(should_start_x11_listener(SHORTCUT_ACTIVE, x11))

    def test_global_shortcut_portal_is_only_selected_for_non_gnome_wayland(self):
        wayland = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "wayland",
        })
        gnome = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "GNOME",
            "XDG_SESSION_TYPE": "wayland",
        })
        callback = mock.Mock()
        with mock.patch("xdg_portal.GlobalShortcutPortal") as portal:
            self.assertIs(
                build_wayland_shortcut_listener("F8", callback, wayland),
                portal.return_value,
            )
            self.assertIsNone(build_wayland_shortcut_listener("F8", callback, gnome))
        portal.assert_called_once_with("F8", callback)


if __name__ == "__main__":
    unittest.main()
