#!/bin/bash

echo "=================================================="
echo "🚀 Welcome to VoiceTyping Installer"
echo "=================================================="

# 1. Check and install system packages
echo "📦 Checking system packages..."
if command -v dnf &> /dev/null; then
    echo "Fedora/RHEL system detected. Installing packages..."
    sudo dnf install -y wl-clipboard xclip portaudio python3-tkinter libappindicator-gtk3 libayatana-appindicator-gtk3
elif command -v apt &> /dev/null; then
    echo "Ubuntu/Debian system detected. Installing packages..."
    sudo apt update
    sudo apt install -y xclip wl-clipboard libportaudio2 python3-tk libappindicator3-1
else
    echo "⚠️ System type not recognized. Please install xclip, wl-clipboard, and portaudio manually."
fi

# 2. Install Python libraries
echo "🐍 Installing Python libraries..."
pip install --user google-genai sounddevice soundfile numpy pyperclip pynput pystray pillow

# 3. Copy files to the system
echo "📂 Placing files into the system..."
mkdir -p ~/.local/bin/voicetyping
cp voicetyping.py ~/.local/bin/voicetyping/voicetyping.py
cp setup_shortcut.py ~/.local/bin/voicetyping/setup_shortcut.py
chmod +x ~/.local/bin/voicetyping/voicetyping.py

# 4. Add to applications menu (.desktop)
echo "🖥️ Adding to the Applications menu..."
mkdir -p ~/.local/share/applications
mkdir -p ~/.config/autostart

cat > ~/.local/share/applications/voicetyping.desktop << EOL
[Desktop Entry]
Version=1.0
Name=VoiceTyping
Comment=Speech to text converter
Exec=python $HOME/.local/bin/voicetyping/voicetyping.py
Icon=audio-input-microphone
Terminal=false
Type=Application
Categories=Utility;
EOL

# Copy for autostart
cp ~/.local/share/applications/voicetyping.desktop ~/.config/autostart/voicetyping.desktop

# If it's already running, kill it and restart
pkill -f voicetyping.py
pkill -f sesle_yaz_linux.py

# Remove old files if they exist to clean up
rm -rf ~/.local/bin/sesle_yaz
rm -f ~/.local/share/applications/sesle_yaz.desktop
rm -f ~/.config/autostart/sesle_yaz.desktop

# 5. Automatically setup F8 shortcut for GNOME
echo "⌨️ Configuring GNOME shortcut..."
python ~/.local/bin/voicetyping/setup_shortcut.py || true

echo "=================================================="
echo "✅ Installation completed successfully!"
echo "You can now launch 'VoiceTyping' from your Applications menu."
echo "Note: Do not forget to enter your Gemini API key in the new window."
echo "=================================================="
