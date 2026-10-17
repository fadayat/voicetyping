"""Opt-in real Ptyxis paste test, with a raw input reader instead of a shell.

Run from the repository with python3 tests/manual_terminal_paste_smoke.py. The
test opens its own standalone terminal, uses synthetic audio/final text, and
restores existing clipboard text. It never executes pasted text, records the
microphone, contacts Google, or inspects any existing terminal content.
"""

from pathlib import Path
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time

SAMPLE = "Azərbaycan dili: ə, ğ, ı, ö, ş, ü."


def reader(socket_path):
    import termios
    import tty

    channel = socket.socket(socket.AF_UNIX)
    channel.connect(socket_path)
    fd = sys.stdin.fileno()
    original_settings = termios.tcgetattr(fd)
    received = b""
    try:
        tty.setraw(fd)
        os.write(sys.stdout.fileno(), b"\x1b[?2004hVoiceTyping isolated paste test. No shell is running.\r\n")
        channel.sendall(b"ready\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([fd], [], [], .1)[0]:
                received += os.read(fd, 8192)
                if SAMPLE.encode() in received:
                    break
        payload = received.removeprefix(b"\x1b[200~").removesuffix(b"\x1b[201~")
        channel.sendall(json.dumps({
            "matched": payload == SAMPLE.encode(),
            "enter_received": b"\r" in payload or b"\n" in payload,
        }).encode() + b"\n")
    finally:
        os.write(sys.stdout.fileno(), b"\x1b[?2004l")
        termios.tcsetattr(fd, termios.TCSANOW, original_settings)
        channel.close()


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    import pyperclip
    import auto_paste as ap
    import voicetyping as vt

    class Client:
        def close(self): pass

    class Live:
        def start(self): pass
        def feed_audio(self, _pcm): return True
        def finish(self, timeout): return SAMPLE
        def cancel(self): pass

    class Stream:
        def __init__(self, callback, **kwargs): self.callback = callback
        def start(self):
            chunk = np.full((6400, 1), 1200, dtype=np.int16)
            self.callback(chunk, len(chunk), None, None)
        def stop(self): pass
        def close(self): pass

    types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, timeout=2)
    if types.returncode == 0 and not any(
        mime.startswith(b"text/") or mime in (b"UTF8_STRING", b"STRING")
        for mime in types.stdout.splitlines()
    ):
        print("SKIP: preserving non-text clipboard")
        return 77
    original = pyperclip.paste()
    process = None
    app = None
    with tempfile.TemporaryDirectory(prefix="voicetyping-terminal-test-") as directory:
        socket_path = str(Path(directory) / "reader.sock")
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(socket_path)
            server.listen(1)
            server.settimeout(6)
            try:
                process = subprocess.Popen([
                    "ptyxis", "--standalone", "--title=VoiceTyping isolated test",
                    "--", sys.executable, str(Path(__file__).resolve()), "--reader", socket_path,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                channel, _ = server.accept()
                with channel:
                    channel.settimeout(7)
                    stream = channel.makefile("rb")
                    if stream.readline() != b"ready\n":
                        raise RuntimeError("test reader did not start")
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if ap._active_application_pids() == frozenset({process.pid}):
                            break
                        time.sleep(.02)
                    else:
                        raise RuntimeError("isolated terminal not focused; no input sent")
                    panel = vt.NotificationPanel()
                    app = vt.VoiceTypingApp(
                        Client(), panel, stream_factory=Stream, live_factory=Live,
                        paste_action=ap.AutomaticPaste(),
                    )
                    app.toggle()
                    time.sleep(.35)  # Exercise F8 debounce, not clipboard waiting.
                    app.toggle()
                    app.worker.join(3)
                    result = json.loads(stream.readline())
                    ok = result["matched"] and not result["enter_received"]
                    print("Ptyxis full application paste:", "PASS" if ok else "FAIL")
                    print("Enter sent:", result["enter_received"])
                    print("Stop-to-shortcut ms:", app.last_timing.get("stop_to_paste_ms"))
                    return 0 if ok else 1
            finally:
                if app is not None:
                    app.shutdown()
                pyperclip.copy(original)
                if process is not None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.terminate()  # Only the standalone test instance.
                        process.wait(timeout=2)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--reader":
        reader(sys.argv[2])
    else:
        raise SystemExit(main())
