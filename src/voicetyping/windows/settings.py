"""Secrets-free user preferences for the Windows VoiceTyping app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


DEFAULT_SETTINGS = {
    "auto_paste": True,
    "live_transcription": True,
    "shortcut": "F8",
    "language_codes": ["az-AZ", "en-US"],
    "custom_vocabulary": [
        "fallback",
        "backend",
        "frontend",
        "API",
        "GitHub",
        "Docker",
        "Python",
        "Linux",
        "AppImage",
        "Fedora",
        "Ubuntu",
        "Gemini",
    ],
}

_LIST_LIMITS = {"language_codes": 16, "custom_vocabulary": 1000}


def settings_path() -> Path:
    """Return the per-user settings path without reading or storing secrets."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "VoiceTyping" / "settings.json"


def _clean_string_list(value: Any, limit: int) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        item = item.strip()
        if item not in result:
            result.append(item)
            if len(result) == limit:
                break
    return result


def _sanitize_settings(value: Any) -> dict:
    # Never expose module-level list defaults to caller mutation.
    result = dict(DEFAULT_SETTINGS)
    for name in _LIST_LIMITS:
        result[name] = list(result[name])
    if not isinstance(value, dict):
        return result

    for name in ("auto_paste", "live_transcription"):
        if type(value.get(name)) is bool:
            result[name] = value[name]

    shortcut = value.get("shortcut")
    if isinstance(shortcut, str) and 0 < len(shortcut.strip()) <= 64:
        result["shortcut"] = shortcut.strip()

    for name, limit in _LIST_LIMITS.items():
        if name in value:
            clean = _clean_string_list(value[name], limit)
            if clean is not None:
                result[name] = clean

    return result


def load_settings(path: Path | None = None) -> dict:
    """Load validated preferences; return defaults when the file is absent or invalid."""
    target = Path(path) if path is not None else settings_path()
    try:
        with target.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return _sanitize_settings(None)
    return _sanitize_settings(raw)


def save_settings(settings: dict, path: Path | None = None) -> Path:
    """Atomically save validated, whitelisted preferences as UTF-8 JSON."""
    target = Path(path) if path is not None else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = _sanitize_settings(settings)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return target
