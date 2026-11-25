import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import release_harness


ROOT = Path(__file__).resolve().parents[1]


class ReleaseHarnessTests(unittest.TestCase):
    def test_package_version_comes_from_canonical_module(self):
        self.assertEqual(release_harness.read_package_version(), "2.3.1")

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

    def test_appimage_runtime_rejects_readelf_parse_failure(self):
        failed = subprocess.CompletedProcess(
            ["readelf"], returncode=1, stdout="", stderr="not an ELF file"
        )
        with mock.patch.object(
            release_harness.shutil, "which", return_value="/usr/bin/readelf"
        ), mock.patch.object(release_harness.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "not an ELF file"):
                release_harness.check_static_appimage_runtime(Path("broken.AppImage"))

    def test_appimage_runtime_accepts_static_elf(self):
        results = [
            subprocess.CompletedProcess(
                ["readelf", "-d"], returncode=0, stdout="No dynamic section", stderr=""
            ),
            subprocess.CompletedProcess(
                ["readelf", "-l"], returncode=0, stdout="Program Headers", stderr=""
            ),
        ]
        with mock.patch.object(
            release_harness.shutil, "which", return_value="/usr/bin/readelf"
        ), mock.patch.object(release_harness.subprocess, "run", side_effect=results):
            release_harness.check_static_appimage_runtime(Path("static.AppImage"))

    def test_deb_version_comparison_is_exact(self):
        with tempfile.NamedTemporaryFile(suffix=".deb") as package, mock.patch.object(
            release_harness, "run_capture", return_value="2.3.0-10\n"
        ):
            arguments = SimpleNamespace(
                package=package.name,
                version="2.3.0",
                install=False,
            )

            with self.assertRaisesRegex(RuntimeError, "expected 2.3.0-1"):
                release_harness.deb_checks(arguments)


if __name__ == "__main__":
    unittest.main()
