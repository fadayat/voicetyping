# Release Harness

`scripts/release_harness.py` runs without a microphone, Gemini API call, or
access to the user's configuration and API-key files. The `source` checks are
offline after the locked environment exists. Clean wheel/installer checks and
external package builds may need network access to download dependencies.

## Commands

Run from the repository root:

```bash
uv run --locked python scripts/release_harness.py source
uv build --wheel --out-dir dist
uv run --locked python scripts/release_harness.py wheel --wheel dist/voicetyping-2.3.0-py3-none-any.whl
```

The `source` command checks `uv.lock`, compares the generated hash-pinned
requirements export, compiles application and test code, and runs the complete
unit and integration suite in temporary config, runtime, and recovery
directories. It fails if `requirements.txt` differs from the lock export.

The `wheel` command installs the selected wheel into a temporary clean virtual
environment. It checks the installed `voicetyping --version` command, discovers
every package module, imports modules that do not require distro desktop
bindings, verifies every expected module in the archive, and checks both
console commands. It then runs `install.sh` twice with an isolated `HOME` and
the wheel provided through `VOICETYPING_WHEEL`.

Build and validate the Linux artifacts with the same wheel:

```bash
VOICETYPING_WHEEL="$PWD/dist/voicetyping-2.3.0-py3-none-any.whl" \
  ./packaging/appimage/build-appimage.sh 2.3.0
uv run --locked python scripts/release_harness.py appimage \
  dist/VoiceTyping-2.3.0-x86_64.AppImage --version 2.3.0

VOICETYPING_WHEEL="$PWD/dist/voicetyping-2.3.0-py3-none-any.whl" \
  ./packaging/deb/build-deb.sh 2.3.0
uv run --locked python scripts/release_harness.py deb \
  dist/voicetyping_2.3.0-1_ubuntu24.04_amd64.deb --version 2.3.0
```

The `appimage` command checks the adjacent SHA-256 file, normal `--version`,
extract-and-run `--version`, and the embedded ELF runtime's dynamic
dependencies and interpreter. It requires `sha256sum` and `readelf`.

The `deb` command checks package metadata, amd64 architecture, and application
modules. Run `--install` only inside a disposable Ubuntu 24.04 environment as
root to install with apt and verify the installed command:

```bash
sudo python3 scripts/release_harness.py deb dist/voicetyping_2.3.0-1_ubuntu24.04_amd64.deb \
  --version 2.3.0 --install
```

Any failed check exits nonzero. See each command's `--help` for options.

## Manual desktop smoke test

Before publishing a reviewed draft, test the actual AppImage and `.deb` on a
desktop session:

1. Verify the AppImage SHA-256 file and launch the app. Confirm that the tray
   and floating indicator appear.
2. Press F8, speak, then press F8 again. Confirm text reaches the focused field.
3. Test the built-in microphone and USB and Bluetooth headset routes. Also test
   a pre-muted headset source. Confirm the prior PipeWire route and mute state
   return after recording.
4. Complete a successful transcription. Confirm no audio archive remains.
5. Disconnect the network during a recording, verify recovery audio is stored
   privately, restore connectivity, and confirm retry removes the audio while
   making the recovered text available from the tray.
6. Check the desktop shortcut and autostart entry after package installation.
