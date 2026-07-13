#!/usr/bin/env python3
import subprocess
import ast
import os
import json

CONFIG_DIR = os.path.expanduser("~/.config/voicetyping")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

def get_shortcut():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                if "shortcut" in config:
                    return config["shortcut"]
        except Exception:
            pass
    return "F8"

def setup_gnome_shortcut():
    try:
        subprocess.run(["which", "gsettings"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("gsettings not found. Skipping GNOME shortcut setup.")
        return

    name = "voicetyping"
    command = f"python {os.path.expanduser('~')}/.local/bin/voicetyping/voicetyping.py --toggle"
    binding = get_shortcut()
    shortcut_path = f"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/{name}/"

    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings"],
            capture_output=True, text=True
        )
        current = result.stdout.strip()
        
        if current == "@as []" or current == "":
            current_list = []
        else:
            current = current.replace("@as ", "")
            current_list = ast.literal_eval(current)
            
        if shortcut_path not in current_list:
            current_list.append(shortcut_path)
            new_list_str = str(current_list).replace("'", '"')
            subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings", new_list_str])
            
        subprocess.run(["gsettings", "set", f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{shortcut_path}", "name", f"'{name}'"])
        subprocess.run(["gsettings", "set", f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{shortcut_path}", "command", f"'{command}'"])
        subprocess.run(["gsettings", "set", f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{shortcut_path}", "binding", f"'{binding}'"])
        
        print(f"✅ Shortcut ({binding}) configured automatically for GNOME!")
    except Exception as e:
        print(f"⚠️ Failed to set GNOME shortcut: {e}")

if __name__ == "__main__":
    setup_gnome_shortcut()
