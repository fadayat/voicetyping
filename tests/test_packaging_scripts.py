import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WheelDrivenPackagingTests(unittest.TestCase):
    def read(self, relative_path):
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_install_script_installs_a_wheel_without_staging_modules(self):
        installer = self.read("install.sh")

        self.assertIn("VOICETYPING_WHEEL", installer)
        self.assertIn('pip install --no-deps "$WHEEL_PATH"', installer)
        self.assertNotIn("Staging VoiceTyping modules", installer)
        self.assertNotIn("2.2.0", installer)

    def test_appimage_recipe_installs_the_supplied_wheel(self):
        recipe = self.read("packaging/appimage/AppImageBuilder.yml")
        launcher = self.read("packaging/appimage/voicetyping-launcher")

        self.assertIn("VOICETYPING_WHEEL", recipe)
        self.assertIn('"$WHEEL_PATH"', recipe)
        self.assertNotIn("/usr/src/voicetyping", recipe)
        self.assertNotIn("2.2.0", recipe)
        self.assertIn("-m voicetyping.app", launcher)

    def test_deb_builder_installs_wheel_into_vendor_directory(self):
        builder = self.read("packaging/deb/build-deb.sh")
        launcher = self.read("packaging/deb/voicetyping")

        self.assertIn("VOICETYPING_WHEEL", builder)
        self.assertIn("--no-deps", builder)
        self.assertIn('--target "$APP_ROOT/vendor"', builder)
        self.assertNotIn('"$APP_ROOT/app"', builder)
        self.assertNotIn("for source_file in", builder)
        self.assertNotIn("2.2.0", builder)
        self.assertIn("-m voicetyping.app", launcher)
        self.assertIn("-m voicetyping.voicetyping_toggle", launcher)


if __name__ == "__main__":
    unittest.main()
