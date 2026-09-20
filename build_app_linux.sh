#!/usr/bin/env bash
# Build DX Spotter as a Linux executable.
# Run from the project root:  ./build_app_linux.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
PYI="$PROJECT_ROOT/.venv/bin/pyinstaller"
APP_NAME="DXSpotter"
BUILD_DIR="$PROJECT_ROOT/build_linux"
DIST_DIR="$PROJECT_ROOT/dist_linux"

mkdir -p "$BUILD_DIR" "$DIST_DIR"

echo "=== Step 1: Ensure pyinstaller is installed in the project venv ==="
cd "$PROJECT_ROOT"
if [ ! -f "$PYI" ]; then
    echo "    Adding pyinstaller to dev dependencies..."
    uv add --dev pyinstaller
fi

echo "    Python:      $($PYTHON --version)"
echo "    pyinstaller: $($PYI --version)"

echo "=== Step 2: Download fresh CTY country file ==="
mkdir -p "$PROJECT_ROOT/src/data"
"$PYTHON" - "$PROJECT_ROOT/src/data/cty.plist" <<'PY'
import sys
import urllib.request

url = 'https://www.country-files.com/cty/cty.plist'
dest = sys.argv[1]
print(f'    Downloading {url}')
urllib.request.urlretrieve(url, dest)
print(f'    Saved: {dest}')
PY

echo "=== Step 3: Clean previous Linux build artifacts ==="
rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

echo "=== Step 4: Build Linux executable ==="
"$PYI" \
    --noconfirm \
    --clean \
    --workpath "$BUILD_DIR" \
    --distpath "$DIST_DIR" \
    "$PROJECT_ROOT/build_linux.spec"

echo ""
echo "=== Build complete ==="
echo "    Executable: $DIST_DIR/$APP_NAME/$APP_NAME"
echo ""
echo "To test:"
echo "    $DIST_DIR/$APP_NAME/$APP_NAME --help"
