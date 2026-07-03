"""Local cache for the pyhamtools CTY country-file (``cty.plist``).

pyhamtools downloads ``cty.plist`` from the internet on every startup and
discards it after parsing.  This module maintains a persistent copy under
the DX Spotter application-support directory so subsequent starts are fast
and the bundled ``.app`` can work offline on first launch.

Cache location: ``~/Library/Application Support/DXSpotter/cty.plist``
Source URL: ``https://www.country-files.com/cty/cty.plist``
"""
from __future__ import annotations

import shutil
import sys
import time
import urllib.request
from pathlib import Path

CTY_URL: str = "https://www.country-files.com/cty/cty.plist"

_MAX_AGE_DAYS: int = 30


def cty_plist_path() -> Path:
    """Return the path to the cached CTY plist file.

    Returns
    -------
    Path
        ``~/Library/Application Support/DXSpotter/cty.plist`` on macOS.
    """
    return Path.home() / "Library" / "Application Support" / "DXSpotter" / "cty.plist"


def cty_age_days() -> float | None:
    """Return the age of the cached CTY file in days.

    Returns
    -------
    float or None
        Age in fractional days since the file was last modified, or ``None``
        when the file does not exist.
    """
    path = cty_plist_path()
    if not path.exists():
        return None
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds / 86400.0


def cty_is_fresh(max_age_days: int = _MAX_AGE_DAYS) -> bool:
    """Return whether the cached CTY file exists and is within *max_age_days*.

    Parameters
    ----------
    max_age_days : int, optional
        Maximum acceptable file age in days.  Default is ``30``.

    Returns
    -------
    bool
        ``True`` when the file exists and its age is less than *max_age_days*.
    """
    age = cty_age_days()
    return age is not None and age < max_age_days


def download_cty(dest: Path | None = None) -> Path:
    """Download the CTY plist from the internet to *dest*.

    Parameters
    ----------
    dest : Path or None, optional
        Destination file path.  Defaults to :func:`cty_plist_path`.

    Returns
    -------
    Path
        Path to the downloaded file.

    Raises
    ------
    urllib.error.URLError
        When the download fails (no internet, server unreachable, etc.).
    """
    target = dest if dest is not None else cty_plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading CTY file from {CTY_URL}")
    urllib.request.urlretrieve(CTY_URL, target)
    print(f"CTY file saved: {target}")
    return target


def ensure_cty(max_age_days: int = _MAX_AGE_DAYS) -> Path:
    """Ensure a fresh CTY file exists in the cache, downloading if necessary.

    Checks the local cache first.  When the file is missing or older than
    *max_age_days*, the function attempts to use a seed bundled inside a
    PyInstaller ``.app`` (offline first-run), then falls back to downloading
    from the internet.

    Parameters
    ----------
    max_age_days : int, optional
        Maximum acceptable file age in days.  Default is ``30``.

    Returns
    -------
    Path
        Path to the (now-current) cached CTY plist file.
    """
    if cty_is_fresh(max_age_days):
        return cty_plist_path()

    # Prefer a bundled seed when running from a PyInstaller bundle so the
    # app can start offline on first launch.
    if getattr(sys, "frozen", False):
        seed = Path(getattr(sys, "_MEIPASS", "")) / "data" / "cty.plist"
        if seed.exists():
            dest = cty_plist_path()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed, dest)
            print(f"CTY file seeded from bundle: {dest}")
            return dest

    return download_cty()
