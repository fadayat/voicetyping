"""Deterministic, audio-reactive orb renderer for the VoiceTyping status UI."""

from __future__ import annotations

import math

import numpy as np
from PIL import Image


_COLORS = {
    "recording": np.array((0.36, 0.78, 0.91), dtype=np.float32),
    "transcribing": np.array((0.56, 0.76, 0.97), dtype=np.float32),
    "success": np.array((0.96, 0.84, 0.68), dtype=np.float32),
    "warning": np.array((0.94, 0.55, 0.46), dtype=np.float32),
    "idle": np.array((0.61, 0.75, 0.83), dtype=np.float32),
}


def _state_name(value: str | None) -> str:
    value = (value or "idle").lower()
    if value in ("copied", "success", "done"):
        return "success"
    if value in ("error", "warning", "failed"):
        return "warning"
    if value in ("recording", "listening", "capturing"):
        return "recording"
    if value in ("transcribing", "processing", "converting"):
        return "transcribing"
    return "idle"


class VoiceVisual:
    """Render a transparent RGBA frame containing a shaded, living orb.

    Audio inputs are normalized to [0, 1]. ``elapsed`` is seconds since the
    current state began. The output is supersampled internally for clean edges.
    """

    def __init__(self, width: int = 128, height: int = 128, scale: int = 2):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.scale = max(1, int(scale))
        self._pixel_size = (self.width * self.scale, self.height * self.scale)
        self._x, self._y = np.meshgrid(
            (np.arange(self._pixel_size[0], dtype=np.float32) + 0.5) / self.scale - self.width / 2,
            (np.arange(self._pixel_size[1], dtype=np.float32) + 0.5) / self.scale - self.height / 2,
        )
        self._distance = np.hypot(self._x, self._y)
        self._angle = np.arctan2(self._y, self._x)
        band_pos = (self._angle + math.pi) / (2.0 * math.pi) * 12.0
        self._band_lo = np.floor(band_pos).astype(np.int16) % 12
        self._band_frac = (band_pos - np.floor(band_pos)).astype(np.float32)

    def render(
        self,
        state: str,
        elapsed: float,
        level: float,
        envelope: tuple | list,
        transition: float = 1.0,
        previous_state: str | None = None,
        reduced_motion: bool = False,
    ) -> Image.Image:
        state = _state_name(state)
        previous = _state_name(previous_state) if previous_state else state
        t = float(np.clip(transition, 0.0, 1.0))
        t = 1.0 - (1.0 - t) ** 3
        elapsed = max(0.0, float(elapsed))
        level = float(np.clip(level, 0.0, 1.0))
        samples = np.asarray(tuple(envelope)[:12], dtype=np.float32)
        if not samples.size:
            samples = np.zeros(12, dtype=np.float32)
        elif samples.size < 12:
            samples = np.pad(samples, (0, 12 - samples.size), mode="edge")
        samples = np.clip(samples, 0.0, 1.0)

        x, y = self._x, self._y
        distance, angle = self._distance, self._angle
        energy = 0.72 * level + 0.28 * float(np.mean(samples))
        phase = 0.0 if reduced_motion else elapsed * (1.45 if state == "recording" else 0.82)

        # Speech swells the orb continuously; the twelve analyzer bands add
        # small, correlated changes to its circumference instead of bar shapes.
        radius = 17.0 + 31.0 * energy
        if state == "idle":
            radius = 17.0 + 1.4 * (0.5 + 0.5 * math.sin(phase))
        elif state == "transcribing":
            radius = 28.0 + 2.2 * (0.5 + 0.5 * math.sin(phase))
        elif state == "success":
            collapse = 1.0 - math.exp(-elapsed * 6.0)
            radius = 28.0 * (1.0 - 0.72 * collapse)
        elif state == "warning":
            contraction = 0.88 - (0.10 * math.exp(-((elapsed - 0.12) / 0.20) ** 2))
            radius = 25.0 * contraction

        if reduced_motion:
            # Keep the silhouette still while preserving an obvious live level.
            radius = 26.0 if state == "recording" else radius

        # Ease the previous state's size into the new pose, including a useful
        # visual distinction between transcription and recording.
        previous_radius = {
            "idle": 17.7,
            "recording": 17.0 + 31.0 * energy,
            "transcribing": 29.1,
            "success": 28.0 * (1.0 - 0.72 * (1.0 - math.exp(-elapsed * 6.0))),
            "warning": 21.0,
        }[previous]
        radius = previous_radius * (1.0 - t) + radius * t
        radius = min(radius, min(self.width, self.height) * 0.385)

        # Smooth flowing edge. It stays round at rest and becomes more expressive
        # with speech; analyzer bands gently travel around the edge over time.
        band = samples[self._band_lo] * (1.0 - self._band_frac) + samples[(self._band_lo + 1) % 12] * self._band_frac
        if reduced_motion:
            edge_wave = 0.28 * np.sin(3.0 * angle) + 0.18 * np.sin(6.0 * angle + 0.7)
        else:
            edge_wave = (
                0.65 * np.sin(3.0 * angle + phase * 0.72)
                + 0.43 * np.sin(5.0 * angle - phase * 0.51 + 0.8)
                + 0.22 * np.sin(8.0 * angle + phase * 0.37)
                + 0.85 * (band - float(np.mean(samples)))
            )
        edge_amount = 0.42 + 1.45 * energy if state == "recording" else 0.62
        if reduced_motion:
            edge_amount = 0.42
        boundary = radius + edge_wave * edge_amount
        signed = boundary - distance

        # A soft, bounded aura sits behind the surface and falls to true alpha 0.
        aura_sigma = 5.5 + 3.0 * energy
        aura = np.exp(-np.maximum(distance - boundary, 0.0) ** 2 / (2.0 * aura_sigma * aura_sigma))
        aura *= np.clip((distance - boundary + aura_sigma * 2.7) / (aura_sigma * 2.7), 0.0, 1.0)
        aura_alpha = 0.20 * aura

        # Sphere normals give the face genuine depth; broad cool light and a
        # small warm reflected glint create a polished pearl/glass surface.
        normalized = distance / np.maximum(boundary, 1e-3)
        z = np.sqrt(np.maximum(1.0 - normalized * normalized, 0.0))
        nx = x / np.maximum(boundary, 1e-3)
        ny = y / np.maximum(boundary, 1e-3)
        light_dot = np.clip((-0.52 * nx - 0.62 * ny + 0.59 * z), 0.0, 1.0)
        diffuse = 0.22 + 0.64 * light_dot
        # The fresnel-like rim makes the changing contour readable without a ring.
        rim = np.power(np.clip(1.0 - z, 0.0, 1.0), 1.75)
        key = np.exp(-(((x + radius * 0.29) / (radius * 0.23 + 1.0)) ** 2
                       + ((y + radius * 0.43) / (radius * 0.10 + 1.0)) ** 2) * 1.8)
        glint = np.exp(-(((x - radius * 0.43) / (radius * 0.20 + 1.0)) ** 2
                         + ((y - radius * 0.35) / (radius * 0.14 + 1.0)) ** 2) * 2.0)

        color = _COLORS[previous] * (1.0 - t) + _COLORS[state] * t
        if state == "success":
            color = color * 0.68 + np.array((1.0, 0.91, 0.77), dtype=np.float32) * 0.32
            fade = float(np.clip((0.36 - elapsed) / 0.10, 0.0, 1.0))
        elif state == "warning":
            fade = 1.0
            # One small contraction carries the warning without flashing.
            color = color * 0.88 + np.array((1.0, 0.55, 0.43), dtype=np.float32) * 0.12
        else:
            fade = 1.0

        interior = np.clip(0.5 + signed * self.scale * 0.55, 0.0, 1.0)
        sheen = 0.22 * key + 0.10 * glint + 0.18 * rim
        # Several broad, low-contrast color currents drift through one shared
        # surface. Their slow phase shifts make the pearl feel lit from within.
        flow_phase = 0.0 if reduced_motion else phase
        flow = 0.5 + 0.5 * np.sin(angle * 2.0 - flow_phase * 0.7 + normalized * 5.4)
        ice = np.array((0.73, 0.91, 1.0), dtype=np.float32)
        sapphire = np.array((0.39, 0.62, 0.90), dtype=np.float32)
        rose = np.array((0.94, 0.73, 0.70), dtype=np.float32)
        pearl = np.array((0.91, 0.96, 1.0), dtype=np.float32)
        flowing = ice[None, None, :] * (0.60 + 0.20 * flow[..., None])
        flowing += sapphire[None, None, :] * (0.13 + 0.08 * (1.0 - flow[..., None]))
        reflection = (0.5 + 0.5 * np.cos(angle - 2.5)) ** 8
        flowing += rose[None, None, :] * (0.035 + 0.075 * reflection[..., None])
        rgb = color[None, None, :] * (0.36 + 0.48 * diffuse[..., None])
        rgb = rgb * 0.53 + flowing * 0.47
        rgb += sheen[..., None] * np.array((0.58, 0.69, 0.78), dtype=np.float32)
        # A pearly, softly luminous core replaces the hard plastic-like fill.
        core = np.exp(-(normalized * normalized) * 1.45) * (0.19 + 0.035 * energy)
        rgb = rgb * (1.0 - core[..., None]) + pearl[None, None, :] * core[..., None]
        rgb += (0.045 + 0.035 * energy) * pearl[None, None, :]
        rgb = np.clip(rgb, 0.0, 1.0)

        alpha = np.maximum(interior, aura_alpha) * fade
        # Preserve a crisp antialiased silhouette and let the aura vanish cleanly.
        alpha = np.clip(alpha, 0.0, 1.0)
        halo_color = color[None, None, :] * 0.68 + np.array((0.68, 0.81, 0.92), dtype=np.float32) * 0.32
        rgb = np.where((interior < 0.01)[..., None], halo_color, rgb)

        # Fine, broken filaments hover just beyond the contour like reflections
        # on a liquid lens. Their angular gates keep them irregular and incomplete.
        filament_alpha = np.zeros_like(distance, dtype=np.float32)
        filament_rgb = np.zeros((*distance.shape, 3), dtype=np.float32)
        filament_specs = ((1.8, 0.30, 0.18), (4.2, 0.20, 2.6))
        for index, (offset, strength, center) in enumerate(filament_specs):
            drift = 0.0 if reduced_motion else flow_phase * (0.24 + index * 0.11)
            radial_wobble = 0.45 * np.sin(angle * (2.0 + index) + drift * 1.4 + center)
            radial_wobble += 0.20 * np.sin(angle * 5.0 - drift * 0.7)
            filament_d = distance - (boundary + offset + radial_wobble)
            stroke = np.exp(-((filament_d / 0.48) ** 2) * 0.5)
            gate = 0.5 + 0.5 * np.sin(angle * (1 + index) + center + drift)
            gate = np.clip((gate - 0.34) * 2.5, 0.0, 1.0)
            gate *= 0.74 + 0.26 * np.sin(angle * 3.0 - drift + index) ** 2
            amount = strength * (0.32 + 0.68 * energy) * stroke * gate * fade
            filament_alpha = 1.0 - (1.0 - filament_alpha) * (1.0 - amount)
            filament_tint = (ice * 0.68 + rose * 0.32) if index == 1 else ice
            filament_rgb += amount[..., None] * filament_tint[None, None, :]
        filament_rgb /= np.maximum(filament_alpha[..., None], 1e-5)
        combined_alpha = alpha + filament_alpha * (1.0 - alpha)
        premult = rgb * alpha[..., None] * (1.0 - filament_alpha[..., None])
        premult += filament_rgb * filament_alpha[..., None]
        rgb = premult / np.maximum(combined_alpha[..., None], 1e-5)
        alpha = combined_alpha
        # The aura must reach zero before the transparent window's boundary.
        alpha *= np.clip((min(self.width, self.height) / 2 - distance - 1) / 5, 0, 1)
        rgba = np.empty((*x.shape, 4), dtype=np.uint8)
        rgba[..., :3] = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
        rgba[..., 3] = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)

        frame = Image.fromarray(rgba, mode="RGBA")
        if self.scale != 1:
            frame = frame.resize((self.width, self.height), Image.Resampling.LANCZOS)
        return frame
