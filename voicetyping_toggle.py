#!/usr/bin/env python3
"""Fast, standard-library-only signal sender for the global shortcut."""

import os
import signal
import tempfile
from pathlib import Path


RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
PID_FILE = RUNTIME_DIR / f"voicetyping-{os.getuid()}.pid"


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


def main():
    pid = read_pid()
    if not pid or not is_voicetyping_process(pid):
        return 1
    try:
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
