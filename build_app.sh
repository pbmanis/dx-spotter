#!/usr/bin/env bash
# Build DX Spotter as a macOS .app bundle.
# Run from the project root:  ./build_app.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
ICON_PNG="$PROJECT_ROOT/src/icons/dxspot.png"
ICON_ICNS="$PROJECT_ROOT/src/icons/dxspot.icns"
ICONSET_DIR="/tmp/dxspot.iconset"

echo "=== Step 1: Convert icon PNG → ICNS ==="
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"

sips -z 16   16   "$ICON_PNG" --out "$ICONSET_DIR/icon_16x16.png"        >/dev/null
sips -z 32   32   "$ICON_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png"     >/dev/null
sips -z 32   32   "$ICON_PNG" --out "$ICONSET_DIR/icon_32x32.png"        >/dev/null
sips -z 64   64   "$ICON_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png"     >/dev/null
sips -z 128  128  "$ICON_PNG" --out "$ICONSET_DIR/icon_128x128.png"      >/dev/null
sips -z 256  256  "$ICON_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png"   >/dev/null
sips -z 256  256  "$ICON_PNG" --out "$ICONSET_DIR/icon_256x256.png"      >/dev/null
sips -z 512  512  "$ICON_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png"   >/dev/null
sips -z 512  512  "$ICON_PNG" --out "$ICONSET_DIR/icon_512x512.png"      >/dev/null
sips -z 1024 1024 "$ICON_PNG" --out "$ICONSET_DIR/icon_512x512@2x.png"   >/dev/null

iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS"
rm -rf "$ICONSET_DIR"
echo "    Created: $ICON_ICNS"

PYTHON="$PROJECT_ROOT/.venv/bin/python"
PYI="$PROJECT_ROOT/.venv/bin/pyinstaller"

echo "=== Step 2: Ensure pyinstaller is installed in the project venv ==="
cd "$PROJECT_ROOT"
if [ ! -f "$PYI" ]; then
    echo "    Adding pyinstaller to dev dependencies..."
    uv add --dev pyinstaller
fi
echo "    Python:      $("$PYTHON" --version)"
echo "    pyinstaller: $("$PYI" --version)"

echo "=== Step 3: Clean previous build artifacts ==="
rm -rf "$PROJECT_ROOT/build" "$PROJECT_ROOT/dist"

echo "=== Step 4: Build .app bundle ==="
"$PYI" "$PROJECT_ROOT/dx-spotter.spec" \
    --distpath "$PROJECT_ROOT/dist" \
    --workpath "$PROJECT_ROOT/build" \
    --noconfirm

echo ""
echo "=== Build complete ==="
echo "    App bundle: $PROJECT_ROOT/dist/DXSpotter.app"
echo ""
echo "To test: open \"$PROJECT_ROOT/dist/DXSpotter.app\""
