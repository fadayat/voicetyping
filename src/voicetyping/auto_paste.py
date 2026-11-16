"""Paste the existing clipboard through the active desktop's input API.

No transcript is passed to this module. The GNOME backend uses a short-lived
Mutter input session, X11 uses pynput, and other Wayland sessions can use a
user-approved XDG Remote Desktop portal session. Unsupported paths leave the
transcript on the clipboard.
"""

from __future__ import annotations

import threading
import time
from functools import partial
from pathlib import Path


BUS_NAME = "org.gnome.Mutter.RemoteDesktop"
OBJECT_PATH = "/org/gnome/Mutter/RemoteDesktop"
SESSION_INTERFACE = BUS_NAME + ".Session"
CALL_TIMEOUT_MS = 500
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_V = 47
FOCUS_LOOKUP_SECONDS = 0.08
ACCESSIBLE_INTERFACE = "org.a11y.atspi.Accessible"
ACTIVE_STATE_MASK = 1 << 1
TERMINAL_EXECUTABLES = frozenset({
    "ptyxis", "kgx", "gnome-console", "gnome-terminal-server",
    "konsole", "xfce4-terminal", "mate-terminal", "tilix", "guake",
    "yakuake", "alacritty", "kitty", "foot", "footclient", "wezterm-gui",
    "xterm", "uxterm", "urxvt", "urxvtd", "lxterminal", "terminology",
})


def _connect_accessibility_bus(Gio, timeout_ms):
    session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    address = session_bus.call_sync(
        "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress",
        None, None, Gio.DBusCallFlags.NO_AUTO_START, timeout_ms, None,
    ).unpack()[0]
    return Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None,
    )


def _load_gio_modules():
    """Load GNOME's optional D-Bus bindings only when that backend is used."""
    from gi.repository import Gio, GLib

    return Gio, GLib


def _active_application_pids() -> frozenset[int]:
    """Query shallow window states and process IDs, never titles or text.

    Direct D-Bus calls avoid loading an accessibility cache of document content.
    There is no tree traversal below top-level windows and no fixed sleep. An
    unavailable, slow, or unusually large accessibility tree is inconclusive.
    """
    deadline = time.monotonic() + FOCUS_LOOKUP_SECONDS

    def timeout_ms():
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("focus lookup exceeded budget")
        return min(20, remaining)

    timeout_ms()
    Gio, GLib = _load_gio_modules()
    connection = _connect_accessibility_bus(Gio, timeout_ms())
    try:
        def call(name, path, interface, method, parameters=None):
            return connection.call_sync(
                name, path, interface, method, parameters, None,
                Gio.DBusCallFlags.NO_AUTO_START, timeout_ms(), None,
            ).unpack()[0]

        applications = call(
            "org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root",
            ACCESSIBLE_INTERFACE, "GetChildren",
        )
        if len(applications) > 32:
            return frozenset()
        active = set()
        for application_name, application_path in applications:
            windows = call(
                application_name, application_path, ACCESSIBLE_INTERFACE, "GetChildren",
            )
            if len(windows) > 16:
                return frozenset()
            for window_name, window_path in windows:
                states = call(window_name, window_path, ACCESSIBLE_INTERFACE, "GetState")
                if states and states[0] & ACTIVE_STATE_MASK:
                    active.add(call(
                        "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "GetConnectionUnixProcessID",
                        GLib.Variant("(s)", (window_name,)),
                    ))
        return frozenset(active)
    finally:
        try:
            connection.close_sync(None)
        except Exception:
            pass


def is_terminal_focused() -> bool:
    """Recognize an unambiguous active standalone terminal without reading it."""
    try:
        pids = _active_application_pids()
        if len(pids) != 1:
            return False
        pid = next(iter(pids))
        if not isinstance(pid, int) or pid <= 0:
            return False
        executable = (Path("/proc") / str(pid) / "exe").readlink().name
        return executable in TERMINAL_EXECUTABLES
    except Exception:
        # Keep the existing Ctrl+V behavior when metadata cannot be determined.
        return False


class AutomaticPaste:
    """Prepare before copying: wl-copy can temporarily change keyboard focus."""

    def prepare(self):
        return partial(paste_clipboard, terminal=is_terminal_focused())

    def __call__(self):
        self.prepare()()


