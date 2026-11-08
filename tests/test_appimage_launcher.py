import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_DIR / "packaging/appimage/voicetyping-launcher"


class AppImageLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.appdir = self.root / "App Dir"
        python = self.appdir / "usr/bin/python3"
        python.parent.mkdir(parents=True)
        python.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
        (self.appdir / "usr/src/voicetyping").mkdir(parents=True)
        self.capture_args = self.root / "arguments.txt"
        self.environment = os.environ.copy()
        self.environment.update(
            APPDIR=str(self.appdir),
            CAPTURE_ARGS=str(self.capture_args),
        )

    def run_launcher(self, *arguments):
        return subprocess.run(
            ["/bin/sh", str(LAUNCHER), *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_non_toggle_arguments_reach_bundled_python_unchanged(self):
        result = self.run_launcher("--version", "value with spaces")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.capture_args.read_text(encoding="utf-8").splitlines(),
            [
                str(self.appdir / "usr/src/voicetyping/voicetyping.py"),
                "--version",
                "value with spaces",
            ],
        )

    def test_toggle_rejects_unrelated_process_without_starting_python(self):
        runtime_dir = self.root / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / f"voicetyping-{os.getuid()}.pid").write_text(
            str(os.getpid()), encoding="ascii"
        )
        self.environment["XDG_RUNTIME_DIR"] = str(runtime_dir)

        result = self.run_launcher("--toggle")

        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.capture_args.exists())

    def test_toggle_without_pid_file_does_not_start_python(self):
        runtime_dir = self.root / "runtime"
        runtime_dir.mkdir()
        self.environment["XDG_RUNTIME_DIR"] = str(runtime_dir)

        result = self.run_launcher("--toggle")

        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.capture_args.exists())

    def test_toggle_rejects_stale_pid_without_starting_python(self):
        runtime_dir = self.root / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / f"voicetyping-{os.getuid()}.pid").write_text(
            "2147483647\n", encoding="ascii"
        )
        self.environment["XDG_RUNTIME_DIR"] = str(runtime_dir)

        result = self.run_launcher("--toggle")

        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.capture_args.exists())


if __name__ == "__main__":
    unittest.main()
