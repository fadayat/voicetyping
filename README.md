# VoiceTyping

## Version 2.1.0

VoiceTyping is a Linux desktop voice-to-text utility. Press `F8` to start recording
and live transcription, and press it again to stop. Audio streams to Google's
Gemini API while you speak. After the second `F8`, the finalized transcript is
copied to the system clipboard and pasted into the currently focused field.

Current support is Linux only. GNOME custom shortcuts are configured automatically when available. Other desktop environments can use the tray and in-app `F8` listener while VoiceTyping is running, or configure a desktop shortcut to run the installed `voicetyping_toggle.py` helper.

## How it works and limits

- Audio is captured from the default microphone as mono 16 kHz PCM and streamed to
  `gemini-3.5-transcribe-live` during an F8 recording. Each recording owns its own
  connection; no microphone audio is sent while idle. An Azerbaijani language hint
  is used by default.
- If Live is unavailable, the connection breaks, audio buffering overflows, or
  finalization exceeds three seconds, the complete in-memory recording is sent
  through the existing `gemini-3.5-transcribe`/Flash fallback path. Partial Live text
  is never copied as a final result. Failed Live attempts have a 60-second cooldown.
- Transcription requires an internet connection and a Google Gemini API key. Audio leaves the computer for processing; the app does not keep an audio archive.
- After a successful transcription, VoiceTyping copies the final text and sends
  one paste shortcut to the currently focused field. On GNOME Wayland, the app
  identifies a standalone terminal from accessibility window-state metadata and
  its process ID, without reading terminal text or window titles.
  Recognized terminals receive `Ctrl+Shift+V`; ordinary fields receive `Ctrl+V`.
  Unknown terminals, terminal applications embedded in IDEs, and custom terminal
  setups may still need their manual paste shortcut. If automatic paste is not
  supported or fails, the copied text remains available for manual pasting.
  VoiceTyping sends no Enter key. Pasting multiline text into a terminal may
  still run commands depending on the shell and terminal settings.
- GNOME Wayland automatic paste uses a temporary local Mutter input session. It
  does not capture the screen, need root access, or start another daemon. This
  uses a private desktop API, so GNOME upgrades can change compatibility. There
  is no fixed 0.2-second wait before pasting.
- The app records until you press `F8` again and caps recordings at seven minutes.
  Recordings shorter than 0.25 seconds and recordings detected as silence are
  rejected before copying or batch fallback. The Live service may already have
  received such audio during the user-initiated recording.
- F8 toggles recording only while the application is running. On GNOME, the installed custom shortcut sends the toggle signal to the running app. Launch VoiceTyping normally from the Applications menu first.
- Clipboard support depends on the desktop session and clipboard tools (`wl-clipboard` on Wayland and/or `xclip` on X11). Microphone recording requires PortAudio and a working input device.

## Install on Linux

Python 3.10 or newer is required. Install the system packages for your distribution:

```bash
# Fedora
sudo dnf install wl-clipboard xclip portaudio python3-tkinter \
  python3-gobject python3-cairo gtk3 libappindicator-gtk3 libayatana-appindicator-gtk3

# Debian / Ubuntu
sudo apt update
sudo apt install xclip wl-clipboard libportaudio2 python3-tk python3-gi \
  python3-gi-cairo gir1.2-gtk-3.0 libappindicator3-1
```

Clone the repository, then run the installer as your regular user (do not run it
with `sudo`):

```bash
git clone https://github.com/fadayat/voicetyping.git
cd voicetyping
chmod +x install.sh
./install.sh
```

The installer installs the Python dependencies from `requirements.txt` into your user environment, copies the app under `~/.local/bin/voicetyping`, creates an Applications menu entry and autostart entry, and attempts GNOME shortcut setup. If the configured key is already assigned to another GNOME custom shortcut, setup leaves that shortcut alone and reports the collision.

## First run and configuration

Launch **VoiceTyping** from the Applications menu. On first run, enter a Gemini API key from [Google AI Studio](https://aistudio.google.com/). The key is stored as plain text in `~/.config/voicetyping/config.json` with owner-only permissions (`0600`); protect access to your local account and do not share that file. Anyone who can read it can use the key and may incur API charges. You can instead supply `GEMINI_API_KEY` in the environment. Replace a stored key through the tray menu's **Settings (API Key)** item.

No API key is bundled with this repository. Each user supplies their own key;
available models, free-tier quotas, and billing depend on that user's Google
project. Configuration, environment files, recordings, and logs must remain local.

Typical use:

1. Launch VoiceTyping and focus the destination text field.
2. Press `F8`, speak, then press `F8` again. Allow microphone access if requested.
3. Keep the destination focused until the finalized text is copied and pasted.
   The app targets the field focused when the result arrives, not a saved window.
   If automatic paste is unavailable, use the destination's manual paste shortcut.

The screen shows a transparent round animation: it grows with your voice, shrinks
during silence, flows while processing, then contracts and disappears on completion.
It shows no text or desktop notifications and leaves keyboard focus in your editor.
A warm-colored orb indicates a warning or error; diagnostic details remain in the
terminal or user service journal.

Live transcription and automatic paste are enabled by default. To opt out of
automatic paste, add `"auto_paste": false` to the existing configuration JSON
without replacing the API key or other settings. To opt out of Live transcription,
add `"live_transcription": false`. `"language_codes": []` selects automatic
language detection instead of the default Azerbaijani hint. Restart the application
after changing these options.
The service logs the selected route, audio duration, and stop-to-copy milliseconds;
it does not log audio, transcript text, or API keys.

## Development

Python dependencies are listed in `requirements.txt`. The application uses Tkinter
for the API-key dialog, GTK3/Cairo for its transparent animation, a microphone/audio
input library, the Google GenAI SDK, a global keyboard listener, and a system tray
icon. The animation needs a composited desktop; GNOME Wayland uses XWayland for the
positioned, click-through overlay. GTK's animation preference reduces its motion.

```bash
python3 -m pip install --user -r requirements.txt
python3 voicetyping.py
python3 -m unittest discover -s tests -v
```

## License

This project is open source and available under the [MIT License](LICENSE).