class _MutterKeyboard:
    """Own a dedicated D-Bus connection, never the shared GUI connection."""

    def __init__(self):
        self.connection = None
        self.path = None

    def __enter__(self):
        # Import lazily: absence of GI must not disable recording or copying.
        from gi.repository import Gio, GLib

        self.Gio = Gio
        self.GLib = GLib
        try:
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.connection = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
            self.path = self._call(OBJECT_PATH, BUS_NAME, "CreateSession").unpack()[0]
            self._call(self.path, SESSION_INTERFACE, "Start")
            return self
        except BaseException:
            self.close()
            raise

    def _call(self, path, interface, method, parameters=None):
        return self.connection.call_sync(
            BUS_NAME,
            path,
            interface,
            method,
            parameters,
            None,
            self.Gio.DBusCallFlags.NO_AUTO_START,
            CALL_TIMEOUT_MS,
            None,
        )

    def key(self, code: int, pressed: bool) -> None:
        self._call(
            self.path,
            SESSION_INTERFACE,
            "NotifyKeyboardKeycode",
            self.GLib.Variant("(ub)", (code, pressed)),
        )

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            if self.path is not None:
                self._call(self.path, SESSION_INTERFACE, "Stop")
        except Exception:
            # Closing our dedicated connection also destroys its input session.
            pass
        finally:
            try:
                self.connection.close_sync(None)
            except Exception:
                pass
            self.connection = None
            self.path = None

    def __exit__(self, *_):
        self.close()


def _load_pynput_keyboard():
    """Load X11 keyboard injection lazily; Wayland does not allow this path."""
    from pynput.keyboard import Controller, Key, KeyCode

    return Controller, Key, KeyCode


class _X11Keyboard:
    """Send one paste shortcut through X11's user-session input API."""

    def __enter__(self):
        Controller, Key, KeyCode = _load_pynput_keyboard()
        self.controller = Controller()
        self.keys = {
            KEY_LEFTCTRL: Key.ctrl_l,
            KEY_LEFTSHIFT: Key.shift_l,
            KEY_V: KeyCode.from_char("v"),
        }
        return self

    def key(self, code: int, pressed: bool) -> None:
        key = self.keys[code]
        if pressed:
            self.controller.press(key)
        else:
            self.controller.release(key)

    def __exit__(self, *_):
        return None


def _emit_paste(keyboard, *, terminal: bool = False, settle_after_release: float = 0.0) -> None:
    """Emit Ctrl+Shift+V for a terminal, otherwise Ctrl+V; never Enter.

    Do not retry an uncertain paste: it may already have reached the editor.
    Key releases and session cleanup are attempted even after an input error.
    Success means the shortcut was emitted, not that the target accepted paste.
    """
    attempted = []
    try:
        codes = (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V) if terminal else (KEY_LEFTCTRL, KEY_V)
        for code in codes:
            # A failed input acknowledgement does not prove the press was not delivered.
            attempted.append(code)
            keyboard.key(code, True)
            time.sleep(0.01)
    finally:
        release_error = None
        for code in reversed(attempted):
            try:
                keyboard.key(code, False)
            except Exception as error:
                release_error = error
        if settle_after_release > 0:
            # Mutter queues input for the compositor's next frame. Let the
            # release events reach the client before destroying the device.
            time.sleep(settle_after_release)
        if release_error is not None:
            raise release_error


def paste_clipboard(*, terminal: bool = False) -> None:
    """Paste the existing clipboard through GNOME's Mutter input session."""
    with _MutterKeyboard() as keyboard:
        _emit_paste(keyboard, terminal=terminal, settle_after_release=0.04)


def paste_clipboard_x11(*, terminal: bool = False) -> None:
    """Paste the existing clipboard through X11's user-session input API."""
    with _X11Keyboard() as keyboard:
        _emit_paste(keyboard, terminal=terminal)


class X11AutomaticPaste:
    """Prepare an X11 paste action while preserving focused-terminal routing."""

    def prepare(self):
        return partial(paste_clipboard_x11, terminal=is_terminal_focused())

    def __call__(self):
        self.prepare()()


class PortalAutomaticPaste:
    """Use a pre-approved XDG Remote Desktop session on Wayland."""

    def __init__(self, keyboard_session):
        self.keyboard_session = keyboard_session
        self._lock = threading.Lock()
        self._disabled = False

    def prepare(self):
        with self._lock:
            if self._disabled:
                return None
        return partial(self._paste, terminal=is_terminal_focused())

    def _paste(self, *, terminal: bool = False):
        with self._lock:
            if self._disabled:
                return
        try:
            self.keyboard_session.paste(terminal=terminal)
        except Exception:
            self.close()
            raise

    def __call__(self):
        prepared = self.prepare()
        if prepared is not None:
            prepared()

    def close(self):
        with self._lock:
            if self._disabled:
                return
            self._disabled = True
        try:
            self.keyboard_session.close()
        except Exception:
            pass
