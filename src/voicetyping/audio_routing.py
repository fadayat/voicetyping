"""PipeWire input routing for VoiceTyping.

The router briefly points PipeWire's default input at the microphone belonging
to the active output device.  The caller opens its capture stream while that
temporary default is active, then restores the user's previous default.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


COMMAND_TIMEOUT_SECONDS = 2.0
ROUTE_SETTLE_SECONDS = 2.0
ROUTE_POLL_SECONDS = 0.05


class AudioRoutingError(RuntimeError):
    """Raised when an attached headset cannot provide a usable microphone."""


@dataclass(frozen=True)
class PipeWireObject:
    object_id: int
    properties: dict[str, object]

    @property
    def name(self) -> str:
        return str(self.properties.get("node.name") or self.properties.get("device.name") or "")

    @property
    def description(self) -> str:
        return str(
            self.properties.get("node.description")
            or self.properties.get("device.description")
            or self.name
            or f"PipeWire object {self.object_id}"
        )


@dataclass(frozen=True)
class AudioSelection:
    source_id: int | None
    source_name: str
    source_description: str
    output_description: str
    device_api: str
    reason: str

    @property
    def uses_system_default(self) -> bool:
        return self.source_id is None


class ActiveInputRoute:
    """A temporary default-source change that can be restored exactly once."""

    def __init__(
        self,
        selection: AudioSelection,
        restore_action: Callable[[], None] | None = None,
    ):
        self.selection = selection
        self._restore_action = restore_action
        self._cancelled = threading.Event()
        self._activation_lock = threading.Lock()
        self._changed = False
        self._restored = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _activate(self, action: Callable[[], None]) -> bool:
        """Apply the route unless restoration already won the race."""
        with self._activation_lock:
            if self._cancelled.is_set() or self._restored:
                return False
            # Assume the command may mutate before reporting failure.  A
            # failed command therefore still restores the preserved source.
            self._changed = True
            action()
            return True

    def restore(self) -> None:
        self._cancelled.set()
        with self._activation_lock:
            if self._restored:
                return
            self._restored = True
            action = self._restore_action if self._changed else None
            self._restore_action = None
            # Keep concurrent restore callers behind the lock until the
            # previous default is actually back, not merely scheduled.
            if action is not None:
                action()

    def __enter__(self) -> "ActiveInputRoute":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.restore()


class SystemAudioRouter:
    """Choose the input belonging to the current PipeWire output device."""

    def __init__(
        self,
        runner: Callable[[list[str]], str] | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._runner = runner or self._run
        self._which = which
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _run(command: list[str]) -> str:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return completed.stdout

    def supported(self) -> bool:
        return bool(self._which("wpctl") and self._which("pw-dump"))

    def _inspect(self, identifier: str) -> PipeWireObject:
        output = self._runner(["wpctl", "inspect", identifier])
        first_line = output.splitlines()[0] if output.splitlines() else ""
        match = re.search(r"\bid\s+(\d+)\b", first_line)
        if match is None:
            raise AudioRoutingError(f"Could not inspect PipeWire object {identifier}")
        properties: dict[str, object] = {}
        for line in output.splitlines()[1:]:
            property_match = re.match(
                r"^\s*\*?\s*([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$", line
            )
            if property_match is None:
                continue
            key, raw_value = property_match.groups()
            value = raw_value
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            properties[key] = value
        return PipeWireObject(int(match.group(1)), properties)

    def _objects(self) -> list[PipeWireObject]:
        payload = json.loads(self._runner(["pw-dump"]))
        if not isinstance(payload, list):
            raise AudioRoutingError("PipeWire returned invalid device data")
        objects: list[PipeWireObject] = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            info = item.get("info")
            properties = info.get("props") if isinstance(info, dict) else None
            if isinstance(properties, dict):
                objects.append(PipeWireObject(item["id"], properties))
        return objects

    @staticmethod
    def _device_id(node: PipeWireObject) -> int | None:
        value = node.properties.get("device.id")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _priority(node: PipeWireObject) -> int:
        value = node.properties.get("priority.session", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_input(node: PipeWireObject) -> bool:
        media_class = str(node.properties.get("media.class", ""))
        if not media_class.startswith("Audio/Source"):
            return False
        name = node.name.casefold()
        description = node.description.casefold()
        if str(node.properties.get("stream.monitor", "")).casefold() == "true":
            return False
        return not name.endswith(".monitor") and not description.startswith("monitor of ")

    @staticmethod
    def _strict_headset(device: PipeWireObject) -> bool:
        api = str(device.properties.get("device.api", "")).casefold()
        if api == "bluez5":
            return True
        bus = str(device.properties.get("device.bus", "")).casefold()
        form_factor = str(device.properties.get("device.form-factor", "")).casefold()
        text = f"{device.name} {device.description} {form_factor}".casefold()
        return bus == "usb" and any(
            keyword in text for keyword in ("headset", "headphone", "headphones")
        )

    def select(self) -> AudioSelection:
        if not self.supported():
            return AudioSelection(
                None, "", "System default microphone", "", "", "system-default"
            )

        try:
            default_sink = self._inspect("@DEFAULT_AUDIO_SINK@")
            objects = self._objects()
        except (AudioRoutingError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
            raise AudioRoutingError("Could not inspect PipeWire audio routing") from error

        output_device_id = self._device_id(default_sink)
        if output_device_id is None:
            raise AudioRoutingError("The active output has no PipeWire device")

        device = next(
            (
                item
                for item in objects
                if item.object_id == output_device_id
                and item.properties.get("media.class") == "Audio/Device"
            ),
            None,
        )
        if device is None:
            raise AudioRoutingError("The active output device is unavailable")

        candidates = [
            item
            for item in objects
            if self._device_id(item) == output_device_id and self._is_input(item)
        ]
        if not candidates:
            if self._strict_headset(device):
                raise AudioRoutingError(
                    f"{device.description} has no available microphone source"
                )
            return AudioSelection(
                None,
                "",
                "System default microphone",
                default_sink.description,
                str(device.properties.get("device.api", "")),
                "system-default",
            )

        candidates.sort(
            key=lambda item: (
                str(item.properties.get("bluez5.loopback", "")).casefold() == "true",
                self._priority(item),
            ),
            reverse=True,
        )
        source = candidates[0]
        return AudioSelection(
            source.object_id,
            source.name,
            source.description,
            default_sink.description,
            str(device.properties.get("device.api", "")),
            "active-output-device",
        )

    def _find_source_by_name(self, name: str) -> PipeWireObject | None:
        if not name:
            return None
        try:
            return next(
                (item for item in self._objects() if self._is_input(item) and item.name == name),
                None,
            )
        except (AudioRoutingError, json.JSONDecodeError, OSError, subprocess.SubprocessError):
            return None

    def _set_default(self, source_id: int) -> None:
        self._runner(["wpctl", "set-default", str(source_id)])

    def _source_muted(self, source_id: int) -> bool:
        try:
            output = self._runner(["wpctl", "get-volume", str(source_id)])
        except (OSError, subprocess.SubprocessError) as error:
            raise AudioRoutingError("Could not inspect microphone mute state") from error
        if "[MUTED]" in output.upper():
            return True
        if "volume:" not in output.casefold():
            raise AudioRoutingError("PipeWire returned invalid microphone volume data")
        return False

    def _set_source_muted(self, source_id: int, muted: bool) -> None:
        self._runner(
            ["wpctl", "set-mute", str(source_id), "1" if muted else "0"]
        )

    def _restore_source_mute(
        self,
        source_id: int,
        source_name: str,
        muted: bool,
    ) -> bool:
        try:
            self._set_source_muted(source_id, muted)
            return True
        except (OSError, subprocess.SubprocessError):
            replacement = self._find_source_by_name(source_name)
            if replacement is None:
                return False
            try:
                self._set_source_muted(replacement.object_id, muted)
                return True
            except (OSError, subprocess.SubprocessError):
                return False

    def _wait_for_default(
        self,
        source_id: int,
        source_name: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        deadline = self._monotonic() + ROUTE_SETTLE_SECONDS
        while True:
            if cancelled is not None and cancelled():
                return False
            try:
                current = self._inspect("@DEFAULT_AUDIO_SOURCE@")
            except (AudioRoutingError, OSError, subprocess.SubprocessError):
                current = None
            if current is not None and (
                current.object_id == source_id or current.name == source_name
            ):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(ROUTE_POLL_SECONDS)

    def _restore_source(self, previous: PipeWireObject) -> bool:
        try:
            self._set_default(previous.object_id)
            if self._wait_for_default(previous.object_id, previous.name):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        replacement = self._find_source_by_name(previous.name)
        if replacement is None:
            return False
        try:
            self._set_default(replacement.object_id)
            return self._wait_for_default(replacement.object_id, replacement.name)
        except (OSError, subprocess.SubprocessError):
            return False

    def activate(
        self,
        on_route: Callable[[ActiveInputRoute], bool] | None = None,
    ) -> ActiveInputRoute:
        selection = self.select()
        if selection.uses_system_default:
            route = ActiveInputRoute(selection)
            if on_route is not None and not on_route(route):
                route.restore()
                raise AudioRoutingError("Microphone activation was cancelled")
            return route

        try:
            previous = self._inspect("@DEFAULT_AUDIO_SOURCE@")
            source_was_muted = self._source_muted(selection.source_id)
        except (AudioRoutingError, OSError, subprocess.SubprocessError) as error:
            raise AudioRoutingError(
                "Could not preserve the current microphone state"
            ) from error

        default_changed = not (
            previous.object_id == selection.source_id
            or previous.name == selection.source_name
        )

        def restore() -> None:
            if source_was_muted and not self._restore_source_mute(
                selection.source_id,
                selection.source_name,
                True,
            ):
                print("Could not restore the microphone mute state.")
            if default_changed and not self._restore_source(previous):
                print("Could not restore the previous default microphone.")

        route = ActiveInputRoute(selection, restore)
        if on_route is not None and not on_route(route):
            route.restore()
            raise AudioRoutingError("Microphone activation was cancelled")

        try:
            if default_changed:
                if not route._activate(lambda: self._set_default(selection.source_id)):
                    raise AudioRoutingError("Microphone activation was cancelled")
                if not self._wait_for_default(
                    selection.source_id,
                    selection.source_name,
                    cancelled=lambda: route.cancelled,
                ):
                    if route.cancelled:
                        raise AudioRoutingError("Microphone activation was cancelled")
                    raise AudioRoutingError(
                        f"Could not activate microphone {selection.source_description}"
                    )
            if source_was_muted and not route._activate(
                lambda: self._set_source_muted(selection.source_id, False)
            ):
                raise AudioRoutingError("Microphone activation was cancelled")
        except (AudioRoutingError, OSError, subprocess.SubprocessError) as error:
            route.restore()
            if isinstance(error, AudioRoutingError):
                raise
            raise AudioRoutingError(
                f"Could not activate microphone {selection.source_description}"
            ) from error
        return route

    def audio_info(self) -> list[str]:
        selection = self.select()
        lines = [
            "Audio backend: PipeWire" if self.supported() else "Audio backend: system default",
            f"Active output: {selection.output_description or 'unknown'}",
            f"Selected input: {selection.source_description}",
            f"Selection policy: {selection.reason}",
        ]
        if selection.device_api:
            lines.append(f"Device API: {selection.device_api}")
        return lines
