#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/bin/voicetyping"
APPLICATIONS_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
PYTHON_BIN="$(command -v python3)"

echo "=================================================="
echo "🚀 Welcome to VoiceTyping Installer"
echo "=================================================="

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "❌ VoiceTyping requires Python 3.10 or newer." >&2
    exit 1
fi

echo "📦 Install the required system packages if they are missing:"
if command -v dnf >/dev/null 2>&1; then
    echo "  Fedora: wl-clipboard xclip portaudio python3-tkinter libappindicator-gtk3 libayatana-appindicator-gtk3"
elif command -v apt >/dev/null 2>&1; then
    echo "  Debian/Ubuntu: xclip wl-clipboard libportaudio2 python3-tk libappindicator3-1"
else
    echo "  Install clipboard tools, PortAudio, and Tkinter using your distribution's package manager."
fi

echo "🐍 Installing Python libraries..."
"$PYTHON_BIN" -m pip install --user -r "$SCRIPT_DIR/requirements.txt"

echo "📂 Placing files into $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR" "$APPLICATIONS_DIR" "$AUTOSTART_DIR"
install -m 0644 "$SCRIPT_DIR/voicetyping.py" "$INSTALL_DIR/voicetyping.py"
install -m 0644 "$SCRIPT_DIR/voicetyping_toggle.py" "$INSTALL_DIR/voicetyping_toggle.py"
install -m 0644 "$SCRIPT_DIR/setup_shortcut.py" "$INSTALL_DIR/setup_shortcut.py"

desktop_quote() {
    local escaped=${1//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    escaped=${escaped//%/%%}
    printf '"%s"' "$escaped"
}

DESKTOP_FILE="$APPLICATIONS_DIR/voicetyping.desktop"
DESKTOP_PYTHON_PATH="$(desktop_quote "$PYTHON_BIN")"
DESKTOP_APP_PATH="$(desktop_quote "$INSTALL_DIR/voicetyping.py")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Name=VoiceTyping
Comment=Speech to text converter
Exec=$DESKTOP_PYTHON_PATH $DESKTOP_APP_PATH
Icon=audio-input-microphone
Terminal=false
Type=Application
Categories=Utility;
EOF
install -m 0644 "$DESKTOP_FILE" "$AUTOSTART_DIR/voicetyping.desktop"

echo "⌨️ Configuring GNOME shortcut..."
"$PYTHON_BIN" "$INSTALL_DIR/setup_shortcut.py" || true

echo "=================================================="
echo "✅ Installation completed. Launch VoiceTyping from the Applications menu."
echo "The app stores its Gemini API key in ~/.config/voicetyping/config.json."
echo "If VoiceTyping was already running, quit and relaunch it to load the new files."
echo "=================================================="
