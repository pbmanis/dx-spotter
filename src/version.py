"""Single source of truth for the DX Spotter version string."""
from importlib.metadata import version as _pkg_version, PackageNotFoundError

# Hardcoded version is authoritative for source-tree runs.  The package
# metadata (populated by `uv sync` / pip install) is used only when it
# agrees with this string, so a stale installed record doesn't shadow it.
_HARDCODED = "0.2.1"

try:
    _installed = _pkg_version("dx-spotter")
    __version__: str = _installed if _installed == _HARDCODED else _HARDCODED
except PackageNotFoundError:
    __version__ = _HARDCODED
