"""Exercise the real desktop constructor without credentials or Win32 effects."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from voicetyping.windows import app, settings


class DesktopTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:
            self.skipTest('No display for Tk integration test')

    def tearDown(self):
        if hasattr(self, 'root'):
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def test_stored_credential_constructs_engine_and_copy_stays_available(self):
        desktop = Mock()
        desktop.poll_hotkey.return_value = False
        with patch.object(app, 'load_key', return_value='test-only-key'), \
             patch.object(app, 'startup_enabled', return_value=False), \
             patch.object(app, 'create_client', return_value=Mock()), \
             patch.object(app.DesktopApp, 'setup_tray'):
            ui = app.DesktopApp(self.root, desktop)
            self.assertIsNotNone(ui.engine)
            self.assertIn('Hazırdır', ui.status.get())
            ui.engine.last_text = 'Azərbaycan: ə ı İ ş ç ö ü ğ'
            ui.poll()
            self.assertIn('Azərbaycan', ui.transcript.get('1.0', 'end'))
            self.assertEqual(str(ui.copy_button['state']), 'normal')
            ui.quit()

    def test_saved_hotkey_is_registered_and_can_be_changed(self):
        desktop = Mock()
        desktop.poll_hotkey.return_value = False
        preferences = dict(settings.DEFAULT_SETTINGS)
        preferences['language_codes'] = list(preferences['language_codes'])
        preferences['custom_vocabulary'] = list(preferences['custom_vocabulary'])
        preferences['shortcut'] = 'Ctrl+F9'
        with patch.object(app, 'load_settings', return_value=preferences), \
             patch.object(app, 'load_key', return_value=''), \
             patch.object(app, 'startup_enabled', return_value=False), \
             patch.object(app.DesktopApp, 'setup_tray'), \
             patch.object(app, 'save_settings') as save:
            ui = app.DesktopApp(self.root, desktop)
            desktop.register_hotkey.assert_called_once_with('Ctrl+F9')
            self.assertIn('Ctrl+F9', ui.shortcut_help.get())

            ui.shortcut.delete(0, 'end')
            ui.shortcut.insert(0, 'shift+f10')
            ui.change_shortcut()
            self.assertEqual(ui.preferences['shortcut'], 'Shift+F10')
            self.assertIn('Shift+F10', ui.shortcut_help.get())
            save.assert_called_once()
            ui.quit()


if __name__ == '__main__':
    unittest.main()
