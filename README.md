# VoiceTyping (Linux Native)

VoiceTyping is a lightweight, native Linux speech-to-text utility powered by **Google Gemini**. Simply press `F8` to start recording your voice, press it again to stop, and the transcribed text will be automatically copied to your clipboard so you can paste it anywhere.

It features a native floating overlay panel for real-time status updates and integrates seamlessly with the Linux system tray.

---

## Features

- 🎙️ **Global Hotkey Toggle**: Press `F8` from any application to start/stop recording.
- 💬 **Real-time Status Overlay**: A native, semi-transparent Tkinter overlay panel that displays recording, transcribing, and success states.
- ⚡ **Powered by Gemini**: Uses the fast and efficient `gemini-3.1-flash-lite` model for high-accuracy transcribing.
- 🎨 **System Tray Integration**: Quietly runs in the system tray with options to change the Gemini API Key or quit.
- ⚙️ **Automatic GNOME Shortcut Configuration**: Automatically registers the `F8` keyboard shortcut on GNOME environments.
- 🔄 **Wayland Support**: Includes a `--toggle` signal flag specifically to facilitate window manager shortcuts (especially useful on Wayland).

---

## Requirements

### System Packages
The application depends on system clipboard utilities, portaudio (for recording), and Tkinter.

- **Ubuntu / Debian**:
  ```bash
  sudo apt update
  sudo apt install -y xclip wl-clipboard libportaudio2 python3-tk libappindicator3-1
  ```
- **Fedora / RHEL**:
  ```bash
  sudo dnf install -y wl-clipboard xclip portaudio python3-tkinter libappindicator-gtk3 libayatana-appindicator-gtk3
  ```

### Python Libraries
Dependencies will be automatically installed via `install.sh`:
- `google-genai` (Official Google GenAI SDK)
- `sounddevice` & `soundfile` & `numpy` (For high-quality audio recording)
- `pyperclip` (For clipboard integration)
- `pynput` (For global hotkey detection)
- `pystray` & `pillow` (For system tray icon)

---

## Installation

1. Clone or copy this repository to your computer.
2. Open a terminal in the project directory.
3. Run the installer script:
   ```bash
   chmod +x install.sh
   ./install.sh
   ```
4. Launch **VoiceTyping** from your desktop applications menu, or search for it in your application launcher.

---

## Configuration

### Gemini API Key
When you run VoiceTyping for the first time, it will prompt you for your Gemini API key via a native dialog.
- You can get a free API Key from [Google AI Studio](https://aistudio.google.com/).
- The key is stored locally on your computer in `~/.config/voicetyping/config.json`.
- You can change the API key at any time by right-clicking the tray icon and selecting **Settings (API Key)**.

---

## How It Works Under the Hood

1. **Recording**: When `F8` is pressed, `sounddevice` captures audio at a sample rate of 16kHz.
2. **Overlay Display**: Tkinter shows a floating panel positioned at the top-center of the screen.
3. **Transcription**: The WAV audio is sent to the Gemini API using the `google-genai` SDK.
4. **Action**: The text is returned and placed in your system clipboard via `pyperclip`.

---

## License

This project is open source and available under the [MIT License](LICENSE).
