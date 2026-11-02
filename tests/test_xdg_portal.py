"""Portal adapters are exercised with fake D-Bus and GLib objects only."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from xdg_portal import (
    GLOBAL_SHORTCUTS_INTERFACE,
    KEYBOARD_DEVICE,
    REMOTE_DESKTOP_INTERFACE,
    GlobalShortcutPortal,
    PortalRequestDenied,
    RemoteDesktopKeyboard,
    XdgDesktopPortal,
)


class FakeVariant:
    def __init__(self, signature, value):
        self.signature = signature
        self.value = value

    def unpack(self):
        return self.value


class FakeLoop:
    def __init__(self, *_args):
        self.stopped = threading.Event()

    @classmethod
    def new(cls, *args):
        return cls(*args)

    def run(self):
        self.stopped.wait(1)

    def quit(self):
        self.stopped.set()


class FakeContext:
    @classmethod
    def new(cls):
        return cls()

    def push_thread_default(self):
        pass

    def pop_thread_default(self):
        pass


class FakeConnection:
    def __init__(self, response=(0, {"session_handle": "/session"})):
        self.response = response
        self.unique_name = ":1.42"
        self.subscriptions = {}
        self.events = []
        self.calls = []
        self.reply_path_override = None
        self.response_path_override = None
        self._next_subscription = 1

    def get_unique_name(self):
        return self.unique_name

    def signal_subscribe(self, *args):
        subscription = self._next_subscription
        self._next_subscription += 1
        self.subscriptions[subscription] = args
        self.events.append("subscribe")
        return subscription

    def signal_unsubscribe(self, subscription):
        self.subscriptions.pop(subscription, None)
        self.events.append("unsubscribe")

    def call_sync(self, destination, path, interface, method, parameters, *_args):
        self.calls.append((path, interface, method, parameters))
        if method == "Close":
            return FakeVariant("()", ())
        options = parameters.value[0]
        token = options["handle_token"].value
        sender = self.unique_name.removeprefix(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        self.events.append("call")
        callback = next(iter(self.subscriptions.values()))[6]
        if self.response is not None:
            callback(
                self,
                destination,
                self.response_path_override or request_path,
                "org.freedesktop.portal.Request",
                "Response",
                FakeVariant("(ua{sv})", self.response),
                None,
            )
        return FakeVariant("(o)", (self.reply_path_override or request_path,))


class FakeGio:
    class DBusSignalFlags:
        NONE = 0

    class DBusCallFlags:
        NONE = 0

    class BusType:
        SESSION = 0


class FakeGLib:
    Variant = FakeVariant
    MainLoop = FakeLoop
    MainContext = FakeContext

    @staticmethod
    def timeout_add_seconds(_seconds, callback):
        callback()
        return 17

    @staticmethod
    def source_remove(_source_id):
        pass


class PortalRequestTests(unittest.TestCase):
    def make_client(self, response=(0, {"session_handle": "/session"})):
        connection = FakeConnection(response)
        client = XdgDesktopPortal(
            gio=FakeGio,
            glib=FakeGLib,
            connection=connection,
            request_timeout_seconds=0,
        )
        return client, connection

    def test_request_subscribes_before_call_and_returns_portal_response(self):
        client, connection = self.make_client()
        result = client.request(
            "org.freedesktop.portal.RemoteDesktop",
            "CreateSession",
            lambda options: client.variant("(a{sv})", (options,)),
        )

        self.assertEqual(result, {"session_handle": "/session"})
        self.assertEqual(connection.events[:2], ["subscribe", "call"])
        self.assertEqual(connection.events[-1], "unsubscribe")
        self.assertEqual(connection.calls[0][2], "CreateSession")

    def test_declined_request_is_reported_without_exposing_response_data(self):
        client, _connection = self.make_client(response=(1, {}))
        with self.assertRaises(PortalRequestDenied):
            client.request(
                "org.freedesktop.portal.RemoteDesktop",
                "Start",
                lambda options: client.variant("(a{sv})", (options,)),
            )

    def test_buffers_response_from_legacy_portal_request_path(self):
        client, connection = self.make_client()
        alternate_path = "/org/freedesktop/portal/desktop/request/1_42/legacy-token"
        connection.reply_path_override = alternate_path
        connection.response_path_override = alternate_path

        result = client.request(
            "org.freedesktop.portal.RemoteDesktop",
            "CreateSession",
            lambda options: client.variant("(a{sv})", (options,)),
        )

        self.assertEqual(result, {"session_handle": "/session"})


    def test_request_timeout_closes_request_handle(self):
        client, connection = self.make_client(response=None)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            client.request(
                "org.freedesktop.portal.RemoteDesktop",
                "Start",
                lambda options: client.variant("(a{sv})", (options,)),
            )
        self.assertEqual(connection.calls[-1][1:], (
            "org.freedesktop.portal.Request",
            "Close",
            mock.ANY,
        ))


class FakePortalClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.Gio = FakeGio
        self.GLib = FakeGLib
        self.connection = FakeConnection(response=None)
        self.calls = []
        self.requests = []
        self.closed = False

    def variant(self, signature, value):
        return FakeVariant(signature, value)

    def request(self, interface, method, parameters, options=None):
        self.requests.append((interface, method, parameters, options))
        return self.responses.pop(0)

    def call(self, interface, method, parameters=None, *, object_path=None):
        self.calls.append((interface, method, parameters, object_path))

    def close(self):
        self.closed = True


class GlobalShortcutPortalTests(unittest.TestCase):
    def test_shortcut_registers_dispatches_and_closes_session(self):
        client = FakePortalClient([
            {"session_handle": "/session/1"},
            {"shortcuts": [("toggle-recording", {})]},
        ])
        activated = mock.Mock()
        portal = GlobalShortcutPortal("F8", activated, client=client)

        self.assertTrue(portal.start())
        self.assertEqual(client.requests[0][0:2], (GLOBAL_SHORTCUTS_INTERFACE, "CreateSession"))
        self.assertEqual(client.requests[1][0:2], (GLOBAL_SHORTCUTS_INTERFACE, "BindShortcuts"))
        bind_parameters = client.requests[1][2]({})
        self.assertEqual(bind_parameters.signature, "(oa(sa{sv})sa{sv})")
        self.assertEqual(
            bind_parameters.value[1][0][1]["preferred_trigger"].value,
            "F8",
        )
        self.assertIsInstance(bind_parameters.value[1], list)
        self.assertIsInstance(
            bind_parameters.value[1][0][1]["description"], FakeVariant
        )
        self.assertTrue(client.connection.subscriptions)

        callback = next(iter(client.connection.subscriptions.values()))[6]
        callback(
            client.connection,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            GLOBAL_SHORTCUTS_INTERFACE,
            "Activated",
            FakeVariant("(osta{sv})", ("/session/1", "toggle-recording", 123, {})),
            None,
        )
        activated.assert_called_once_with()

        portal.stop()
        self.assertFalse(portal.active)
        self.assertEqual(client.calls[-1][1:], ("Close", mock.ANY, "/session/1"))
        self.assertFalse(client.connection.subscriptions)
        self.assertTrue(client.closed)

    def test_gnome_shortcut_syntax_is_converted_before_portal_binding(self):
        client = FakePortalClient([
            {"session_handle": "/session/ctrl-alt"},
            {"shortcuts": [("toggle-recording", {})]},
        ])
        portal = GlobalShortcutPortal("<Ctrl><Alt>F8", mock.Mock(), client=client)

        self.assertTrue(portal.start())
        parameters = client.requests[1][2]({})
        self.assertEqual(
            parameters.value[1][0][1]["preferred_trigger"].value,
            "CTRL+ALT+F8",
        )
        portal.stop()

    def test_unbound_shortcut_closes_session_and_returns_false(self):
        client = FakePortalClient([
            {"session_handle": "/session/2"},
            {"shortcuts": []},
        ])
        portal = GlobalShortcutPortal("F8", mock.Mock(), client=client)

        self.assertFalse(portal.start())
        self.assertEqual(client.calls[-1][3], "/session/2")
        self.assertFalse(portal.active)


class RemoteDesktopKeyboardTests(unittest.TestCase):
    def test_keyboard_permission_and_paste_release_keys_then_close(self):
        client = FakePortalClient([
            {"session_handle": "/session/keyboard"},
            {},
            {"devices": KEYBOARD_DEVICE},
        ])
        keyboard = RemoteDesktopKeyboard(client=client)
        self.assertTrue(keyboard.start())
        self.assertEqual(client.requests[1][0:2], (REMOTE_DESKTOP_INTERFACE, "SelectDevices"))
        self.assertEqual(client.requests[1][3]["types"].value, KEYBOARD_DEVICE)

        with mock.patch("xdg_portal.time.sleep"):
            keyboard.paste(terminal=True)
        events = [call[2].value[2:] for call in client.calls]
        self.assertEqual(events, [
            (29, 1), (42, 1), (47, 1), (47, 0), (42, 0), (29, 0),
        ])

        keyboard.close()
        self.assertEqual(client.calls[-1][1:], ("Close", mock.ANY, "/session/keyboard"))
        self.assertFalse(keyboard.active)
        self.assertTrue(client.closed)

    def test_unapproved_keyboard_device_closes_session_and_falls_back(self):
        client = FakePortalClient([
            {"session_handle": "/session/denied"},
            {},
            {"devices": 0},
        ])
        keyboard = RemoteDesktopKeyboard(client=client)

        self.assertFalse(keyboard.start())
        self.assertEqual(client.calls[-1][3], "/session/denied")
        self.assertFalse(keyboard.active)

    def test_failed_key_release_closes_session_and_disables_backend(self):
        client = FakePortalClient([
            {"session_handle": "/session/release-error"},
            {},
            {"devices": KEYBOARD_DEVICE},
        ])
        keyboard = RemoteDesktopKeyboard(client=client)
        self.assertTrue(keyboard.start())
        original_call = client.call

        def fail_release(interface, method, parameters=None, *, object_path=None):
            if method == "NotifyKeyboardKeycode" and parameters.value[3] == 0:
                raise RuntimeError("key release failed")
            return original_call(interface, method, parameters, object_path=object_path)

        client.call = fail_release
        with mock.patch("xdg_portal.time.sleep"), self.assertRaisesRegex(
            RuntimeError, "key release failed"
        ):
            keyboard.paste()

        self.assertFalse(keyboard.active)
        self.assertIsNone(keyboard.session_handle)
        self.assertEqual(client.calls[-1][1:], ("Close", mock.ANY, "/session/release-error"))


if __name__ == "__main__":
    unittest.main()
