"""Tests for the WAS (Worked All States) support in src/adif_log.py.

Sample data
-----------
The fixture ADIF contains four records:

* W1AW     20230401  20M  FT8  DXCC 291  STATE CT  LoTW confirmed → CT confirmed on 20M
* K2ABC    20230402  40M  FT8  DXCC 291  STATE NY  unconfirmed    → NY worked (unconfirmed) on 40M
* VK2DEF   20230403  20M  CW   DXCC 150  (no STATE)               → non-US, no WAS state
* K3XYZ/6  20230404  6M   FT8  DXCC 291  STATE OH  unconfirmed    → portable call with a logged state

These cover the three WAS statuses (`confirmed`, `worked`, `new`) plus the
`n/a` fallback for an empty/unresolved state, and give `resolve_was_state`
both a plain callsign with a logged state (W1AW), a portable callsign with a
logged state (K3XYZ/6), and unlogged callsigns to exercise the FCC-database
fallback and the portable/mobile guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import adif_log
from adif_log import ADIFLog

_SAMPLE_ADIF: str = """\
ADIF WAS test
<EOH>
<CALL:4>W1AW <QSO_DATE:8>20230401 <TIME_ON:4>1200 <BAND:3>20M <MODE:3>FT8 <DXCC:3>291 <STATE:2>CT <LOTW_QSL_RCVD:1>Y <EOR>
<CALL:5>K2ABC <QSO_DATE:8>20230402 <TIME_ON:4>0300 <BAND:3>40M <MODE:3>FT8 <DXCC:3>291 <STATE:2>NY <EOR>
<CALL:6>VK2DEF <QSO_DATE:8>20230403 <TIME_ON:4>0500 <BAND:3>20M <MODE:2>CW <DXCC:3>150 <EOR>
<CALL:7>K3XYZ/6 <QSO_DATE:8>20230404 <TIME_ON:4>0700 <BAND:2>6M <MODE:3>FT8 <DXCC:3>291 <STATE:2>OH <EOR>
"""


@pytest.fixture
def adif(tmp_path: Path) -> ADIFLog:
    """Return an ADIFLog loaded from the sample WAS fixture data."""
    path = tmp_path / "was_sample.adif"
    path.write_text(_SAMPLE_ADIF, encoding="utf-8")
    return ADIFLog(str(path))


# ---------------------------------------------------------------------------
# call_state / was_status / was_qso_details
# ---------------------------------------------------------------------------


class TestCallState:
    """Tests for :meth:`ADIFLog.call_state`."""

    def test_known_call_returns_state(self, adif: ADIFLog) -> None:
        assert adif.call_state("W1AW") == "CT"

    def test_case_insensitive(self, adif: ADIFLog) -> None:
        assert adif.call_state("w1aw") == "CT"

    def test_unknown_call_returns_empty(self, adif: ADIFLog) -> None:
        assert adif.call_state("NOCALL") == ""

    def test_non_us_call_not_recorded(self, adif: ADIFLog) -> None:
        assert adif.call_state("VK2DEF") == ""


class TestWasStatus:
    """Tests for :meth:`ADIFLog.was_status`."""

    def test_confirmed_state_and_band(self, adif: ADIFLog) -> None:
        assert adif.was_status("CT", "20M") == "confirmed"

    def test_worked_state_and_band(self, adif: ADIFLog) -> None:
        assert adif.was_status("NY", "40M") == "worked"

    def test_never_worked_state_is_new(self, adif: ADIFLog) -> None:
        assert adif.was_status("OH", "20M") == "new"

    def test_worked_state_wrong_band_is_new(self, adif: ADIFLog) -> None:
        # NY was worked on 40M, not 20M.
        assert adif.was_status("NY", "20M") == "new"

    def test_empty_state_is_na(self, adif: ADIFLog) -> None:
        assert adif.was_status("", "20M") == "n/a"

    def test_case_insensitive(self, adif: ADIFLog) -> None:
        assert adif.was_status("ct", "20m") == "confirmed"


class TestWasQsoDetails:
    """Tests for :meth:`ADIFLog.was_qso_details`."""

    def test_confirmed_list_contains_call(self, adif: ADIFLog) -> None:
        conf, wkd = adif.was_qso_details("CT", "20M")
        assert [e["call"] for e in conf] == ["W1AW"]
        assert wkd == []

    def test_worked_list_contains_call(self, adif: ADIFLog) -> None:
        conf, wkd = adif.was_qso_details("NY", "40M")
        assert conf == []
        assert [e["call"] for e in wkd] == ["K2ABC"]

    def test_unknown_state_returns_empty_lists(self, adif: ADIFLog) -> None:
        conf, wkd = adif.was_qso_details("OH", "20M")
        assert conf == []
        assert wkd == []


# ---------------------------------------------------------------------------
# resolve_was_state
# ---------------------------------------------------------------------------


class TestResolveWasState:
    """Tests for :meth:`ADIFLog.resolve_was_state`."""

    def test_non_291_dxcc_returns_empty(self, adif: ADIFLog) -> None:
        # W1AW has a logged state, but a non-291 dxcc must short-circuit to ''.
        assert adif.resolve_was_state("W1AW", 150) == ""

    def test_logged_state_used_without_fcc_lookup(
        self, adif: ADIFLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(call: str) -> dict[str, str] | None:
            raise AssertionError("FCC lookup should not be called when a logged state exists")

        monkeypatch.setattr(adif_log.fcc_db, "lookup_callsign_info", _boom)
        assert adif.resolve_was_state("W1AW", 291) == "CT"

    def test_falls_back_to_fcc_lookup(
        self, adif: ADIFLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adif_log.fcc_db,
            "lookup_callsign_info",
            lambda call: {"name": "Test", "license_class": "E", "city": "X", "state": "FL"},
        )
        assert adif.resolve_was_state("N0NEW", 291) == "FL"

    def test_fcc_lookup_miss_returns_empty(
        self, adif: ADIFLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adif_log.fcc_db, "lookup_callsign_info", lambda call: None)
        assert adif.resolve_was_state("N0NEW", 291) == ""

    def test_portable_call_without_logged_state_skips_fcc(
        self, adif: ADIFLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(call: str) -> dict[str, str] | None:
            raise AssertionError("FCC lookup should be skipped for portable/mobile calls")

        monkeypatch.setattr(adif_log.fcc_db, "lookup_callsign_info", _boom)
        assert adif.resolve_was_state("N0NEW/6", 291) == ""

    def test_portable_call_with_logged_state_still_used(
        self, adif: ADIFLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the exact portable callsign string was already logged with a
        # state, that logged state is honored ahead of the portable guard.
        def _boom(call: str) -> dict[str, str] | None:
            raise AssertionError("FCC lookup should not be called when a logged state exists")

        monkeypatch.setattr(adif_log.fcc_db, "lookup_callsign_info", _boom)
        assert adif.resolve_was_state("K3XYZ/6", 291) == "OH"
