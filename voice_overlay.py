#!/usr/bin/env python3
"""Transparent, non-interactive voice animation. Receives only status and levels."""

from __future__ import annotations

import json
import math
import os
import sys
import time

import cairo
import gi
import numpy as np

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from voice_visual import VoiceVisual


class VoiceOverlay:
    """Own the GTK loop in a separate process so the tray cannot block painting."""

    SIZE = 128
    FRAME_MS = 25

    def __init__(self):
        # The analytic alpha edge is antialiased at native resolution. A 2x
        # export is available from VoiceVisual; desktop frames keep CPU bounded.
        self.visual = VoiceVisual(self.SIZE, self.SIZE, scale=1)
        self.state = "ready"
        self.previous_state = "ready"
        self.state_at = time.monotonic()
        self.last_frame_at = self.state_at
        self.shown_at = self.state_at
        self.hide_at = None
        self.exit_at = None
        self.level = 0.0
        self.envelope = np.zeros(12, dtype=np.float32)
        self.target_level = 0.0
        self.target_envelope = np.zeros(12, dtype=np.float32)
        self.audio_at = 0.0
        self.visible = False
        self.surface = None
        self.pixels = None
        self.opacity = 0.0
        self.buffer = b""
        self.tick_id = None

        settings = Gtk.Settings.get_default()
        self.reduced_motion = bool(settings and not settings.get_property("gtk-enable-animations"))
        self.window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.window.set_title("VoiceTyping Animation")
        self.window.set_default_size(self.SIZE, self.SIZE)
        self.window.set_size_request(self.SIZE, self.SIZE)
        self.window.resize(self.SIZE, self.SIZE)
        self.window.set_resizable(False)
        self.window.set_decorated(False)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_keep_above(True)
        self.window.set_app_paintable(True)
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None or not screen.is_composited():
            raise RuntimeError("The voice animation requires a composited RGBA display")
        self.window.set_visual(visual)
        self.window.connect("draw", self._draw)
        self.window.connect("realize", self._realize)
        self.window.connect("map-event", self._realize)
        self.window.connect("destroy", lambda *_: Gtk.main_quit())
        os.set_blocking(sys.stdin.fileno(), False)
        GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read)

    def _realize(self, *_):
        # Entire window is click-through. The text field keeps keyboard focus.
        native = self.window.get_window()
        native.set_pass_through(True)
        native.input_shape_combine_region(cairo.Region(), 0, 0)
        return False

    def _position(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_workarea()
        self.window.move(geometry.x + (geometry.width - self.SIZE) // 2, geometry.y + 10)

    def _read(self, fd, condition):
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return True
        except OSError:
            Gtk.main_quit()
            return False
        if not chunk:
            Gtk.main_quit()
            return False
        self.buffer += chunk
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        if len(self.buffer) > 65536:
            Gtk.main_quit()
            return False
        for line in lines:
            try:
                command = json.loads(line)
                if not self._command(command):
                    return False
            except (ValueError, TypeError, KeyError, IndexError):
                continue
        return True

    def _command(self, command):
        action = command[0]
        now = time.monotonic()
        if action == "close":
            Gtk.main_quit()
            return False
        if action == "show":
            state, hide_ms = command[1:3]
            if state == "ready" and self.visible and self.state in ("recording", "transcribing"):
                return True
            if state != self.state or not self.visible:
                self.previous_state = self.state
                self.state = state
                self.state_at = now
            if state == "recording":
                self.target_level = 0.0
                self.audio_at = now
            self.exit_at = None
            self.hide_at = now + hide_ms / 1000 if hide_ms else None
            if state in ("recording", "transcribing"):
                self.hide_at = None
            elif state == "copied":
                self.hide_at = now + 0.30
            elif state == "ready":
                self.hide_at = now + 0.85
            if not self.visible:
                self.visible = True
                self.shown_at = now
                self.last_frame_at = now
                self._position()
                self.window.show_all()
                if self.tick_id is None:
                    self.tick_id = GLib.timeout_add(self.FRAME_MS, self._tick)
        elif action == "audio":
            self.target_level = min(1.0, max(0.0, command[1] * 0.8 + command[2] * 0.2))
            envelope = np.asarray(command[3][:12], dtype=np.float32)
            self.target_envelope[:] = 0
            self.target_envelope[:len(envelope)] = np.clip(envelope, 0.0, 1.0)
            self.audio_at = now
        elif action == "hide" and self.visible:
            self.exit_at = now
        return True

    def _tick(self):
        try:
            return self._animate_frame()
        except Exception as error:
            # GTK swallows callback exceptions. Exit so the parent can restore
            # feedback rather than leaving an apparently healthy frozen window.
            print(f"Voice animation stopped ({type(error).__name__}).", file=sys.stderr, flush=True)
            Gtk.main_quit()
            return False

    def _animate_frame(self):
        now = time.monotonic()
        step = min(0.1, max(0.001, now - self.last_frame_at))
        self.last_frame_at = now
        if self.hide_at is not None and now >= self.hide_at and self.exit_at is None:
            self.exit_at = now
        if self.exit_at is not None and now - self.exit_at >= 0.18:
            self.window.hide()
            self.visible = False
            self.surface = None
            self.pixels = None
            self.tick_id = None
            return False

        stale = now - self.audio_at > 0.18 or self.state != "recording"
        target = 0.0 if stale else self.target_level
        # Time-based attack/release: neither frame rate nor silence changes the feel.
        response = 0.055 if target > self.level else 0.19
        self.level += (target - self.level) * (1 - math.exp(-step / response))
        targets = np.zeros(12) if stale else self.target_envelope
        self.envelope += (targets - self.envelope) * (1 - math.exp(-step / 0.09))
        elapsed = now - self.state_at
        image = self.visual.render(
            self.state, elapsed, self.level, self.envelope,
            transition=min(1.0, elapsed / 0.28), previous_state=self.previous_state,
            reduced_motion=self.reduced_motion,
        ).convert("RGBA")
        rgba = np.asarray(image, dtype=np.uint16)
        # Cairo expects premultiplied ARGB32; byte order follows the host.
        premultiplied = (rgba[:, :, :3] * rgba[:, :, 3:4] + 127) // 255
        argb = ((rgba[:, :, 3].astype(np.uint32) << 24)
                | (premultiplied[:, :, 0].astype(np.uint32) << 16)
                | (premultiplied[:, :, 1].astype(np.uint32) << 8)
                | premultiplied[:, :, 2].astype(np.uint32))
        self.pixels = bytearray(argb.tobytes())
        self.surface = cairo.ImageSurface.create_for_data(
            self.pixels, cairo.FORMAT_ARGB32, self.SIZE, self.SIZE, self.SIZE * 4,
        )
        opacity = min(1.0, (now - self.shown_at) / 0.15)
        if self.exit_at is not None:
            opacity *= max(0.0, 1 - (now - self.exit_at) / 0.18)
        self.opacity = opacity
        self.window.queue_draw()
        return True

    def _draw(self, _window, context):
        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0, 0, 0, 0)
        context.paint()
        if self.surface is not None:
            context.set_operator(cairo.OPERATOR_OVER)
            context.set_source_surface(self.surface)
            context.paint_with_alpha(self.opacity)
        return False


def main():
    try:
        overlay = VoiceOverlay()
        print("ready", flush=True)
        Gtk.main()
        return 0
    except Exception as error:
        print(f"Voice animation unavailable ({type(error).__name__}).", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
