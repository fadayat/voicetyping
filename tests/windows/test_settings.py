import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from voicetyping.windows import settings


class SettingsTests(unittest.TestCase):
    def test_path_uses_local_app_data_or_windows_fallback(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Ada\AppData\Local"}):
            self.assertEqual(
                settings.settings_path(),
                Path(r"C:\Users\Ada\AppData\Local") / "VoiceTyping" / "settings.json",
            )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            Path, "home", return_value=Path("/user")
        ):
            self.assertEqual(
                settings.settings_path(),
                Path("/user/AppData/Local/VoiceTyping/settings.json"),
            )

    def test_missing_and_corrupt_files_return_independent_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            first = settings.load_settings(path)
            self.assertIsNot(first["language_codes"], settings.DEFAULT_SETTINGS["language_codes"])
            first["language_codes"].append("fr-FR")
            self.assertEqual(settings.load_settings(path), settings.DEFAULT_SETTINGS)
            self.assertEqual(settings.DEFAULT_SETTINGS["language_codes"], ["az-AZ", "en-US"])
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(settings.load_settings(path), settings.DEFAULT_SETTINGS)

    def test_sanitizes_types_lists_lengths_and_unknown_fields(self):
        supplied = {
            "auto_paste": 1,
            "live_transcription": False,
            "shortcut": "  Ctrl+F8  ",
            "language_codes": [" az-AZ ", "en-US", "az-AZ"],
            "custom_vocabulary": [f"term-{index}" for index in range(1002)],
            "extra": "ignored",
            "api_key": "never-persist-this",
        }
        clean = settings._sanitize_settings(supplied)
        self.assertIs(clean["auto_paste"], True)
        self.assertIs(clean["live_transcription"], False)
        self.assertEqual(clean["shortcut"], "Ctrl+F8")
        self.assertEqual(clean["language_codes"], ["az-AZ", "en-US"])
        self.assertEqual(len(clean["custom_vocabulary"]), 1000)
        self.assertNotIn("api_key", clean)
        self.assertNotIn("extra", clean)

    def test_invalid_setting_values_use_defaults(self):
        clean = settings._sanitize_settings(
            {
                "shortcut": " " * 2,
                "language_codes": ["az-AZ", ""],
                "custom_vocabulary": "not-a-list",
            }
        )
        self.assertEqual(clean["shortcut"], settings.DEFAULT_SETTINGS["shortcut"])
        self.assertEqual(clean["language_codes"], settings.DEFAULT_SETTINGS["language_codes"])
        self.assertEqual(clean["custom_vocabulary"], settings.DEFAULT_SETTINGS["custom_vocabulary"])

    def test_atomic_round_trip_is_utf8_and_excludes_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "settings.json"
            saved = settings.save_settings(
                {
                    "auto_paste": False,
                    "shortcut": "F9",
                    "language_codes": ["az-AZ"],
                    "custom_vocabulary": ["Azərbaycan dili"],
                    "api_key": "never-persist-this",
                    "ignored": "also ignored",
                },
                path,
            )
            self.assertEqual(saved, path)
            self.assertFalse(list(path.parent.glob("*.tmp")))
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("never-persist-this", contents)
            self.assertNotIn("api_key", contents)
            self.assertEqual(settings.load_settings(path)["auto_paste"], False)
            self.assertEqual(settings.load_settings(path)["shortcut"], "F9")
            self.assertEqual(settings.load_settings(path)["language_codes"], ["az-AZ"])
            self.assertEqual(
                settings.load_settings(path)["custom_vocabulary"], ["Azərbaycan dili"]
            )
            self.assertIn("Azərbaycan dili", contents)
            self.assertEqual(json.loads(contents)["language_codes"], ["az-AZ"])


if __name__ == "__main__":
    unittest.main()
