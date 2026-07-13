"""
VoiceTyping (Linux Native) — Speech to Text Converter
========================================================
Press F8 to start speaking, press it again to stop,
It transcribes via Gemini and pastes the text wherever your cursor is.

Linux features:
- Floating Tkinter panel overlay.
- Uses Shift+Insert and Ctrl+V for pasting.
- Includes a --toggle signal for Wayland support.

Requirements:
  - wl-clipboard, xclip, portaudio, python3-tkinter, libappindicator-gtk3
  - Python libraries: google-genai, sounddevice, soundfile, numpy, pyperclip, pynput, pystray, pillow
"""

import os
import sys
import time
import threading
import io
import signal
import json
import tkinter as tk
from tkinter import simpledialog
import subprocess

PID_FILE = "/tmp/voicetyping.pid"

if len(sys.argv) > 1 and sys.argv[1] == "--toggle":
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGUSR1)
            print("Signal sent (Toggle)")
        except ProcessLookupError:
            print("Background program not found.")
    else:
        print("Program is not running. Please start it normally first.")
    sys.exit(0)
else:
    # Single instance check: Prevent opening 2 instances
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                pid_str = f.read().strip()
                if pid_str:
                    pid = int(pid_str)
                    os.kill(pid, 0) # If no error, process is alive
                    print("Program is already running in the background! Not opening a new instance.")
                    sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass # Process is dead or PID file is corrupted, continue

# Write our PID to file
with open(PID_FILE, "w") as f:
    f.write(str(os.getpid()))

from google import genai
from google.genai import types
import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
from pynput import keyboard as pynput_keyboard

# ─── API Key (Local Storage and Prompt) ─────────────────────────────────
CONFIG_DIR = os.path.expanduser("~/.config/voicetyping")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

def get_api_key(force=False):
    # 1. If config file exists, read from it
    if not force and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                if config.get("api_key"):
                    return config["api_key"]
        except Exception:
            pass
            
    # 2. If it doesn't exist, prompt the user
    # Using "zenity" for a modern native look on Linux
    try:
        result = subprocess.run([
            "zenity", "--entry",
            "--title=VoiceTyping - Setup",
            "--text=Please enter your Gemini API key to activate VoiceTyping:\n(The key will only be stored locally on your computer)",
            "--width=450"
        ], capture_output=True, text=True)
        
        if result.returncode == 0 and result.stdout.strip():
            api_key = result.stdout.strip()
        else:
            if force: return None
            print("❌ API key was not entered. Program closing.")
            sys.exit(1)
            
    except FileNotFoundError:
        # If Zenity is not found, fallback to Tkinter dialog
        root = tk.Tk()
        root.withdraw()
        api_key = simpledialog.askstring(
            "VoiceTyping - API Key",
            "Please enter your Gemini API key:\n(It will only be stored locally on your computer)",
            parent=root
        )
        root.destroy()
        
        if not api_key or not api_key.strip():
            if force: return None
            print("❌ API key was not entered. Program closing.")
            sys.exit(1)
        api_key = api_key.strip()
    
    # 3. Save the entered key to the config file
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_key": api_key}, f)
        
    return api_key

API_KEY = get_api_key()
client = genai.Client(api_key=API_KEY)


# ═════════════════════════════════════════════════════════════════════
# FLOATING NOTIFICATION PANEL (Tkinter)
# ═════════════════════════════════════════════════════════════════════

