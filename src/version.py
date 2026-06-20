"""Single source of truth for the DX Spotter version string."""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__: str = version("dx-spotter")
except PackageNotFoundError:
    # Running from source tree without an installed package record.
    __version__ = "0.1.0"
