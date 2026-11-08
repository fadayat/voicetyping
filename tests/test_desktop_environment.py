"""Desktop session detection must be deterministic and side-effect free."""

import unittest

from desktop_environment import detect_desktop_environment


class DesktopEnvironmentTests(unittest.TestCase):
    def test_gnome_wayland_stays_wayland_when_xwayland_is_available(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            "XDG_SESSION_TYPE": "Wayland",
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
        })
        self.assertTrue(environment.is_gnome)
        self.assertTrue(environment.is_wayland)
        self.assertFalse(environment.is_x11)

    def test_x11_is_detected_from_session_type(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "x11",
            "DISPLAY": ":0",
        })
        self.assertFalse(environment.is_gnome)
        self.assertTrue(environment.is_x11)
        self.assertFalse(environment.is_wayland)

    def test_display_fallback_requires_unambiguous_session(self):
        x11 = detect_desktop_environment({"DISPLAY": ":0"})
        wayland = detect_desktop_environment({"WAYLAND_DISPLAY": "wayland-0"})
        ambiguous = detect_desktop_environment({
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
        })
        self.assertTrue(x11.is_x11)
        self.assertTrue(wayland.is_wayland)
        self.assertFalse(ambiguous.is_x11)
        self.assertFalse(ambiguous.is_wayland)

    def test_desktop_names_are_normalized(self):
        environment = detect_desktop_environment({
            "XDG_CURRENT_DESKTOP": "KDE:Plasma",
        })
        self.assertEqual(environment.desktops, ("kde", "plasma"))


if __name__ == "__main__":
    unittest.main()
