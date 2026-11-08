"""Small, testable description of the current Linux desktop session."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class DesktopEnvironment:
    desktops: tuple[str, ...]
    session_type: str
    display: str
    wayland_display: str

    @property
    def is_gnome(self) -> bool:
        return any("gnome" in desktop for desktop in self.desktops)

    @property
    def is_wayland(self) -> bool:
        if self.session_type:
            return self.session_type == "wayland"
        return bool(self.wayland_display and not self.display)

    @property
    def is_x11(self) -> bool:
        if self.session_type:
            return self.session_type == "x11"
        return bool(self.display and not self.wayland_display)


def detect_desktop_environment(
    environ: Mapping[str, str] | None = None,
) -> DesktopEnvironment:
    """Read desktop identifiers without probing or changing desktop state."""
    source = os.environ if environ is None else environ
    desktops = tuple(
        item.strip().casefold()
        for item in source.get("XDG_CURRENT_DESKTOP", "").split(":")
        if item.strip()
    )
    return DesktopEnvironment(
        desktops=desktops,
        session_type=source.get("XDG_SESSION_TYPE", "").strip().casefold(),
        display=source.get("DISPLAY", "").strip(),
        wayland_display=source.get("WAYLAND_DISPLAY", "").strip(),
    )
