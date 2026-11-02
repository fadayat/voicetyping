"""Parse and normalize shortcut syntax shared by desktop backends."""

from __future__ import annotations

import re
from dataclasses import dataclass


MODIFIER_ORDER = ("CTRL", "ALT", "SHIFT", "SUPER")
_MODIFIER_ALIASES = {
    "CTRL": "CTRL",
    "CONTROL": "CTRL",
    "PRIMARY": "CTRL",
    "ALT": "ALT",
    "MOD1": "ALT",
    "SHIFT": "SHIFT",
    "SUPER": "SUPER",
    "LOGO": "SUPER",
    "META": "SUPER",
    "WIN": "SUPER",
    "MOD4": "SUPER",
}
_GNOME_MODIFIERS = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "SUPER": "Super"}
_KEY_ALIASES = {" ": "space", "return": "enter", "kp_enter": "kp_enter"}
_MODIFIER_KEY_ALIASES = {
    "ctrl": "CTRL", "ctrl_l": "CTRL", "ctrl_r": "CTRL", "control": "CTRL",
    "shift": "SHIFT", "shift_l": "SHIFT", "shift_r": "SHIFT",
    "alt": "ALT", "alt_l": "ALT", "alt_r": "ALT", "alt_gr": "ALT",
    "super": "SUPER", "super_l": "SUPER", "super_r": "SUPER",
    "meta_l": "SUPER", "meta_r": "SUPER", "cmd": "SUPER",
}


@dataclass(frozen=True)
class Shortcut:
    modifiers: frozenset[str]
    key: str

    def _ordered_modifiers(self) -> tuple[str, ...]:
        return tuple(item for item in MODIFIER_ORDER if item in self.modifiers)

    def to_xdg_trigger(self) -> str:
        return "+".join((*self._ordered_modifiers(), self.key))

    def to_gnome_binding(self) -> str:
        prefix = "".join(f"<{_GNOME_MODIFIERS[item]}>" for item in self._ordered_modifiers())
        return prefix + self.key

    def to_label(self) -> str:
        names = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "SUPER": "Super"}
        return "+".join((*[names[item] for item in self._ordered_modifiers()], self.key))

    def matches(self, key_name: str, pressed_modifiers: set[str]) -> bool:
        normalized_key = _KEY_ALIASES.get(key_name.casefold(), key_name.casefold())
        wanted_key = _KEY_ALIASES.get(self.key.casefold(), self.key.casefold())
        return normalized_key == wanted_key and self.modifiers == frozenset(pressed_modifiers)


def parse_shortcut(value: str) -> Shortcut:
    """Accept GNOME ``<Ctrl>F8`` and plus-separated ``CTRL+F8`` forms."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("shortcut must be a non-empty string")
    value = value.strip()

    markup_modifiers = []
    if value.startswith("<"):
        markup_modifiers = re.findall(r"<([^<>]+)>", value)
        value = re.sub(r"<[^<>]+>", "", value)
        if "<" in value or ">" in value:
            raise ValueError("invalid GNOME shortcut syntax")
        parts = [*markup_modifiers, value]
    else:
        parts = value.split("+")

    if any(not part.strip() for part in parts):
        raise ValueError("shortcut contains an empty key or modifier")
    parts = [part.strip() for part in parts]
    modifiers = set()
    for part in parts[:-1]:
        modifier = _MODIFIER_ALIASES.get(part.upper())
        if modifier is None:
            raise ValueError(f"unsupported shortcut modifier: {part}")
        if modifier in modifiers:
            raise ValueError(f"duplicate shortcut modifier: {part}")
        modifiers.add(modifier)

    key = parts[-1]
    if key.startswith("<") or key.endswith(">") or not re.fullmatch(r"[A-Za-z0-9_./-]+", key):
        raise ValueError("shortcut key must be a named key such as F8 or space")
    if key.upper() in _MODIFIER_ALIASES:
        raise ValueError("shortcut must include a non-modifier key")
    return Shortcut(frozenset(modifiers), key)


def pynput_modifier(key) -> str | None:
    """Return a canonical modifier for a pynput key object."""
    name = getattr(key, "name", None)
    if name is None:
        return None
    return _MODIFIER_KEY_ALIASES.get(str(name).casefold())


def pynput_key_name(key) -> str | None:
    """Return the named key or printable character represented by pynput."""
    name = getattr(key, "name", None)
    if name is not None and str(name).casefold() not in _MODIFIER_KEY_ALIASES:
        return str(name)
    char = getattr(key, "char", None)
    if isinstance(char, str) and char:
        return _KEY_ALIASES.get(char.casefold(), char)
    return None
