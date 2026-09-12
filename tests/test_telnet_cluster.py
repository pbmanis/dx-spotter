"""Tests for the DX cluster telnet client in src/telnet_cluster.py.

These are network-free unit tests: they drive :meth:`TelnetCluster._process_line`
directly with sample lines and inspect the spot dicts passed to the ``on_spot``
callback, so no TCP connection is made.  (The live, network-dependent probes
live alongside these as ``probe_*.py`` and are not collected by pytest.)

Covered:

* the four recognized spot line formats (standard, alternate, RBN, no-colon);
* SNR and mode extraction, including frequency-based mode inference;
* stripping of trailing control characters (e.g. the terminal-bell some nodes
  append to alert spots);
* the IAC option-negotiation stripper;
* suppression of "unrecognized line" logging for non-spot lines (banners,
  prompts) while genuine spot-shaped parse failures are still reported.
"""

from __future__ import annotations

import pytest

from telnet_cluster import TelnetCluster, _strip_iac, _infer_mode_from_freq


@pytest.fixture
def cluster() -> tuple[TelnetCluster, list[dict]]:
    """Return a TelnetCluster (never started) and the list its callback fills."""
    spots: list[dict] = []
    tc = TelnetCluster("host", 7300, "n0call", on_spot=spots.append, index=9)
    return tc, spots


# ---------------------------------------------------------------------------
# Spot line formats
# ---------------------------------------------------------------------------


class TestSpotFormats:
    """Each recognized line format parses into the expected spot dict fields."""

    def test_standard_format(self, cluster: tuple[TelnetCluster, list[dict]]) -> None:
        tc, spots = cluster
        tc._process_line(
            "DX de W9XYZ:     14074.0  VK2ABC       FT8 -10 dB 2314Z JO22", 9
        )
        assert len(spots) == 1
        s = spots[0]
        assert s["call"] == "VK2ABC"
        assert s["rc"] == "W9XYZ"
        assert s["abs_freq_hz"] == 14074000
        assert s["md"] == "FT8"
        assert s["rp"] == "-10"

    def test_alternate_format(self, cluster: tuple[TelnetCluster, list[dict]]) -> None:
        tc, spots = cluster
        tc._process_line(" 14228.0  IK1PMR      12-Sep-2026 2126Z  wae   <VE7ZFR>", 9)
        assert len(spots) == 1
        s = spots[0]
        assert s["call"] == "IK1PMR"
        assert s["rc"] == "VE7ZFR"
        assert s["abs_freq_hz"] == 14228000

    def test_rbn_format(self, cluster: tuple[TelnetCluster, list[dict]]) -> None:
        tc, spots = cluster
        tc._process_line(
            "DX de OE3KLU-#: 28187.90  OE3XAC         CW     5 dB  14 WPM  BEACON  0032Z",
            9,
        )
        assert len(spots) == 1
        s = spots[0]
        assert s["call"] == "OE3XAC"
        assert s["rc"] == "OE3KLU-#"
        assert s["abs_freq_hz"] == 28187900
        assert s["md"] == "CW"
        assert s["rp"] == "5"

    def test_no_colon_self_spot(
        self, cluster: tuple[TelnetCluster, list[dict]]
    ) -> None:
        tc, spots = cluster
        tc._process_line(
            "DX de JI1IZS/     3500.0  JI1IZS/2     WWFF JAFF-0262    1201Z", 9
        )
        assert len(spots) == 1
        s = spots[0]
        assert s["call"] == "JI1IZS/2"
        assert s["rc"] == "JI1IZS/"
        assert s["abs_freq_hz"] == 3500000


# ---------------------------------------------------------------------------
# Mode / SNR extraction
# ---------------------------------------------------------------------------


