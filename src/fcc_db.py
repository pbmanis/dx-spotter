"""FCC amateur callsign database for spotter location lookup.

Downloads the FCC ULS (Universal Licensing System) amateur radio license
database and builds a local SQLite cache that maps callsigns to approximate
geographic coordinates.

US coordinates come from the FCC LA (Location) table when present — this
covers club stations, repeater trustees, and some fixed stations.  For the
majority of individual HF operators (who have no LA record), the operator's
state from the EN (Entity) table is used together with a hardcoded state
centroid.  Canadian stations are resolved to province centroids via callsign-
prefix mapping.

Database location: ``~/Library/Application Support/DXSpotter/fcc_calls.db``
Source URL: ``https://data.fcc.gov/download/pub/uls/complete/l_amat.zip``
"""
from __future__ import annotations

import functools
import io
import math
import sqlite3
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

FCC_ULS_URL: str = (
    "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
)

# US state / territory centroid coordinates (decimal degrees)
_US_STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AK": (64.200841, -153.493843),
    "AL": (32.806671, -86.791130),
    "AR": (34.969704, -92.373123),
    "AZ": (33.729759, -111.431221),
    "CA": (36.116203, -119.681564),
    "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371),
    "DC": (38.897438, -77.026817),
    "DE": (39.318523, -75.507141),
    "FL": (27.766279, -81.686783),
    "GA": (33.040619, -83.643074),
    "GU": (13.444304, 144.793731),
    "HI": (21.094318, -157.498337),
    "IA": (42.011539, -93.210526),
    "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137),
    "IN": (39.849426, -86.258278),
    "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067),
    "LA": (31.169960, -91.867805),
    "MA": (42.230171, -71.530106),
    "MD": (39.063946, -76.802101),
    "ME": (44.693947, -69.381927),
    "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192),
    "MO": (38.456085, -92.288368),
    "MP": (15.097803, 145.673450),
    "MS": (32.741646, -89.678696),
    "MT": (46.921925, -110.454353),
    "NC": (35.630066, -79.806419),
    "ND": (47.528912, -99.784012),
    "NE": (41.125370, -98.268082),
    "NH": (43.452492, -71.563896),
    "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482),
    "NV": (38.313515, -117.055374),
    "NY": (42.165726, -74.948051),
    "OH": (40.388783, -82.764915),
    "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938),
    "PA": (40.590752, -77.209755),
    "PR": (18.220833, -66.589977),
    "RI": (41.680893, -71.511780),
    "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828),
    "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461),
    "UT": (40.150032, -111.862434),
    "VA": (37.769337, -78.169968),
    "VI": (17.739625, -64.896335),
    "VT": (44.045876, -72.710686),
    "WA": (47.400902, -121.490494),
    "WI": (44.268543, -89.616508),
    "WV": (38.491226, -80.954453),
    "WY": (42.755966, -107.302490),
}

# Canadian province/territory centroids keyed by the 3-character VE/VA/VO/VY
# callsign prefix (prefix letter + area digit).
_CA_CENTROIDS: dict[str, tuple[float, float]] = {
    "VE1": (45.277775, -63.264988),  # Nova Scotia / New Brunswick
    "VE2": (52.939916, -73.549140),  # Quebec
    "VE3": (51.253775, -85.323214),  # Ontario
    "VE4": (53.760886, -98.813965),  # Manitoba
    "VE5": (52.939916, -106.450989),  # Saskatchewan
    "VE6": (53.933271, -116.576503),  # Alberta
    "VE7": (53.726669, -127.647621),  # British Columbia
    "VE8": (64.825560, -124.846479),  # Northwest Territories
    "VE9": (46.565685, -66.461914),  # New Brunswick
    "VA2": (52.939916, -73.549140),  # Quebec
    "VA3": (51.253775, -85.323214),  # Ontario
    "VA4": (53.760886, -98.813965),  # Manitoba
    "VA5": (52.939916, -106.450989),  # Saskatchewan
    "VA6": (53.933271, -116.576503),  # Alberta
    "VA7": (53.726669, -127.647621),  # British Columbia
    "VO1": (53.135509, -57.660435),  # Newfoundland
    "VO2": (53.135509, -60.000000),  # Labrador
    "VY0": (70.453262, -86.798981),  # Nunavut
    "VY1": (63.000000, -135.000000),  # Yukon
    "VY2": (46.250000, -63.000000),  # Prince Edward Island
}


