# VoiceTyping

VoiceTyping is a Linux desktop voice-to-text utility. Press `F8` to start recording and press it again to stop. The audio is sent to Google's Gemini API for transcription; the returned text is copied to the system clipboard. You then paste it manually into the application you are using.

Current support is Linux only. GNOME custom shortcuts are configured automatically when available. Other desktop environments can use the tray and in-app `F8` listener while VoiceTyping is running, or configure a desktop shortcut to run the installed `voicetyping_toggle.py` helper.

## How it works and limits

- Audio is captured from the default microphone as mono 16 kHz audio, then sent to Gemini. The configured primary transcription model is `gemini-3.5-transcribe`, with automatic fallback to `gemini-2.5-flash` and `gemini-3.5-flash` for high-volume quota resilience.
- Transcription requires an internet connection and a Google Gemini API key. Audio leaves the computer for processing; the app does not keep an audio archive.
- VoiceTyping copies text to the clipboard. It does not type or paste the result into the focused window. Use `Ctrl+V` (or your desktop's paste shortcut).
- The app records until you press `F8` again and caps recordings at seven minutes to remain below Gemini's inline-request size limit. Recordings shorter than 0.25 seconds and recordings detected as silence are rejected before transcription. There is no streaming transcription, so long recordings take longer to upload and process.
- F8 toggles recording only while the application is running. On GNOME, the installed custom shortcut sends the toggle signal to the running app. Launch VoiceTyping normally from the Applications menu first.
- Clipboard support depends on the desktop session and clipboard tools (`wl-clipboard` on Wayland and/or `xclip` on X11). Microphone recording requires PortAudio and a working input device.

## Install on Linux

Python 3.10 or newer is required. Install the system packages for your distribution:

```bash
# Fedora
sudo dnf install wl-clipboard xclip portaudio python3-tkinter \
  libappindicator-gtk3 libayatana-appindicator-gtk3

# Debian / Ubuntu
sudo apt update
sudo apt install xclip wl-clipboard libportaudio2 python3-tk libappindicator3-1
```

Then run the installer as your regular user (do not run it with `sudo`):

```bash
chmod +x install.sh
./install.sh
```

The installer installs the Python dependencies from `requirements.txt` into your user environment, copies the app under `~/.local/bin/voicetyping`, creates an Applications menu entry and autostart entry, and attempts GNOME shortcut setup. If the configured key is already assigned to another GNOME custom shortcut, setup leaves that shortcut alone and reports the collision.

## First run and configuration

Launch **VoiceTyping** from the Applications menu. On first run, enter a Gemini API key from [Google AI Studio](https://aistudio.google.com/). The key is stored as plain text in `~/.config/voicetyping/config.json` with owner-only permissions (`0600`); protect access to your local account and do not share that file. Anyone who can read it can use the key and may incur API charges. You can instead supply `GEMINI_API_KEY` in the environment. Replace a stored key through the tray menu's **Settings (API Key)** item.

Typical use:

1. Launch VoiceTyping and allow microphone access if your desktop requests it.
2. Press `F8`, speak, then press `F8` again.
3. Wait for the completion status, switch to the destination app, and paste with `Ctrl+V`.

An empty or unsupported recording, unavailable microphone, API/network error, or clipboard failure can prevent text from being copied. The app displays an error status and writes diagnostic details to the terminal from which it was launched.

## Development

Python dependencies are listed in `requirements.txt`. The application uses Tkinter, a microphone/audio input library, the Google GenAI SDK, a global keyboard listener, and a system tray icon. Linux system packages above provide Tkinter, PortAudio, clipboard integration, and tray support.

```bash
python3 -m pip install --user -r requirements.txt
python3 voicetyping.py
python3 -m unittest discover -s tests -v
```

## License

This project is open source and available under the [MIT License](LICENSE).
