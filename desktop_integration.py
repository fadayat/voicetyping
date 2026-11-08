"""Choose desktop integrations from the current session capabilities."""

from __future__ import annotations

from desktop_environment import DesktopEnvironment, detect_desktop_environment


def setup_global_shortcut(environment: DesktopEnvironment | None = None) -> str:
    """Configure GNOME's existing keybinding; other desktops keep their own."""
    session = environment or detect_desktop_environment()
    if not session.is_gnome:
        from setup_shortcut import SHORTCUT_NOT_GNOME

        return SHORTCUT_NOT_GNOME

    from setup_shortcut import setup_gnome_shortcut

    return setup_gnome_shortcut()


def build_paste_action(
    enabled: bool = True,
    environment: DesktopEnvironment | None = None,
    portal_keyboard=None,
):
    """Select a native paste path, or leave text on the clipboard."""
    if not enabled:
        return None

    session = environment or detect_desktop_environment()
    if session.is_x11:
        from auto_paste import X11AutomaticPaste

        return X11AutomaticPaste()
    if session.is_gnome:
        from auto_paste import AutomaticPaste

        return AutomaticPaste()
    if session.is_wayland and portal_keyboard is not None:
        from auto_paste import PortalAutomaticPaste

        return PortalAutomaticPaste(portal_keyboard)
    return None


def should_start_x11_listener(
    shortcut_status: str,
    environment: DesktopEnvironment | None = None,
) -> bool:
    """Use pynput only when the session explicitly provides X11 input access."""
    from setup_shortcut import SHORTCUT_NOT_GNOME

    session = environment or detect_desktop_environment()
    return shortcut_status == SHORTCUT_NOT_GNOME and session.is_x11


def build_wayland_shortcut_listener(
    shortcut: str,
    on_activate,
    environment: DesktopEnvironment | None = None,
):
    """Create a portal shortcut controller for non-GNOME Wayland sessions."""
    session = environment or detect_desktop_environment()
    if not session.is_wayland or session.is_gnome:
        return None

    from xdg_portal import GlobalShortcutPortal

    return GlobalShortcutPortal(shortcut, on_activate)
