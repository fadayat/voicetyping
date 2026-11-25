"""Run a non-destructive Windows desktop readiness check."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk

from .platform import WindowsDesktop


def _write_report(path: Path, checks: list[dict[str, str]], success: bool) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                {"success": success, "checks": checks},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def run_self_test(
    report_path: Path,
    *,
    platform_name=sys.platform,
    desktop_factory=WindowsDesktop,
    tk_factory=tk.Tk,
) -> int:
    """Check the native window, process mutex and global F8 registration.

    The check does not start recording, contact Gemini, touch credentials or
    clipboard, modify startup settings, or inject keyboard input.
    """
    checks: list[dict[str, str]] = []
    root = None
    desktop = None
    success = True

    def result(name: str, status: str) -> None:
        nonlocal success
        checks.append({"name": name, "status": status})
        if status != "passed":
            success = False

    if platform_name != "win32":
        result("windows_platform", "failed")
        try:
            _write_report(Path(report_path), checks, success)
        except OSError:
            return 1
        return 1
    result("windows_platform", "passed")

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        result("pointer_width_64", "passed")
    else:
        result("pointer_width_64", "failed")

    try:
        root = tk_factory()
        root.withdraw()
        result("tk_root_create", "passed")
    except Exception:
        result("tk_root_create", "failed")

    try:
        desktop = desktop_factory()
        acquired = desktop.acquire()
        if acquired:
            result("single_instance_mutex", "passed")
        else:
            result("single_instance_mutex", "already_running")
            success = False
        if acquired:
            try:
                desktop.register_hotkey()
            except OSError:
                result("global_hotkey_f8", "conflict")
            except Exception:
                result("global_hotkey_f8", "failed")
            else:
                result("global_hotkey_f8", "passed")
    except Exception:
        result("windows_desktop", "failed")
    finally:
        if desktop is not None:
            try:
                desktop.close()
            except Exception:
                result("desktop_cleanup", "failed")
            else:
                result("desktop_cleanup", "passed")
        if root is not None:
            try:
                root.destroy()
            except Exception:
                result("tk_root_destroy", "failed")
            else:
                result("tk_root_destroy", "passed")

    try:
        _write_report(Path(report_path), checks, success)
    except OSError:
        return 1
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("windows-selftest-report.json"),
        help="path for the non-sensitive JSON report",
    )
    arguments = parser.parse_args(argv)
    return run_self_test(arguments.report)


if __name__ == "__main__":
    raise SystemExit(main())
