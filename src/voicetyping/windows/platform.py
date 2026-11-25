"""Windows integration. Importable elsewhere for deterministic unit tests."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import queue
import threading


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


def parse_hotkey(value: str) -> tuple[int, int, str]:
    """Return Win32 modifiers, virtual key and a canonical display label."""
    if not isinstance(value, str):
        raise TypeError('Hotkey must be text.')
    parts = [part.strip() for part in value.split('+') if part.strip()]
    if not parts:
        raise ValueError('Hotkey is empty.')

    modifier_names = {
        'ctrl': ('Ctrl', MOD_CONTROL),
        'control': ('Ctrl', MOD_CONTROL),
        'alt': ('Alt', MOD_ALT),
        'shift': ('Shift', MOD_SHIFT),
    }
    chosen: dict[str, int] = {}
    key_name = None
    virtual_key = None
    for part in parts:
        normalized = part.casefold()
        modifier = modifier_names.get(normalized)
        if modifier is not None:
            label, flag = modifier
            if label in chosen:
                raise ValueError(f'{label} bir dəfə yazılmalıdır.')
            chosen[label] = flag
            continue
        if key_name is not None:
            raise ValueError('Yalnız bir əsas düymə seçin.')
        upper = part.upper()
        if len(upper) == 1 and upper in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789':
            key_name = upper
            virtual_key = ord(upper)
        elif upper.startswith('F') and upper[1:].isdigit():
            number = int(upper[1:])
            if not 1 <= number <= 11:
                raise ValueError('F1–F11 düymələrindən birini seçin.')
            key_name = f'F{number}'
            virtual_key = 0x70 + number - 1
        else:
            raise ValueError('Dəstəklənməyən hotkey düyməsidir.')
    if key_name is None or virtual_key is None:
        raise ValueError('Hotkey üçün əsas düymə seçilməyib.')

    ordered_labels = [name for name in ('Ctrl', 'Alt', 'Shift') if name in chosen]
    modifiers = MOD_NOREPEAT
    for flag in chosen.values():
        modifiers |= flag
    return modifiers, virtual_key, '+'.join((*ordered_labels, key_name))


def paste_allowed(original: int, foreground: int, modifiers_down: bool) -> bool:
    return bool(original and original == foreground and not modifiers_down)


class WindowsDesktop:
    HOTKEY_ID = 0x5654

    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('Windows 10/11 tələb olunur.')
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        self.user.RegisterHotKey.restype = w.BOOL
        self.user.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        self.user.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
        self.kernel.CreateMutexW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.mutex = None
        self.registered = False
        self._hotkeys = queue.SimpleQueue()
        self._hotkey_stop = threading.Event()
        self._hotkey_thread = None
        self._hotkey_lock = threading.Lock()
        self.shortcut_label = 'F8'

    def acquire(self) -> bool:
        ctypes.set_last_error(0)
        self.mutex = self.kernel.CreateMutexW(None, False, 'Local\\VoiceTyping.Windows.v1')
        error = ctypes.get_last_error()
        if not self.mutex:
            raise ctypes.WinError(error)
        if error == 183:
            self.close()
            return False
        return True

    def _stop_hotkey_listener(self):
        self._hotkey_stop.set()
        thread = self._hotkey_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._hotkey_thread = None

    def register_hotkey(self, shortcut: str = 'F8'):
        modifiers, virtual_key, label = parse_hotkey(shortcut)
        with self._hotkey_lock:
            self._stop_hotkey_listener()
            self._hotkey_stop = threading.Event()
            while self.poll_hotkey():
                pass
        ready = threading.Event()

        def listen():
            # This thread owns the Win32 queue. Tk must never consume WM_HOTKEY.
            self.registered = bool(
                self.user.RegisterHotKey(
                    None, self.HOTKEY_ID, modifiers, virtual_key
                )
            )
            ready.set()
            if not self.registered:
                return
            try:
                msg = w.MSG()
                while not self._hotkey_stop.is_set():
                    while self.user.PeekMessageW(ctypes.byref(msg), None, 0x312, 0x312, 1):
                        if msg.wParam == self.HOTKEY_ID:
                            self._hotkeys.put(True)
                    self._hotkey_stop.wait(0.02)
            finally:
                self.user.UnregisterHotKey(None, self.HOTKEY_ID)
                self.registered = False
        self._hotkey_thread = threading.Thread(target=listen, name='windows-hotkey', daemon=True)
        self._hotkey_thread.start()
        if not ready.wait(3) or not self.registered:
            self._stop_hotkey_listener()
            raise OSError(
                f'{label} başqa proqram tərəfindən istifadə olunur. '
                'Ekrandakı düymədən istifadə edin.'
            )
        self.shortcut_label = label

    def poll_hotkey(self) -> bool:
        try:
            return self._hotkeys.get_nowait()
        except queue.Empty:
            return False

    def external_foreground(self) -> int:
        window = self.user.GetForegroundWindow()
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        return int(window or 0) if pid.value != os.getpid() else 0

    def paste(self, target: int) -> bool:
        modifiers = any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))
        if not paste_allowed(target, self.external_foreground(), modifiers):
            return False
        ulong_ptr = ctypes.c_size_t

        class Mouse(ctypes.Structure):
            _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD),
                        ('dwFlags', w.DWORD), ('time', w.DWORD), ('dwExtraInfo', ulong_ptr)]

        class Keyboard(ctypes.Structure):
            _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD),
                        ('time', w.DWORD), ('dwExtraInfo', ulong_ptr)]

        class Payload(ctypes.Union):
            _fields_ = [('mi', Mouse), ('ki', Keyboard)]

        class Input(ctypes.Structure):
            _anonymous_ = ('payload',)
            _fields_ = [('type', w.DWORD), ('payload', Payload)]

        events = (Input * 4)()
        for event, (key, flags) in zip(events, ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2))):
            event.type = 1
            event.ki = Keyboard(key, 0, flags, 0, 0)
        self.user.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = w.UINT
        sent = self.user.SendInput(4, events, ctypes.sizeof(Input))
        if sent != 4:
            # Release only keys this call pressed and did not already release.
            pressed = []
            sequence = ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2))
            for key, flags in sequence[:max(0, min(int(sent), len(sequence)))]:
                if flags == 0:
                    pressed.append(key)
                elif key in pressed:
                    pressed.remove(key)
            releases = (Input * len(pressed))()
            for event, key in zip(releases, reversed(pressed)):
                event.type = 1
                event.ki = Keyboard(key, 0, 2, 0, 0)
            if pressed:
                self.user.SendInput(len(pressed), releases, ctypes.sizeof(Input))
        return sent == 4

    def close(self):
        with self._hotkey_lock:
            self._stop_hotkey_listener()
        if self.mutex:
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None


def credential_store():
    # Select the Windows backend explicitly; never fall back to plaintext files.
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def load_key() -> str:
    return credential_store().get_password('VoiceTyping.Windows', 'GeminiAPI') or ''


def save_key(value: str):
    credential_store().set_password('VoiceTyping.Windows', 'GeminiAPI', value)


def startup_command() -> str:
    if getattr(sys, 'frozen', False):
        return subprocess.list2cmdline([sys.executable, '--hidden'])
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    return subprocess.list2cmdline(
        [str(pythonw), '-m', 'voicetyping.windows.app', '--hidden']
    )


def set_startup(enabled: bool):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if enabled:
            winreg.SetValueEx(key, 'VoiceTyping', 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, 'VoiceTyping')
            except FileNotFoundError:
                pass


def startup_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            value, _ = winreg.QueryValueEx(key, 'VoiceTyping')
            return value == startup_command()
    except FileNotFoundError:
        return False
