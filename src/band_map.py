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

from dataclasses import dataclass
import time
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QPushButton, QWidget

if TYPE_CHECKING:
    from adif_log import ADIFLog

_DEFAULT_COLOR: str = "#ffff00"

_LINE_WIDTH_NORMAL: int = 1
_LINE_WIDTH_AWARD: int = 3

# Canonical mode -> RGB color, shared by the plot-line palette (_MODE_COLORS),
# the submode zone shading (_SUBMODE_BRUSH_COLORS), and the mode-zoom button
# backgrounds (_MODE_BUTTON_COLORS) so all three stay visually consistent.
_MODE_RGB: dict[str, tuple[int, int, int]] = {
    "All":  (255, 255, 0),    # yellow
    "CW":   (0, 204, 0),      # green
    "FT8":  (68, 136, 255),   # blue
    "FT4":  (110, 165, 255),  # medium blue
    "FT2":  (160, 200, 255),  # pale blue
    "SSB":  (255, 68, 255),   # magenta
    "RTTY": (255, 136, 0),    # orange
}

# Mode → plot color (matches on_message colorама assignments)
_MODE_COLORS: dict[str, str] = {
    mode: f"#{r:02x}{g:02x}{b:02x}" for mode, (r, g, b) in _MODE_RGB.items()
}

# RGBA fill colors for mode-zone shading (very low alpha keeps spots legible).
_SUBMODE_ALPHA = 25
_SUBMODE_BRUSH_COLORS: dict[str, tuple[int, int, int, int]] = {
    mode: (*rgb, _SUBMODE_ALPHA) for mode, rgb in _MODE_RGB.items() if mode != "All"
}
_BTN_ALPHA = 128
_MODE_BUTTON_COLORS: dict[str, tuple[int, int, int, int]] = {
    mode: (*rgb, _BTN_ALPHA) for mode, rgb in _MODE_RGB.items()
}


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


@dataclass
class ButtonStyle:
    """Encapsulates a QPushButton's style, color and tooltip for a mode-zoom button.

    Parameters
    ----------
    style : str
        CSS stylesheet string.
    tooltip : str
        Tooltip text.
    """

    button: QPushButton
    name: str
    tooltip: str
    style: str = _BTN_STYLE

    def __post_init__(self) -> None:
        self.button.setFlat(True)
        self.button.setFixedHeight(22)
        self.button.setStyleSheet(self.style)
        self.button.setToolTip(self.tooltip)

    def set(self, style: str, tooltip: str) -> None:
        """Update the style and tooltip.

        Parameters
        ----------
        style : str
            CSS stylesheet string.
        tooltip : str
            Tooltip text.
        """
        self.style = style
        self.tooltip = tooltip
        self.button.setStyleSheet(self.style)
        self.button.setToolTip(self.tooltip)

    def update_style(
        self,
        color: tuple[int, int, int, int],
        border: str = "border: 2px solid #404040; border-radius: 3px",
    ) -> None:
        """Update the button's background color and border.

        Parameters
        ----------
        color : tuple[int, int, int, int]
            RGBA color tuple.
        border : str, optional
            CSS border string (default is a dark gray border with rounded corners).
        """
        self.style = f"background-color: rgba{str(color)}; {border}; color: white;"
        self.button.setStyleSheet(self.style)