class TestModeAndSnr:
    """Mode comes from the comment first, then from frequency inference."""

    def test_snr_absent_defaults_to_zero(
        self, cluster: tuple[TelnetCluster, list[dict]]
    ) -> None:
        tc, spots = cluster
        # SSB sub-band, no dB in the comment -> rp == '0'
        tc._process_line("DX de PP1DX:     14241.0  LZ5R         wae 2026", 9)
        assert spots[0]["rp"] == "0"

    def test_mode_inferred_from_frequency(
        self, cluster: tuple[TelnetCluster, list[dict]]
    ) -> None:
        tc, spots = cluster
        # No mode keyword in the comment; 14074 kHz is the 20m FT8 channel.
        tc._process_line("DX de K1ABC:     14074.0  DL1XYZ       hi", 9)
        assert spots[0]["md"] == "FT8"

    def test_mode_keyword_beats_frequency(
        self, cluster: tuple[TelnetCluster, list[dict]]
    ) -> None:
        tc, spots = cluster
        # 14074 would infer FT8, but an explicit CW keyword wins.
        tc._process_line("DX de K1ABC:     14074.0  DL1XYZ       CW test", 9)
        assert spots[0]["md"] == "CW"

    @pytest.mark.parametrize(
        "freq_khz,expected",
        [
            (14074.0, "FT8"),
            (14080.0, "FT4"),
            (14020.0, "CW"),
            (14250.0, "SSB"),
        ],
    )
    def test_infer_mode_from_freq(self, freq_khz: float, expected: str) -> None:
        assert _infer_mode_from_freq(freq_khz) == expected


# ---------------------------------------------------------------------------
# Comment sanitizing
# ---------------------------------------------------------------------------


class TestCommentSanitizing:
    """Trailing control characters must not leak into the displayed comment."""

    def test_terminal_bell_stripped(
        self, cluster: tuple[TelnetCluster, list[dict]]
    ) -> None:
        tc, spots = cluster
        tc._process_line(
            "DX de HK4SAN:    28074.0  VP5/K5UR     FT8 +02dB 1458Hz 2128Z FJ26\x07\x07",
            9,
        )
        assert len(spots) == 1
        assert "\x07" not in spots[0]["comment"]


# ---------------------------------------------------------------------------
# IAC stripping
# ---------------------------------------------------------------------------


class TestStripIac:
    """The telnet IAC option-negotiation stripper removes control sequences."""

    def test_plain_bytes_unchanged(self) -> None:
        assert _strip_iac(b"hello world") == b"hello world"

    def test_three_byte_option_removed(self) -> None:
        # IAC WILL ECHO  ->  0xFF 0xFB 0x01
        assert _strip_iac(b"a\xff\xfb\x01b") == b"ab"

    def test_escaped_literal_ff(self) -> None:
        # IAC IAC collapses to a single literal 0xFF.
        assert _strip_iac(b"a\xff\xffb") == b"a\xffb"


# ---------------------------------------------------------------------------
# Unrecognized-line logging
# ---------------------------------------------------------------------------


class TestUnrecognizedLineLogging:
    """Banners/prompts stay quiet; spot-shaped parse failures are reported."""

    def test_banner_line_is_silent(
        self,
        cluster: tuple[TelnetCluster, list[dict]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        tc, spots = cluster
        tc._process_line("NC3G de W3LPL 12-Sep-2026 2109Z dxspider >", 9)
        tc._process_line("|   Welcome to the DX-Spider Telnet Server   |", 9)
        assert spots == []
        assert capsys.readouterr().out == ""

    def test_malformed_spot_line_warns(
        self,
        cluster: tuple[TelnetCluster, list[dict]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        tc, spots = cluster
        tc._process_line("DX de BROKEN GARBAGE THAT SHOULD WARN", 9)
        assert spots == []
        assert "unrecognized line" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Login command
# ---------------------------------------------------------------------------


class TestLoginCommand:
    """The optional post-login command is stored, stripped, and defaults empty."""

    def test_default_is_empty(self) -> None:
        tc = TelnetCluster("host", 7300, "n0call", on_spot=lambda s: None)
        assert tc._command == ""

    def test_command_is_stripped(self) -> None:
        tc = TelnetCluster(
            "host", 7300, "n0call", on_spot=lambda s: None, command="  sh/dx/50  "
        )
        assert tc._command == "sh/dx/50"
