"""Command-line tool to look up FCC licensee information for a callsign.

Usage
-----
    uv run python tests/tools/fcc_lookup.py <callsign> [<callsign> ...]
    uv run python tests/tools/fcc_lookup.py --location W1AW
    uv run python tests/tools/fcc_lookup.py --db-info

The tool queries the local FCC SQLite database built by ``fcc_db.build_fcc_db()``
and prints the licensee name, license class, city, state, and (optionally) the
resolved location coordinates.  The database must be built first via
Settings → Update FCC Database in the DX Spotter application.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import fcc_db


def _print_row(label: str, value: object) -> None:
    print(f"  {label:<20} {value}")


def show_db_info() -> None:
    """Print metadata about the local FCC database."""
    p = fcc_db.fcc_db_path()
    print(f"DB path:  {p}")
    if not p.exists():
        print("  (not found — run Settings → Update FCC Database)")
        return
    age = fcc_db.fcc_db_age_days()
    count = fcc_db.fcc_db_entry_count()
    age_str = f"{age:.1f} days" if age is not None else "unknown"
    print(f"Age:      {age_str}")
    print(f"Entries:  {count:,}" if count is not None else "Entries:  (unreadable)")


def lookup(call: str, *, show_location: bool = False) -> None:
    """Look up *call* in the local FCC database and print results.

    Parameters
    ----------
    call : str
        Amateur callsign to look up (case-insensitive).
    show_location : bool, optional
        When ``True``, also print the resolved lat/lon coordinates.
    """
    print(f"\nCallsign: {call.upper()}")
    print("─" * 40)

    if not fcc_db.fcc_db_path().exists():
        print("  FCC database not found.")
        print("  Run Settings → Update FCC Database in DX Spotter first.")
        return

    info = fcc_db.lookup_callsign_info(call)
    if info is None:
        print("  Not found in FCC database.")
    else:
        _print_row("Name:", info["name"])
        _print_row("License class:", info["license_class"])
        _print_row("City:", info["city"])
        _print_row("State:", info["state"])

    if show_location:
        loc = fcc_db.lookup_location(call)
        if loc is not None:
            _print_row("Latitude:", f"{loc[0]:.4f}")
            _print_row("Longitude:", f"{loc[1]:.4f}")
        else:
            _print_row("Location:", "not resolved")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Look up FCC licensee data for one or more callsigns.",
    )
    parser.add_argument(
        "callsign",
        nargs="*",
        help="Callsign(s) to look up.",
    )
    parser.add_argument(
        "--location",
        action="store_true",
        help="Also show resolved lat/lon coordinates.",
    )
    parser.add_argument(
        "--db-info",
        action="store_true",
        help="Print database path, age, and entry count, then exit.",
    )
    args = parser.parse_args()

    if args.db_info:
        show_db_info()
        return

    if not args.callsign:
        parser.print_help()
        sys.exit(0)

    for call in args.callsign:
        try:
            lookup(call, show_location=args.location)
        except Exception as exc:
            print(f"Error looking up {call}: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
