#!/usr/bin/env python3
"""Fast, standard-library-only signal sender for the global shortcut."""

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
PID_FILE = RUNTIME_DIR / f"voicetyping-{os.getuid()}.pid"
AUTOSTART_UNIT = "app-voicetyping@autostart.service"
START_WAIT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.1


def read_pid(path=PID_FILE):
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def is_voicetyping_process(pid):
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(Path(os.fsdecode(argument)).name == "voicetyping.py" for argument in arguments if argument)


def valid_pid():
    pid = read_pid()
    return pid if pid and is_voicetyping_process(pid) else None


def start_and_wait_for_pid():
    try:
        subprocess.run(
            ["systemctl", "--user", "start", AUTOSTART_UNIT],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    deadline = time.monotonic() + START_WAIT_SECONDS
    while True:
        pid = valid_pid()
        if pid:
            return pid
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    pid = valid_pid()
    if not pid:
        pid = start_and_wait_for_pid()
        if not pid:
            return 1
    try:
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
