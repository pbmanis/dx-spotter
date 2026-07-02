"""Bandmap display widget for DX Spotter.

Provides :class:`BandMap`, a ``QWidget`` containing a pyqtgraph
:class:`~pyqtgraph.PlotWidget` that shows spotted stations on a
frequency-vs-SNR plot for the current band.  Each station is drawn as
a vertical line whose height encodes SNR (or dB for CW/SSB), with the
callsign label rotated 90° at the top of the line.  Lines are colored
by mode and drawn thicker when the spot has a non-``'n/a'`` award status
under the active criterion.

Double-clicking near a line emits :attr:`BandMap.spot_activated` with
the same payload dict as :class:`~spot_window.SpotTable`, so the
existing :meth:`~dxspotter.DXSpotter._on_spot_activated` routing
(WSJT-X for digital, Commander for CW/SSB) applies without change.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QPushButton, QWidget

if TYPE_CHECKING:
    from adif_log import ADIFLog


# Mode → plot color (matches on_message colorама assignments)
_MODE_COLORS: dict[str, str] = {
    "CW":  "#00cc00",   # green
    "FT8": "#4488ff",   # blue
    "FT4": "#88bbff",   # light-blue
    "FT2": "#00ffff",   # cyan
    "SSB": "#ff44ff",   # magenta
}
_DEFAULT_COLOR: str = "#ffff00"

_LINE_WIDTH_NORMAL: int = 1
_LINE_WIDTH_AWARD: int = 3

# Stylesheet for the mode-zoom buttons (dark theme, flat).
_BTN_STYLE: str = (
    "QPushButton {"
    "  color: #c0c0c0; background: #2a2a3e;"
    "  border: 1px solid #404060; border-radius: 3px;"
    "  padding: 0 8px; font-size: 11px;"
    "} "
    "QPushButton:hover { background: #3a3a5e; } "
    "QPushButton:disabled { color: #505060; border-color: #303040; } "
)

# Per-band frequency windows (kHz) for mode-group zoom buttons and zone shading.
# None marks mode groups not present on a given band (e.g. no SSB on WARC bands).
# FT windows are derived from the actual dial frequencies in DXSpotter.freqs
# (FT8/FT4/FT2 per band) ± 1 kHz so the view is tight with minimal blank space.
# CW and SSB windows follow IARU Region 2 sub-band boundaries.
_BAND_ZOOM_RANGES: dict[str, dict[str, tuple[float, float] | None]] = {
    '160m': {'CW': (1800.0, 1840.0), 'FT': (1839.0, 1841.0), 'SSB': (1843.0, 2000.0)},
    '80m':  {'CW': (3500.0, 3570.0), 'FT': (3572.0, 3579.0), 'SSB': (3700.0, 4000.0)},
    '40m':  {'CW': (7000.0, 7044.0), 'FT': (7046.5, 7075.0), 'SSB': (7100.0, 7300.0)},
    '30m':  {'CW': (10100.0, 10133.0), 'FT': (10135.0, 10145.0), 'SSB': None},
    '20m':  {'CW': (14000.0, 14070.0), 'FT': (14073.0, 14085.0), 'SSB': (14100.0, 14350.0)},
    '17m':  {'CW': (18068.0, 18098.0), 'FT': (18099.0, 18109.0), 'SSB': (18110.0, 18168.0)},
    '15m':  {'CW': (21000.0, 21070.0), 'FT': (21073.0, 21078.0), 'SSB': (21148.0, 21450.0)},
    '12m':  {'CW': (24890.0, 24912.0), 'FT': (24914.0, 24920.0), 'SSB': (24930.0, 24990.0)},
    '10m':  {'CW': (28000.0, 28070.0), 'FT': (28073.0, 28078.0), 'SSB': (28300.0, 29700.0)},
    '6m':   {'CW': (50000.0, 50110.0), 'FT': (50312.0, 50319.0), 'SSB': (50100.0, 50310.0)},
    '2m':   {'CW': (144000.0, 144100.0), 'FT': (144173.0, 144178.0), 'SSB': (144150.0, 144300.0)},
}

# RGBA fill colors for mode-zone shading (very low alpha keeps spots legible).
_ZONE_BRUSH_COLORS: dict[str, tuple[int, int, int, int]] = {
    'CW':  (0,   204, 0,   25),
    'FT':  (68,  136, 255, 25),
    'SSB': (255, 68,  255, 25),
}

# DXCC entity numbers for mainland US and Canada — matches spot_window._US_CANADA_DXCC
_US_CANADA_DXCC: frozenset[int] = frozenset({1, 291})


class BandMap(QWidget):
    """Frequency-vs-SNR bandmap for the current band.

    Spots arrive via :meth:`add_spot` (same dict format as
    :class:`~spot_window.SpotTable`, extended with an ``abs_freq_hz``
    key).  A 250 ms :class:`~PyQt6.QtCore.QTimer` flushes the pending
    buffer, expires stale entries, and redraws the plot.

    The x-axis shows absolute frequency in kHz; the y-axis shows raw
    SNR (dB).  Positive SNR means the signal is above the noise floor;
    negative values (typical for FT8) are plotted below the y=0
    baseline so stronger signals always produce taller lines.

    Award status under the active criterion is indicated by line width:
    1 px for ``'n/a'`` (mode/band/criterion mismatch), 3 px for any
    other status (confirmed / worked / new / over100 / was_new).

    Signals
    -------
    spot_activated : pyqtSignal(dict)
        Emitted on double-click near a spot.  Payload matches the
        ``_SPOT_ROLE`` format used by :class:`~spot_window.SpotTable`.
    """

    #: Emitted on double-click near a spot.  Payload matches the
    #: ``_SPOT_ROLE`` format used by :class:`~spot_window.SpotTable`.
    spot_activated = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the bandmap widget.

        Parameters
        ----------
        parent : QWidget or None, optional
            Optional Qt parent widget.
        """
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # -- mode zoom button row -----------------------------------------
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.setSpacing(4)

        self._btn_all = QPushButton("All")
        self._btn_cw  = QPushButton("CW")
        self._btn_ft  = QPushButton("FT")
        self._btn_ssb = QPushButton("SSB")

        for btn in (self._btn_all, self._btn_cw, self._btn_ft, self._btn_ssb):
            btn.setFlat(True)
            btn.setFixedHeight(22)
            btn.setStyleSheet(_BTN_STYLE)
            btn_layout.addWidget(btn)
        btn_layout.addStretch()

        self._btn_all.setToolTip("Show all spots (auto-range)")
        self._btn_cw.setToolTip("Zoom to CW sub-band")
        self._btn_ft.setToolTip("Zoom to FT8 / FT4 / FT2 sub-band")
        self._btn_ssb.setToolTip("Zoom to SSB sub-band")

        self._btn_all.clicked.connect(lambda: self._zoom_to('all'))
        self._btn_cw.clicked.connect(lambda: self._zoom_to('CW'))
        self._btn_ft.clicked.connect(lambda: self._zoom_to('FT'))
        self._btn_ssb.clicked.connect(lambda: self._zoom_to('SSB'))

        layout.addWidget(btn_row)

        # -- plot widget --------------------------------------------------
        self._plot = pg.PlotWidget()
        self._plot.setBackground("#1a1a2e")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel("left",   "SNR", units="dB",  color="#c0c0c0")
        self._plot.setLabel("bottom", "Frequency", units="kHz", color="#c0c0c0")
        for axis in ("left", "bottom"):
            ax = self._plot.getAxis(axis)
            ax.setPen("#606060")
            ax.setTextPen("#c0c0c0")
        # Y range: data occupies -24 to +12 dB; the extra headroom to 26 holds
        # the angled callsign labels for the tallest bars.  Y zoom/pan is
        # disabled so mouse wheel/drag only moves the frequency axis.
        self._plot.setYRange(-24, 26, padding=0)
        self._plot.plotItem.vb.setMouseEnabled(x=True, y=False)

        # Horizontal reference at SNR = 0
        self._zero_line = pg.InfiniteLine(
            pos=0, angle=0, pen=pg.mkPen("#404060", width=1, style=Qt.PenStyle.DashLine)
        )
        self._plot.addItem(self._zero_line)

        # Subtle separator marking the top of the data region (y = 12 dB).
        # Labels for spots near the top extend into the zone above this line.
        self._ceil_line = pg.InfiniteLine(
            pos=12, angle=0, pen=pg.mkPen("#303050", width=1, style=Qt.PenStyle.DotLine)
        )
        self._plot.addItem(self._ceil_line)

        layout.addWidget(self._plot)

        self._spot_cache: dict[str, dict] = {}
        self._pending: list[dict] = []
        self._plot_items: list = []   # pg items added per redraw cycle
        self._zone_items: list = []   # pg items added by _update_zones
        self._band: str | None = None
        self._adif_log: ADIFLog | None = None
        self._criterion: str = "mixed"
        self._display_filter: str = "all"
        self._max_age_secs: int = 30 * 60

        self._plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)

        flush_timer = QTimer(self)
        flush_timer.timeout.connect(self._flush_and_redraw)
        flush_timer.start(250)

    # -- public interface -------------------------------------------------------

    def add_spot(self, spot: dict) -> None:
        """Queue a spot for the next draw cycle.

        Parameters
        ----------
        spot : dict
            Spot payload dict; must contain ``'abs_freq_hz'`` (int, Hz)
            for the station to appear on the plot.
        """
        self._pending.append(spot)

    def clear(self) -> None:
        """Remove all spots from the cache and clear the plot."""
        self._pending.clear()
        self._spot_cache.clear()
        self._redraw()

    def set_band(self, band: str | None) -> None:
        """Set the band to display; ``None`` hides all spots.

        Also resets the plot zoom/pan so the new band fills the view.

        Parameters
        ----------
        band : str or None
            Band string (e.g. ``'20m'``), or ``None`` to clear.
        """
        if band == self._band:
            return
        self._band = band
        self._plot.setTitle(
            f"Band Map — {band}" if band else "Band Map — no band selected",
            color="#c0c0c0",
            size="9pt",
        )
        self._plot.enableAutoRange(axis='x')
        self._plot.setYRange(-24, 26, padding=0)
        self._update_zones()
        self._redraw()

    def set_adif_log(self, adif_log: ADIFLog | None) -> None:
        """Replace the contact log used for award-status line weight.

        Parameters
        ----------
        adif_log : ADIFLog or None
            New log, or ``None`` to treat all spots as unworked.
        """
        self._adif_log = adif_log
        self._redraw()

    def set_criterion(self, criterion: str) -> None:
        """Set the active award criterion and redraw.

        Parameters
        ----------
        criterion : str
            Award criterion key (e.g. ``'mixed'``, ``'5bd'``, ``'was'``).
        """
        self._criterion = criterion
        self._redraw()

    def set_display_filter(self, filter_str: str) -> None:
        """Apply the same row-visibility filter used by the spot table.

        Parameters
        ----------
        filter_str : str
            One of ``'all'``, ``'dxcc_only'`` (hide US/Canada), or
            ``'unconfirmed'`` (hide confirmed-DXCC spots).
        """
        if filter_str == self._display_filter:
            return
        self._display_filter = filter_str
        self._redraw()

    def set_max_age(self, minutes: int) -> None:
        """Set spot expiry in minutes; ``0`` disables expiry.

        Parameters
        ----------
        minutes : int
            Spots older than this are dropped on the next flush cycle.
        """
        self._max_age_secs = minutes * 60

    # -- private: zone shading & zoom -----------------------------------------

    def _update_zones(self) -> None:
        # Replace shaded LinearRegionItem decorations for the current band.
        # Regions are purely visual (movable=False, no mouse interaction).
        for item in self._zone_items:
            self._plot.removeItem(item)
        self._zone_items.clear()

        band_ranges = _BAND_ZOOM_RANGES.get(self._band or '', {})

        for mode, (r, g, b, a) in _ZONE_BRUSH_COLORS.items():
            rng = band_ranges.get(mode)
            if rng is None:
                continue
            region = pg.LinearRegionItem(
                values=rng,
                movable=False,
                brush=pg.mkBrush(r, g, b, a),
                pen=pg.mkPen(None),
            )
            region.setZValue(-10)
            # Disable all mouse interaction so clicks reach the spots beneath.
            region.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            for line in region.lines:
                line.setMovable(False)
                line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._plot.addItem(region)
            self._zone_items.append(region)

        # Enable/disable buttons to match the current band's available zones.
        self._btn_cw.setEnabled(band_ranges.get('CW') is not None)
        self._btn_ft.setEnabled(band_ranges.get('FT') is not None)
        self._btn_ssb.setEnabled(band_ranges.get('SSB') is not None)

    def _zoom_to(self, mode: str) -> None:
        # Zoom the x-axis to the frequency window for mode ('CW', 'FT', 'SSB')
        # or restore auto-range ('all').
        if mode == 'all':
            self._plot.enableAutoRange(axis='x')
            return
        if self._band is None:
            return
        rng = _BAND_ZOOM_RANGES.get(self._band, {}).get(mode)
        if rng is None:
            return
        lo, hi = rng
        self._plot.setXRange(lo, hi)

    # -- private: flush & draw -------------------------------------------------

    def _flush_and_redraw(self) -> None:
        # Merge pending into cache, then expire stale entries.
        # Only redraws when something actually changed.
        now = time.time()
        changed = bool(self._pending)

        for spot in self._pending:
            if self._max_age_secs > 0 and now - spot.get("unix_time", now) > self._max_age_secs:
                continue
            self._spot_cache[spot["call"]] = spot
        self._pending.clear()

        if self._max_age_secs > 0:
            old_len = len(self._spot_cache)
            self._spot_cache = {
                k: v
                for k, v in self._spot_cache.items()
                if now - v.get("unix_time", now) <= self._max_age_secs
            }
            if len(self._spot_cache) != old_len:
                changed = True

        if changed:
            self._redraw()

    def _redraw(self) -> None:
        # Remove all dynamically-added items then rebuild from the cache.
        for item in self._plot_items:
            self._plot.removeItem(item)
        self._plot_items.clear()

        if self._band is None:
            return

        band_spots = [
            s
            for s in self._spot_cache.values()
            if s.get("b") == self._band
            and s.get("abs_freq_hz", 0) > 0
            and self._spot_is_visible(s)
        ]
        if not band_spots:
            return

        for spot in band_spots:
            freq_khz = spot["abs_freq_hz"] / 1000.0
            try:
                snr = float(str(spot.get("rp", "0")).lstrip("+"))
            except ValueError:
                snr = 0.0
            mode  = spot.get("md", "").upper()
            call  = spot["call"]
            color = _MODE_COLORS.get(mode, _DEFAULT_COLOR)
            width = self._award_line_width(spot)

            line = pg.PlotCurveItem(
                x=[freq_khz, freq_khz],
                y=[-24.0, snr],   # bars always start at the plot floor
                pen=pg.mkPen(color=color, width=width),
            )
            self._plot.addItem(line)
            self._plot_items.append(line)

            # angle=75 → 15° off vertical; anchor=(0,1) places the bottom of the
            # pre-rotation bbox at (freq_khz, snr) so the label rises from the
            # bar top into the label zone above y=12.
            label = pg.TextItem(text=call, color=color, angle=75, anchor=(0, 1.0))
            label.setPos(freq_khz, snr)
            self._plot.addItem(label)
            self._plot_items.append(label)

    def _spot_is_visible(self, spot: dict) -> bool:
        # Mirror SpotTable._row_is_hidden logic: return True when the spot
        # should be plotted under the current display filter.
        if self._display_filter == "all":
            return True
        dxcc = spot.get("dxcc", -1)
        band = spot.get("b", "")
        mode = spot.get("md", "")
        call = spot.get("call", "")
        if self._display_filter == "dxcc_only":
            return dxcc not in _US_CANADA_DXCC
        if self._display_filter == "unconfirmed":
            adif = self._adif_log
            if adif is None:
                return True
            if self._criterion == "was":
                if dxcc == 291:
                    state  = adif.call_state(call)
                    status = adif.was_status(state, band)
                else:
                    status = "n/a"
            elif adif.mode_matches_criterion(mode, self._criterion):
                status = adif.award_status(dxcc, band, self._criterion)
            else:
                status = "n/a"
            return status != "confirmed"
        return True

    def _award_line_width(self, spot: dict) -> int:
        # Return _LINE_WIDTH_AWARD if the spot has any non-'n/a' award status,
        # else _LINE_WIDTH_NORMAL.  Mirrors the status logic in spot_window.py.
        adif = self._adif_log
        if adif is None:
            return _LINE_WIDTH_NORMAL
        dxcc      = spot.get("dxcc", -1)
        band      = spot.get("b", "")
        mode      = spot.get("md", "")
        call      = spot.get("call", "")
        criterion = self._criterion

        if criterion == "was":
            if dxcc == 291:
                state  = adif.call_state(call)
                status = adif.was_status(state, band)
            else:
                status = "n/a"
        elif adif.mode_matches_criterion(mode, criterion):
            status = adif.award_status(dxcc, band, criterion)
            if status == "new" and criterion == "5bd":
                if adif.confirmed_5bd_count(band) >= 100:
                    status = "over100"
        else:
            status = "n/a"

        return _LINE_WIDTH_NORMAL if status == "n/a" else _LINE_WIDTH_AWARD

    # -- private: click handling -----------------------------------------------

    def _on_mouse_clicked(self, event) -> None:
        # Only act on double left-clicks inside the view box.
        if not event.double():
            return
        vb = self._plot.plotItem.vb
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        view_pos  = vb.mapSceneToView(event.scenePos())
        freq_khz  = view_pos.x()

        # Convert 10 screen pixels to a kHz tolerance in data space.
        p2 = vb.mapSceneToView(
            QPointF(event.scenePos().x() + 10.0, event.scenePos().y())
        )
        tolerance = abs(p2.x() - freq_khz)

        spot = self._nearest_spot(freq_khz, tolerance)
        if spot is None:
            return
        self.spot_activated.emit({
            "call":        spot["call"],
            "md":          spot.get("md", ""),
            "b":           spot.get("b", ""),
            "freq_offset": spot.get("freq_offset", 0),
            "unix_time":   spot.get("unix_time", 0.0),
            "rp":          spot.get("rp", "0"),
            "msg":         spot.get("msg", ""),
            "delta_t":     spot.get("delta_t", 0.0),
            "loc":         spot.get("loc", ""),
            "source":      spot.get("source", "psk"),
        })

    def _nearest_spot(self, freq_khz: float, tolerance_khz: float) -> dict | None:
        # Return the cache entry closest to freq_khz within tolerance_khz, or None.
        band_spots = [
            s
            for s in self._spot_cache.values()
            if s.get("b") == self._band and s.get("abs_freq_hz", 0) > 0
        ]
        if not band_spots:
            return None
        closest = min(
            band_spots,
            key=lambda s: abs(s["abs_freq_hz"] / 1000.0 - freq_khz),
        )
        if abs(closest["abs_freq_hz"] / 1000.0 - freq_khz) <= tolerance_khz:
            return closest
        return None
