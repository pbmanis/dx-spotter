#!/usr/bin/env bash
# Build the DX Spotter Sphinx documentation.
#
# Usage:
#   ./build_docs.sh [--pdf] [sphinx-build flags...]
#
#   (no flags)  Build HTML output → docs/_build/html/index.html
#   --pdf       Build PDF output  → docs/_build/pdf/dxspotter.pdf
#               Requires a LaTeX installation (MacTeX / BasicTeX / TeX Live).
#
# Any extra flags are forwarded to sphinx-build (e.g. -E for a clean build).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_PDF=false
SPHINX_EXTRA=()

for arg in "$@"; do
    if [[ "$arg" == "--pdf" ]]; then
        BUILD_PDF=true
    else
        SPHINX_EXTRA+=("$arg")
    fi
done

if $BUILD_PDF; then
    LATEX_DIR="docs/_build/latex"
    PDF_OUT="docs/_build/pdf"

    echo "Building LaTeX source..."
    uv run sphinx-build -b latex "${SPHINX_EXTRA[@]}" docs "$LATEX_DIR"

    echo "Compiling PDF with latexmk (xelatex)..."
    mkdir -p "$PDF_OUT"
    # -f forces completion even when non-fatal LaTeX warnings are treated as errors
    # (TeX Live 2021 hyperref/babel mismatch; the PDF is produced correctly)
    latexmk -xelatex -interaction=nonstopmode -quiet -f \
        -outdir="$LATEX_DIR" "$LATEX_DIR/dxspotter.tex" 2>/dev/null || true

    if [[ -f "$LATEX_DIR/dxspotter.pdf" ]]; then
        cp "$LATEX_DIR/dxspotter.pdf" "$PDF_OUT/dxspotter.pdf"
        echo ""
        echo "Done. PDF is at $PDF_OUT/dxspotter.pdf"
    else
        echo "Error: PDF was not produced. Check docs/_build/latex/dxspotter.log" >&2
        exit 1
    fi
else
    echo "Building DX Spotter HTML documentation..."
    uv run sphinx-build -b html "${SPHINX_EXTRA[@]}" docs docs/_build/html
    echo ""
    echo "Done. Open docs/_build/html/index.html to view."
fi
