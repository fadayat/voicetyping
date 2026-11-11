import json
import subprocess
import unittest
from unittest import mock

from audio_routing import AudioRoutingError, SystemAudioRouter


def inspect_output(object_id, properties):
    rows = [f"id {object_id}, type PipeWire:Interface:Node"]
    rows.extend(f'    * {key} = "{value}"' for key, value in properties.items())
    return "\n".join(rows)


def pw_object(object_id, **properties):
    return {"id": object_id, "info": {"props": properties}}


class RouterFixture:
    def __init__(self, *, api="alsa", name="Laptop Speakers", inputs=None):
        self.default_source_id = 20
        self.default_source_name = "alsa_input.internal"
        self.set_default_calls = []
        self.api = api
        self.device_name = name
        self.inputs = inputs or []

    def __call__(self, command):
        if command == ["pw-dump"]:
            device_props = {
                "media.class": "Audio/Device",
                "device.api": self.api,
                "device.description": self.device_name,
            }
            return json.dumps(
                [pw_object(10, **device_props)]
                + [self._as_object(item) for item in self.inputs]
            )
        if command == ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"]:
            return inspect_output(
                11,
                {
                    "device.id": "10",
                    "node.name": "alsa_output.active",
                    "node.description": self.device_name,
                    "media.class": "Audio/Sink",
                },
            )
        if command == ["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"]:
            return inspect_output(
                self.default_source_id,
                {
                    "device.id": "10" if self.default_source_id != 20 else "99",
                    "node.name": self.default_source_name,
                    "node.description": "Current microphone",
                    "media.class": "Audio/Source",
                },
            )
        if command[:2] == ["wpctl", "set-default"]:
            source_id = int(command[2])
            self.set_default_calls.append(source_id)
            self.default_source_id = source_id
            selected = next((item for item in self.inputs if item["id"] == source_id), None)
            self.default_source_name = selected["props"]["node.name"] if selected else "alsa_input.internal"
            return ""
        raise AssertionError(f"Unexpected command: {command}")

    @staticmethod
    def _as_object(item):
        return {"id": item["id"], "info": {"props": item["props"]}}


class SystemAudioRouterTests(unittest.TestCase):
    def make_router(self, fixture):
        return SystemAudioRouter(
            runner=fixture,
            which=lambda _name: "/usr/bin/tool",
            sleep=lambda _seconds: None,
        )

    def test_internal_microphone_is_selected_for_internal_output(self):
        fixture = RouterFixture(
            inputs=[
                {
                    "id": 21,
                    "props": {
                        "device.id": "10",
                        "media.class": "Audio/Source",
                        "node.name": "alsa_input.internal",
                        "node.description": "Internal Microphone",
                        "priority.session": "8000",
                    },
                }
            ]
        )

        selection = self.make_router(fixture).select()

        self.assertEqual(selection.source_id, 21)
        self.assertEqual(selection.source_description, "Internal Microphone")
        self.assertEqual(selection.reason, "active-output-device")

    def test_bluetooth_loopback_microphone_is_preferred(self):
        fixture = RouterFixture(api="bluez5", name="Bluetooth Headset", inputs=[
            {
                "id": 21,
                "props": {
                    "device.id": "10",
                    "media.class": "Audio/Source",
                    "node.name": "bluez_input.headset",
                    "node.description": "Headset Microphone",
                    "priority.session": "10000",
                    "bluez5.loopback": "true",
                },
            },
            {
                "id": 22,
                "props": {
                    "device.id": "10",
                    "media.class": "Audio/Source",
                    "node.name": "bluez_input.a2dp_duplex",
                    "node.description": "A2DP Microphone",
                    "priority.session": "1",
                },
            },
        ])

        selection = self.make_router(fixture).select()

        self.assertEqual(selection.source_id, 21)
        self.assertEqual(selection.device_api, "bluez5")

    def test_bluetooth_headset_without_microphone_fails_instead_of_using_laptop(self):
        fixture = RouterFixture(api="bluez5", name="Bluetooth Headset")

        with self.assertRaisesRegex(AudioRoutingError, "no available microphone"):
            self.make_router(fixture).select()

    def test_pipewire_discovery_failure_aborts_instead_of_using_laptop(self):
        def failing_runner(command):
            raise subprocess.CalledProcessError(1, command)

        router = SystemAudioRouter(
            runner=failing_runner,
            which=lambda _name: "/usr/bin/tool",
        )

        with self.assertRaisesRegex(AudioRoutingError, "inspect PipeWire"):
            router.select()

    def test_appimage_fails_closed_for_active_bluetooth_headset(self):
        fixture = RouterFixture(api="bluez5", name="Bluetooth Headset", inputs=[
            {
                "id": 21,
                "props": {
                    "device.id": "10",
                    "media.class": "Audio/Source",
                    "node.name": "bluez_input.headset",
                    "node.description": "Headset Microphone",
                    "bluez5.loopback": "true",
                },
            }
        ])

        with mock.patch.dict("os.environ", {"APPDIR": "/tmp/AppDir"}):
            with self.assertRaisesRegex(AudioRoutingError, "AppImage"):
                self.make_router(fixture).select()

    def test_hdmi_output_without_input_uses_system_default(self):
        fixture = RouterFixture(api="alsa", name="HDMI Monitor")

        selection = self.make_router(fixture).select()

        self.assertTrue(selection.uses_system_default)
        self.assertEqual(selection.reason, "system-default")
        self.assertEqual(selection.output_description, "HDMI Monitor")

    def test_temporary_default_input_is_restored(self):
        fixture = RouterFixture(inputs=[
            {
                "id": 21,
                "props": {
                    "device.id": "10",
                    "media.class": "Audio/Source",
                    "node.name": "usb_input.headset",
                    "node.description": "USB Headset Microphone",
                    "device.api": "alsa",
                },
            }
        ])

        route = self.make_router(fixture).activate()
        self.assertEqual(fixture.default_source_id, 21)
        route.restore()
        route.restore()

        self.assertEqual(fixture.set_default_calls, [21, 20])
        self.assertEqual(fixture.default_source_id, 20)

    def test_missing_previous_default_aborts_before_mutating_routing(self):
        fixture = RouterFixture(inputs=[
            {
                "id": 21,
                "props": {
                    "device.id": "10",
                    "media.class": "Audio/Source",
                    "node.name": "usb_input.headset",
                    "node.description": "USB Headset Microphone",
                },
            }
        ])

        def runner(command):
            if command == ["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"]:
                raise subprocess.CalledProcessError(1, command)
            return fixture(command)

        router = SystemAudioRouter(
            runner=runner,
            which=lambda _name: "/usr/bin/tool",
        )

        with self.assertRaisesRegex(AudioRoutingError, "preserve"):
            router.activate()
        self.assertEqual(fixture.set_default_calls, [])


if __name__ == "__main__":
    unittest.main()
