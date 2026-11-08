#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-2.2.0}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_DIR/dist}"
BUILDER="${APPIMAGE_BUILDER:-appimage-builder}"
APPIMAGETOOL="${APPIMAGETOOL:-}"
APPIMAGE_RUNTIME="${APPIMAGE_RUNTIME:-}"
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
(
    cd "$OUTPUT_DIR"
    sha256sum "$(basename "$OUTPUT_PATH")" >"$(basename "$OUTPUT_PATH").sha256"
)
echo "$OUTPUT_PATH"
