#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="$HOME/.local/bin/voicetyping"
RELEASES_DIR="$INSTALL_ROOT/releases"
APPLICATIONS_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
AUTOSTART_DROPIN_DIR="$SYSTEMD_USER_DIR/app-voicetyping@autostart.service.d"
SYSTEM_PYTHON_BIN="$(command -v python3)"
STAGE_DIR=""
LEGACY_STAGE_DIR=""
CURRENT_TMP=""
WHEEL_BUILD_DIR=""

cleanup() {
    if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
        rm -rf -- "$STAGE_DIR"
    fi
    if [[ -n "$LEGACY_STAGE_DIR" && -d "$LEGACY_STAGE_DIR" ]]; then
        rm -rf -- "$LEGACY_STAGE_DIR"
    fi
    if [[ -n "$CURRENT_TMP" && -L "$CURRENT_TMP" ]]; then
        rm -f -- "$CURRENT_TMP"
    fi
    if [[ -n "$WHEEL_BUILD_DIR" && -d "$WHEEL_BUILD_DIR" ]]; then
        rm -rf -- "$WHEEL_BUILD_DIR"
    fi
}
trap cleanup EXIT

echo "=================================================="
echo "🚀 Welcome to VoiceTyping Installer"
echo "=================================================="

if ! "$SYSTEM_PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "❌ VoiceTyping requires Python 3.10 or newer." >&2
    exit 1
fi

if [[ -e "$INSTALL_ROOT/current" && ! -L "$INSTALL_ROOT/current" ]]; then
    echo "❌ Refusing to replace non-symlink installation path: $INSTALL_ROOT/current" >&2
    exit 1
fi

echo "📦 Install the required system packages if they are missing:"
if command -v dnf >/dev/null 2>&1; then
    echo "  Fedora: gcc python3-devel kernel-headers wl-clipboard xclip portaudio python3-tkinter python3-gobject python3-cairo gtk3 libappindicator-gtk3 libayatana-appindicator-gtk3"
elif command -v apt >/dev/null 2>&1; then
    echo "  Debian/Ubuntu: python3-venv build-essential python3-dev linux-libc-dev xclip wl-clipboard libportaudio2 python3-tk python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1"
elif command -v pacman >/dev/null 2>&1; then
    echo "  Arch: base-devel linux-api-headers python-pip tk python-gobject python-cairo gtk3 libappindicator portaudio libsndfile wl-clipboard xclip"
else
    echo "  Install Python venv/dev tools, Linux API headers, GTK/GObject/Cairo, Tkinter, PortAudio, and clipboard tools using your distribution's package manager."
fi

mkdir -p "$RELEASES_DIR"

