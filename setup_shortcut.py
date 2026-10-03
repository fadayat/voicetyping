#!/usr/bin/env python3
"""Set up VoiceTyping's GNOME custom keyboard shortcut."""

import ast
import json
import os
import shlex
import shutil
import subprocess
import sys

CONFIG_FILE = os.path.expanduser("~/.config/voicetyping/config.json")
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_PREFIX = f"/{MEDIA_KEYS.replace('.', '/')}/custom-keybindings/"
VOICE_TYPING_PATH = f"{CUSTOM_PREFIX}voicetyping/"
SHORTCUT_ACTIVE = "active"
SHORTCUT_COLLISION = "collision"
SHORTCUT_NOT_GNOME = "not_gnome"
SHORTCUT_ERROR = "error"


def get_shortcut():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as config_file:
            shortcut = json.load(config_file).get("shortcut")
            if isinstance(shortcut, str) and shortcut.strip():
                return shortcut.strip()
    except (OSError, ValueError, TypeError):
        pass
    return "F8"


def find_owned_binding(current_list, entries):
    """Return the configured path of an existing VoiceTyping binding, if any."""
    for path in current_list:
        _name, command, _binding = entries.get(path, ("", "", ""))
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = []
        script_names = {os.path.basename(argument) for argument in arguments}
        targets_helper = "voicetyping_toggle.py" in script_names
        targets_legacy = "voicetyping.py" in script_names and "--toggle" in arguments
        dedicated_path = path.startswith(f"{CUSTOM_PREFIX}voicetyping")
        if dedicated_path or targets_helper or targets_legacy:
            return path
    return None


def find_binding_collision(current_list, entries, target_binding, owned_path=None):
    """Find another custom shortcut already using the requested key."""
    wanted = target_binding.casefold()
    for path in current_list:
        if path == owned_path:
            continue
        binding = entries.get(path, ("", "", ""))[2]
        if binding.casefold() == wanted:
            return path
    return None


def _gsettings(*args):
    return subprocess.run(
        ["gsettings", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _read_custom_binding(path):
    schema_path = f"{MEDIA_KEYS}.custom-keybinding:{path}"
    return (
        _gsettings("get", schema_path, "name").strip("'\""),
        _gsettings("get", schema_path, "command").strip("'\""),
        _gsettings("get", schema_path, "binding").strip("'\""),
    )


def setup_gnome_shortcut():
    """Configure a dedicated GNOME binding and return a status string."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "gnome" not in desktop.casefold():
        print("GNOME desktop not detected. Skipping GNOME shortcut setup.")
        return SHORTCUT_NOT_GNOME

    if not shutil.which("gsettings"):
        print("gsettings not found. Skipping GNOME shortcut setup.")
        return SHORTCUT_ERROR

    try:
        raw_list = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
        current_list = ast.literal_eval(raw_list) if raw_list.startswith("[") else []
        if not isinstance(current_list, list):
            raise ValueError("GNOME returned an invalid custom-keybindings list")

        entries = {path: _read_custom_binding(path) for path in current_list}
        owned_path = find_owned_binding(current_list, entries)
        target_binding = get_shortcut()
        effective_binding = (
            entries[owned_path][2] or target_binding if owned_path is not None else target_binding
        )

        collision = find_binding_collision(
            current_list, entries, effective_binding, owned_path=owned_path
        )
        if collision and owned_path is None:
            owner = entries[collision][0] or collision
            print(
                f"⚠️ Shortcut {effective_binding} is already used by {owner}; "
                "VoiceTyping's shortcut was not changed."
            )
            return SHORTCUT_COLLISION
        if collision:
            owner = entries[collision][0] or collision
            print(
                f"⚠️ Shortcut {effective_binding} is also used by {owner}; "
                "the existing VoiceTyping shortcut remains active."
            )

        if owned_path is None:
            # Never commandeer custom0 (or any other unrelated binding).
            owned_path = VOICE_TYPING_PATH
            suffix = 1
            while owned_path in current_list:
                owned_path = f"{CUSTOM_PREFIX}voicetyping-{suffix}/"
                suffix += 1
            current_list.append(owned_path)
            _gsettings("set", MEDIA_KEYS, "custom-keybindings", repr(current_list))

        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voicetyping_toggle.py")
        command = shlex.join([sys.executable, script_path])
        schema_path = f"{MEDIA_KEYS}.custom-keybinding:{owned_path}"
        for key, value in (
            ("name", "VoiceTyping"),
            ("command", command),
        ):
            _gsettings("set", schema_path, key, value)
        if owned_path not in entries or not entries[owned_path][2]:
            _gsettings("set", schema_path, "binding", effective_binding)

        print(f"✅ Shortcut ({effective_binding}) configured for GNOME.")
        return SHORTCUT_ACTIVE
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"⚠️ Failed to set GNOME shortcut: {error}")
        return SHORTCUT_ERROR


if __name__ == "__main__":
    setup_gnome_shortcut()
