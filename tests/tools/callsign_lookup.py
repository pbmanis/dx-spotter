"""Command-line tool to inspect pyhamtools country/DXCC lookup for a callsign.

Usage
-----
    uv run python tests/tools/callsign_lookup.py <callsign> [--cty-plist FILE]

The tool prints every field returned by Callinfo.get_all() plus the results
of the auxiliary checks (home call, AM/MM/beacon flags, validity).  Useful
for diagnosing mis-identifications such as KG4xxx being tagged as Guantanamo
Bay rather than the continental US.
"""
from __future__ import annotations

import argparse
import sys

from pyhamtools import Callinfo, LookupLib


def _print_row(label: str, value: object) -> None:
    print(f"  {label:<22} {value}")


def lookup(call: str, cty_plist: str | None = None) -> None:
    """Look up *call* and print all available pyhamtools data.

    Parameters
    ----------
    call : str
        Amateur callsign to look up.
    cty_plist : str or None, optional
        Path to a local CTY plist file.  When ``None`` pyhamtools downloads
        the file from the internet on first run.
    """
    lib = LookupLib(lookuptype="countryfile", filename=cty_plist)
    ci = Callinfo(lib)

    print(f"\nCallsign: {call.upper()}")
    print("─" * 40)

    # Validity and auxiliary flags
    _print_row("Valid callsign:", ci.is_valid_callsign(call))
    _print_row("Home call:", ci.get_homecall(call))
    _print_row("Maritime mobile:", ci.check_if_mm(call))
    _print_row("Aeronautical mobile:", ci.check_if_am(call))
    _print_row("Beacon:", ci.check_if_beacon(call))

    print()

    # Full country/DXCC data
    try:
        info: dict = ci.get_all(call)
    except Exception as exc:  # noqa: BLE001
        print(f"  get_all() raised {type(exc).__name__}: {exc}")
        return

    field_labels: dict[str, str] = {
        "country":   "Country:",
        "adif":      "ADIF entity #:",
        "cqz":       "CQ zone:",
        "ituz":      "ITU zone:",
        "continent": "Continent:",
        "latitude":  "Latitude:",
        "longitude": "Longitude:",
    }
    for key, label in field_labels.items():
        if key in info:
            _print_row(label, info[key])

    # Print any keys not in the known list (future pyhamtools versions)
    extras = {k: v for k, v in info.items() if k not in field_labels}
    for key, val in extras.items():
        _print_row(f"{key}:", val)

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect pyhamtools country lookup for a callsign.",
    )
    parser.add_argument("callsign", help="Callsign to look up (e.g. KG4AB)")
    parser.add_argument(
        "--cty-plist",
        default=None,
        metavar="FILE",
        help="Path to a local CTY plist file; downloads from internet if omitted.",
    )
    args = parser.parse_args()

    try:
        lookup(args.callsign, args.cty_plist)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