# ── geographic utilities ──────────────────────────────────────────────────────


def grid_to_latlon(grid: str) -> tuple[float, float]:
    """Convert a Maidenhead grid locator to the center (lat, lon) in decimal degrees.

    Parameters
    ----------
    grid : str
        4- or 6-character Maidenhead grid square (e.g. ``'FM05'`` or
        ``'FM05kw'``).

    Returns
    -------
    tuple[float, float]
        ``(latitude, longitude)`` of the grid square center.

    Raises
    ------
    ValueError
        When *grid* is not a valid 4- or 6-character locator.
    """
    g = grid.upper().strip()
    if len(g) < 4:
        raise ValueError(f"Grid too short: {grid!r}")

    lon = (ord(g[0]) - ord("A")) * 20.0 - 180.0
    lat = (ord(g[1]) - ord("A")) * 10.0 - 90.0
    lon += (ord(g[2]) - ord("0")) * 2.0
    lat += ord(g[3]) - ord("0")

    if len(g) >= 6:
        lon += (ord(g[4].lower()) - ord("a") + 0.5) / 12.0
        lat += (ord(g[5].lower()) - ord("a") + 0.5) / 24.0
    else:
        lon += 1.0
        lat += 0.5

    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two lat/lon points.

    Parameters
    ----------
    lat1, lon1 : float
        First point (decimal degrees).
    lat2, lon2 : float
        Second point (decimal degrees).

    Returns
    -------
    float
        Distance in kilometres.
    """
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2.0 * r * math.asin(math.sqrt(a))


# ── database paths and metadata ───────────────────────────────────────────────

def fcc_db_path() -> Path:
    """Return the path to the local FCC callsign SQLite database.

    Returns
    -------
    Path
        ``~/Library/Application Support/DXSpotter/fcc_calls.db`` on macOS.
    """
    return (
        Path.home() / "Library" / "Application Support" / "DXSpotter" / "fcc_calls.db"
    )


def fcc_db_age_days() -> float | None:
    """Return the age of the local FCC database in days.

    Returns
    -------
    float or None
        Age in fractional days since last modification, or ``None`` if the
        database does not exist.
    """
    p = fcc_db_path()
    if not p.exists():
        return None
    return (time.time() - p.stat().st_mtime) / 86400.0


def fcc_db_entry_count() -> int | None:
    """Return the number of callsigns in the local FCC database.

    Returns
    -------
    int or None
        Row count, or ``None`` if the database does not exist or is unreadable.
    """
    p = fcc_db_path()
    if not p.exists():
        return None
    try:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM callsigns").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return None


# ── DMS helpers ───────────────────────────────────────────────────────────────

def _dms_to_decimal(
    degrees: str, minutes: str, seconds: str, direction: str
) -> float | None:
    """Convert degrees/minutes/seconds + compass direction to decimal degrees."""
    try:
        d = float(degrees or 0)
        m = float(minutes or 0)
        s = float(seconds or 0)
        dec = d + m / 60.0 + s / 3600.0
        if direction.upper() in ("S", "W"):
            dec = -dec
        return dec
    except (ValueError, ZeroDivisionError):
        return None


# ── database build ────────────────────────────────────────────────────────────

def build_fcc_db(
    progress_cb: Callable[[str], None] | None = None
) -> None:
    """Download the FCC ULS amateur database and build the local SQLite cache.

    Downloads ``l_amat.zip`` from the FCC, extracts and parses ``HD.dat``,
    ``EN.dat``, and ``LA.dat``, then writes a SQLite database at
    :func:`fcc_db_path`.  Existing data is replaced atomically.

    Parameters
    ----------
    progress_cb : callable or None, optional
        Optional callback receiving a progress string on each major step.
        Safe to call from any thread.

    Raises
    ------
    Exception
        Any network or filesystem error is propagated to the caller.
    """

    def _progress(msg: str) -> None:
        print(msg)
        if progress_cb is not None:
            progress_cb(msg)

    # ── download ──────────────────────────────────────────────────────────────
    _progress("FCC DB: downloading l_amat.zip …")
    dest = fcc_db_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with urllib.request.urlopen(FCC_ULS_URL, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 65536
            with open(tmp_path, "wb") as out:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    out.write(data)
                    downloaded += len(data)
                    if total:
                        pct = downloaded * 100 // total
                        _progress(f"FCC DB: downloading … {pct}%")

        _progress(
            f"FCC DB: download complete ({downloaded // 1_048_576} MB) — parsing …"
        )

        # ── parse ─────────────────────────────────────────────────────────────
        active_calls: set[str] = set()
        # call → (name, city, state)
        call_info: dict[str, tuple[str, str, str]] = {}
        call_class: dict[str, str] = {}
        call_latlon: dict[str, tuple[float, float]] = {}

        with zipfile.ZipFile(tmp_path) as zf:
            names = {n.upper(): n for n in zf.namelist()}

            # HD.dat — collect active amateur licence call signs.
            # Fields (0-based): [1]=usi [3]=ebf(blank) [4]=call_sign [5]=status
            hd_name = names.get("HD.DAT")
            if hd_name:
                _progress("FCC DB: parsing HD.dat …")
                with zf.open(hd_name) as raw:
                    for line in io.TextIOWrapper(raw, encoding="latin-1"):
                        fields = line.rstrip("\n").split("|")
                        if len(fields) < 6:
                            continue
                        if fields[5].upper() == "A":  # Active
                            active_calls.add(fields[4].upper())

            _progress(f"FCC DB: {len(active_calls):,} active licences found")

            # EN.dat — collect name, city, state for licensee entity records.
            # Fields (0-based): [3]=ebf(blank) [4]=call_sign [5]=entity_type
            #   [6]=licensee_id [7]=entity_name [8]=first_name [9]=mi
            #   [10]=last_name [15]=street [16]=city [17]=state
            en_name = names.get("EN.DAT")
            if en_name:
                _progress("FCC DB: parsing EN.dat …")
                with zf.open(en_name) as raw:
                    for line in io.TextIOWrapper(raw, encoding="latin-1"):
                        fields = line.rstrip("\n").split("|")
                        if len(fields) < 18:
                            continue
                        call = fields[4].upper()
                        if call not in active_calls:
                            continue
                        if fields[5].upper() != "L":  # Licensee only
                            continue
                        state = fields[17].upper().strip()
                        if not state:
                            continue
                        city = fields[16].strip().title()
                        first = fields[8].strip()
                        last = fields[10].strip()
                        entity_name = fields[7].strip()
                        if first or last:
                            name = f"{first} {last}".strip()
                        else:
                            name = entity_name
                        call_info[call] = (name, city, state)

            _progress(f"FCC DB: {len(call_info):,} entity records found")

            # AM.dat — collect operator class for amateur licences.
            # Fields (0-based): [3]=ebf(blank) [4]=call_sign [5]=operator_class
            am_name = names.get("AM.DAT")
            if am_name:
                _progress("FCC DB: parsing AM.dat …")
                with zf.open(am_name) as raw:
                    for line in io.TextIOWrapper(raw, encoding="latin-1"):
                        fields = line.rstrip("\n").split("|")
                        if len(fields) < 6:
                            continue
                        call = fields[4].upper()
                        if call not in active_calls:
                            continue
                        cls = fields[5].upper().strip()
                        if cls:
                            call_class[call] = cls

            _progress(f"FCC DB: {len(call_class):,} class records found")

            # LA.dat in l_amat.zip is a "License Activity" table (cancellation
            # notices, etc.), not a location table — no lat/lon data present.
            _progress("FCC DB: no precise location data in l_amat (using state centroids)")

        # ── write SQLite ──────────────────────────────────────────────────────
        _progress("FCC DB: writing database …")
        tmp_db = dest.with_suffix(".db.tmp")
        with sqlite3.connect(tmp_db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS callsigns ("
                "call TEXT PRIMARY KEY, "
                "lat REAL NOT NULL, lon REAL NOT NULL, "
                "name TEXT, license_class TEXT, city TEXT, state TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_call ON callsigns (call)")

            rows: list[tuple[str, float, float, str, str, str, str]] = []
            for call, (name, city, state) in call_info.items():
                if call in call_latlon:
                    lat, lon = call_latlon[call]
                else:
                    centroid = _US_STATE_CENTROIDS.get(state)
                    if centroid is None:
                        continue
                    lat, lon = centroid
                cls = call_class.get(call, "")
                rows.append((call, lat, lon, name, cls, city, state))

            conn.executemany(
                "INSERT OR REPLACE INTO callsigns VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()

        tmp_db.replace(dest)
        clear_lookup_cache()
        _progress(f"FCC DB: done — {len(rows):,} callsigns indexed.")

    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── location lookup ───────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=8192)
def lookup_location(call: str) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` in decimal degrees for a callsign, or ``None``.

    US callsigns are looked up in the local FCC database (precise location or
    state centroid).  Canadian callsigns (VE, VA, VO, VY prefixes) are
    resolved to province centroids via prefix mapping.

    Parameters
    ----------
    call : str
        Amateur radio callsign (case-insensitive).

    Returns
    -------
    tuple[float, float] or None
        ``(latitude, longitude)`` in decimal degrees, or ``None`` when the
        call cannot be resolved.
    """
    call = call.upper().strip()

    # Strip /P, /MM, /AM etc. and prefix (DL/W1AW → W1AW for FCC lookup,
    # but DL/W1AW would not be in the FCC DB anyway; this mainly handles
    # portable designators on US calls like W1AW/4).
    home = call.split("/")[-1] if "/" in call else call
    if not any(c.isdigit() for c in home):
        home = call  # prefix-only fragment; try the full call

    # Canadian province centroid via 3-char prefix
    if home[:2] in ("VE", "VA", "VO", "VY"):
        prefix = home[:3]
        centroid = _CA_CENTROIDS.get(prefix)
        return centroid  # may be None for unrecognised prefix

    # FCC SQLite lookup
    p = fcc_db_path()
    if not p.exists():
        return None
    try:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT lat, lon FROM callsigns WHERE call = ?", (home,)
            ).fetchone()
            if row:
                return float(row[0]), float(row[1])
    except Exception:
        pass

    return None


