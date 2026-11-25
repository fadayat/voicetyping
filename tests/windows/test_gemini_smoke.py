import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from voicetyping.windows import app, gemini_smoke as windows_gemini_smoke


class GeminiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.report = Path(self.temporary.name) / "GEMINI_SMOKE.json"
        self.audio = Path(self.temporary.name) / "fixture.wav"
        self.samples = np.full((8_000, 1), 500, dtype="<i2")

    def load_report(self):
        return json.loads(self.report.read_text(encoding="utf-8"))

    def reader(self, *_args, **_kwargs):
        return self.samples, 16_000

    def test_success_scrubs_key_and_never_reports_key_or_transcript(self):
        environment = {"GEMINI_API_KEY": "test-secret-never-report"}
        status = windows_gemini_smoke.run_gemini_smoke(
            self.audio,
            self.report,
            platform_name="win32",
            environ=environment,
            audio_reader=self.reader,
            transcriber=lambda key, _audio, _rate: (
                "Voice typing integration test"
                if key == "test-secret-never-report"
                else ""
            ),
        )

        report_text = self.report.read_text(encoding="utf-8")
        self.assertEqual(status, 0)
        self.assertTrue(self.load_report()["success"])
        self.assertNotIn("GEMINI_API_KEY", environment)
        self.assertNotIn("test-secret-never-report", report_text)
        self.assertNotIn("Voice typing integration test", report_text)

    def test_transcription_exception_reports_no_exception_text(self):
        environment = {"GEMINI_API_KEY": "test-secret-never-report"}

        def fail(*_args):
            raise RuntimeError("private service response")

        status = windows_gemini_smoke.run_gemini_smoke(
            self.audio,
            self.report,
            platform_name="win32",
            environ=environment,
            audio_reader=self.reader,
            transcriber=fail,
        )
        report_text = self.report.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertFalse(self.load_report()["success"])
        self.assertNotIn("test-secret-never-report", report_text)
        self.assertNotIn("private service response", report_text)
        self.assertNotIn("GEMINI_API_KEY", environment)

    def test_missing_key_stops_before_audio_or_network(self):
        reader = mock.Mock()
        transcriber = mock.Mock()
        status = windows_gemini_smoke.run_gemini_smoke(
            self.audio,
            self.report,
            platform_name="win32",
            environ={},
            audio_reader=reader,
            transcriber=transcriber,
        )

        self.assertEqual(status, 1)
        self.assertFalse(self.load_report()["success"])
        reader.assert_not_called()
        transcriber.assert_not_called()

    def test_non_windows_scrubs_key_without_reading_audio(self):
        environment = {"GEMINI_API_KEY": "test-secret-never-report"}
        reader = mock.Mock()
        status = windows_gemini_smoke.run_gemini_smoke(
            self.audio,
            self.report,
            platform_name="linux",
            environ=environment,
            audio_reader=reader,
        )

        self.assertEqual(status, 1)
        self.assertNotIn("GEMINI_API_KEY", environment)
        reader.assert_not_called()

    def test_app_entrypoint_dispatches_gemini_smoke_without_ui(self):
        arguments = [
            "VoiceTyping.exe",
            "--gemini-smoke",
            "--audio",
            "fixture.wav",
            "--report",
            "report.json",
        ]
        with mock.patch.object(app.sys, "argv", arguments), mock.patch.object(
            app,
            "gemini_smoke_main",
            return_value=9,
        ) as gemini_smoke_main:
            self.assertEqual(app.main(), 9)

        gemini_smoke_main.assert_called_once_with(
            ["--audio", "fixture.wav", "--report", "report.json"]
        )


if __name__ == "__main__":
    unittest.main()
