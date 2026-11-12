#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCH="${ARCH:-amd64}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SOURCE_VERSION="$("$PYTHON_BIN" - "$PROJECT_DIR/src/voicetyping/__init__.py" <<'PY'
import ast
import pathlib
import sys

tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__version__"
        for target in node.targets
    ) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        print(node.value.value)
        raise SystemExit
raise SystemExit("Could not read voicetyping.__version__")
PY
)"
VERSION="${1:-$SOURCE_VERSION}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_DIR/dist}"
PACKAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/voicetyping-deb.XXXXXX")"

cleanup() {
    rm -rf -- "$PACKAGE_ROOT"
}
trap cleanup EXIT

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Version must use MAJOR.MINOR.PATCH format." >&2
    exit 2
fi
if [[ "$VERSION" != "$SOURCE_VERSION" ]]; then
    echo "Version $VERSION does not match source version $SOURCE_VERSION." >&2
    exit 2
fi

WHEEL_PATH="${VOICETYPING_WHEEL:-}"
if [[ -z "$WHEEL_PATH" ]]; then
    shopt -s nullglob
    wheel_candidates=("$PROJECT_DIR"/dist/voicetyping-"$VERSION"-*.whl)
    shopt -u nullglob
    if [[ ${#wheel_candidates[@]} -ne 1 ]]; then
        echo "Expected one VoiceTyping $VERSION wheel in dist; set VOICETYPING_WHEEL." >&2
        exit 2
    fi
    WHEEL_PATH="${wheel_candidates[0]}"
fi
if [[ ! -f "$WHEEL_PATH" ]]; then
    echo "VOICETYPING_WHEEL is not a file: $WHEEL_PATH" >&2
    exit 2
fi
WHEEL_PATH="$("$PYTHON_BIN" -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$WHEEL_PATH")"
WHEEL_VERSION="$("$PYTHON_BIN" - "$WHEEL_PATH" <<'PY'
import email.parser
import pathlib
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    files = [
        name for name in archive.namelist()
        if name.endswith(".dist-info/METADATA")
        and pathlib.PurePosixPath(name).parent.name.startswith("voicetyping-")
    ]
    if len(files) != 1:
        raise SystemExit("Wheel does not contain one VoiceTyping METADATA file")
    metadata = email.parser.BytesParser().parsebytes(archive.read(files[0]))
version = metadata.get("Version")
if not version:
    raise SystemExit("Wheel metadata has no Version")
print(version)
PY
)"
if [[ "$WHEEL_VERSION" != "$VERSION" ]]; then
    echo "Wheel version $WHEEL_VERSION does not match requested version $VERSION." >&2
    exit 2
fi

PYTHON_MINOR="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_MINOR" != "3.12" ]]; then
    echo "The Debian package targets Ubuntu 24.04 and must be built with Python 3.12." >&2
    exit 2
fi

for command in dpkg-deb install; do
    command -v "$command" >/dev/null || {
        echo "Missing build command: $command" >&2
        exit 2
    }
done

APP_ROOT="$PACKAGE_ROOT/opt/voicetyping"
install -d \
    "$PACKAGE_ROOT/DEBIAN" \
    "$APP_ROOT/vendor" \
    "$PACKAGE_ROOT/usr/bin" \
    "$PACKAGE_ROOT/usr/share/applications" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$PACKAGE_ROOT/usr/share/metainfo" \
    "$PACKAGE_ROOT/usr/share/doc/voicetyping" \
    "$PACKAGE_ROOT/etc/xdg/autostart" \
    "$OUTPUT_DIR"

"$PYTHON_BIN" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --require-hashes \
    --target "$APP_ROOT/vendor" \
    --requirement "$PROJECT_DIR/requirements.txt"
"$PYTHON_BIN" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-deps \
    --target "$APP_ROOT/vendor" \
    "$WHEEL_PATH"

find "$APP_ROOT/vendor" -type d -name __pycache__ -prune -exec rm -rf -- {} +

install -m 0755 "$PROJECT_DIR/packaging/deb/voicetyping" "$PACKAGE_ROOT/usr/bin/voicetyping"
ln -s voicetyping "$PACKAGE_ROOT/usr/bin/voicetyping-toggle"
install -m 0644 "$PROJECT_DIR/packaging/io.github.fadayat.voicetyping.desktop" \
    "$PACKAGE_ROOT/usr/share/applications/io.github.fadayat.voicetyping.desktop"
install -m 0644 "$PROJECT_DIR/packaging/io.github.fadayat.voicetyping.desktop" \
    "$PACKAGE_ROOT/etc/xdg/autostart/io.github.fadayat.voicetyping.desktop"
install -m 0644 "$PROJECT_DIR/packaging/voicetyping.svg" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/voicetyping.svg"
install -m 0644 "$PROJECT_DIR/packaging/io.github.fadayat.voicetyping.metainfo.xml" \
    "$PACKAGE_ROOT/usr/share/metainfo/io.github.fadayat.voicetyping.metainfo.xml"
install -m 0644 "$PROJECT_DIR/README.md" "$PACKAGE_ROOT/usr/share/doc/voicetyping/README.md"
install -m 0644 "$PROJECT_DIR/CHANGELOG.md" "$PACKAGE_ROOT/usr/share/doc/voicetyping/CHANGELOG.md"
install -m 0644 "$PROJECT_DIR/LICENSE" "$PACKAGE_ROOT/usr/share/doc/voicetyping/copyright"
if [[ -f "$PROJECT_DIR/PRIVACY.md" ]]; then
    install -m 0644 "$PROJECT_DIR/PRIVACY.md" "$PACKAGE_ROOT/usr/share/doc/voicetyping/PRIVACY.md"
fi

INSTALLED_SIZE="$(du -sk "$PACKAGE_ROOT" | awk '{print $1}')"
cat >"$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: voicetyping
Version: ${VERSION}-1
Section: sound
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Maintainer: fadayat <noreply@github.com>
Depends: python3 (>= 3.12), python3 (<< 3.13), python3-gi, python3-cairo, python3-tk, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, libportaudio2, libsndfile1, wl-clipboard, xclip
Homepage: https://voicetyping1.vercel.app/
Description: F8 voice typing for Linux
 VoiceTyping records speech between two F8 presses, transcribes it with Gemini,
 and pastes the finalized text into the focused field.
EOF

OUTPUT_PATH="$OUTPUT_DIR/voicetyping_${VERSION}-1_ubuntu24.04_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$PACKAGE_ROOT" "$OUTPUT_PATH"
(
    cd "$OUTPUT_DIR"
    sha256sum "$(basename "$OUTPUT_PATH")" >"$(basename "$OUTPUT_PATH").sha256"
)
echo "$OUTPUT_PATH"