# Per-band frequency windows (kHz) for mode-group zoom buttons and zone shading.
# None marks mode groups not present on a given band (e.g. no SSB on WARC bands).
# FT windows are derived from the actual dial frequencies in DXSpotter.freqs
# (FT8/FT4/FT2 per band) ± 1 kHz so the view is tight with minimal blank space.
# CW and SSB windows follow IARU Region 2 sub-band boundaries.
_BAND_MODE_ZOOM_RANGES: dict[str, dict[str, tuple[float, float] | None]] = {
    "160m": {
        "CW": (1800.0, 1840.0),
        "RTTY": (1800.0, 1840.0),
        "FT8": (1839.0, 1841.0),
        "FT4": (1836.0, 1840.0),   # dial 1838 kHz +/- 2
        "FT2": (1843.0, 1847.0),               # not used on 160m
        "SSB": (1843.0, 2000.0),
    },
    "80m": {
        "CW": (3500.0, 3570.0),
        "RTTY": (3560.0, 3600.0),
        "FT8": (3571.0, 3574.0),
        "FT4": (3574.0, 3578.0),   # dial 3575 kHz + 4
        "FT2": (3578.0, 3582.0),   # dial 3577 kHz +/- 1
        "SSB": (3700.0, 4000.0),
    },
    "40m": {
        "CW": (7000.0, 7044.0),
        "RTTY": (7025.0, 7100.0),
        "FT8": (7073.0, 7079.0),
        "FT4": (7046.5, 7051.5),   # dial 7047.5 kHz + 3
        "FT2": (7061.0, 7065.0),   # dial 7062 kHz +/- 1
        "SSB": (7100.0, 7300.0),
    },
    "30m": {
        "CW": (10100.0, 10133.0),
        "RTTY": (10120.0, 10150.0),
        "FT8": (10134.0, 10145.0),
        "FT4": (10139.0, 10143.0),  # dial 10140 kHz +/- 1
        "FT2": (10141.0, 10145.0),  # dial 10144 kHz +/- 1
        "SSB": None,
    },
    "20m": {
        "CW": (14000.0, 14070.0),
        "RTTY": (14080.5, 14150.0),
        "FT8": (14073.0, 14085.0),
        "FT4": (14079.0, 14083.0),  # dial 14080 kHz +/- 1
        "FT2": (14083.0, 14087.0),  # dial 14084 kHz +/- 1
        "SSB": (14100.0, 14350.0),
    },
    "17m": {
        "CW": (18068.0, 18098.0),
        "RTTY": (18100.0, 18109.5),
        "FT8": (18099.0, 18109.0),
        "FT4": (18103.0, 18107.0),  # dial 18104 kHz +/- 1
        "FT2": (18107.0, 18110.0),  # dial 18108 kHz +/- 1
        "SSB": (18110.0, 18168.0),
    },
    "15m": {
        "CW": (21000.0, 21070.0),
        "RTTY": (21080.5, 21150.0),
        "FT8": (21073.0, 21078.0),
        "FT4": (21139.0, 21143.0),  # dial 21140 kHz +/- 1
        "FT2": (21143.0, 21147.0),  # dial 21144 kHz +/- 1
        "SSB": (21148.0, 21450.0),
    },
    "12m": {
        "CW": (24890.0, 24912.0),
        "RTTY": (24910.0, 24929.5),
        "FT8": (24914.0, 24920.0),
        "FT4": (24918.0, 24922.0),  # dial 24919 kHz +/- 1
        "FT2": (24922.0, 24925.0),                # not used on 12m
        "SSB": (24930.0, 24990.0),
    },
    "10m": {
        "CW": (28000.0, 28070.0),
        "RTTY": (28080.5, 28200.0),
        "FT8": (28073.0, 28078.0),
        "FT4": (28179.0, 28183.0),  # dial 28180 kHz +/- 1
        "FT2": (28183.0, 28187.0),  # dial 28184 kHz +/- 1
        "SSB": (28300.0, 29700.0),
    },
    "6m": {
        "CW": (50000.0, 50110.0),
        "RTTY": (50050.0, 50100.0),
        "FT8": (50312.0, 50317.0),
        "FT4": (50317.0, 50323.0),  # dial 50318 kHz +/- 1
        "FT2": (50327.0, 50331.0),  # dial 50316 kHz +/- 1
        "SSB": (50100.0, 50310.0),
    },
    "2m": {
        "CW": (144000.0, 144100.0),
        "RTTY": (144050.0, 144100.0),
        "FT8": (144173.0, 144178.0),
        "FT4": (144170.0, 144174.0),   
        "FT2": (144176.0, 144178.0),     # dial 144177 kHz +/- 1
        "SSB": (144150.0, 144300.0),
    },
}

# Mode bandwiths to calculate the frequency
# range nominally occupied by each spot.
_MODE_BANDWIDTHS: dict[str, float] = {
    "CW": 0.1,
    "FT8": 0.05,
    "FT4": 0.1,
    "FT2": 2.0,
    "SSB": 3.0,
    "RTTY": 0.5,
}

# frequency offsets from nominal with SSB
# based on USB/LSB as customary for each band.
_SSB_FREQ_OFFSETS: dict[str, float] = {
    "160m": -3.0,  # lsb
    "80m": -3.0,
    "40m": -3.0,
    "30m": 0.0,  # no ssb here.
    "20m": +3.0,
    "17m": +3.0,
    "15m": +3.0,
    "12m": +3.0,
    "10m": +3.0,
    "6m": +3.0,
    "2m": +3.0,
}