class NotificationPanel:
    def __init__(self):
        self.root = None
        self.label = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._create_panel, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)

    def _create_panel(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#1e1e2e")

        self.label = tk.Label(
            self.root,
            text="🚀 Ready — Press F8",
            font=("sans-serif", 13, "bold"),
            fg="#cdd6f4",
            bg="#1e1e2e",
            padx=20,
            pady=8,
        )
        self.label.pack()

        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        panel_width = self.root.winfo_reqwidth()
        x_center = (screen_width - panel_width) // 2
        self.root.geometry(f"+{x_center}+10")
        self.root.withdraw()
        self._ready.set()
        self.root.mainloop()

    def show(self, text, color="#cdd6f4", bg_color="#1e1e2e", hide_after=0):
        if self.root is None:
            return

        def _refresh():
            self.label.config(text=text, fg=color, bg=bg_color)
            self.root.configure(bg=bg_color)

            self.root.update_idletasks()
            screen_width = self.root.winfo_screenwidth()
            panel_width = self.root.winfo_reqwidth()
            x_center = (screen_width - panel_width) // 2
            self.root.geometry(f"+{x_center}+10")
            self.root.deiconify()
            if hide_after > 0:
                self.root.after(hide_after, self.root.withdraw)

        self.root.after(0, _refresh)

    def hide(self):
        if self.root is None:
            return
        self.root.after(0, self.root.withdraw)


panel = NotificationPanel()

# ─── Global Variables ──────────────────────────────────────────────
is_recording = False
recordings = []
stream = None

def microphone_callback(indata, frames, time_info, status):
    if status:
        print(f"⚠️ Microphone warning: {status}")
    recordings.append(indata.copy())

def start_recording():
    global is_recording, recordings, stream
    recordings = []

    # Update UI instantly (no lag)
    panel.show(
        "🎙️  Recording...  (F8 — Stop)",
        color="#f38ba8",
        bg_color="#302030",
    )
    print("🎙️  Recording... (Press F8 again to stop)")

    try:
        stream = sd.InputStream(
            samplerate=16000,
            channels=1,
            dtype="int16",
            callback=microphone_callback,
        )
        stream.start()
        is_recording = True
    except Exception as e:
        print(f"❌ Could not open microphone: {e}")
        panel.show("❌ Microphone not found", color="#f38ba8", bg_color="#302020", hide_after=3000)

def stop_and_transcribe():
    global is_recording, stream

    # Switch UI to "Wait" instantly
    panel.show(
        "⏳  Transcribing... please wait",
        color="#f9e2af",
        bg_color="#2d2a20",
    )
    print("🛑 Recording stopped. Transcribing...")

    if stream is not None:
        stream.stop()
        stream.close()
        stream = None
    is_recording = False

    try:
        if not recordings:
            panel.show(
                "⚠️  No audio was recorded",
                color="#fab387",
                bg_color="#2d2520",
                hide_after=3000,
            )
            print("⚠️  No audio was recorded.\n")
            return

        audio_data = np.concatenate(recordings, axis=0)
        wav_io = io.BytesIO()
        sf.write(wav_io, audio_data, 16000, format='WAV')
        audio_bytes = wav_io.getvalue()

        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")

        instruction = (
            "Transcribe this audio exactly into text. Keep the original language or default to English. "
            "Put correct punctuation based on speech. Do not add any extra comments, greetings, or explanations, "
            "return ONLY the transcribed text."
        )

        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=[instruction, audio_part]
        )

        text = response.text.strip()
        if text:
            pyperclip.copy(text)

            panel.show(
                f"✅  Copied!",
                color="#a6e3a1",
                bg_color="#1e2e1e",
                hide_after=4000,
            )
            print(f"✅ Copied to clipboard: {text}\n")
        else:
            panel.show(
                "⚠️  Audio not understood",
                color="#fab387",
                bg_color="#2d2520",
                hide_after=3000,
            )
            print("⚠️  Audio not understood or is empty.\n")

    except Exception as error:
        panel.show(
            f"❌  Error: {str(error)[:50]}",
            color="#f38ba8",
            bg_color="#302020",
            hide_after=5000,
        )
        print(f"❌ An error occurred: {error}\n")


def on_f8_pressed():
    global is_recording
    if not is_recording:
        start_recording()
    else:
        threading.Thread(target=stop_and_transcribe, daemon=True).start()

def signal_handler(sig, frame):
    on_f8_pressed()


signal.signal(signal.SIGUSR1, signal_handler)

print("=" * 50)
print("🚀 VoiceTyping (Linux) — System is ready and active!")
print("=" * 50)
print("➡️  Press 'F8' to start recording.")
print("➡️  Press 'F8' again when finished.")
print("🛑 Press 'Ctrl+C' to exit the program.\n")

panel.show(
    "🚀  Ready — Press F8",
    color="#a6e3a1",
    bg_color="#1e2e1e",
    hide_after=3000,
)

def on_press(key):
    try:
        if key == pynput_keyboard.Key.f8:
            on_f8_pressed()
    except AttributeError:
        pass

def create_tray_image():
    from PIL import Image, ImageDraw
    # Simple blue circular icon (represents Microphone)
    image = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    dc.ellipse((8, 8, 56, 56), fill=(137, 180, 250))
    return image

def setup_tray(listener):
    import pystray
    from pystray import MenuItem as item
    
    def on_change_api_key(icon, item):
        new_key = get_api_key(force=True)
        if new_key:
            global client
            client = genai.Client(api_key=new_key)
            subprocess.run(["notify-send", "VoiceTyping", "API key updated successfully!"])
            
    def on_quit(icon, item):
        icon.stop()
        listener.stop()

    menu = (
        item('Settings (API Key)', on_change_api_key),
        item('Quit', on_quit)
    )
    
    icon = pystray.Icon("voicetyping", create_tray_image(), "VoiceTyping", menu)
    icon.run()

listener = pynput_keyboard.Listener(on_press=on_press)
listener.start()

try:
    setup_tray(listener)
except KeyboardInterrupt:
    pass
finally:
    panel.hide()
    print("\n👋 Program stopped. Goodbye!")
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
