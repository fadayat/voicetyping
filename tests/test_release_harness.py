import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import release_harness


ROOT = Path(__file__).resolve().parents[1]


class ReleaseHarnessTests(unittest.TestCase):
    def test_package_version_comes_from_canonical_module(self):
        self.assertEqual(release_harness.read_package_version(), "2.3.0")

    def test_module_manifest_covers_every_package_module(self):
        expected = {
            path.stem
            for path in (ROOT / "src/voicetyping").glob("*.py")
            if path.name != "__init__.py"
        }

        self.assertEqual(set(release_harness.PACKAGE_MODULES), expected)

    def test_checked_in_requirements_header_is_canonical(self):
        header = tuple(
            (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines(keepends=True)[:2]
        )

        self.assertEqual(header, release_harness.REQUIREMENTS_HEADER)

    @mock.patch.dict(
        os.environ,
        {"GEMINI_API_KEY": "not-for-tests", "PYTHONPATH": "/private/source"},
    )
    def test_isolated_environment_removes_user_key_and_python_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = release_harness.isolated_environment(Path(temporary))

        self.assertNotIn("GEMINI_API_KEY", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["XDG_CURRENT_DESKTOP"], "VoiceTyping-Test")

    def test_missing_artifact_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(
            io.StringIO()
        ):
            missing = Path(temporary) / "missing.whl"

            result = release_harness.main(["wheel", "--wheel", str(missing)])

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
