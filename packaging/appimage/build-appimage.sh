#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-2.1.1}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_DIR/dist}"
BUILDER="${APPIMAGE_BUILDER:-appimage-builder}"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/voicetyping-appimage.XXXXXX")"

cleanup() {
    rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Version must use MAJOR.MINOR.PATCH format." >&2
    exit 2
fi

mkdir -p "$OUTPUT_DIR"
cp "$PROJECT_DIR/packaging/appimage/AppImageBuilder.yml" "$BUILD_DIR/AppImageBuilder.yml"
sed -i "s/version: 2\.1\.1/version: $VERSION/" "$BUILD_DIR/AppImageBuilder.yml"

(
    cd "$BUILD_DIR"
    VOICETYPING_PROJECT_DIR="$PROJECT_DIR" \
        APPIMAGE_EXTRACT_AND_RUN=1 \
        "$BUILDER" --recipe "$BUILD_DIR/AppImageBuilder.yml" --skip-test
)

BUILT_IMAGE="$(find "$BUILD_DIR" -maxdepth 1 -type f -name '*.AppImage' -print -quit)"
if [[ -z "$BUILT_IMAGE" ]]; then
    echo "appimage-builder did not create an AppImage." >&2
    exit 1
fi

OUTPUT_PATH="$OUTPUT_DIR/VoiceTyping-${VERSION}-x86_64.AppImage"
install -m 0755 "$BUILT_IMAGE" "$OUTPUT_PATH"
sha256sum "$OUTPUT_PATH" >"$OUTPUT_PATH.sha256"
echo "$OUTPUT_PATH"
