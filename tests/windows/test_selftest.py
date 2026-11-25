import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from voicetyping.windows import app, selftest as windows_selftest


class FakeRoot:
    def __init__(self):
        self.destroyed = False
        self.withdrawn = False

    def withdraw(self):
        self.withdrawn = True

    def destroy(self):
        self.destroyed = True


class FakeDesktop:
    def __init__(self, *, acquired=True, register_error=None):
        self.acquired = acquired
        self.register_error = register_error
        self.acquire_calls = 0
        self.register_calls = 0
        self.close_calls = 0

    def acquire(self):
        self.acquire_calls += 1
        return self.acquired

    def register_hotkey(self):
        self.register_calls += 1
        if self.register_error:
            raise self.register_error

    def close(self):
        self.close_calls += 1


class SelfTestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.report = Path(self.temporary.name) / "nested" / "report.json"
        self.root = FakeRoot()
        self.desktop = FakeDesktop()

    def load_report(self):
        return json.loads(self.report.read_text(encoding="utf-8"))

    def run_with_fakes(self, desktop=None, *, platform_name="win32"):
        selected_desktop = desktop or self.desktop
        status = windows_selftest.run_self_test(
            self.report,
            platform_name=platform_name,
            desktop_factory=lambda: selected_desktop,
            tk_factory=lambda: self.root,
        )
        return status, selected_desktop

    def test_success_creates_and_destroys_native_resources(self):
        status, desktop = self.run_with_fakes()
        report = self.load_report()
        statuses = {check["name"]: check["status"] for check in report["checks"]}

        self.assertEqual(status, 0)
        self.assertTrue(report["success"])
        self.assertEqual(statuses["windows_platform"], "passed")
        self.assertEqual(statuses["single_instance_mutex"], "passed")
        self.assertEqual(statuses["global_hotkey_f8"], "passed")
        self.assertEqual(statuses["tk_root_destroy"], "passed")
        self.assertTrue(self.root.withdrawn)
        self.assertTrue(self.root.destroyed)
        self.assertEqual(desktop.acquire_calls, 1)
        self.assertEqual(desktop.register_calls, 1)
        self.assertEqual(desktop.close_calls, 1)
        self.assertEqual(list(self.report.parent.glob("*.tmp")), [])
        self.assertTrue(all(set(check) == {"name", "status"} for check in report["checks"]))

    def test_non_windows_reports_failure_without_creating_native_resources(self):
        status, desktop = self.run_with_fakes(platform_name="linux")
        report = self.load_report()

        self.assertEqual(status, 1)
        self.assertFalse(report["success"])
        self.assertEqual(report["checks"], [{"name": "windows_platform", "status": "failed"}])
        self.assertEqual(desktop.acquire_calls, 0)
        self.assertFalse(self.root.destroyed)

    def test_hotkey_conflict_is_reported_and_cleanup_still_runs(self):
        desktop = FakeDesktop(register_error=OSError("F8 is occupied"))
        status, desktop = self.run_with_fakes(desktop)
        statuses = {
            check["name"]: check["status"]
            for check in self.load_report()["checks"]
        }

        self.assertEqual(status, 1)
        self.assertEqual(statuses["global_hotkey_f8"], "conflict")
        self.assertEqual(desktop.close_calls, 1)
        self.assertTrue(self.root.destroyed)

    def test_registration_exception_still_closes_desktop_and_root(self):
        desktop = FakeDesktop(register_error=RuntimeError("secret text must not be reported"))
        status, desktop = self.run_with_fakes(desktop)
        report_text = self.report.read_text(encoding="utf-8")
        statuses = {
            check["name"]: check["status"]
            for check in self.load_report()["checks"]
        }

        self.assertEqual(status, 1)
        self.assertEqual(statuses["global_hotkey_f8"], "failed")
        self.assertNotIn("secret text", report_text)
        self.assertEqual(desktop.close_calls, 1)
        self.assertTrue(self.root.destroyed)

    def test_mutex_already_owned_skips_hotkey_and_cleans_up(self):
        desktop = FakeDesktop(acquired=False)
        status, desktop = self.run_with_fakes(desktop)
        statuses = {
            check["name"]: check["status"]
            for check in self.load_report()["checks"]
        }

        self.assertEqual(status, 1)
        self.assertEqual(statuses["single_instance_mutex"], "already_running")
        self.assertNotIn("global_hotkey_f8", statuses)
        self.assertEqual(desktop.register_calls, 0)
        self.assertEqual(desktop.close_calls, 1)
        self.assertTrue(self.root.destroyed)

    def test_atomic_report_preserves_previous_file_if_replace_fails(self):
        self.report.parent.mkdir(parents=True)
        self.report.write_text("previous report\n", encoding="utf-8")

        with mock.patch.object(
            windows_selftest.os,
            "replace",
            side_effect=OSError("simulated replace failure"),
        ), self.assertRaises(OSError):
            windows_selftest._write_report(
                self.report,
                [{"name": "windows_platform", "status": "passed"}],
                True,
            )

        self.assertEqual(
            self.report.read_text(encoding="utf-8"),
            "previous report\n",
        )
        self.assertEqual(
            list(self.report.parent.glob(f".{self.report.name}.*.tmp")),
            [],
        )

    def test_app_entrypoint_dispatches_self_test_without_starting_ui(self):
        arguments = [
            "VoiceTyping.exe",
            "--self-test",
            "--report",
            "SELFTEST.json",
        ]
        with mock.patch.object(app.sys, "argv", arguments), mock.patch.object(
            app,
            "self_test_main",
            return_value=7,
        ) as self_test_main:
            self.assertEqual(app.main(), 7)

        self_test_main.assert_called_once_with(["--report", "SELFTEST.json"])


if __name__ == "__main__":
    unittest.main()
