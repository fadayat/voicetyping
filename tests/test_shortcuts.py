import unittest
from types import SimpleNamespace

from voicetyping.shortcuts import (
    PynputShortcutState,
    parse_shortcut,
    pynput_key_name,
    pynput_modifier,
)


class ShortcutParsingTests(unittest.TestCase):
    def test_gnome_markup_converts_to_xdg_and_display_forms(self):
        shortcut = parse_shortcut("<Ctrl><Alt>F8")

        self.assertEqual(shortcut.to_xdg_trigger(), "CTRL+ALT+F8")
        self.assertEqual(shortcut.to_gnome_binding(), "<Ctrl><Alt>F8")
        self.assertEqual(shortcut.to_label(), "Ctrl+Alt+F8")

    def test_plus_separated_input_normalizes_modifier_aliases(self):
        shortcut = parse_shortcut("Control+Shift+F9")

        self.assertEqual(shortcut.to_xdg_trigger(), "CTRL+SHIFT+F9")
        self.assertEqual(shortcut.to_gnome_binding(), "<Ctrl><Shift>F9")

    def test_key_matches_only_with_exact_modifier_set(self):
        shortcut = parse_shortcut("<Ctrl>F8")

        self.assertTrue(shortcut.matches("f8", {"CTRL"}))
        self.assertFalse(shortcut.matches("f8", set()))
        self.assertFalse(shortcut.matches("f8", {"CTRL", "ALT"}))
        self.assertFalse(shortcut.matches("f9", {"CTRL"}))

    def test_pynput_key_names_resolve_modifiers_and_space(self):
        self.assertEqual(pynput_modifier(SimpleNamespace(name="ctrl_l")), "CTRL")
        self.assertEqual(pynput_modifier(SimpleNamespace(name="alt_r")), "ALT")
        self.assertEqual(pynput_key_name(SimpleNamespace(name="f8")), "f8")
        self.assertEqual(pynput_key_name(SimpleNamespace(char=" ")), "space")

    def test_pynput_state_emits_one_edge_until_key_release(self):
        now = [1.0]
        state = PynputShortcutState(monotonic=lambda: now[0])
        f8 = SimpleNamespace(name="f8")

        self.assertEqual(state.press(f8), ("f8", set()))
        self.assertIsNone(state.press(f8))
        state.release(f8)
        now[0] += 0.1
        self.assertEqual(state.press(f8), ("f8", set()))

    def test_pynput_state_ignores_x11_repeat_release_press_pairs(self):
        now = [1.0]
        state = PynputShortcutState(monotonic=lambda: now[0])
        f8 = SimpleNamespace(name="f8")

        self.assertEqual(state.press(f8), ("f8", set()))
        for _ in range(10):
            now[0] += 0.04
            state.release(f8)
            now[0] += 0.001
            self.assertIsNone(state.press(f8))

        state.release(f8)
        now[0] += 0.1
        self.assertEqual(state.press(f8), ("f8", set()))

    def test_pynput_state_tracks_modifier_sides_without_repeat(self):
        now = [1.0]
        state = PynputShortcutState(monotonic=lambda: now[0])
        control = SimpleNamespace(name="ctrl_l")
        f8 = SimpleNamespace(name="f8")

        self.assertIsNone(state.press(control))
        self.assertEqual(state.press(f8), ("f8", {"CTRL"}))
        self.assertIsNone(state.press(f8))
        state.release(f8)
        state.release(control)
        now[0] += 0.1
        self.assertEqual(state.press(f8), ("f8", set()))

    def test_rejects_incomplete_or_unknown_shortcuts(self):
        for value in ("", "Ctrl+", "<Ctrl>", "Ctrl+Bogus+F8", "Ctrl+Ctrl+F8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_shortcut(value)


if __name__ == "__main__":
    unittest.main()
