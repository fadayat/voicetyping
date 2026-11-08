"""Interactive XDG portal smoke test for a real Wayland desktop session.

Run this only inside the target desktop session. It asks for Global Shortcuts
and Remote Desktop permission, then uses F8 to paste a fixed test marker into
the focused entry. It does not use a microphone, network service, or API key.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
import traceback
import os
from pathlib import Path
import sys

import pyperclip

app_directory = os.environ.get("VOICETYPING_APP_DIR")
sys.path.insert(
    0,
    app_directory or str(Path(__file__).resolve().parents[1]),
)

from xdg_portal import GlobalShortcutPortal, RemoteDesktopKeyboard


TEST_TEXT = "VoiceTyping Wayland portal smoke test"
TEST_TIMEOUT_SECONDS = 90


def main() -> int:
    keyboard = RemoteDesktopKeyboard()
    shortcut = None
    root = None
    activated = threading.Event()

    try:
        if not keyboard.start():
            print("PORTAL_SMOKE_FAIL: Remote Desktop keyboard permission was not granted")
            return 1
        shortcut = GlobalShortcutPortal("F8", activated.set)
        if not shortcut.start():
            print("PORTAL_SMOKE_FAIL: F8 was not bound by Global Shortcuts")
            return 1

        pyperclip.copy(TEST_TEXT)
        root = tk.Tk()
        root.title("VoiceTyping Wayland portal smoke test")
        root.geometry("560x150")
        tk.Label(root, text="Focus the field, then press F8 once.").pack(pady=(18, 8))
        entry = tk.Entry(root, width=64)
        entry.pack(padx=20, pady=4)
        status = tk.StringVar(value="Waiting for the XDG Global Shortcuts activation…")
        tk.Label(root, textvariable=status).pack(pady=12)
        deadline = time.monotonic() + TEST_TIMEOUT_SECONDS
        checked = {"value": False}

        def poll_activation():
            if activated.is_set() and not checked["value"]:
                checked["value"] = True
                try:
                    keyboard.paste()
                except Exception as error:
                    status.set(f"Paste request failed: {type(error).__name__}")
                    print(f"PORTAL_SMOKE_FAIL: paste request failed ({type(error).__name__})")
                    root.after(2500, root.quit)
                    return

                def check_entry():
                    if entry.get() == TEST_TEXT:
                        status.set("PASS — F8 portal shortcut pasted the clipboard marker.")
                        print("PORTAL_SMOKE_PASS")
                    else:
                        status.set("FAIL — F8 activated, but the text did not reach the entry.")
                        print("PORTAL_SMOKE_FAIL: marker was not pasted")
                    root.after(2500, root.quit)

                root.after(500, check_entry)
                return

            if time.monotonic() >= deadline:
                status.set("Timed out waiting for F8. Check the portal binding and retry.")
                print("PORTAL_SMOKE_FAIL: F8 activation timed out")
                root.after(2500, root.quit)
                return

            root.after(50, poll_activation)

        root.after(300, entry.focus_force)
        root.after(50, poll_activation)
        root.mainloop()
        return 0 if entry.get() == TEST_TEXT else 1
    except Exception as error:
        traceback.print_exc()
        print(f"PORTAL_SMOKE_FAIL: {type(error).__name__}: {error}")
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if shortcut is not None:
            shortcut.stop()
        keyboard.close()


if __name__ == "__main__":
    raise SystemExit(main())
