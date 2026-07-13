# VoiceTyping

VoiceTyping is a lightweight, native cross-platform speech-to-text utility powered by **Google Gemini**. Simply press a hotkey (like `F8`) to start recording your voice, press it again to stop, and the transcribed text will be automatically copied to your clipboard so you can paste it anywhere.

This repository supports both **Linux** and **Windows** environments.

---

## Features

- 🎙️ **Global Hotkey Toggle**: Press `F8` from any application to start/stop recording.
- 💬 **Real-time Status Overlay**: A native, semi-transparent overlay panel that displays recording, transcribing, and success states.
- ⚡ **Powered by Gemini**: Uses the fast and efficient `gemini-3.1-flash-lite` model for high-accuracy transcribing.
- 🎨 **System Tray Integration**: Quietly runs in the system tray with options to change the Gemini API Key or quit.
- ⚙️ **Automatic OS Integration**:
  - **Linux**: Automatically configures GNOME keyboard shortcuts and application launcher/autostart entries.
  - **Windows**: (Support files being integrated)

---

## Configuration

### Gemini API Key
When you run VoiceTyping for the first time on any system, it will prompt you for your Gemini API key via a native dialog.
- You can get a free API Key from [Google AI Studio](https://aistudio.google.com/).
- The key is stored locally on your computer in `~/.config/voicetyping/config.json` (on Linux) or the corresponding user config path (on Windows).
- You can change the API key at any time by right-clicking the tray icon and selecting **Settings (API Key)**.
  - **Note for GNOME users**: If your desktop environment hides system tray icons, you can also easily change your API key at any time by running the application from the terminal with the `--settings` argument (e.g. `voicetyping --settings` or `python voicetyping.py --settings`).

---

## Platform-Specific Setup

### 🐧 Linux Setup

#### 1. System Packages
The Linux version depends on system clipboard utilities, portaudio (for recording), and Tkinter.

- **Ubuntu / Debian**:
  ```bash
  sudo apt update
  sudo apt install -y xclip wl-clipboard libportaudio2 python3-tk libappindicator3-1
  ```
- **Fedora / RHEL**:
  ```bash
  sudo dnf install -y wl-clipboard xclip portaudio python3-tkinter libappindicator-gtk3 libayatana-appindicator-gtk3
  ```

#### 2. Installation
Run the installer script:
```bash
chmod +x install.sh
./install.sh
```

---

### 🪟 Windows Setup
*(Windows integration files will be uploaded soon)*

The Windows version leverages native Windows APIs and does not require Linux-specific clipboard or audio utilities.

1. Ensure Python 3.x is installed and added to your `PATH`.
2. Install the required Python libraries.
3. Run the Windows entry point script.

---

## License

This project is open source and available under the [MIT License](LICENSE).
