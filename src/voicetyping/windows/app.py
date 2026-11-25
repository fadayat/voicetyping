"""VoiceTyping Windows desktop entry point. No credentials or transcripts logged."""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .engine import VoiceTypingApp, create_client, create_live_transcriber
from .settings import DEFAULT_SETTINGS, load_settings, save_settings
from .platform import (
    WindowsDesktop,
    load_key,
    parse_hotkey,
    save_key,
    set_startup,
    startup_enabled,
)
from .selftest import main as self_test_main
from .gemini_smoke import main as gemini_smoke_main


class StatusPanel:
    def __init__(self, events):
        self.events = events
        self.level = 0.0
        self.closed = False

    def show(self, text, **kwargs):
        if not self.closed:
            self.events.put(('status', text))

    def feed_audio(self, rms, peak, envelope):
        self.level = rms

    def close(self):
        self.closed = True


class DesktopApp:
    def __init__(self, root, desktop):
        self.root, self.desktop = root, desktop
        self.events = queue.Queue()
        self.panel = StatusPanel(self.events)
        self.engine = None
        self._active_api_key = ''
        self.tray = None
        self.target = 0
        self.closed = False
        self.last_rendered = None
        self._poll_count = 0
        self.preferences = load_settings()
        try:
            _, _, configured_shortcut = parse_hotkey(self.preferences['shortcut'])
        except (TypeError, ValueError):
            configured_shortcut = DEFAULT_SETTINGS['shortcut']
            self.preferences['shortcut'] = configured_shortcut
        root.title('VoiceTyping')
        scale = max(1.0, float(root.tk.call('tk', 'scaling')) / (96 / 72))
        width = min(int(660 * scale), root.winfo_screenwidth() - 80)
        height = min(int(760 * scale), root.winfo_screenheight() - 120)
        root.geometry(f'{width}x{height}')
        root.minsize(min(width, int(540 * scale)), min(height, int(520 * scale)))
        root.protocol('WM_DELETE_WINDOW', self.hide)
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Title.TLabel', font=('Segoe UI', 22, 'bold'))
        style.configure('TLabel', font=('Segoe UI', 11))
        style.configure('TButton', padding=(12, 8))
        # Scroll the form at high DPI or on short displays; settings and exit
        # must remain reachable even when six transcript lines cannot fit.
        surface = ttk.Frame(root)
        surface.pack(fill='both', expand=True)
        canvas = tk.Canvas(surface, highlightthickness=0)
        scroll = ttk.Scrollbar(surface, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        main = ttk.Frame(canvas, padding=24)
        content = canvas.create_window((0, 0), window=main, anchor='nw')
        main.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        def resize(event):
            canvas.itemconfigure(content, width=event.width)
            for widget in main.winfo_children():
                if widget.winfo_class() == 'TLabel':
                    widget.configure(wraplength=max(200, event.width - 48))
        canvas.bind('<Configure>', resize)
        ttk.Label(main, text='Danışın, mətnə çevrilsin.', style='Title.TLabel').pack(anchor='w')
        self.shortcut_help = tk.StringVar(
            value=f'{configured_shortcut} ilə başlayın. Bitirəndə yenidən {configured_shortcut} basın.'
        )
        ttk.Label(main, textvariable=self.shortcut_help, wraplength=550).pack(anchor='w', pady=(6, 20))
        self.status = tk.StringVar(value='Başlamaq üçün Gemini API açarını daxil edin.')
        ttk.Label(main, textvariable=self.status, wraplength=550).pack(anchor='w')
        self.meter = ttk.Progressbar(main, maximum=1)
        self.meter.pack(fill='x', pady=(8, 12))
        self.toggle_button = ttk.Button(main, text='Səs yazmağa başla', command=self.toggle)
        self.toggle_button.pack(anchor='w')
        ttk.Label(main, text='Son mətn').pack(anchor='w', pady=(20, 6))
        text_frame = ttk.Frame(main)
        text_frame.pack(fill='both', expand=True)
        self.transcript = tk.Text(text_frame, height=6, wrap='word', font=('Segoe UI', 11), state='disabled')
        scrollbar = ttk.Scrollbar(text_frame, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.transcript.pack(side='left', fill='both', expand=True)
        self.copy_button = ttk.Button(main, text='Mətni kopyala', command=self.copy, state='disabled')
        self.copy_button.pack(anchor='w', pady=(8, 16))
        recovery = ttk.LabelFrame(main, text='Bərpa', padding=10)
        recovery.pack(fill='x', pady=(0, 16))
        self.recovery_status = tk.StringVar(
            value='Uğursuz transkripsiyaların səsi lokal saxlanır.'
        )
        ttk.Label(recovery, textvariable=self.recovery_status).pack(anchor='w')
        recovery_actions = ttk.Frame(recovery)
        recovery_actions.pack(fill='x', pady=(8, 0))
        ttk.Button(
            recovery_actions,
            text='Yenidən yoxla',
            command=self.retry_recovery,
        ).pack(side='left')
        ttk.Button(
            recovery_actions,
            text='Bərpa mətnini kopyala',
            command=self.copy_recovered,
        ).pack(side='left', padx=(8, 0))
        ttk.Button(
            recovery_actions,
            text='Qovluğu aç',
            command=self.open_recovery,
        ).pack(side='right')
        ttk.Separator(main).pack(fill='x')
        ttk.Label(main, text='Gemini API açarı').pack(anchor='w', pady=(16, 4))
        key_row = ttk.Frame(main)
        key_row.pack(fill='x')
        self.key = ttk.Entry(key_row, show='*')
        self.key.pack(side='left', fill='x', expand=True)
        ttk.Button(key_row, text='Yadda saxla', command=self.configure_key).pack(side='left', padx=(8, 0))
        self.auto_paste = tk.BooleanVar(value=self.preferences['auto_paste'])
        ttk.Checkbutton(
            main,
            text='Aktiv mətn sahəsinə avtomatik yapışdır',
            variable=self.auto_paste,
            command=self.change_preferences,
        ).pack(anchor='w', pady=(12, 0))
        self.live_transcription = tk.BooleanVar(
            value=self.preferences['live_transcription']
        )
        ttk.Checkbutton(
            main,
            text='Sürətli Live transkripsiyanı istifadə et',
            variable=self.live_transcription,
            command=self.change_preferences,
        ).pack(anchor='w', pady=(4, 0))
        ttk.Label(main, text='Qlobal qısayol').pack(anchor='w', pady=(12, 4))
        shortcut_row = ttk.Frame(main)
        shortcut_row.pack(fill='x')
        self.shortcut = ttk.Entry(shortcut_row)
        self.shortcut.insert(0, configured_shortcut)
        self.shortcut.pack(side='left', fill='x', expand=True)
        ttk.Button(
            shortcut_row, text='Tətbiq et', command=self.change_shortcut
        ).pack(side='left', padx=(8, 0))
        self.startup = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(main, text='Windows açıldıqda başlat', variable=self.startup, command=self.change_startup).pack(anchor='w', pady=(4, 10))
        ttk.Label(
            main,
            text=(
                'Səs mətnə çevrilmək üçün Google-a göndərilir. '
                'Uğursuz yazılar lokal Bərpa qovluğunda qalır.\n'
                'Hər istifadəçi öz API açarından və kvotasından istifadə edir.'
            ),
            wraplength=550,
        ).pack(anchor='w')
        footer = ttk.Frame(main)
        footer.pack(fill='x', pady=(12, 0))
        ttk.Button(footer, text='Arxa plana keçir', command=self.hide).pack(side='left')
        ttk.Button(footer, text='Çıxış', command=self.quit).pack(side='right')
        self.paste_enabled = True
        try:
            stored = load_key()
            if stored:
                self.start_engine(stored)
        except Exception:
            self.status.set('Saxlanmış açar oxunmadı. Açarı yenidən daxil edin.')
        try:
            desktop.register_hotkey(configured_shortcut)
        except OSError as error:
            messagebox.showwarning('Qısayol əlçatan deyil', str(error), parent=root)
        self.setup_tray()
        root.after(50, self.poll)
        if '--hidden' in sys.argv and self.engine and self.tray:
            root.withdraw()

    def current_shortcut_label(self):
        try:
            return parse_hotkey(self.preferences['shortcut'])[2]
        except (TypeError, ValueError):
            return DEFAULT_SETTINGS['shortcut']

    def live_factory(self, api_key):
        if not self.live_transcription.get():
            return None
        language_codes = tuple(self.preferences['language_codes'])
        custom_vocabulary = tuple(self.preferences['custom_vocabulary'])
        return lambda: create_live_transcriber(
            api_key,
            language_codes=language_codes,
            custom_vocabulary=custom_vocabulary,
        )

    def start_engine(self, api_key):
        import pyperclip
        live_factory = self.live_factory(api_key)
        if self.engine:
            if not self.engine.update_client(
                api_key,
                live_factory=live_factory,
                recovery_client_factory=lambda: create_client(api_key),
            ):
                return False
        else:
            self.engine = VoiceTypingApp(create_client(api_key), self.panel,
                copy_action=pyperclip.copy,
                paste_action=lambda: self.paste_enabled and self.desktop.paste(self.target),
                live_factory=live_factory,
                language_codes=self.preferences['language_codes'],
                custom_vocabulary=self.preferences['custom_vocabulary'],
                shortcut_label=self.current_shortcut_label(),
                recovery_client_factory=lambda: create_client(api_key))
        self._active_api_key = api_key
        self.status.set(
            f'Hazırdır — {self.current_shortcut_label()} ilə başlaya bilərsiniz.'
        )
        self.engine.retry_saved_recordings()
        return True

    def change_preferences(self):
        self.preferences['auto_paste'] = bool(self.auto_paste.get())
        self.preferences['live_transcription'] = bool(self.live_transcription.get())
        try:
            save_settings(self.preferences)
        except OSError:
            self.status.set('Parametrlər saxlanmadı. Yenidən sınayın.')
            return
        if self.engine:
            self.engine.update_preferences(
                live_factory=(
                    self.live_factory(self._active_api_key)
                    if self._active_api_key
                    else None
                ),
                language_codes=self.preferences['language_codes'],
                custom_vocabulary=self.preferences['custom_vocabulary'],
                shortcut_label=self.current_shortcut_label(),
            )
        self.status.set('Parametrlər saxlanıldı.')

    def change_shortcut(self):
        candidate = self.shortcut.get().strip()
        try:
            _, _, label = parse_hotkey(candidate)
        except (TypeError, ValueError) as error:
            self.status.set(str(error))
            return
        previous = self.preferences['shortcut']
        try:
            self.desktop.register_hotkey(label)
        except OSError as error:
            try:
                self.desktop.register_hotkey(previous)
            except OSError:
                pass
            self.status.set(str(error))
            return
        self.preferences['shortcut'] = label
        try:
            save_settings(self.preferences)
        except OSError:
            self.status.set('Qısayol aktivdir, amma parametr saxlanmadı.')
        else:
            self.status.set(f'Qısayol dəyişdi: {label}.')
        self.shortcut.delete(0, 'end')
        self.shortcut.insert(0, label)
        self.shortcut_help.set(
            f'{label} ilə başlayın. Bitirəndə yenidən {label} basın.'
        )
        if self.tray:
            try:
                self.tray.title = f'VoiceTyping — {label}'
            except Exception:
                pass
        if self.engine:
            self.engine.shortcut_label = label

    def configure_key(self):
        value = self.key.get().strip()
        if not value:
            self.status.set('Gemini API açarını daxil edin.')
            return
        if self.engine and self.engine.state != 'idle':
            self.status.set('Açarı dəyişmək üçün səs yazısının bitməsini gözləyin.')
            return
        try:
            save_key(value)
            self.start_engine(value)
            self.key.delete(0, 'end')
        except Exception:
            self.status.set('Açar saxlanmadı. Windows Credential Manager-i yoxlayın.')

    def toggle(self):
        if not self.engine:
            self.show()
            self.status.set('Əvvəl Gemini API açarını daxil edin.')
            self.key.focus_set()
            return
        if self.engine.state == 'idle':
            self.target = self.desktop.external_foreground()
            self.paste_enabled = self.auto_paste.get()
        # Device startup and shutdown can block; they never run inside Tk's loop.
        threading.Thread(target=self.engine.toggle, daemon=True).start()

    def copy(self):
        if self.engine:
            threading.Thread(target=self.engine.copy_last_transcription, daemon=True).start()

    def retry_recovery(self):
        if self.engine:
            self.engine.retry_saved_recordings(notify=True)

    def copy_recovered(self):
        if self.engine:
            threading.Thread(
                target=self.engine.copy_recovered_transcriptions,
                daemon=True,
            ).start()

    def open_recovery(self):
        if self.engine:
            self.engine.open_recovery_folder()

    def refresh_recovery_status(self):
        if not self.engine:
            return
        pending = self.engine.pending_recovery_count()
        recovered = self.engine.recovered_text_count()
        if recovered:
            self.recovery_status.set(
                f'{recovered} bərpa edilmiş mətn kopyalanmağa hazırdır.'
            )
        elif pending:
            self.recovery_status.set(
                f'{pending} səs yazısı yenidən transkripsiya gözləyir.'
            )
        else:
            self.recovery_status.set('Bərpa gözləyən səs yazısı yoxdur.')

    def change_startup(self):
        try:
            set_startup(self.startup.get())
        except OSError:
            self.startup.set(not self.startup.get())
            self.status.set('Avtomatik başlanğıc dəyişdirilmədi. Yenidən sınayın.')

    def setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new('RGBA', (64, 64))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((24, 8, 40, 38), radius=8, fill='#12615b')
            draw.arc((16, 18, 48, 48), 0, 180, fill='#12615b', width=4)
            draw.line((32, 48, 32, 55), fill='#12615b', width=4)
            def post(action):
                return lambda icon, item: self.events.put((action, None))
            menu = pystray.Menu(
                pystray.MenuItem('VoiceTyping-i aç', post('open'), default=True),
                pystray.MenuItem('Səs yaz / dayandır', post('toggle')),
                pystray.MenuItem('Son mətni kopyala', post('copy')),
                pystray.MenuItem('Saxlanmış səsləri yenidən yoxla', post('retry')),
                pystray.MenuItem('Bərpa mətnini kopyala', post('copy_recovered')),
                pystray.MenuItem('Çıxış', post('quit')))
            self.tray = pystray.Icon(
                'VoiceTyping',
                image,
                f'VoiceTyping — {self.current_shortcut_label()}',
                menu,
            )
            self.tray.run_detached()
        except Exception:
            self.tray = None
            self.status.set('Tray açıla bilmədi. Tətbiqi bu pəncərədən idarə edin.')

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def poll(self):
        if self.closed:
            return
        if self.desktop.poll_hotkey():
            self.toggle()
        for _ in range(50):
            try:
                action, value = self.events.get_nowait()
            except queue.Empty:
                break
            if action == 'status':
                self.status.set(value)
            else:
                {
                    'open': self.show,
                    'toggle': self.toggle,
                    'copy': self.copy,
                    'retry': self.retry_recovery,
                    'copy_recovered': self.copy_recovered,
                    'quit': self.quit,
                }[action]()
                if self.closed:
                    return
        self.meter['value'] = self.panel.level if self.engine and self.engine.state == 'recording' else 0
        if self.engine:
            busy = self.engine.state in ('starting', 'processing', 'shutting_down')
            self.toggle_button.configure(state='disabled' if busy else 'normal',
                text='Səs yazısını dayandır' if self.engine.state == 'recording' else 'Səs yazmağa başla')
            text = self.engine.last_text
            if text and text != self.last_rendered:
                self.transcript.configure(state='normal')
                self.transcript.delete('1.0', 'end')
                self.transcript.insert('1.0', text)
                self.transcript.configure(state='disabled')
                self.copy_button.configure(state='normal')
                self.last_rendered = text
        self._poll_count += 1
        if self._poll_count % 20 == 0:
            self.refresh_recovery_status()
        self.root.after(50, self.poll)

    def quit(self):
        if self.closed:
            return
        self.closed = True
        self.desktop.close()
        if self.tray:
            self.tray.stop()
        # Mark shutdown before destroying UI; late requests cannot copy or paste.
        if self.engine:
            self.engine.shutdown()
        self.root.destroy()


def main():
    if '--self-test' in sys.argv[1:]:
        self_test_arguments = list(sys.argv[1:])
        self_test_arguments.remove('--self-test')
        return self_test_main(self_test_arguments)
    if '--gemini-smoke' in sys.argv[1:]:
        smoke_arguments = list(sys.argv[1:])
        smoke_arguments.remove('--gemini-smoke')
        return gemini_smoke_main(smoke_arguments)
    if sys.platform != 'win32':
        print('VoiceTyping Windows üçündür. Mövcud Linux tətbiqindən istifadə edin.')
        return 1
    root = tk.Tk()
    root.withdraw()
    desktop = WindowsDesktop()
    try:
        if not desktop.acquire():
            messagebox.showinfo('VoiceTyping', 'Tətbiq artıq işləyir. Sistem tray-indən aça bilərsiniz.', parent=root)
            return 0
        app = DesktopApp(root, desktop)
        if not ('--hidden' in sys.argv and app.engine and app.tray):
            root.deiconify()
        root.mainloop()
        return 0
    finally:
        desktop.close()


if __name__ == '__main__':
    raise SystemExit(main())
