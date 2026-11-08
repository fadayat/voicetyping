#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-2.2.0}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_DIR/dist}"
BUILDER="${APPIMAGE_BUILDER:-appimage-builder}"
APPIMAGETOOL="${APPIMAGETOOL:-}"
APPIMAGE_RUNTIME="${APPIMAGE_RUNTIME:-}"
BUILDER_APPRUN="${APPIMAGE_BUILDER_APPRUN:-}"
BUILDER_HOOKS="${APPIMAGE_BUILDER_HOOKS:-}"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/voicetyping-appimage.XXXXXX")"

cleanup() {
    rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Version must use MAJOR.MINOR.PATCH format." >&2
    exit 2
fi

if [[ -z "$APPIMAGETOOL" || ! -x "$APPIMAGETOOL" ]]; then
    echo "APPIMAGETOOL must point to an executable appimagetool binary." >&2
    exit 2
fi
if [[ -z "$APPIMAGE_RUNTIME" || ! -r "$APPIMAGE_RUNTIME" ]]; then
    echo "APPIMAGE_RUNTIME must point to a readable AppImage type-2 runtime." >&2
    exit 2
fi
if [[ -n "$BUILDER_APPRUN" || -n "$BUILDER_HOOKS" ]]; then
    if [[ ! -r "$BUILDER_APPRUN" || ! -r "$BUILDER_HOOKS" ]]; then
        echo "APPIMAGE_BUILDER_APPRUN and APPIMAGE_BUILDER_HOOKS must both point to readable files." >&2
        exit 2
    fi
    BUILDER_CACHE="$BUILD_DIR/appimage-build/AppRun/v2.0.0"
    mkdir -p "$BUILDER_CACHE"
    install -m 0755 "$BUILDER_APPRUN" "$BUILDER_CACHE/AppRun-Release-x86_64"
    install -m 0644 "$BUILDER_HOOKS" "$BUILDER_CACHE/libapprun_hooks-Release-x86_64.so"
fi
if [[ "$BUILDER" == */* ]]; then
    if [[ ! -x "$BUILDER" ]]; then
        echo "APPIMAGE_BUILDER is not executable: $BUILDER" >&2
        exit 2
    fi
elif ! command -v "$BUILDER" >/dev/null 2>&1; then
    echo "Could not find appimage-builder. Set APPIMAGE_BUILDER to its executable path." >&2
    exit 2
fi

mkdir -p "$OUTPUT_DIR"
cp "$PROJECT_DIR/packaging/appimage/AppImageBuilder.yml" "$BUILD_DIR/AppImageBuilder.yml"
sed -i -E "s/version: [0-9]+\.[0-9]+\.[0-9]+/version: $VERSION/" "$BUILD_DIR/AppImageBuilder.yml"

(
    cd "$BUILD_DIR"
    VOICETYPING_PROJECT_DIR="$PROJECT_DIR" \
        APPIMAGE_EXTRACT_AND_RUN=1 \
        "$BUILDER" --recipe "$BUILD_DIR/AppImageBuilder.yml" \
        --skip-tests --skip-appimage
)

if [[ ! -f "$BUILD_DIR/AppDir/AppRun.env" ]]; then
    echo "appimage-builder did not create AppRun.env." >&2
    exit 1
fi
sed -i \
    -e 's|^APPDIR_EXEC_PATH=.*|APPDIR_EXEC_PATH=$APPDIR/usr/bin/voicetyping-launcher|' \
    -e 's|^APPDIR_EXEC_ARGS=.*|APPDIR_EXEC_ARGS=$@|' \
    "$BUILD_DIR/AppDir/AppRun.env"

PACKAGED_DIR="$BUILD_DIR/packaged"
mkdir -p "$PACKAGED_DIR"
PACKAGED_IMAGE="$PACKAGED_DIR/VoiceTyping-${VERSION}-x86_64.AppImage"
(
    cd "$BUILD_DIR"
    ARCH=x86_64 VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 \
        "$APPIMAGETOOL" \
        --no-appstream \
        --runtime-file "$APPIMAGE_RUNTIME" \
        --updateinformation 'gh-releases-zsync|fadayat|voicetyping|latest|VoiceTyping-*-x86_64.AppImage.zsync' \
        "$BUILD_DIR/AppDir" "$PACKAGED_IMAGE"
)

OUTPUT_PATH="$OUTPUT_DIR/VoiceTyping-${VERSION}-x86_64.AppImage"
install -m 0755 "$PACKAGED_IMAGE" "$OUTPUT_PATH"
GENERATED_ZSYNC="$BUILD_DIR/$(basename "$PACKAGED_IMAGE").zsync"
if [[ ! -f "$GENERATED_ZSYNC" ]]; then
    echo "appimagetool did not create the zsync update file." >&2
    exit 1
fi
install -m 0644 "$GENERATED_ZSYNC" "$OUTPUT_PATH.zsync"
(
    cd "$OUTPUT_DIR"
    sha256sum "$(basename "$OUTPUT_PATH")" >"$(basename "$OUTPUT_PATH").sha256"
)
echo "$OUTPUT_PATH"
