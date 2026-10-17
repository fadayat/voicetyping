"""Opt-in real desktop paste smoke; never run by unittest discovery.

Run from the repository: GDK_BACKEND=wayland python3 tests/manual_autopaste_smoke.py
and optionally repeat with GDK_BACKEND=x11. Only the test's own focused entry is
targeted. No microphone/network is used. Existing clipboard text stays in memory
and is restored; non-text clipboard content causes the test to skip.
"""

from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk
import pyperclip

from auto_paste import AutomaticPaste


def main():
    types = subprocess.run(
        ["wl-paste", "--list-types"], capture_output=True, timeout=2, check=False,
    )
    if types.returncode == 0 and not any(
        mime.startswith(b"text/") or mime in (b"UTF8_STRING", b"STRING")
        for mime in types.stdout.splitlines()
    ):
        print("SKIP: non-text clipboard; preserving it unchanged")
        return 77
    original = pyperclip.paste()
    sample = "Salam, dünya! Azərbaycan: ə, ğ, ı, ö, ş, ü."
    outcome = {"started": False, "copied": False, "error": None, "ok": False}
    window = Gtk.Window(title="VoiceTyping — automatic paste test")
    window.set_default_size(520, 80)
    entry = Gtk.Entry()
    entry.set_placeholder_text("Isolated paste test — closes automatically")
    window.add(entry)
    key_events = []
    paste_requests = []
    entry.connect("key-press-event", lambda _entry, event: key_events.append(event.keyval) or False)
    entry.connect("paste-clipboard", lambda *_: paste_requests.append(True))

    def finish():
        outcome["ok"] = entry.get_text() == sample
        window.destroy()
        Gtk.main_quit()
        return False

    def paste_worker():
        try:
            paste = AutomaticPaste().prepare()
            pyperclip.copy(sample)
            outcome["copied"] = True
            paste()
        except Exception as error:
            outcome["error"] = type(error).__name__
        GLib.timeout_add(350, finish)

    def ready(*_):
        if outcome["started"] or not window.is_active():
            return False
        entry.grab_focus()
        outcome["started"] = True

        def start_after_focus_settles():
            # A newly mapped XWayland entry can receive focus-in before focus
            # assignment has finished. Production targets are already focused.
            if not window.is_active() or not entry.is_focus():
                outcome["error"] = "test field lost focus; no input sent"
                finish()
                return False
            threading.Thread(target=paste_worker, daemon=True).start()
            return False

        GLib.timeout_add(100, start_after_focus_settles)
        return False

    def watchdog():
        if not outcome["started"]:
            outcome["error"] = "test window did not receive focus; no input sent"
            finish()
        return False

    window.connect("focus-in-event", ready)
    window.show_all()
    window.present()
    GLib.timeout_add(250, ready)
    GLib.timeout_add(4000, watchdog)
    try:
        Gtk.main()
    finally:
        if outcome["copied"]:
            pyperclip.copy(original)
    backend = type(Gdk.Display.get_default()).__name__
    print("Backend:", backend)
    print("Unicode paste into isolated field:", "PASS" if outcome["ok"] else "FAIL")
    if outcome["error"]:
        print("Diagnostic:", outcome["error"])
    if not outcome["ok"]:
        print("Key events:", key_events, "paste requests:", len(paste_requests))
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
