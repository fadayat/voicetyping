import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import voicetyping as vt


class FakePanel:
    def __init__(self):
        self.messages = []

    def show(self, text, **kwargs):
        self.messages.append(text)


class FakeStream:
    def stop(self):
        pass

    def close(self):
        pass


class FakeLiveSession:
    def finish(self, timeout):
        return "live result"

    def cancel(self):
        pass


class FakeClient:
    def __init__(self, text="batch result"):
        self.models = self
        self.text = text

    def generate_content(self, **kwargs):
        return SimpleNamespace(text=self.text)

    def close(self):
        pass


class PreparedPaste:
    def __init__(self, events):
        self.events = events

    def prepare(self):
        self.events.append("prepare")
        return lambda: self.events.append("paste")


def voiced_audio():
    return np.full((int(vt.SAMPLE_RATE * 0.3), 1), 1000, dtype=np.int16)


class AutomaticPasteAppTests(unittest.TestCase):
    def make_app(self, *, client=None, paste_action=None):
        panel = FakePanel()
        app = vt.VoiceTypingApp(
            client or FakeClient(),
            panel,
            paste_action=paste_action,
        )
        app.state = "processing"
        app.test_panel = panel
        return app

    def finish(self, app, live_session=None):
        app._stop_and_transcribe(
            FakeStream(), [voiced_audio()], live_session=live_session
        )

    def test_live_and_batch_copy_then_paste_in_order(self):
        for live_session, expected_text in (
            (FakeLiveSession(), "live result"),
            (None, "batch result"),
        ):
            with self.subTest(route="live" if live_session else "batch"):
                events = []
                app = self.make_app(paste_action=PreparedPaste(events))
                with mock.patch.object(
                    vt,
                    "copy_to_clipboard",
                    side_effect=lambda text: events.append(("copy", text)),
                ) as copy:
                    self.finish(app, live_session)

                copy.assert_called_once_with(expected_text)
                self.assertEqual(
                    events, ["prepare", ("copy", expected_text), "paste"]
                )
                self.assertEqual(app.state, "idle")
                self.assertEqual(app.test_panel.messages, ["Mətn kopyalandı"])
                self.assertEqual(
                    vt.NotificationPanel._determine_state(
                        app.test_panel.messages[-1]
                    ),
                    "copied",
                )

    def test_clipboard_failure_does_not_paste(self):
        paste = mock.Mock()
        app = self.make_app(paste_action=paste)
        with mock.patch.object(
            vt, "copy_to_clipboard", side_effect=RuntimeError("clipboard")
        ):
            self.finish(app)
        paste.assert_not_called()
        self.assertEqual(app.last_text, "batch result")
        self.assertEqual(app.state, "idle")

    def test_prepare_failure_still_copies_and_does_not_paste(self):
        action = mock.Mock()
        action.prepare.side_effect = RuntimeError("private detail")
        app = self.make_app(paste_action=action)
        output = io.StringIO()
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            with contextlib.redirect_stdout(output):
                self.finish(app)
        action.prepare.assert_called_once_with()
        action.assert_not_called()
        copy.assert_called_once_with("batch result")
        self.assertEqual(app.last_text, "batch result")
        self.assertEqual(app.state, "idle")
        self.assertNotIn("private detail", output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())

    def test_no_final_text_does_not_prepare_or_paste(self):
        action = mock.Mock()
        app = self.make_app(client=FakeClient(text=""), paste_action=action)
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            self.finish(app)
        action.prepare.assert_not_called()
        action.assert_not_called()
        copy.assert_not_called()

    def test_already_shutdown_does_not_prepare_or_paste(self):
        action = mock.Mock()
        app = self.make_app(paste_action=action)
        app._shutdown.set()
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            self.finish(app)
        action.prepare.assert_not_called()
        action.assert_not_called()
        copy.assert_not_called()

    def test_paste_failure_keeps_text_copied_and_app_idle(self):
        def paste():
            raise RuntimeError("private detail")

        app = self.make_app(paste_action=paste)
        output = io.StringIO()
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            with contextlib.redirect_stdout(output):
                self.finish(app)
        copy.assert_called_once_with("batch result")
        self.assertEqual(app.last_text, "batch result")
        self.assertEqual(app.state, "idle")
        self.assertEqual(
            app.test_panel.messages,
            ["Hazırdır — Ctrl+V ilə yapışdırın"],
        )
        self.assertNotIn("private detail", output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())

    def test_default_opt_out_does_not_paste(self):
        app = self.make_app()
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            self.finish(app)
        copy.assert_called_once_with("batch result")
        self.assertEqual(app.state, "idle")

    def test_shutdown_after_copy_skips_paste(self):
        paste = mock.Mock()
        app = self.make_app(paste_action=paste)

        def copy_then_shutdown(_text):
            app._shutdown.set()

        with mock.patch.object(
            vt, "copy_to_clipboard", side_effect=copy_then_shutdown
        ):
            self.finish(app)
        paste.assert_not_called()
        self.assertEqual(app.last_text, "batch result")

    def test_copy_last_transcription_never_pastes(self):
        paste = mock.Mock()
        app = self.make_app(paste_action=paste)
        app.last_text = "saved text"
        with mock.patch.object(vt, "copy_to_clipboard") as copy:
            self.assertTrue(app.copy_last_transcription())
        copy.assert_called_once_with("saved text")
        paste.assert_not_called()


if __name__ == "__main__":
    unittest.main()