# Preserve a legacy flat source installation on its first migration. Only copy
# known application files from the install root; never recurse through releases
# or follow the current symlink. Existing snapshots make reruns idempotent.
if [[ ! -L "$INSTALL_ROOT/current" && -f "$INSTALL_ROOT/voicetyping.py" && ! -L "$INSTALL_ROOT/voicetyping.py" ]]; then
    shopt -s nullglob
    legacy_snapshots=("$RELEASES_DIR"/legacy-flat-*)
    shopt -u nullglob
    if [[ ${#legacy_snapshots[@]} -eq 0 ]]; then
        LEGACY_SNAPSHOT_DIR="$RELEASES_DIR/legacy-flat-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        LEGACY_STAGE_DIR="$(mktemp -d "$RELEASES_DIR/.legacy-snapshot.XXXXXX")"
        legacy_files=()
        for source_file in \
            voicetyping.py \
            audio_routing.py \
            live_transcription.py \
            recording_recovery.py \
            auto_paste.py \
            voice_overlay.py \
            voice_visual.py \
            voicetyping_toggle.py \
            setup_shortcut.py \
            desktop_environment.py \
            desktop_integration.py \
            shortcuts.py \
            xdg_portal.py; do
            if [[ -f "$INSTALL_ROOT/$source_file" && ! -L "$INSTALL_ROOT/$source_file" ]]; then
                install -m 0644 "$INSTALL_ROOT/$source_file" "$LEGACY_STAGE_DIR/$source_file"
                legacy_files+=("$LEGACY_STAGE_DIR/$source_file")
            fi
        done
        if [[ ${#legacy_files[@]} -eq 0 ]]; then
            rmdir "$LEGACY_STAGE_DIR"
            LEGACY_STAGE_DIR=""
        else
            (
                cd "$LEGACY_STAGE_DIR"
                sha256sum -- *.py > SHA256SUMS
                cat > SNAPSHOT-INFO.txt <<EOF
Legacy flat source snapshot created before migration to release-based installation.
Original installation files remain at: $INSTALL_ROOT
Verify its Python files with: sha256sum --check SHA256SUMS
EOF
            )
            mv -- "$LEGACY_STAGE_DIR" "$LEGACY_SNAPSHOT_DIR"
            LEGACY_STAGE_DIR=""
            echo "📦 Saved the previous source installation to $LEGACY_SNAPSHOT_DIR"
        fi
    fi
fi

STAGE_DIR="$(mktemp -d "$RELEASES_DIR/.stage.XXXXXX")"
VENV_PYTHON_BIN="$STAGE_DIR/.venv/bin/python"

echo "🐍 Creating an isolated environment with access to system desktop libraries..."
"$SYSTEM_PYTHON_BIN" -m venv --system-site-packages "$STAGE_DIR/.venv" || {
    echo "❌ Could not create the Python environment. Install your distribution's venv package and retry." >&2
    exit 1
}
if ! "$VENV_PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "❌ The installation environment requires Python 3.10 or newer." >&2
    exit 1
fi

SOURCE_VERSION="$("$SYSTEM_PYTHON_BIN" - "$SCRIPT_DIR/src/voicetyping/__init__.py" <<'PY'
import ast
import pathlib
import sys

tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__version__"
        for target in node.targets
    ):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            print(node.value.value)
            raise SystemExit
raise SystemExit("Could not read voicetyping.__version__")
PY
)"

WHEEL_PATH="${VOICETYPING_WHEEL:-}"
if [[ -n "$WHEEL_PATH" ]]; then
    if [[ ! -f "$WHEEL_PATH" ]]; then
        echo "❌ VOICETYPING_WHEEL is not a file: $WHEEL_PATH" >&2
        exit 1
    fi
else
    WHEEL_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/voicetyping-wheel.XXXXXX")"
    echo "📦 Building the VoiceTyping wheel from this source checkout..."
    if command -v uv >/dev/null 2>&1; then
        uv build --wheel --out-dir "$WHEEL_BUILD_DIR" "$SCRIPT_DIR"
    else
        "$VENV_PYTHON_BIN" -m pip wheel --no-deps --wheel-dir "$WHEEL_BUILD_DIR" "$SCRIPT_DIR"
    fi
    shopt -s nullglob
    built_wheels=("$WHEEL_BUILD_DIR"/voicetyping-*.whl)
    shopt -u nullglob
    if [[ ${#built_wheels[@]} -ne 1 ]]; then
        echo "❌ Expected one built VoiceTyping wheel, found ${#built_wheels[@]}." >&2
        exit 1
    fi
    WHEEL_PATH="${built_wheels[0]}"
fi
WHEEL_PATH="$("$SYSTEM_PYTHON_BIN" -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$WHEEL_PATH")"
WHEEL_VERSION="$("$SYSTEM_PYTHON_BIN" - "$WHEEL_PATH" <<'PY'
import email.parser
import pathlib
import sys
import zipfile

wheel = pathlib.Path(sys.argv[1])
with zipfile.ZipFile(wheel) as archive:
    metadata_files = [
        name for name in archive.namelist()
        if name.endswith(".dist-info/METADATA")
        and pathlib.PurePosixPath(name).parent.name.startswith("voicetyping-")
    ]
    if len(metadata_files) != 1:
        raise SystemExit("Wheel does not contain one VoiceTyping METADATA file")
    metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_files[0]))
version = metadata.get("Version")
if not version:
    raise SystemExit("Wheel metadata has no Version")
print(version)
PY
)"
if [[ "$WHEEL_VERSION" != "$SOURCE_VERSION" ]]; then
    echo "❌ Wheel version $WHEEL_VERSION does not match source version $SOURCE_VERSION." >&2
    exit 1
fi

echo "🐍 Installing locked Python libraries into the private environment..."
PYTHONNOUSERSITE=1 "$VENV_PYTHON_BIN" -m pip install \
    --ignore-installed \
    --require-hashes \
    --requirement "$SCRIPT_DIR/requirements.txt"
PYTHONNOUSERSITE=1 "$VENV_PYTHON_BIN" -m pip install --no-deps "$WHEEL_PATH"
PYTHONNOUSERSITE=1 "$VENV_PYTHON_BIN" - "$STAGE_DIR/.venv" <<'PY'
import importlib.metadata
import pathlib
import sys

venv = pathlib.Path(sys.argv[1]).resolve()
required_distributions = (
    "google-genai",
    "numpy",
    "pillow",
    "pyperclip",
    "pynput",
    "pystray",
    "sounddevice",
    "soundfile",
    "voicetyping",
)
for name in required_distributions:
    location = pathlib.Path(importlib.metadata.distribution(name).locate_file("")).resolve()
    if not location.is_relative_to(venv):
        raise SystemExit(f"{name} resolved outside the release environment: {location}")
print("Locked application dependencies are installed inside the release environment.")
PY

# Virtual-environment entry-point shebangs contain the temporary staging path.
# Replace the two application commands with relocatable wrappers before the
# release directory is moved into place.
cat > "$STAGE_DIR/.venv/bin/voicetyping" <<'EOF'
#!/bin/sh
bindir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$bindir/python" -m voicetyping.app "$@"
EOF
cat > "$STAGE_DIR/.venv/bin/voicetyping-toggle" <<'EOF'
#!/bin/sh
bindir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$bindir/python" -m voicetyping.voicetyping_toggle "$@"
EOF
chmod 0755 "$STAGE_DIR/.venv/bin/voicetyping" "$STAGE_DIR/.venv/bin/voicetyping-toggle"

PYTHONNOUSERSITE=1 "$VENV_PYTHON_BIN" -c 'import voicetyping, voicetyping.audio_routing, voicetyping.recording_recovery'
APP_VERSION="$(PYTHONNOUSERSITE=1 "$VENV_PYTHON_BIN" -c 'import voicetyping; print(voicetyping.__version__)')"
if [[ "$APP_VERSION" != "$SOURCE_VERSION" ]]; then
    echo "❌ Installed VoiceTyping version $APP_VERSION does not match $SOURCE_VERSION." >&2
    exit 1
fi
if [[ "$(PYTHONNOUSERSITE=1 "$STAGE_DIR/.venv/bin/voicetyping" --version)" != "VoiceTyping $APP_VERSION" ]]; then
    echo "❌ The staged VoiceTyping command failed its version check." >&2
    exit 1
fi

RELEASE_NAME="${APP_VERSION}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_NAME"
if [[ -e "$RELEASE_DIR" ]]; then
    echo "❌ Release path already exists: $RELEASE_DIR" >&2
    exit 1
fi
mv -- "$STAGE_DIR" "$RELEASE_DIR"
STAGE_DIR=""

mkdir -p "$APPLICATIONS_DIR" "$AUTOSTART_DIR" "$AUTOSTART_DROPIN_DIR"

desktop_quote() {
    local escaped=${1//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    escaped=${escaped//%/%%}
    printf '"%s"' "$escaped"
}

DESKTOP_FILE="$APPLICATIONS_DIR/voicetyping.desktop"
DESKTOP_COMMAND="$(desktop_quote "$INSTALL_ROOT/current/.venv/bin/voicetyping")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Name=VoiceTyping
Comment=Speech to text converter
Exec=$DESKTOP_COMMAND
Icon=audio-input-microphone
Terminal=false
Type=Application
Categories=Utility;
EOF
install -m 0644 "$DESKTOP_FILE" "$AUTOSTART_DIR/voicetyping.desktop"

cat > "$AUTOSTART_DROPIN_DIR/override.conf" <<'EOF'
[Service]
Restart=on-failure
RestartSec=2s
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONNOUSERSITE=1
EOF

CURRENT_TMP="$INSTALL_ROOT/.current.$$"
ln -s "releases/$RELEASE_NAME" "$CURRENT_TMP"
mv -Tf -- "$CURRENT_TMP" "$INSTALL_ROOT/current"
CURRENT_TMP=""

# The XDG autostart generator ignores desktop entries whose Exec path does not
# exist yet. Reload only after the current release symlink is in place.
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
fi

echo "⌨️ Configuring GNOME shortcut..."
VOICETYPING_LAUNCHER="$INSTALL_ROOT/current/.venv/bin/voicetyping" \
    PYTHONNOUSERSITE=1 \
    "$INSTALL_ROOT/current/.venv/bin/python" -m voicetyping.setup_shortcut || true

echo "=================================================="
echo "✅ Installation completed. Launch VoiceTyping from the Applications menu."
echo "The app stores its Gemini API key in ~/.config/voicetyping/config.json."
echo "Previous releases are kept under $RELEASES_DIR for rollback."
echo "If VoiceTyping was already running, quit and relaunch it to load the new files."
echo "=================================================="