def clear_lookup_cache() -> None:
    """Discard the in-process callsign location cache.

    Call after :func:`build_fcc_db` completes so that subsequent lookups
    read the newly built database.
    """
    lookup_location.cache_clear()


_CLASS_NAMES: dict[str, str] = {
    "N": "Novice",
    "T": "Technician",
    "G": "General",
    "A": "Advanced",
    "E": "Extra",
}


def lookup_callsign_info(call: str) -> dict[str, str] | None:
    """Return FCC licensee details for a US callsign, or ``None``.

    Parameters
    ----------
    call : str
        Amateur radio callsign (case-insensitive).  Portable and mobile
        designators (``/P``, ``/MM``, etc.) are stripped before lookup.

    Returns
    -------
    dict[str, str] or None
        Dictionary with keys ``'name'``, ``'license_class'``, ``'city'``,
        and ``'state'``, or ``None`` if the callsign is not found in the
        local FCC database.
    """
    home = call.upper().strip().split("/")[-1]
    if not any(c.isdigit() for c in home):
        home = call.upper().strip()

    p = fcc_db_path()
    if not p.exists():
        return None
    try:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT name, license_class, city, state "
                "FROM callsigns WHERE call = ?",
                (home,),
            ).fetchone()
            if row is None:
                return None
            cls_code = (row[1] or "").upper()
            return {
                "name": (row[0] or "").title(),
                "license_class": _CLASS_NAMES.get(cls_code, cls_code),
                "city": row[2] or "",
                "state": row[3] or "",
            }
    except Exception:
        return None
