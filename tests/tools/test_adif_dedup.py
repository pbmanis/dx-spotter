"""Tests for src/tools/adif_dedup.py.

Covers ADIF parsing, exact-duplicate detection, file round-trip, and
the DXCC confirmation statistics derived from ADIFLog.

Sample data
-----------
The fixture ADIF contains four records:

* W1AW  20230401  20M  FT8  DXCC 291  LoTW confirmed  ← kept (first)
* W1AW  20230401  20M  FT8  DXCC 291  LoTW confirmed  ← duplicate, removed
* VK2AB 20230402  40M  CW   DXCC 150  paper confirmed
* JA1XY 20230403  15M  FT8  DXCC 339  unconfirmed

After deduplication: 3 records, 2 confirmed DXCC entities (1 LoTW, 1 paper-only).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.adif_dedup import (
    _load_adif_quiet,
    deduplicate,
    format_record,
    parse_adif,
    print_retained,
    print_stats,
    write_adif,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SAMPLE_ADIF: str = """\
ADIF export test
<EOH>
<CALL:4>W1AW <QSO_DATE:8>20230401 <TIME_ON:4>1200 <BAND:3>20M <MODE:3>FT8 <DXCC:3>291 <LOTW_QSL_RCVD:1>Y <EOR>
<CALL:4>W1AW <QSO_DATE:8>20230401 <TIME_ON:4>1200 <BAND:3>20M <MODE:3>FT8 <DXCC:3>291 <LOTW_QSL_RCVD:1>Y <EOR>
<CALL:5>VK2AB <QSO_DATE:8>20230402 <TIME_ON:4>0300 <BAND:3>40M <MODE:2>CW <DXCC:3>150 <QSL_RCVD:1>Y <EOR>
<CALL:5>JA1XY <QSO_DATE:8>20230403 <TIME_ON:4>0500 <BAND:3>15M <MODE:3>FT8 <DXCC:3>339 <EOR>
"""


@pytest.fixture
def sample_adif_text() -> str:
    """Return the four-record sample ADIF string."""
    return _SAMPLE_ADIF


@pytest.fixture
def parsed_adif(sample_adif_text: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """Return the parsed (header, records) pair from the sample ADIF."""
    return parse_adif(sample_adif_text)


@pytest.fixture
def deduped_adif_file(
    parsed_adif: tuple[str, list[list[tuple[str, str]]]], tmp_path: Path
) -> Path:
    """Write a deduplicated copy of the sample ADIF to a temporary file."""
    header, records = parsed_adif
    unique, _ = deduplicate(records)
    out = tmp_path / "sample_deduped.adif"
    write_adif(header, unique, out)
    return out


# ---------------------------------------------------------------------------
# parse_adif
# ---------------------------------------------------------------------------


class TestParseAdif:
    """Tests for :func:`parse_adif`."""

    def test_record_count(self, sample_adif_text: str) -> None:
        _, records = parse_adif(sample_adif_text)
        assert len(records) == 4

    def test_header_contains_eoh(self, sample_adif_text: str) -> None:
        header, _ = parse_adif(sample_adif_text)
        assert "<EOH>" in header.upper()

    def test_header_contains_original_text(self, sample_adif_text: str) -> None:
        header, _ = parse_adif(sample_adif_text)
        assert "ADIF export test" in header

    def test_field_names_are_uppercased(self, sample_adif_text: str) -> None:
        _, records = parse_adif(sample_adif_text)
        all_names = {name for rec in records for name, _ in rec}
        assert "CALL" in all_names
        assert "QSO_DATE" in all_names
        assert "BAND" in all_names
        assert "MODE" in all_names

    def test_values_are_stripped(self, sample_adif_text: str) -> None:
        _, records = parse_adif(sample_adif_text)
        for rec in records:
            for _, value in rec:
                assert value == value.strip()

    def test_headerless_adif(self) -> None:
        text = "<CALL:4>W1AW <BAND:3>20M <MODE:3>FT8 <EOR>\n"
        header, records = parse_adif(text)
        assert header == ""
        assert len(records) == 1

    def test_empty_string(self) -> None:
        header, records = parse_adif("")
        assert header == ""
        assert records == []


# ---------------------------------------------------------------------------
# deduplicate
# ---------------------------------------------------------------------------


class TestDeduplicate:
    """Tests for :func:`deduplicate`."""

    def test_removes_exact_duplicate(
        self, parsed_adif: tuple[str, list[list[tuple[str, str]]]]
    ) -> None:
        _, records = parsed_adif
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 3

    def test_first_occurrence_is_kept(
        self, parsed_adif: tuple[str, list[list[tuple[str, str]]]]
    ) -> None:
        _, records = parsed_adif
        unique, _ = deduplicate(records)
        # W1AW is first in both the original and deduplicated lists
        assert dict(unique[0]).get("CALL") == "W1AW"

    def test_case_insensitive_comparison(self) -> None:
        # Records differing only in field-value capitalisation are duplicates.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"), ("BAND", "20M"), ("MODE", "FT8"), ("DXCC", "291")],
            [("CALL", "w1aw"), ("BAND", "20m"), ("MODE", "ft8"), ("DXCC", "291")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1

    def test_conflict_non_key_field_keeps_one(self) -> None:
        # Records sharing CALL/DATE/TIME but differing in a non-identity field
        # are treated as a conflict: one record is kept, the rest discarded.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("COMMENT", "first")],
            [("CALL", "W1AW"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("COMMENT", "second")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1

    def test_qsl_confirmation_preferred(self) -> None:
        # When QSL fields differ, the record with more confirmations is kept.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "W1AW"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("LOTW_QSL_RCVD", "Y")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("LOTW_QSL_RCVD") == "Y"

    def test_comment_field_preferred_over_empty(self) -> None:
        # When QSL scores are equal, the record with a COMMENT is kept.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "W1AW"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("COMMENT", "nice qso")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("COMMENT") == "nice qso"

    # ------------------------------------------------------------------
    # Silent-field tests (IOTA, TX_PWR, APP_RUMLOG_POWER)
    # ------------------------------------------------------------------

    def test_iota_only_kept_silently(self) -> None:
        # IOTA is the sole difference: keep the record with IOTA, no conflict printed.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("IOTA", "EU-005")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("IOTA") == "EU-005"

    def test_tx_pwr_only_kept_silently(self) -> None:
        # TX_PWR is the sole difference: keep the record that has it.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "100")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("TX_PWR") == "100"

    def test_app_rumlog_power_only_kept_silently(self) -> None:
        # APP_RUMLOG_POWER is the sole difference: keep the record that has it.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_POWER", "50")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("APP_RUMLOG_POWER") == "50"

    def test_silent_fields_only_no_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Any combination of silent-only differences produces no stderr output.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"),
             ("IOTA", "EU-005"), ("TX_PWR", "100")],
        ]
        deduplicate(records)
        assert capsys.readouterr().err == ""

    def test_silent_field_plus_other_diff_prints_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Silent field + a non-silent differing field → full conflict report.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "40M"), ("MODE", "FT8"), ("IOTA", "EU-005")],
        ]
        deduplicate(records)
        err = capsys.readouterr().err
        assert "[CONFLICT]" in err
        assert "BAND" in err
        assert "IOTA" in err

    def test_conflicting_silent_values_prints_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Two different non-empty values for the same silent field → conflict printed.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "100")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "50")],
        ]
        deduplicate(records)
        err = capsys.readouterr().err
        assert "[CONFLICT]" in err
        assert "TX_PWR" in err

    # Power-alias normalisation tests
    # ------------------------------------------------------------------

    def test_power_alias_vs_numeric_silent(self) -> None:
        # "Mid Power" and "100" are the same alias group → resolved silently,
        # merged record carries the canonical numeric value "100".
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "Mid Power")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "100")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("TX_PWR") == "100"

    def test_power_text_only_normalized_to_numeric(self) -> None:
        # Only text descriptors present (no numeric) → merged record still gets
        # the canonical numeric form.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "Mid Power")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("COMMENT", "nice qso")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("TX_PWR") == "100"

    def test_power_qrp_alias_silent(self) -> None:
        # "QRP" and "5" are the same alias group → resolved silently to "5".
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "QRP")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "5")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("TX_PWR") == "5"

    def test_power_high_power_aliases_silent(self) -> None:
        # "High Power", "1KW", and "1000" are all the same alias group → "1000".
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "High Power")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "1KW")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "1000")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 2
        assert len(unique) == 1
        assert dict(unique[0]).get("TX_PWR") == "1000"

    def test_power_different_groups_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "Mid Power" (100 W) vs "QRP" (5 W) are different alias groups → conflict.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "Mid Power")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("TX_PWR", "QRP")],
        ]
        deduplicate(records)
        err = capsys.readouterr().err
        assert "[CONFLICT]" in err
        assert "TX_PWR" in err

    def test_app_rumlog_power_alias_normalized(self) -> None:
        # APP_RUMLOG_POWER follows the same alias rules as TX_PWR.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_POWER", "Mid Power")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_POWER", "100")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 1
        assert len(unique) == 1
        assert dict(unique[0]).get("APP_RUMLOG_POWER") == "100"

    # QSL auto-resolution and FREQ band-edge tests
    # ------------------------------------------------------------------

    def test_app_rumlog_qsl_confirmed_chosen(self) -> None:
        # APP_RUMLOG_QSL: "X" (confirmed) is auto-chosen without prompting.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_QSL", "S")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_QSL", "X")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("APP_RUMLOG_QSL") == "X"

    def test_app_rumlog_qsl_confirmed_no_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Auto-choosing "X" for APP_RUMLOG_QSL produces no [AUTO] stderr warning.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_QSL", "S")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("APP_RUMLOG_QSL", "X")],
        ]
        deduplicate(records, choices={}, can_prompt=False)
        err = capsys.readouterr().err
        assert "[AUTO]" not in err

    def test_lotw_qsl_rcvd_confirmed_chosen(self) -> None:
        # LOTW_QSL_RCVD: "Y" is auto-chosen without prompting.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("LOTW_QSL_RCVD", "N")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("LOTW_QSL_RCVD", "Y")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("LOTW_QSL_RCVD") == "Y"

    def test_qsl_rcvd_confirmed_chosen(self) -> None:
        # QSL_RCVD (paper QSL): "Y" is auto-chosen without prompting.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_RCVD", "N")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_RCVD", "Y")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("QSL_RCVD") == "Y"

    def test_qsl_sent_y_chosen(self) -> None:
        # QSL_SENT: "Y" auto-chosen over "N".
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_SENT", "N")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_SENT", "Y")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("QSL_SENT") == "Y"

    def test_qsl_sent_r_promoted_to_y(self) -> None:
        # QSL_SENT: "R" (invalid — means received) is promoted to "Y".
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_SENT", "N")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_SENT", "R")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("QSL_SENT") == "Y"

    def test_qsl_sent_r_only_promoted_to_y(self) -> None:
        # QSL_SENT: "R" present alongside "Y" still yields "Y".
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_SENT", "Y")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("QSL_SENT", "R")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("QSL_SENT") == "Y"

    def test_freq_exact_over_band_edge(self) -> None:
        # FREQ: exact frequency preferred over band-edge placeholder.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "40M"), ("MODE", "FT8"), ("FREQ", "7.000000")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "40M"), ("MODE", "FT8"), ("FREQ", "7.031000")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("FREQ") == "7.031000"

    def test_freq_17m_exact_over_band_edge(self) -> None:
        # FREQ: 17 M band edge (18.068) treated as placeholder; 18.105 preferred.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "17M"), ("MODE", "FT8"), ("FREQ", "18.068000")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "17M"), ("MODE", "FT8"), ("FREQ", "18.105000")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("FREQ") == "18.105000"

    def test_freq_band_edge_silent_when_only_diff(self) -> None:
        # FREQ where one is a band edge and the other is exact should not be in
        # the conflict report — the exact value is chosen automatically.
        records: list[list[tuple[str, str]]] = [
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("FREQ", "14.000000")],
            [("CALL", "G3ABC"), ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8"), ("FREQ", "14.074000")],
        ]
        unique, n_removed = deduplicate(records, choices={}, can_prompt=False)
        assert n_removed == 1
        assert dict(unique[0]).get("FREQ") == "14.074000"

    def test_no_duplicates_unchanged(self) -> None:
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"), ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "VK2AB"), ("BAND", "40M"), ("MODE", "CW")],
        ]
        unique, n_removed = deduplicate(records)
        assert n_removed == 0
        assert len(unique) == 2

    def test_empty_input(self) -> None:
        unique, n_removed = deduplicate([])
        assert unique == []
        assert n_removed == 0

    def test_all_duplicates(self) -> None:
        rec = [("CALL", "W1AW"), ("BAND", "20M"), ("MODE", "FT8")]
        unique, n_removed = deduplicate([rec, rec, rec])
        assert len(unique) == 1
        assert n_removed == 2


# ---------------------------------------------------------------------------
# format_record / write_adif round-trip
# ---------------------------------------------------------------------------


class TestFormatAndWrite:
    """Tests for :func:`format_record` and :func:`write_adif`."""

    def test_format_record_contains_eor(self) -> None:
        rec = [("CALL", "W1AW"), ("BAND", "20M")]
        assert "<EOR>" in format_record(rec).upper()

    def test_format_record_encodes_length(self) -> None:
        rec = [("CALL", "W1AW")]
        # W1AW is 4 characters → <CALL:4>
        assert "<CALL:4>W1AW" in format_record(rec)

    def test_output_file_created(self, deduped_adif_file: Path) -> None:
        assert deduped_adif_file.exists()

    def test_output_record_count(self, deduped_adif_file: Path) -> None:
        text = deduped_adif_file.read_text(encoding="utf-8")
        _, records = parse_adif(text)
        assert len(records) == 3

    def test_output_eor_count(self, deduped_adif_file: Path) -> None:
        text = deduped_adif_file.read_text(encoding="utf-8")
        assert text.upper().count("<EOR>") == 3

    def test_header_preserved_in_output(
        self,
        parsed_adif: tuple[str, list[list[tuple[str, str]]]],
        tmp_path: Path,
    ) -> None:
        header, records = parsed_adif
        unique, _ = deduplicate(records)
        out = tmp_path / "out.adif"
        write_adif(header, unique, out)
        written = out.read_text(encoding="utf-8")
        assert "ADIF export test" in written

    def test_round_trip_preserves_values(self, deduped_adif_file: Path) -> None:
        text = deduped_adif_file.read_text(encoding="utf-8")
        _, records = parse_adif(text)
        calls = {dict(rec).get("CALL") for rec in records}
        assert calls == {"W1AW", "VK2AB", "JA1XY"}


# ---------------------------------------------------------------------------
# DXCC statistics via ADIFLog
# ---------------------------------------------------------------------------


class TestDxccStats:
    """Tests for DXCC confirmation counts derived from :class:`~adif_log.ADIFLog`."""

    def test_lotw_dxcc_count(self, deduped_adif_file: Path) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        # W1AW (DXCC 291) confirmed via LoTW
        assert adif.confirmed_lotw_dxcc_count == 1

    def test_paper_only_dxcc_count(self, deduped_adif_file: Path) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        # VK2AB (DXCC 150) confirmed via paper QSL only
        assert adif.confirmed_paper_only_dxcc_count == 1

    def test_total_confirmed_dxcc(self, deduped_adif_file: Path) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        # W1AW + VK2AB = 2 unique confirmed entities
        assert adif.confirmed_dxcc_count == 2

    def test_unconfirmed_not_counted(self, deduped_adif_file: Path) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        # JA1XY (DXCC 339) has no QSL — should not appear in confirmed counts
        assert adif.confirmed_dxcc_count == 2

    def test_print_stats_qso_counts(
        self, deduped_adif_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        print_stats(n_before=4, n_after=3, adif=adif)
        out = capsys.readouterr().out
        assert "4" in out
        assert "3" in out

    def test_print_stats_duplicate_label(
        self, deduped_adif_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        print_stats(n_before=4, n_after=3, adif=adif)
        out = capsys.readouterr().out
        assert "1 duplicate removed" in out

    def test_print_stats_plural_label(
        self, deduped_adif_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        adif = _load_adif_quiet(deduped_adif_file)
        print_stats(n_before=5, n_after=3, adif=adif)
        out = capsys.readouterr().out
        assert "2 duplicates removed" in out


# ---------------------------------------------------------------------------
# print_retained
# ---------------------------------------------------------------------------


class TestPrintRetained:
    """Tests for :func:`print_retained`."""

    def test_count_in_header(self, capsys: pytest.CaptureFixture[str]) -> None:
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"),  ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "VK2AB"), ("QSO_DATE", "20230402"), ("TIME_ON", "0300"),
             ("BAND", "40M"), ("MODE", "CW")],
        ]
        print_retained(records)
        out = capsys.readouterr().out
        assert "Retained records (2)" in out

    def test_each_call_appears(self, capsys: pytest.CaptureFixture[str]) -> None:
        records: list[list[tuple[str, str]]] = [
            [("CALL", "W1AW"),  ("QSO_DATE", "20230401"), ("TIME_ON", "1200"),
             ("BAND", "20M"), ("MODE", "FT8")],
            [("CALL", "VK2AB"), ("QSO_DATE", "20230402"), ("TIME_ON", "0300"),
             ("BAND", "40M"), ("MODE", "CW")],
        ]
        print_retained(records)
        out = capsys.readouterr().out
        assert "W1AW" in out
        assert "VK2AB" in out

    def test_empty_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_retained([])
        out = capsys.readouterr().out
        assert "Retained records (0)" in out
