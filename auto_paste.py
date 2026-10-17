"""Paste the existing clipboard once through GNOME's local input API.

No transcript is passed to this module. It neither captures the screen nor
starts a remote-desktop daemon. A short-lived Mutter input session emits one
paste shortcut, releases its keys, and closes. Mutter's API is private: on unsupported
desktops/versions the caller must retain the clipboard as the manual fallback.
"""

from __future__ import annotations

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


def _active_application_pids() -> frozenset[int]:
    """Query shallow window states and process IDs, never titles or text.

    Direct D-Bus calls avoid loading an accessibility cache of document content.
    There is no tree traversal below top-level windows and no fixed sleep. An
    unavailable, slow, or unusually large accessibility tree is inconclusive.
    """
    from gi.repository import Gio, GLib
    deadline = time.monotonic() + FOCUS_LOOKUP_SECONDS

    def timeout_ms():
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("focus lookup exceeded budget")
        return min(20, remaining)

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


def paste_clipboard(*, terminal: bool = False) -> None:
    """Emit Ctrl+Shift+V for a terminal, otherwise Ctrl+V; never Enter.

    Do not retry an uncertain paste: it may already have reached the editor.
    Key releases and session cleanup are attempted even after an input error.
    Success means the shortcut was emitted, not that the target accepted paste.
    """
    with _MutterKeyboard() as keyboard:
        attempted = []
        try:
            codes = (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V) if terminal else (KEY_LEFTCTRL, KEY_V)
            for code in codes:
                # A failed D-Bus reply does not prove the press was not delivered.
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
            # Mutter queues input for the compositor's next frame. Let the
            # release events reach the client before destroying the device.
            time.sleep(0.04)
            if release_error is not None:
                raise release_error
