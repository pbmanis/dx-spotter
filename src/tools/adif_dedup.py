"""Remove exact-duplicate QSO records from an ADIF log file.

Records are grouped by the three identity fields ``CALL``, ``QSO_DATE``,
and ``TIME_ON``.  Within each group the following rules apply (in order):

1. **All fields identical** — keep the first; discard the rest.
2. **QSL confirmations differ** — keep the record with the most received
   confirmations (``LOTW_QSL_RCVD``, ``QSL_RCVD``, ``EQSL_QSL_RCVD``
   with value ``Y``).
3. **QSL fields identical, comment / user fields differ** — keep the record
   with the most non-empty ``COMMENT``, ``NOTES``, or ``APP_*`` fields.
4. **Still tied** — keep the first occurrence.

Whenever a group contains any discrepancy (rules 2–4) a conflict summary
is printed to *stderr* so the user can review it.

The output file is written to the same directory as the input file with
``_deduped`` appended to the file stem (e.g. ``log.adif`` → ``log_deduped.adif``).

DXCC confirmation statistics are derived from :class:`~adif_log.ADIFLog`
and require each QSO record to contain ``DXCC``, ``BAND``, and ``MODE`` fields.

Usage
-----
::

    python adif_dedup.py <filename> [--dry-run] [--lotw LOTW_FILE] [-v]

Examples
--------
::

    # Report duplicates and DXCC stats; write output file
    python adif_dedup.py mylog.adif

    # Show what would be removed without writing
    python adif_dedup.py mylog.adif --dry-run -v
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# Allow importing adif_log from the parent src/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent))
from adif_log import ADIFLog  # noqa: E402

# ---------------------------------------------------------------------------
# Hardcoded base path — edit this to match the local log file directory.
# ---------------------------------------------------------------------------
_BASE_PATH: Path = Path.home() / "Documents" / "personal" / "Radio" / "Logs" / "NC3G_All_2026.06.21_dupes.adif"


# ---------------------------------------------------------------------------
# ADIF parsing
# ---------------------------------------------------------------------------

def parse_adif(text: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """Parse an ADIF file into a preserved header block and a list of records.

    Parameters
    ----------
    text : str
        Full text of the ADIF file (UTF-8 decoded).

    Returns
    -------
    header : str
        Everything up to and including the ``<EOH>`` tag.  Empty string when
        the file has no header block (header-less ADIF files are valid).
    records : list[list[tuple[str, str]]]
        Each record is an ordered list of ``(field_name, value)`` tuples
        preserving the field order from the source file.  Field names are
        upper-cased; values are stripped of surrounding whitespace.
    """
    tag_re = re.compile(r'<([^:>\s]+)(?::(\d+)(?::[^>]*)?)?>',  re.IGNORECASE)

    header = ''
    eoh = re.search(r'<EOH>', text, re.IGNORECASE)
    if eoh:
        header = text[:eoh.end()]
        body = text[eoh.end():]
    else:
        body = text

    records: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    pos = 0

    while pos < len(body):
        m = tag_re.search(body, pos)
        if not m:
            break
        tag = m.group(1).upper()
        pos = m.end()
        if tag == 'EOR':
            if current:
                records.append(current)
            current = []
        elif m.group(2):
            length = int(m.group(2))
            value = body[pos:pos + length].strip()
            current.append((tag, value))
            pos += length

    return header, records


def format_record(record: list[tuple[str, str]]) -> str:
    """Serialise one ADIF record to a text line ending with ``<EOR>``.

    Parameters
    ----------
    record : list[tuple[str, str]]
        Ordered list of ``(field_name, value)`` tuples.

    Returns
    -------
    str
        ADIF-encoded record string with a trailing newline.
    """
    parts = [f'<{name}:{len(value)}>{value}' for name, value in record]
    return ' '.join(parts) + ' <EOR>\n'


def write_adif(header: str, records: list[list[tuple[str, str]]], path: Path) -> None:
    """Write an ADIF file preserving the original header block.

    Parameters
    ----------
    header : str
        Header text including ``<EOH>``.  Written verbatim when non-empty.
    records : list[list[tuple[str, str]]]
        Records to serialise.
    path : Path
        Destination file path (created or overwritten).
    """
    with open(path, 'w', encoding='utf-8') as fh:
        if header:
            fh.write(header)
            fh.write('\n')
        for rec in records:
            fh.write(format_record(rec))


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

# Three fields that identify whether two records describe the same QSO contact.
_IDENTITY_FIELDS: tuple[str, ...] = ("CALL", "QSO_DATE", "TIME_ON")

# All QSL fields shown in conflict reports.
_QSL_ALL_FIELDS: frozenset[str] = frozenset(
    {"LOTW_QSL_RCVD", "LOTW_QSL_SENT", "QSL_RCVD", "QSL_SENT",
     "EQSL_QSL_RCVD", "EQSL_QSL_SENT"}
)

# Free-text fields included in info-score comparisons and conflict reports.
_INFO_FIELDS: frozenset[str] = frozenset({"COMMENT", "NOTES"})

# Fields resolved silently (no conflict printed): keep the record with the value
# present.  Falls back to a printed conflict if two records carry *different*
# non-empty values for the same field, which requires human review.
_SILENT_FIELDS: frozenset[str] = frozenset({"IOTA", "TX_PWR", "APP_RUMLOG_POWER"})

# Power fields whose text labels are treated as aliases for numeric watt values.
_POWER_FIELDS: frozenset[str] = frozenset({"TX_PWR", "APP_RUMLOG_POWER"})

# Map upper-cased text labels → canonical numeric string (watts).
_POWER_ALIASES: dict[str, str] = {
    "MID POWER": "100",
    "QRP": "5",
    "HIGH POWER": "1000",
    "1KW": "1000",
}

# Lower band edges in MHz (160 M – 2 M).  A FREQ value at one of these edges
# is treated as a band-fill placeholder rather than an actual logged frequency.
_BAND_LOWER_EDGES_MHZ: frozenset[float] = frozenset({
    1.800,    # 160 M
    3.500,    # 80 M
    7.000,    # 40 M
    10.100,   # 30 M
    14.000,   # 20 M
    18.068,   # 17 M
    21.000,   # 15 M
    24.890,   # 12 M
    28.000,   # 10 M
    50.000,   # 6 M
    144.000,  # 2 M
})

# Tolerance (MHz) for matching a frequency to a band lower edge (± 500 Hz).
_FREQ_EDGE_TOLERANCE_MHZ: float = 0.0005


def _normalize_power(val: str) -> str:
    # Return the canonical (numeric) form of a power value, or the original stripped value.
    return _POWER_ALIASES.get(val.strip().upper(), val.strip())


def _is_band_edge_freq(val: str) -> bool:
    # Return True if val parses as a frequency within 500 Hz of a known band lower edge.
    try:
        f = float(val)
    except ValueError:
        return False
    return any(abs(f - edge) < _FREQ_EDGE_TOLERANCE_MHZ for edge in _BAND_LOWER_EDGES_MHZ)


def _choose_freq(options: list[str]) -> str | None:
    # Prefer a non-band-edge frequency over a band-edge placeholder.
    # Returns the chosen value, or None when multiple distinct non-edge frequencies
    # exist and a human needs to decide.
    exact = [v for v in options if not _is_band_edge_freq(v)]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        return options[0]  # all are band-edge placeholders; keep first
    return None  # multiple distinct exact freqs — needs user input


def _identity_key(record: list[tuple[str, str]]) -> tuple[str, ...]:
    # CALL / QSO_DATE / TIME_ON — upper-cased and stripped.
    d = {name.upper(): value.strip().upper() for name, value in record}
    return tuple(d.get(f, "") for f in _IDENTITY_FIELDS)


def _all_fields_key(record: list[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    # Full comparison key used to detect exact duplicates.
    return frozenset((name.upper(), value.strip().upper()) for name, value in record)


def _differing_fields(group: list[list[tuple[str, str]]]) -> set[str]:
    # Field names (excluding identity fields) whose normalised value varies across the group.
    # Power fields use alias-aware normalisation so "100" and "Mid Power" are equal.
    ndicts = [{n.upper(): v.strip() for n, v in rec} for rec in group]
    identity_set = set(_IDENTITY_FIELDS)
    all_names = {n for d in ndicts for n in d if n not in identity_set}
    result: set[str] = set()
    for name in all_names:
        if name in _POWER_FIELDS:
            vals = {_normalize_power(d.get(name, "")) for d in ndicts}
        else:
            vals = {d.get(name, "").upper() for d in ndicts}
        if len(vals) > 1:
            result.add(name)
    return result


def _merge_fields(
    group: list[list[tuple[str, str]]],
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    # Build an initial merged record from a conflict group.
    # Identity fields first, then remaining fields in first-seen order.
    # Returns (merged_record, contested) where contested maps field → distinct
    # non-empty values found across the group (fields needing user resolution).
    ndicts = [{n.upper(): v.strip() for n, v in rec} for rec in group]
    identity_set = set(_IDENTITY_FIELDS)

    ordered: list[str] = list(_IDENTITY_FIELDS)
    seen: set[str] = set(_IDENTITY_FIELDS)
    for rec in group:
        for name, _ in rec:
            nu = name.upper()
            if nu not in seen:
                ordered.append(nu)
                seen.add(nu)

    merged: dict[str, str] = {}
    contested: dict[str, list[str]] = {}

    for name in ordered:
        if name in identity_set:
            merged[name] = ndicts[0].get(name, "")
            continue
        present = [d[name] for d in ndicts if d.get(name)]
        if name in _POWER_FIELDS:
            # Normalise aliases to canonical numeric form before comparing.
            norm = list(dict.fromkeys(_normalize_power(v) for v in present))
            if not norm:
                pass
            elif len(norm) == 1:
                merged[name] = norm[0]
            else:
                merged[name] = norm[0]
                contested[name] = norm
        else:
            unique_present: list[str] = list(dict.fromkeys(present))
            if not unique_present:
                pass  # absent in all records — omit
            elif len(unique_present) == 1:
                merged[name] = unique_present[0]  # unambiguous — auto-fill
            else:
                merged[name] = unique_present[0]  # default; may be overridden
                contested[name] = unique_present

    return list(merged.items()), contested


def _choose_rst_cw(values: list[str]) -> tuple[str | None, bool]:
    # CW RST rule: prefer a non-"599" value when one exists.
    # Returns (auto_choice, needs_confirm).
    # auto_choice is None when there are multiple distinct non-599 values (user must pick).
    if all(v == "599" for v in values):
        return values[0], False
    non_599 = [v for v in values if v != "599"]
    if len(set(non_599)) == 1:
        return non_599[0], True
    return None, True


def _prompt_choice(prefix: str, field: str, options: list[str]) -> int:
    # Prompt the user to pick one option. Returns the chosen 0-based index.
    print(f"\n  {prefix}")
    print(f"  Field {field!r} has multiple values:")
    for i, val in enumerate(options):
        print(f"    [{i + 1}] {val!r}")
    while True:
        raw = input(f"  Choose [1-{len(options)}] (Enter=1): ").strip()
        if not raw:
            return 0
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(options)}.")


def _resolve_conflict(
    group: list[list[tuple[str, str]]],
    key_str: str,
    choices: dict[str, dict[str, str]],
    can_prompt: bool,
) -> list[tuple[str, str]]:
    # Build a merged record from conflicting records, resolving each contested
    # field via saved choices, CW RST rules, or interactive prompts.
    merged_list, contested = _merge_fields(group)
    merged = dict(merged_list)
    saved = choices.get(key_str, {})
    new_choices: dict[str, str] = {}

    d0 = {n.upper(): v.strip() for n, v in group[0]}
    call = d0.get("CALL", "?")
    date = d0.get("QSO_DATE", "?")
    time_on = d0.get("TIME_ON", "?")
    prefix = f"{call}  {date}  {time_on}"
    mode = merged.get("MODE", "").upper()

    for field, options in sorted(contested.items()):
        if field in saved:
            merged[field] = saved[field]
            continue

        chosen: str | None = None

        if mode == "CW" and field in ("RST_SENT", "RST_RCVD"):
            auto, needs_confirm = _choose_rst_cw(options)
            if auto is None:
                if not can_prompt:
                    chosen = options[0]
                    print(
                        f"  [AUTO CW RST] {prefix} — {field}: "
                        f"choosing {chosen!r} (multiple non-599, no interaction)",
                        file=sys.stderr,
                    )
                # else chosen stays None → interactive prompt below
            elif needs_confirm:
                print(f"\n  [CW RST] {prefix} — {field}: auto-choosing {auto!r} over '599'")
                if can_prompt:
                    raw = input("  Confirm? [Y/n]: ").strip().lower()
                    if raw not in ("n", "no"):
                        chosen = auto
                    # else chosen stays None → interactive prompt below
                else:
                    chosen = auto
            else:
                chosen = auto
        elif field == "APP_RUMLOG_QSL":
            upper_opts = [v.upper() for v in options]
            if "X" in upper_opts:
                chosen = options[upper_opts.index("X")]
        elif field in ("LOTW_QSL_RCVD", "QSL_RCVD"):
            upper_opts = [v.upper() for v in options]
            if "Y" in upper_opts:
                chosen = options[upper_opts.index("Y")]
        elif field == "QSL_SENT":
            upper_opts = [v.upper() for v in options]
            if "Y" in upper_opts or "R" in upper_opts:
                chosen = "Y"  # "R" (received) implies sent; normalise to "Y"
        elif field == "FREQ":
            chosen = _choose_freq(options)  # None when multiple distinct exact freqs

        if chosen is None:
            if can_prompt:
                chosen = options[_prompt_choice(f"[CHOICE] {prefix}", field, options)]
            else:
                chosen = options[0]
                print(
                    f"  [AUTO] {prefix} — {field}: "
                    f"choosing {chosen!r} (multiple values, no interaction)",
                    file=sys.stderr,
                )

        merged[field] = chosen
        new_choices[field] = chosen

    if new_choices:
        choices.setdefault(key_str, {}).update(new_choices)

    return list(merged.items())


def _print_conflict(
    group: list[list[tuple[str, str]]],
) -> None:
    # Write a conflict summary to stderr, marking every differing field with *.
    d0 = dict(group[0])
    call = d0.get("CALL", "?")
    date = d0.get("QSO_DATE", "?")
    time_on = d0.get("TIME_ON", "?")

    # Normalised field dicts (upper-cased names, stripped values) — needed for display.
    ndicts: list[dict[str, str]] = [
        {n.upper(): v.strip() for n, v in rec}
        for rec in group
    ]
    identity_set = set(_IDENTITY_FIELDS)
    all_names: set[str] = {n for d in ndicts for n in d if n not in identity_set}
    differing: set[str] = _differing_fields(group)

    print(
        f"\n  [CONFLICT] {call}  {date}  {time_on}"
        f"  — {len(group)} records match on CALL/DATE/TIME; resolving by merge:",
        file=sys.stderr,
    )
    print(
        f"    Differs in: {', '.join(sorted(differing)) if differing else '(field presence only)'}",
        file=sys.stderr,
    )

    for i, (_, nd) in enumerate(zip(group, ndicts)):
        parts: list[str] = []

        # BAND and MODE — always shown; marked with * when differing.
        for fname in ("BAND", "MODE"):
            val = nd.get(fname, "(absent)")
            marker = "*" if fname in differing else " "
            parts.append(f"{marker}{fname}={val}")

        # Other differing fields not handled by the QSL or INFO sections.
        other_diff = sorted(
            f for f in differing
            if f not in {"BAND", "MODE"}
            and f not in _QSL_ALL_FIELDS
            and f not in _INFO_FIELDS
            and not f.startswith("APP_")
        )
        for fname in other_diff:
            parts.append(f"*{fname}={nd.get(fname, '(absent)')}")

        # QSL fields: show any that are differing or non-empty in this record.
        qsl_to_show = sorted(
            f for f in _QSL_ALL_FIELDS
            if f in differing or nd.get(f, "")
        )
        if qsl_to_show:
            qsl_parts = [
                f"{'*' if f in differing else ' '}{f}={nd.get(f, '(absent)')}"
                for f in qsl_to_show
            ]
            parts.append("QSL: " + ", ".join(qsl_parts))

        # INFO / APP_* fields: show any that are differing or non-empty.
        info_to_show = sorted(
            f for f in (all_names | differing)
            if (f in _INFO_FIELDS or f.startswith("APP_"))
            and (f in differing or nd.get(f, ""))
        )
        if info_to_show:
            info_parts = [
                f"{'*' if f in differing else ' '}{f}={nd.get(f, '(absent)')!r}"
                for f in info_to_show
            ]
            parts.append("INFO: " + ", ".join(info_parts))

        print(f"    [{i + 1}] " + "  |  ".join(parts), file=sys.stderr)


def deduplicate(
    records: list[list[tuple[str, str]]],
    verbose: bool = False,
    call_filter: str | None = None,
    choices: dict[str, dict[str, str]] | None = None,
    can_prompt: bool = False,
    singletons_out: list[list[tuple[str, str]]] | None = None,
) -> tuple[list[list[tuple[str, str]]], int]:
    """Remove duplicate records, merging conflicting fields into one record.

    Records are grouped by ``CALL``, ``QSO_DATE``, and ``TIME_ON``.  Within
    each group the following rules are applied in order:

    1. **All fields identical** — keep the first; discard the rest silently
       (or verbosely when *verbose* is ``True``).
    2. **Only silent fields differ** (``IOTA``, ``TX_PWR``,
       ``APP_RUMLOG_POWER``) — keep the record with the most of those fields
       present; no conflict is printed unless two records carry *different*
       non-empty values for the same silent field.
    3. **Real conflict** — build a merged record via :func:`_resolve_conflict`,
       which auto-fills absent fields, applies CW RST rules, and prompts the
       user (or uses saved choices) for remaining contested fields.

    Parameters
    ----------
    records : list[list[tuple[str, str]]]
        Parsed ADIF records as returned by :func:`parse_adif`.
    verbose : bool, optional
        When ``True``, print a line to stdout for each exact-duplicate set
        removed (conflicts are always reported regardless of this flag).
    call_filter : str or None, optional
        When provided, conflict output is restricted to records whose
        ``CALL`` field matches this value (case-insensitive).  All records
        are still de-duplicated; only the reporting is filtered.
    choices : dict or None, optional
        Mapping of ``key_str → {field: chosen_value}`` loaded from a previous
        run.  Mutated in-place with any new choices made during this call.
        Pass ``None`` (or an empty dict) to start fresh.
    can_prompt : bool, optional
        When ``True`` and stdin is a TTY, contested fields are resolved via
        interactive prompts.  When ``False`` the first candidate is chosen
        automatically and a warning is printed to stderr.
    singletons_out : list or None, optional
        When a list is provided, every record that had no duplicates is
        appended to it.

    Returns
    -------
    unique : list[list[tuple[str, str]]]
        De-duplicated record list (order of first occurrence preserved).
    n_removed : int
        Number of duplicate records discarded.
    """
    if choices is None:
        choices = {}

    groups: dict[tuple[str, ...], list[list[tuple[str, str]]]] = defaultdict(list)
    order: list[tuple[str, ...]] = []

    for rec in records:
        key = _identity_key(rec)
        if key not in groups:
            order.append(key)
        groups[key].append(rec)

    unique: list[list[tuple[str, str]]] = []
    n_removed = 0

    for key in order:
        group = groups[key]
        if len(group) == 1:
            unique.append(group[0])
            if singletons_out is not None:
                singletons_out.append(group[0])
            continue

        all_keys = [_all_fields_key(rec) for rec in group]
        if len(set(all_keys)) == 1:
            # Exact duplicates — all fields identical.
            unique.append(group[0])
            n_removed += len(group) - 1
            if verbose:
                d = dict(group[0])
                n = len(group) - 1
                print(
                    f"  [dup ×{n}] {d.get('CALL','?')}  {d.get('QSO_DATE','?')}"
                    f"  {d.get('BAND','?')}  {d.get('MODE','?')}"
                )
        else:
            # Conflict — same identity, fields differ.
            differing = _differing_fields(group)
            if differing <= _SILENT_FIELDS:
                # Every differing field is silently resolvable (IOTA, TX_PWR,
                # APP_RUMLOG_POWER).  Keep the record with the most of those
                # fields present — unless two records carry *different* non-empty
                # values for the same field, which needs human review.
                ndicts_s = [{n.upper(): v.strip() for n, v in r} for r in group]
                silent_conflict = any(
                    len({d.get(f, "") for d in ndicts_s if d.get(f)}) > 1
                    for f in differing
                )
                if not silent_conflict:
                    # _merge_fields fills absent silent fields and normalises
                    # power aliases to their canonical numeric form.
                    merged_silent, _ = _merge_fields(group)
                    unique.append(merged_silent)
                    n_removed += len(group) - 1
                    continue
            key_str = "|".join(key)
            if call_filter is None or key[0] == call_filter.upper():
                _print_conflict(group)
            merged = _resolve_conflict(group, key_str, choices, can_prompt)
            unique.append(merged)
            n_removed += len(group) - 1

    return unique, n_removed


# ---------------------------------------------------------------------------
# Choice-file helpers
# ---------------------------------------------------------------------------

def _choices_path(in_path: Path) -> Path:
    # Derive the path for the JSON choices file from the input ADIF path.
    return in_path.with_stem(in_path.stem + "_dedup_choices").with_suffix(".json")


def _load_choices(in_path: Path) -> dict[str, dict[str, str]]:
    # Load saved field choices from the JSON file, returning {} if absent.
    p = _choices_path(in_path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_choices(in_path: Path, choices: dict[str, dict[str, str]]) -> None:
    # Persist the choices dict to the JSON file next to the input file.
    p = _choices_path(in_path)
    p.write_text(json.dumps(choices, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _load_adif_quiet(path: Path) -> ADIFLog:
    # Suppress the ADIFLog progress print so we control output format.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        adif = ADIFLog(str(path))
    return adif


def print_stats(n_before: int, n_after: int, adif: ADIFLog) -> None:
    """Print deduplication counts and DXCC confirmation totals.

    Parameters
    ----------
    n_before : int
        Number of QSO records in the original file.
    n_after : int
        Number of QSO records after deduplication.
    adif : ADIFLog
        Loaded :class:`~adif_log.ADIFLog` built from the deduplicated data.
        Used for DXCC confirmation statistics.

    Notes
    -----
    *Paper-only* DXCC entities are those confirmed by paper QSL card but
    **not** via LoTW.  The total confirmed count is the union of both groups.
    """
    n_removed = n_before - n_after
    lotw = adif.confirmed_lotw_dxcc_count
    paper_only = adif.confirmed_paper_only_dxcc_count
    total = adif.confirmed_dxcc_count

    print()
    print("  QSOs before deduplication   :", n_before)
    print(f"  QSOs after  deduplication   : {n_after}"
          f"  ({n_removed} duplicate{'s' if n_removed != 1 else ''} removed)")
    print()
    print("  DXCC confirmed via LoTW      :", lotw)
    print("  DXCC confirmed via paper only:", paper_only, " (not already LoTW-confirmed)")
    print("  DXCC confirmed total          :", total, " (LoTW + paper combined)")


# ---------------------------------------------------------------------------
# Retained-record listing
# ---------------------------------------------------------------------------

def print_retained(records: list[list[tuple[str, str]]]) -> None:
    """Print a compact list of every record kept after deduplication.

    Parameters
    ----------
    records : list[list[tuple[str, str]]]
        De-duplicated records as returned by :func:`deduplicate`.
    """
    print(f"\n  Retained records ({len(records)}):")
    for rec in records:
        d = {n.upper(): v.strip() for n, v in rec}
        call = d.get("CALL", "?")
        date = d.get("QSO_DATE", "?")
        time_on = d.get("TIME_ON", "?")
        band = d.get("BAND", "?")
        mode = d.get("MODE", "?")
        print(f"    {call:<12} {date}  {time_on}  {band:<5} {mode}")


def print_singletons(records: list[list[tuple[str, str]]]) -> None:
    """Print a compact list of every record that had no duplicate.

    Parameters
    ----------
    records : list[list[tuple[str, str]]]
        Records that appeared exactly once in the input (never grouped with
        another record sharing the same CALL / QSO_DATE / TIME_ON).
    """
    print(f"\n  Non-duplicate records ({len(records)}):")
    for rec in records:
        d = {n.upper(): v.strip() for n, v in rec}
        call = d.get("CALL", "?")
        date = d.get("QSO_DATE", "?")
        time_on = d.get("TIME_ON", "?")
        band = d.get("BAND", "?")
        mode = d.get("MODE", "?")
        print(f"    {call:<12} {date}  {time_on}  {band:<5} {mode}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the ADIF deduplication tool."""
    parser = argparse.ArgumentParser(
        prog='adif_dedup',
        description='Remove exact-duplicate QSO records from an ADIF log file.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f'Input files are resolved relative to BASE_PATH = {_BASE_PATH}\n'
            'Output is written to the same directory with "_deduped" in the filename.'
        ),
    )
    parser.add_argument(
        "-f", 
        '--filename',
        default = _BASE_PATH,
        help='ADIF filename relative to the hardcoded base path.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report duplicates and statistics without writing the output file.',
    )
    parser.add_argument(
        '--lotw',
        metavar='LOTW_FILE',
        help='[stub] LoTW ADIF export file for future cross-reference (not yet implemented).',
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Print a line for each duplicate record removed.',
    )
    parser.add_argument(
        '-c', '--check',
        metavar='CALL',
        default=None,
        help='Only report conflicts for this callsign (case-insensitive). '
             'All records are still de-duplicated; only conflict output is filtered.',
    )
    parser.add_argument(
        '-k', '--keep-choices',
        action='store_true',
        help='Load previously saved conflict choices from the JSON file next to '
             'the input file and reuse them without prompting.',
    )

    args = parser.parse_args()

    in_path = _BASE_PATH / args.filename
    if not in_path.exists():
        print(f'Error: file not found: {in_path}', file=sys.stderr)
        sys.exit(1)

    out_path = in_path.with_stem(in_path.stem + '_deduped')

    if args.lotw:
        print(f'[stub] --lotw {args.lotw!r}: LoTW cross-reference not yet implemented.')

    print(f'Input : {in_path}')
    if not args.dry_run:
        print(f'Output: {out_path}')

    choices: dict[str, dict[str, str]] = (
        _load_choices(in_path) if args.keep_choices else {}
    )
    can_prompt = sys.stdin.isatty()
    singletons: list[list[tuple[str, str]]] = []

    text = in_path.read_text(encoding='utf-8', errors='replace')
    header, records = parse_adif(text)
    n_before = len(records)

    if args.verbose:
        print(f'Parsed {n_before} records.  Scanning for duplicates...')

    unique, n_removed = deduplicate(
        records,
        verbose=args.verbose,
        call_filter=args.check,
        choices=choices,
        can_prompt=can_prompt,
        singletons_out=singletons,
    )
    n_after = len(unique)

    if choices:
        _save_choices(in_path, choices)

    if args.dry_run:
        print(f'[dry-run] {n_removed} duplicate'
              f"{'s' if n_removed != 1 else ''} found; output file not written.")
        # Compute stats from a temporary file so ADIFLog can parse it.
        with tempfile.NamedTemporaryFile(
            suffix='.adif', mode='w', encoding='utf-8', delete=False
        ) as tf:
            tmp_path = Path(tf.name)
        try:
            write_adif(header, unique, tmp_path)
            adif = _load_adif_quiet(tmp_path)
            print_stats(n_before, n_after, adif)
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        write_adif(header, unique, out_path)
        print(f'Wrote {n_after} records.')
        adif = _load_adif_quiet(out_path)
        print_stats(n_before, n_after, adif)

    print_singletons(singletons)


if __name__ == '__main__':
    main()
