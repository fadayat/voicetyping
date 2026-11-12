import os
import subprocess
import sys
import tempfile
import time
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
        self.capture_args = self.root / "arguments.txt"
        self.environment = os.environ.copy()
        self.environment.update(
            APPDIR=str(self.appdir),
            APPIMAGE=str(self.root / "Voice Typing.AppImage"),
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
                "-m",
                "voicetyping.app",
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

    def test_toggle_accepts_same_image_running_from_a_different_mount(self):
        runtime_dir = self.root / "runtime"
        runtime_dir.mkdir()
        running_appdir = self.root / "different mount"
        running_python = running_appdir / "usr/bin/python3"
        running_python.parent.mkdir(parents=True)
        running_python.symlink_to(sys.executable)
        module_root = self.root / "module-root"
        package = module_root / "voicetyping"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "app.py").write_text(
            "import os\n"
            "import signal\n"
            "import time\n"
            "from pathlib import Path\n"
            "def stop(*_args):\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGUSR1, stop)\n"
            "Path(os.environ['READY_FILE']).write_text('ready', encoding='ascii')\n"
            "while True:\n"
            "    time.sleep(1)\n",
            encoding="utf-8",
        )
        ready_file = self.root / "ready"
        running_environment = os.environ.copy()
        running_environment.update(
            APPDIR=str(running_appdir),
            APPIMAGE=self.environment["APPIMAGE"],
            PYTHONPATH=str(module_root),
            READY_FILE=str(ready_file),
        )
        process = subprocess.Popen(
            [str(running_python), "-m", "voicetyping.app"],
            env=running_environment,
            cwd=self.root,
        )
        self.addCleanup(self.stop_process, process)
        for _attempt in range(100):
            if ready_file.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(ready_file.is_file(), "test process did not become ready")
        (runtime_dir / f"voicetyping-{os.getuid()}.pid").write_text(
            str(process.pid), encoding="ascii"
        )
        self.environment["XDG_RUNTIME_DIR"] = str(runtime_dir)

        result = self.run_launcher("--toggle")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(process.wait(timeout=2), 0)
        self.assertFalse(self.capture_args.exists())

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
