#!/usr/bin/env bash
# Build the DX Spotter Sphinx HTML documentation.
# Output: docs/_build/html/index.html
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Building DX Spotter documentation..."
uv run sphinx-build -b html docs docs/_build/html "$@"

echo ""
echo "Done. Open docs/_build/html/index.html to view."