# DXCC entity number for mainland US, excluded by the 'dxcc_only' filter —
# matches spot_window._MAINLAND_US_DXCC.
_MAINLAND_US_DXCC: int = 291


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

        # -- subband zoom button row -----------------------------------------
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.setSpacing(4)
        self.mode_buttons: dict[str, ButtonStyle] = {}
        for mode in ("All", "CW", "FT8", "FT4", "FT2", "SSB", "RTTY"):
            btn = ButtonStyle(
                QPushButton(mode),
                name=mode,
                style=_BTN_STYLE,
                tooltip=f"Zoom to {mode} sub-band",
            )
            btn.update_style(_MODE_BUTTON_COLORS[mode])
            btn.button.clicked.connect(lambda checked, m=mode: self._zoom_to(m))
            btn_layout.addWidget(btn.button)
            self.mode_buttons[mode] = btn
        btn_layout.addStretch()  # cluster the buttons on the left

        layout.addWidget(btn_row)

        # -- plot widget --------------------------------------------------
        self._plot = pg.PlotWidget()
        self._plot.setBackground("#1a1a2e")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel("left", "SNR", units="dB", color="#c0c0c0")
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
        self._plot_items: list = []  # pg items added per redraw cycle
        self._submode_items: list = []  # pg items added by _update_zones
        self._band: str | None = None
        self._adif_log: ADIFLog | None = None
        self._criterion: str = "mixed"
        self._display_filter: str = "all"
        self._max_age_secs: int = 30 * 60
        self.freq_map = []  # List of frequencies "in use" to help select an open spot.
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
        self._plot.enableAutoRange(axis="x")
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
        for item in self._submode_items:
            self._plot.removeItem(item)
        self._submode_items.clear()

        band_ranges = _BAND_MODE_ZOOM_RANGES.get(self._band or "", {})

        for mode, (r, g, b, a) in _SUBMODE_BRUSH_COLORS.items():
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
            self._submode_items.append(region)

        # Enable/disable every mode button to match the current band's
        # available zoom ranges.  "All" has no per-band range and always
        # stays enabled (it resets to auto-range).
        for mode, btn in self.mode_buttons.items():
            if mode == "All":
                continue
            btn.button.setEnabled(band_ranges.get(mode) is not None)

    def _zoom_to(self, mode: str) -> None:
        # Zoom the x-axis to the frequency window for mode ('CW', 'FT', 'SSB')
        # or restore auto-range ('All').
        if mode.lower() == "all":
            self._plot.enableAutoRange(axis="x")
            return
        if self._band is None:
            return
        rng = _BAND_MODE_ZOOM_RANGES.get(self._band, {}).get(mode)
        if rng is None:
            return
        lo, hi = rng
        self._plot.setXRange(lo, hi)

    # -- private: flush & draw -------------------------------------------------

    def _flush_and_redraw(self) -> None:
        # Merge pending spots into cache, then expire stale entries.
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
            if s.get("b") == self._band and s.get("abs_freq_hz", 0) > 0 and self._spot_is_visible(s)
        ]
        if not band_spots:
            return

        for spot in band_spots:
            freq_khz = spot["abs_freq_hz"] / 1000.0
            try:
                snr = float(str(spot.get("rp", "0")).lstrip("+"))
            except ValueError:
                snr = 0.0
            mode = spot.get("md", "").upper()
            call = spot["call"]
            color = _MODE_COLORS.get(mode, _DEFAULT_COLOR)
            width = self._award_line_width(spot)

            line = pg.PlotCurveItem(
                x=[freq_khz, freq_khz],
                y=[-24.0, snr],  # bars always start at the plot floor
                pen=pg.mkPen(color=color, width=width),
            )
            self._plot.addItem(line)
            self._plot_items.append(line)

            # calculate bandwidth for the mode, default to 0 if not found
            bandwidth_value = _MODE_BANDWIDTHS.get(mode, 0)
            bandwidth_offset = _SSB_FREQ_OFFSETS.get(self._band, 0) if mode == "SSB" else 0
            freq_khz += bandwidth_offset  # adjust frequency for SSB offset if applicable
            f_low_high = (freq_khz - bandwidth_value / 2, freq_khz + bandwidth_value / 2)
            bandwidth = pg.LinearRegionItem(
                values=f_low_high,
                orientation=pg.LinearRegionItem.Vertical,
                movable=False,
                brush=pg.mkBrush(color + "40"),  # semi-transparent fill
                pen=pg.mkPen(None),  # no border
            )
            self._plot.addItem(bandwidth)
            self._plot_items.append(bandwidth)
            self.freq_map.append(list(f_low_high))

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
            return dxcc != _MAINLAND_US_DXCC
        if self._display_filter == "unconfirmed":
            adif = self._adif_log
            if adif is None:
                return True
            if self._criterion == "was":
                status = adif.was_status(adif.resolve_was_state(call, dxcc), band)
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
        dxcc = spot.get("dxcc", -1)
        band = spot.get("b", "")
        mode = spot.get("md", "")
        call = spot.get("call", "")
        criterion = self._criterion

        if criterion == "was":
            status = adif.was_status(adif.resolve_was_state(call, dxcc), band)
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
        view_pos = vb.mapSceneToView(event.scenePos())
        freq_khz = view_pos.x()

        # Convert 10 screen pixels to a kHz tolerance in data space.
        p2 = vb.mapSceneToView(QPointF(event.scenePos().x() + 10.0, event.scenePos().y()))
        tolerance = abs(p2.x() - freq_khz)

        spot = self._nearest_spot(freq_khz, tolerance)
        if spot is None:
            return
        self.spot_activated.emit(
            {
                "call": spot["call"],
                "md": spot.get("md", ""),
                "b": spot.get("b", ""),
                "freq_offset": spot.get("freq_offset", 0),
                "unix_time": spot.get("unix_time", 0.0),
                "rp": spot.get("rp", "0"),
                "msg": spot.get("msg", ""),
                "delta_t": spot.get("delta_t", 0.0),
                "loc": spot.get("loc", ""),
                "source": spot.get("source", "psk"),
            }
        )

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
