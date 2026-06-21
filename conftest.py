"""Root pytest configuration.

Adds src/ to sys.path before any test module is imported, so that
``from tools.adif_dedup import ...`` and ``from adif_log import ...``
resolve correctly regardless of the pytest version or how pytest is invoked.
"""
import sys
from pathlib import Path

_src = str(Path(__file__).parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
