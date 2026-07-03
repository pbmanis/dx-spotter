"""Post-processing corrections for pyhamtools DXCC entity lookups.

pyhamtools resolves callsign prefixes against the CTY country file but
cannot handle all ambiguous prefix blocks.  This module provides
:func:`correct_entity` to fix known misidentifications after
``Callinfo.get_all()`` has been called.

Corrections applied
-------------------
KG4
    Two-letter suffixes (e.g. ``KG4AB``) are Guantanamo Bay (ADIF 105) —
    already correct in pyhamtools.  Three-or-more-letter suffixes
    (e.g. ``KG4ZZZ``) are US stations; pyhamtools returns Guantanamo, but
    the correct entity is United States (ADIF 291).

JD1
    The JD1 prefix covers two entities.  Suffixes beginning with ``M``
    (e.g. ``JD1MBA``) indicate Minami Torishima (ADIF 168); all other
    suffixes indicate Ogasawara (ADIF 177).

3Y
    District digit ``0`` → Bouvet Island (ADIF 24);
    district digit ``9`` → Peter I Island (ADIF 199).
"""
from __future__ import annotations

import re

# -- compiled patterns -------------------------------------------------------

_KG4 = re.compile(r"^KG4([A-Z]{2,})$", re.IGNORECASE)
_JD1 = re.compile(r"^JD1([A-Z])", re.IGNORECASE)
_3Y = re.compile(r"^3Y(\d)", re.IGNORECASE)


def _home_call(call: str) -> str:
    """Strip /P, /MM, /QRP and similar suffixes; also strip DL/ prefixes.

    Returns the bare home callsign for prefix matching.
    """
    # Remove leading country prefix (DL/W1AW → W1AW)
    if "/" in call:
        parts = call.split("/")
        # Determine which part is the home call: the one containing a digit
        for part in parts:
            if any(c.isdigit() for c in part):
                call = part
                break
        else:
            call = parts[0]
    return call.upper()


def correct_entity(call: str, info: dict) -> dict:
    """Post-process a pyhamtools ``get_all()`` result to fix known mis-IDs.

    Parameters
    ----------
    call : str
        The original callsign passed to ``Callinfo.get_all()``.
    info : dict
        The dict returned by ``Callinfo.get_all(call)``.

    Returns
    -------
    dict
        A shallow copy of *info* with ``'adif'`` and ``'country'`` corrected
        where necessary.  The original dict is not mutated.
    """
    home = _home_call(call)

    # KG4: 3+ letter suffix → US, not Guantanamo Bay
    m = _KG4.match(home)
    if m and len(m.group(1)) >= 3:
        return {**info, "adif": 291, "country": "United States"}

    # JD1 split: M-suffix → Minami Torishima, others → Ogasawara
    m = _JD1.match(home)
    if m:
        if m.group(1).upper() == "M":
            return {**info, "adif": 168, "country": "Minami Torishima"}
        return {**info, "adif": 177, "country": "Ogasawara"}

    # 3Y split: 0-district → Bouvet Island, 9-district → Peter I Island
    m = _3Y.match(home)
    if m:
        if m.group(1) == "0":
            return {**info, "adif": 24, "country": "Bouvet Island"}
        if m.group(1) == "9":
            return {**info, "adif": 199, "country": "Peter I Island"}

    return info
