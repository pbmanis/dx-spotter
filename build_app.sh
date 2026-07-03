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

# Composite the source PNG onto a white rounded-rectangle background so that:
#   - The dock icon has an opaque fill (won't vanish under macOS click-dimming).
#   - Transparent corners let macOS render the natural shaped outline in the dock.
# Corner radius matches the macOS Big Sur squircle (~22.5% of icon width).
SHAPED_PNG="/tmp/dxspot_shaped.png"
"$PROJECT_ROOT/.venv/bin/python3" -c "
from PIL import Image, ImageDraw
src, dst = '$ICON_PNG', '$SHAPED_PNG'
img    = Image.open(src).convert('RGBA')
size   = img.size
radius = int(size[0] * 0.225)
bg     = Image.new('RGBA', size, (255, 255, 255, 255))
bg.paste(img, mask=img.split()[3])   # composite artwork onto white
mask   = Image.new('L', size, 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0]-1, size[1]-1], radius=radius, fill=255)
result = Image.new('RGBA', size, (0, 0, 0, 0))
result.paste(bg, mask=mask)          # clip to rounded rect
result.save(dst)
print(f'    Shaped icon: {src} -> {dst}')
"

sips -z 16   16   "$SHAPED_PNG" --out "$ICONSET_DIR/icon_16x16.png"        >/dev/null
sips -z 32   32   "$SHAPED_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png"     >/dev/null
sips -z 32   32   "$SHAPED_PNG" --out "$ICONSET_DIR/icon_32x32.png"        >/dev/null
sips -z 64   64   "$SHAPED_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png"     >/dev/null
sips -z 128  128  "$SHAPED_PNG" --out "$ICONSET_DIR/icon_128x128.png"      >/dev/null
sips -z 256  256  "$SHAPED_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png"   >/dev/null
sips -z 256  256  "$SHAPED_PNG" --out "$ICONSET_DIR/icon_256x256.png"      >/dev/null
sips -z 512  512  "$SHAPED_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png"   >/dev/null
sips -z 512  512  "$SHAPED_PNG" --out "$ICONSET_DIR/icon_512x512.png"      >/dev/null
sips -z 1024 1024 "$SHAPED_PNG" --out "$ICONSET_DIR/icon_512x512@2x.png"   >/dev/null

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

echo "=== Step 3: Download fresh CTY country file ==="
mkdir -p "$PROJECT_ROOT/src/data"
"$PYTHON" -c "
import urllib.request, sys
url = 'https://www.country-files.com/cty/cty.plist'
dest = sys.argv[1]
print('    Downloading', url)
urllib.request.urlretrieve(url, dest)
print('    Saved:', dest)
" "$PROJECT_ROOT/src/data/cty.plist"

echo "=== Step 4: Clean previous build artifacts ==="
rm -rf "$PROJECT_ROOT/build" "$PROJECT_ROOT/dist"

echo "=== Step 5: Build .app bundle ==="
"$PYI" "$PROJECT_ROOT/dx-spotter.spec" \
    --distpath "$PROJECT_ROOT/dist" \
    --workpath "$PROJECT_ROOT/build" \
    --noconfirm

echo ""
echo "=== Build complete ==="
echo "    App bundle: $PROJECT_ROOT/dist/DXSpotter.app"
echo ""
echo "To test: open \"$PROJECT_ROOT/dist/DXSpotter.app\""
