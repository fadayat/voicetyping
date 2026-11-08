"""XDG desktop portal adapters for Wayland shortcut and keyboard access."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping

from shortcuts import parse_shortcut


PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
GLOBAL_SHORTCUTS_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REMOTE_DESKTOP_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
KEYBOARD_DEVICE = 1
KEY_RELEASED = 0
KEY_PRESSED = 1
PORTAL_CALL_TIMEOUT_MS = 5_000


class PortalError(RuntimeError):
    """A portal is missing, unavailable, or returned an invalid response."""


class PortalRequestDenied(PortalError):
    """The user declined or dismissed a portal request."""


def _load_gio_modules():
    from gi.repository import Gio, GLib

    return Gio, GLib


class XdgDesktopPortal:
    """Issue portal requests with race-safe Request::Response handling."""

    def __init__(self, *, gio=None, glib=None, connection=None, request_timeout_seconds=120):
        if gio is None or glib is None:
            loaded_gio, loaded_glib = _load_gio_modules()
            gio = loaded_gio if gio is None else gio
            glib = loaded_glib if glib is None else glib
        self.Gio = gio
        self.GLib = glib
        self.connection = connection or gio.bus_get_sync(gio.BusType.SESSION, None)
        self.request_timeout_seconds = request_timeout_seconds
        self._request_lock = threading.Lock()

    def variant(self, signature, value):
        return self.GLib.Variant(signature, value)

    def request(
        self,
        interface: str,
        method: str,
        parameters: Callable[[Mapping[str, object]], object],
        options: Mapping[str, object] | None = None,
    ) -> dict:
        """Call a portal method and wait for its request response signal."""
        with self._request_lock:
            handle_token = f"voicetyping_{uuid.uuid4().hex}"
            request_options = dict(options or {})
            request_options["handle_token"] = self.variant("s", handle_token)
            unique_name = self.connection.get_unique_name()
            if not unique_name:
                raise PortalError("The session bus did not provide a unique name")
            sender = unique_name.removeprefix(":").replace(".", "_")
            expected_path = f"{PORTAL_PATH}/request/{sender}/{handle_token}"
            sender_request_prefix = f"{PORTAL_PATH}/request/{sender}/"

            context = self.GLib.MainContext.new()
            context.push_thread_default()
            loop = None
            target_path = {"value": None}
            responses = {}
            timed_out = {"value": False}

            def on_response(_connection, _sender, object_path, _interface, _signal, values, _data):
                target = target_path["value"]
                if target is None and (
                    object_path == expected_path
                    or object_path.startswith(sender_request_prefix)
                ):
                    responses[object_path] = values.unpack()
                elif target is not None and object_path == target:
                    responses[object_path] = values.unpack()
                    if target == object_path:
                        loop.quit()

            subscription = None
            timeout_timer = None
            request_path = None

            def on_timeout():
                timed_out["value"] = True
                loop.quit()

            try:
                loop = self.GLib.MainLoop.new(context, False)
                subscription = self.connection.signal_subscribe(
                    PORTAL_NAME,
                    REQUEST_INTERFACE,
                    "Response",
                    None,
                    None,
                    self.Gio.DBusSignalFlags.NONE,
                    on_response,
                    None,
                )
                reply = self.connection.call_sync(
                    PORTAL_NAME,
                    PORTAL_PATH,
                    interface,
                    method,
                    parameters(request_options),
                    None,
                    self.Gio.DBusCallFlags.NONE,
                    PORTAL_CALL_TIMEOUT_MS,
                    None,
                )
                request_path = reply.unpack()[0]
                target_path["value"] = request_path
                response = responses.get(request_path)
                if response is None:
                    timeout_timer = threading.Timer(
                        self.request_timeout_seconds, on_timeout
                    )
                    timeout_timer.daemon = True
                    timeout_timer.start()
                    loop.run()
                    response = responses.get(request_path)
                if response is None:
                    if timed_out["value"]:
                        self._close_request(request_path)
                        raise PortalError(f"Portal request timed out: {method}")
                    raise PortalError(f"Portal response was missing: {method}")
                response_code, result = response
                if response_code != 0:
                    raise PortalRequestDenied(f"Portal request declined: {method}")
                return result
            finally:
                try:
                    if timeout_timer is not None:
                        timeout_timer.cancel()
                    if subscription is not None:
                        self.connection.signal_unsubscribe(subscription)
                finally:
                    context.pop_thread_default()

    def call(self, interface, method, parameters=None, *, object_path=PORTAL_PATH):
        return self.connection.call_sync(
            PORTAL_NAME,
            object_path,
            interface,
            method,
            parameters,
            None,
            self.Gio.DBusCallFlags.NONE,
            PORTAL_CALL_TIMEOUT_MS,
            None,
        )

    def _close_request(self, request_path):
        try:
            self.connection.call_sync(
                PORTAL_NAME,
                request_path,
                REQUEST_INTERFACE,
                "Close",
                self.variant("()", ()),
                None,
                self.Gio.DBusCallFlags.NONE,
                PORTAL_CALL_TIMEOUT_MS,
                None,
            )
        except Exception:
            pass


class GlobalShortcutPortal:
    """Register one global shortcut and deliver activations to the app."""

    SHORTCUT_ID = "toggle-recording"

    def __init__(self, shortcut: str, on_activate: Callable[[], None], *, client=None):
        self.shortcut = parse_shortcut(shortcut).to_xdg_trigger()
        self.on_activate = on_activate
        self.client = client or XdgDesktopPortal()
        self.session_handle = None
        self._signal_subscription = None
        self._signal_thread = None
        self._signal_loop = None
        self._signal_ready = threading.Event()
        self._signal_error = None
        self.active = False

    def start(self) -> bool:
        if self.active:
            return True
        self._signal_ready.clear()
        self._signal_error = None
        glib = self.client.GLib
        try:
            session_results = self.client.request(
                GLOBAL_SHORTCUTS_INTERFACE,
                "CreateSession",
                lambda options: self.client.variant("(a{sv})", (options,)),
                {
                    "session_handle_token": self.client.variant(
                        "s", f"voicetyping_session_{uuid.uuid4().hex}"
                    )
                },
            )
            self.session_handle = session_results.get("session_handle")
            if not isinstance(self.session_handle, str) or not self.session_handle:
                raise PortalError("Global Shortcuts did not return a session")

            self._start_signal_dispatcher()
            shortcut_spec = [(
                self.SHORTCUT_ID,
                {
                    "description": glib.Variant(
                        "s", "Start or stop VoiceTyping recording"
                    ),
                    "preferred_trigger": glib.Variant("s", self.shortcut),
                },
            )]
            self.active = True
            results = self.client.request(
                GLOBAL_SHORTCUTS_INTERFACE,
                "BindShortcuts",
                lambda options: self.client.variant(
                    "(oa(sa{sv})sa{sv})",
                    (self.session_handle, shortcut_spec, "", options),
                ),
            )
            bound = results.get("shortcuts", ())
            if self.SHORTCUT_ID not in {item[0] for item in bound}:
                self.stop()
                return False
            self.active = True
            return True
        except Exception:
            self.stop()
            raise

    def _start_signal_dispatcher(self):
        """Keep portal signals flowing even when no GTK main loop is running."""
        def run():
            context = None
            try:
                context = self.client.GLib.MainContext.new()
                context.push_thread_default()
                loop = self.client.GLib.MainLoop.new(context, False)
                self._signal_loop = loop
                self._signal_subscription = self.client.connection.signal_subscribe(
                    PORTAL_NAME,
                    GLOBAL_SHORTCUTS_INTERFACE,
                    "Activated",
                    PORTAL_PATH,
                    None,
                    self.client.Gio.DBusSignalFlags.NONE,
                    self._on_activated,
                    None,
                )
                self._signal_ready.set()
                loop.run()
            except Exception as error:
                self._signal_error = error
                self._signal_ready.set()
            finally:
                if self._signal_subscription is not None:
                    try:
                        self.client.connection.signal_unsubscribe(self._signal_subscription)
                    except Exception:
                        pass
                    self._signal_subscription = None
                self._signal_loop = None
                if context is not None:
                    try:
                        context.pop_thread_default()
                    except Exception:
                        pass

        self._signal_thread = threading.Thread(
            target=run, daemon=True, name="voicetyping-portal-shortcuts"
        )
        self._signal_thread.start()
        if not self._signal_ready.wait(5):
            raise PortalError("Global Shortcuts signal dispatcher did not start")
        if self._signal_error is not None:
            raise PortalError("Global Shortcuts signal dispatcher failed") from self._signal_error

    def _on_activated(self, _connection, _sender, _path, _interface, _signal, values, _data):
        session, shortcut_id, _timestamp, _options = values.unpack()
        if self.active and session == self.session_handle and shortcut_id == self.SHORTCUT_ID:
            self.on_activate()

    def stop(self):
        if self._signal_subscription is not None:
            if self._signal_loop is not None:
                self._signal_loop.quit()
            elif self._signal_thread is None:
                try:
                    self.client.connection.signal_unsubscribe(self._signal_subscription)
                except Exception:
                    pass
                self._signal_subscription = None
        elif self._signal_loop is not None:
            self._signal_loop.quit()
        if self._signal_thread is not None:
            if self._signal_loop is not None:
                self._signal_loop.quit()
            self._signal_thread.join(timeout=5)
            self._signal_thread = None
        if self.session_handle:
            try:
                self.client.call(
                    SESSION_INTERFACE,
                    "Close",
                    self.client.variant("()", ()),
                    object_path=self.session_handle,
                )
            except Exception:
                pass
            self.session_handle = None
        self.active = False
        close_client = getattr(self.client, "close", None)
        if callable(close_client):
            try:
                close_client()
            except Exception:
                pass


class RemoteDesktopKeyboard:
    """Hold a user-approved keyboard session for automatic Wayland paste."""

    def __init__(self, *, client=None):
        self.client = client or XdgDesktopPortal()
        self.session_handle = None
        self.active = False
        self._lock = threading.RLock()

    def start(self) -> bool:
        try:
            session_results = self.client.request(
                REMOTE_DESKTOP_INTERFACE,
                "CreateSession",
                lambda options: self.client.variant("(a{sv})", (options,)),
                {
                    "session_handle_token": self.client.variant(
                        "s", f"voicetyping_session_{uuid.uuid4().hex}"
                    )
                },
            )
            self.session_handle = session_results.get("session_handle")
            if not isinstance(self.session_handle, str) or not self.session_handle:
                raise PortalError("Remote Desktop did not return a session")

            self.client.request(
                REMOTE_DESKTOP_INTERFACE,
                "SelectDevices",
                lambda options: self.client.variant(
                    "(oa{sv})", (self.session_handle, options)
                ),
                {"types": self.client.variant("u", KEYBOARD_DEVICE)},
            )
            result = self.client.request(
                REMOTE_DESKTOP_INTERFACE,
                "Start",
                lambda options: self.client.variant(
                    "(osa{sv})", (self.session_handle, "", options)
                ),
            )
            devices = int(result.get("devices", 0))
            if not devices & KEYBOARD_DEVICE:
                self.close()
                return False
            self.active = True
            return True
        except Exception:
            self.close()
            raise

    def _notify_key(self, code: int, pressed: bool) -> None:
        state = KEY_PRESSED if pressed else KEY_RELEASED
        self.client.call(
            REMOTE_DESKTOP_INTERFACE,
            "NotifyKeyboardKeycode",
            self.client.variant(
                "(oa{sv}iu)",
                (self.session_handle, {}, code, state),
            ),
        )

    def paste(self, *, terminal: bool = False) -> None:
        codes = (29, 42, 47) if terminal else (29, 47)
        with self._lock:
            if not self.active or not self.session_handle:
                raise PortalError("Remote Desktop keyboard session is inactive")
            attempted = []
            press_error = None
            try:
                for code in codes:
                    attempted.append(code)
                    self._notify_key(code, True)
                    time.sleep(0.01)
            except Exception as error:
                press_error = error
            finally:
                release_error = None
                for code in reversed(attempted):
                    try:
                        self._notify_key(code, False)
                    except Exception as error:
                        release_error = error
                if release_error is not None or press_error is not None:
                    self.close()
                    raise release_error or press_error

    def close(self):
        with self._lock:
            if self.session_handle:
                try:
                    self.client.call(
                        SESSION_INTERFACE,
                        "Close",
                        self.client.variant("()", ()),
                        object_path=self.session_handle,
                    )
                except Exception:
                    pass
            self.session_handle = None
            self.active = False
            close_client = getattr(self.client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    pass
