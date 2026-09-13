"""Spot table widget and supporting helpers for DX Spotter.

The central UI component is :class:`SpotTable`, a ``QWidget`` wrapping a
``QTableWidget`` that displays incoming DX spots from PSK Reporter and WSJT-X,
colors rows by DXCC award status, and provides right-click QSO-detail context
menus.

Module-level constants
----------------------
COLUMNS : list[str]
    Ordered list of column header strings for the spot table.
SPOT_AGE_COL, QSL_COL, CALL_COL : int
    Pre-computed column indices for the Age, QSL, and DX Call columns.
AWARD_COLORS : dict[str, tuple[str, str]]
    Maps award status → ``(background_hex, foreground_hex)`` color pairs.
_US_CANADA_AK_HI_DXCC : frozenset[int]
    ADIF DXCC entity numbers for mainland US (291), Canada (1), Alaska (6),
    and Hawaii (110) — the entities shown by the ``'us_canada'`` display
    filter.
_MAINLAND_US_DXCC : int
    ADIF DXCC entity number for mainland US (291), excluded by the
    ``'dxcc_only'`` display filter.
"""
from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QMenu, QTableWidget, QTableWidgetItem, QAbstractItemView,
)
from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontDatabase, QIcon, QPixmap

import fcc_db

if TYPE_CHECKING:
    from adif_log import ADIFLog


COLUMNS = ["DX Call", "SNR", "Country", "DX Grid", "Time", "Spot Age",
           "dHz", "Dist", "Mode", "Band", "Src", "Reporter", "Rptr Grid",
           "Range", "QSL"]

# DXCC entity numbers shown by the 'us_canada' filter: Canada, mainland US,
# Alaska, and Hawaii.
_US_CANADA_AK_HI_DXCC: frozenset[int] = frozenset({1, 6, 110, 291})

# DXCC entity number excluded by the 'dxcc_only' filter: mainland US only.
_MAINLAND_US_DXCC: int = 291

SPOT_AGE_COL = COLUMNS.index("Spot Age")
QSL_COL      = COLUMNS.index("QSL")
CALL_COL     = COLUMNS.index("DX Call")
COUNTRY_COL  = COLUMNS.index("Country")
REPORTER_COL = COLUMNS.index("Reporter")
TIME_COL     = COLUMNS.index("Time")

# Separate role for the spot-action dict on CALL_COL (avoids collision with
# the dxcc/band/mode dict stored in UserRole on the same cell).
_SPOT_ROLE = Qt.ItemDataRole.UserRole + 1

# Source abbreviations shown in the Src column.
_SRC_ABBR: dict[str, str] = {
    'psk': 'P', 'wsjt': 'W',
    'telnet1': 'T1', 'telnet2': 'T2', 'telnet3': 'T3', 'telnet4': 'T4',
}

# Columns visible in Laptop Mode.
_LAPTOP_COLS: frozenset[str] = frozenset(
    {"DX Call", "SNR", "Country", "DX Grid", "Time", "Mode", "Src", "Reporter", "QSL"}
)

# 3-state award color scheme
# confirmed = grey (already in the log for this award)
# worked    = orange (QSO in log, need confirmation)
# new       = red (never worked, needed for award)
AWARD_COLORS: dict[str, tuple[str, str]] = {
    'confirmed': ("#505050", "#d0d0d0"),
    'worked':    ("#05a995", "#ffffff"),
    'new':       ("#8b0000", "#ffffff"),
    'n/a':       ("#505050", "#808080"),  # same bg as confirmed, dimmer text
    'over100':   ("#C47A65", "#00e0e0"),  # 5BD-only: band already ≥100 confirmed
    'was_new':   ("#ca653a", "#ffff00"),  # WAS: state not yet worked on this band
}

_CRIT_ABBR: dict[str, str] = {
    '5bd':     '5BD',
    'warc':    'WARC',
    'cw':      'CW',
    'mixed':   'Mix',
    'digital': 'Dig',
    'ssb':     'SSB',
    '6m':      '6M',
    'was':     'WAS',
}


def make_app_icon() -> QIcon:
    """Load and return the DX Spotter application icon.

    The source PNG is 1024×1024 px.  Loading it as a single ``QIcon`` tells
    macOS the icon's natural size is 1024 logical points, so the dock shows
    it larger than other icons when the app registers itself at runtime.

    Instead, pre-scale the source into the standard macOS icon sizes and add
    each as a separate pixmap.  Qt builds a multi-representation ``NSImage``
    from these, macOS picks the representation closest to the current dock
    tile size, and the icon appears the same apparent size as every other
    dock icon — identical behaviour to a bundle ``.icns`` file.

    Returns
    -------
    QIcon
        Multi-resolution icon for the dock and title bar.
    """
    from PyQt6.QtCore import Qt as _Qt
    path = os.path.join(os.path.dirname(__file__), "icons", "dxspot.png")
    src = QPixmap(path)  # 1024×1024 master
    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512):
        icon.addPixmap(
            src.scaled(size, size,
                       _Qt.AspectRatioMode.KeepAspectRatio,
                       _Qt.TransformationMode.SmoothTransformation)
        )
    return icon


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def _effective_status(status: str, criterion: str, band: str, adif) -> str:
    """For 5BD only: promote 'new' to 'over100' when the band already has ≥100 confirmed."""
    if status == 'new' and criterion == '5bd' and adif is not None:
        if adif.confirmed_5bd_count(band) >= 100:
            return 'over100'
    return status


def _award_qsl_label(status: str, criterion: str, band: str,
                     conf_list: list[dict[str, str]],
                     wkd_list: list[dict[str, str]]) -> str:
    """Build the QSL column text for the given award status and criterion."""
    abbr = _CRIT_ABBR.get(criterion, criterion.upper())
    if criterion == '5bd' and band:
        abbr = f"5BD/{band.lower()}"
    elif criterion == 'warc' and band:
        abbr = f"WARC/{band.lower()}"

    if status == 'confirmed' and conf_list:
        first = conf_list[0]
        n = len(conf_list)
        cnt = f"({n})" if n > 1 else ""
        return f"Conf{cnt}[{abbr}]: {first['call']} {first['band'].lower()}/{first['mode']} {_fmt_date(first['date'])}"
    if status == 'worked' and wkd_list:
        first = wkd_list[0]
        n = len(wkd_list)
        cnt = f"({n})" if n > 1 else ""
        return f"Wkd{cnt}[{abbr}]: {first['call']} {first['band'].lower()}/{first['mode']} {_fmt_date(first['date'])}"
    if status == 'confirmed':
        return f"Conf [{abbr}]"
    if status == 'worked':
        return f"Wkd [{abbr}]"
    if status == 'n/a':
        return f"— [{abbr}]"
    if status == 'over100':
        return f"New &  band>100 [{abbr}]"
    return f"New [{abbr}]"


def _was_qsl_label(status: str, state: str, band: str) -> str:
    """Build the QSL column text for the WAS criterion."""
    abbr = f"WAS/{band.lower()}" if band else "WAS"
    state_part = f": {state}" if state else ""
    if status == 'confirmed':
        return f"Conf [{abbr}]{state_part}"
    if status == 'worked':
        return f"Wkd [{abbr}]{state_part}"
    if status == 'n/a':
        return f"— [{abbr}]"
    return f"New [{abbr}]{state_part}"


class _AgeItem(QTableWidgetItem):
    """Age column item: displays human-readable age, sorts by unix_time stored in UserRole.

    Overrides ``__lt__`` so that Qt's built-in sort compares by Unix timestamp
    (larger timestamp = more recent = smaller displayed age) rather than by the
    human-readable text string.
    """

    def __lt__(self, other: QTableWidgetItem) -> bool:
        my_ts = self.data(Qt.ItemDataRole.UserRole)
        other_ts = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(my_ts, (int, float)) and isinstance(other_ts, (int, float)):
            return my_ts > other_ts   # larger timestamp = more recent = smaller age
        return super().__lt__(other)


class SpotTable(QWidget):
    """Scrollable spot table designed to be embedded in a DockArea dock.

    Spots arrive via :meth:`add_spot` (called from the Qt main thread through
    the :attr:`~main_window.MainWindow.new_spot` signal) and are buffered in
    ``_pending_spots``.  A 250 ms :class:`~PyQt6.QtCore.QTimer` flushes the
    buffer in batch, deduplicating by callsign so only the most recent spot
    per call is kept.

    A separate 15 s timer updates the Age column and removes rows that have
    exceeded ``_max_age_secs``.

    Each row is colored according to the active DXCC award criterion and the
    spot's mode, using the :data:`AWARD_COLORS` palette.  When the criterion
    or ADIF log changes, :meth:`set_criterion` and :meth:`set_adif_log` trigger
    a full re-style pass.

    Signals
    -------
    spot_activated : pyqtSignal(dict)
        Emitted when the user double-clicks a spot row.  The dict contains
        the spot-action fields stored in ``_SPOT_ROLE`` on the DX Call cell.
    spots_expired : pyqtSignal(int, int, int, int, int, int)
        Emitted after the age-expiry pass with ``(psk_removed, wsjt_removed,
        telnet1_removed, telnet2_removed, telnet3_removed, telnet4_removed)``
        counts so :class:`~dxspotter.DXSpotter` can decrement its counters.
    """

    spot_activated = pyqtSignal(dict)        # double-click on a row → DXSpotter
    # (psk_removed, wsjt_removed, telnet1_removed, telnet2_removed,
    #  telnet3_removed, telnet4_removed) on row expiry
    spots_expired  = pyqtSignal(int, int, int, int, int, int)

    _MODE_SORT: dict[str, int] = {"CW": 0, "SSB": 1, "FT4": 2, "FT8": 3, "FT2": 4}

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the spot table widget and start internal timers.

        Parameters
        ----------
        parent : QWidget or None, optional
            Optional Qt parent widget.
        """
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)  # type: ignore[union-attr]
        self.table.horizontalHeader().setStretchLastSection(True)  # type: ignore[union-attr]

        fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed_font.setPointSize(12)
        self.table.setFont(fixed_font)
        self._fixed_font = fixed_font

        italic_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        italic_font.setPointSize(12)
        italic_font.setItalic(True)
        self._italic_font = italic_font

        bold_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        bold_font.setPointSize(12)
        bold_font.setBold(True)
        self._bold_font = bold_font

        bold_italic_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        bold_italic_font.setPointSize(12)
        bold_italic_font.setBold(True)
        bold_italic_font.setItalic(True)
        self._bold_italic_font = bold_italic_font

        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table)

        age_timer = QTimer(self)
        age_timer.timeout.connect(self._update_ages)
        age_timer.start(15_000)

        self._pending_spots: list[dict] = []
        batch_timer = QTimer(self)
        batch_timer.timeout.connect(self._flush_pending)
        batch_timer.start(1000)

        self._adif_log: ADIFLog | None = None
        self._criterion: str = 'mixed'
        self._display_filter: str = 'all'
        self._dimmed_calls: set[str] = set()
        self._selected_call: str = ''
        self._max_age_secs: int = 30 * 60  # 0 = no expiry
        self._laptop_mode: bool = False

    # -- public interface -----------------------------------------------------

    def add_spot(self, spot: dict) -> None:
        """Queue a spot for the next batch-flush cycle.

        This method is called from the Qt main thread via the
        :attr:`~main_window.MainWindow.new_spot` signal.  The spot is not
        inserted into the table immediately; it is appended to
        ``_pending_spots`` and processed by :meth:`_flush_pending` every 250 ms.

        Parameters
        ----------
        spot : dict
            Spot payload dict (see module docstring for key descriptions).
        """
        self._pending_spots.append(spot)

    def clear(self) -> None:
        """Remove all rows from the table and reset transient state."""
        self._pending_spots.clear()
        self._dimmed_calls.clear()
        self._selected_call = ''
        self.table.setRowCount(0)

    def dim_call(self, call: str) -> None:
        """Dim all rows for this callsign (call entered a QSO, no longer actively CQ-ing)."""
        if call in self._dimmed_calls:
            return
        self._dimmed_calls.add(call)
        if call == self._selected_call:
            self._selected_call = ''
        for row in range(self.table.rowCount()):
            item = self.table.item(row, CALL_COL)
            if item is None or item.text() != call:
                continue
            self._set_row_font(row, bold=False)
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is None:
                    continue
                bg = cell.background().color()
                fg = cell.foreground().color()
                cell.setBackground(QColor(bg.red() // 2, bg.green() // 2, bg.blue() // 2))
                cell.setForeground(QColor(fg.red() // 2, fg.green() // 2, fg.blue() // 2))

    def undim_call(self, call: str) -> None:
        """Restore full award colors for a callsign that has returned to calling CQ."""
        self._dimmed_calls.discard(call)
        adif = self._adif_log
        criterion = self._criterion
        for row in range(self.table.rowCount()):
            item = self.table.item(row, CALL_COL)
            if item is None or item.text() != call:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            dxcc = data.get('dxcc', -1)
            band = data.get('band', '')
            mode = data.get('mode', '')
            if adif is not None:
                if criterion == 'was':
                    status = adif.was_status(adif.resolve_was_state(call, dxcc), band)
                elif adif.mode_matches_criterion(mode, criterion):
                    status = _effective_status(adif.award_status(dxcc, band, criterion),
                                               criterion, band, adif)
                else:
                    status = 'n/a'
            else:
                status = 'new'
            color_key = 'was_new' if criterion == 'was' and status == 'new' else status
            bg_hex, fg_hex = AWARD_COLORS.get(color_key, AWARD_COLORS['new'])
            bg = QColor(bg_hex)
            fg = QColor(fg_hex)
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is not None:
                    cell.setBackground(bg)
                    cell.setForeground(fg)

    def set_adif_log(self, adif_log: 'ADIFLog | None') -> None:
        """Replace the contact log used for award-status coloring.

        Parameters
        ----------
        adif_log : ADIFLog or None
            New log instance, or ``None`` to clear (all spots colored as
            ``'new'``).  Call :meth:`set_criterion` after this to trigger a
            re-style pass.
        """
        self._adif_log = adif_log

    def set_criterion(self, criterion: str) -> None:
        """Set the active award criterion and restyle all visible rows.

        Also re-applies the display filter because the ``'unconfirmed'``
        filter depends on the current criterion.

        Parameters
        ----------
        criterion : str
            Award criterion key (see :meth:`~adif_log.ADIFLog.award_status`).
        """
        self._criterion = criterion
        self._restyle_all()
        self._apply_display_filter()  # 'unconfirmed' depends on criterion

    def set_max_age(self, minutes: int) -> None:
        """Set the maximum spot age; older rows are removed on the next timer tick.

        Parameters
        ----------
        minutes : int
            Spots older than this many minutes are expired.  ``0`` disables
            expiry (rows are kept indefinitely).
        """
        self._max_age_secs = minutes * 60

    def set_display_filter(self, filter_str: str) -> None:
        """Apply a row-visibility filter to the spot table.

        Parameters
        ----------
        filter_str : str
            One of ``'all'`` (no filtering), ``'dxcc_only'`` (hide US/Canada
            entities), or ``'unconfirmed'`` (hide confirmed-DXCC rows).
        """
        self._display_filter = filter_str
        self._apply_display_filter()

    def set_laptop_mode(self, enabled: bool) -> None:
        """Toggle laptop mode: show only essential columns and abbreviated timestamps.

        Parameters
        ----------
        enabled : bool
            When ``True``, hides non-essential columns and displays only the
            ``HH:MM:SS`` portion of each spot timestamp.  When ``False``,
            restores all columns and full timestamps.
        """
        self._laptop_mode = enabled
        for i, name in enumerate(COLUMNS):
            self.table.setColumnHidden(i, enabled and name not in _LAPTOP_COLS)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, TIME_COL)
            if item is not None:
                full_ts: str = item.data(Qt.ItemDataRole.UserRole) or item.text()
                item.setText(full_ts[-8:] if enabled else full_ts)
        self.table.resizeColumnsToContents()

    def _apply_display_filter(self) -> None:
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, self._row_is_hidden(row))

    def _row_is_hidden(self, row: int) -> bool:
        if self._display_filter == 'all':
            return False
        call_item = self.table.item(row, CALL_COL)
        if call_item is None:
            return False
        data = call_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return False
        dxcc = data.get('dxcc', -1)
        band = data.get('band', '')
        mode = data.get('mode', '')
        if self._display_filter == 'us_canada':
            return dxcc not in _US_CANADA_AK_HI_DXCC
        if self._display_filter == 'dxcc_only':
            return dxcc == _MAINLAND_US_DXCC
        if self._display_filter == 'unconfirmed':
            adif = self._adif_log
            if adif is None:
                return False
            if self._criterion == 'was':
                status = adif.was_status(adif.resolve_was_state(call_item.text(), dxcc), band)
            elif adif.mode_matches_criterion(mode, self._criterion):
                status = adif.award_status(dxcc, band, self._criterion)
            else:
                status = 'n/a'
            return status == 'confirmed'
        return False

    # -- private: batch flush -------------------------------------------------

    def _flush_pending(self) -> None:
        if not self._pending_spots:
            return
        now = time.time()
        spots, self._pending_spots = self._pending_spots, []
        if self._max_age_secs > 0:
            spots = [s for s in spots
                     if now - s.get('unix_time', now) <= self._max_age_secs]
        if not spots:
            return
        # Keep the most-recently-heard report per call, not just the last one
        # in arrival order — reports for the same call can land in this batch
        # out of chronological order.
        deduped: dict[str, dict] = {}
        for spot in spots:
            existing = deduped.get(spot['call'])
            if existing is None or spot.get('unix_time', 0) >= existing.get('unix_time', 0):
                deduped[spot['call']] = spot
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        for spot in deduped.values():
            self._insert_spot(spot)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)   # may reorder rows
        self.table.resizeColumnsToContents()
        self.table.scrollToTop()

    def _insert_spot(self, spot: dict) -> None:
        dxcc = spot.get('dxcc', -1)
        band = spot.get('b', '')
        mode = spot.get('md', '')
        call = spot['call']

        adif = self._adif_log
        criterion = self._criterion
        state = ''
        if criterion == 'was':
            conf_list, wkd_list = [], []
            if adif is None:
                status = 'new'
            else:
                state = adif.resolve_was_state(call, dxcc)
                status = adif.was_status(state, band)
        elif adif is not None:
            if adif.mode_matches_criterion(mode, criterion):
                status = _effective_status(adif.award_status(dxcc, band, criterion),
                                           criterion, band, adif)
                conf_list, wkd_list = adif.criterion_qso_details(dxcc, band, criterion)
            else:
                status, conf_list, wkd_list = 'n/a', [], []
        else:
            status, conf_list, wkd_list = 'new', [], []

        color_key = 'was_new' if criterion == 'was' and status == 'new' else status
        bg_hex, fg_hex = AWARD_COLORS.get(color_key, AWARD_COLORS['new'])
        bg = QColor(bg_hex)
        fg = QColor(fg_hex)

        # Remove existing row for this call so it reappears at the top
        for r in range(self.table.rowCount() - 1, -1, -1):
            existing = self.table.item(r, CALL_COL)
            if existing is not None and existing.text() == spot['call']:
                self.table.removeRow(r)

        self.table.insertRow(0)
        row = 0

        src = spot.get('source', 'psk')
        ts = spot['timestamp']
        range_km = spot.get('range', 0)
        if criterion == 'was':
            qsl_text = _was_qsl_label(status, state, band)
        else:
            qsl_text = _award_qsl_label(status, criterion, band, conf_list, wkd_list)

        # WAS mode: replace Country with "US <state>" for US stations.
        orig_country = spot['country']
        country_display = orig_country
        if criterion == 'was' and dxcc == 291:
            was_state = state if state else ('?' if '/' in call else '')
            country_display = f"US {was_state}" if was_state else "US"

        values = [
            spot['call'],                                                    # DX Call
            f"{spot['rp']} dB",                                              # SNR
            country_display,                                                 # Country
            spot['loc'][:6],                                                 # DX Grid
            ts[-8:] if self._laptop_mode else ts,                           # Time
            _format_age(int(time.time() - spot['unix_time'])),              # Spot Age
            str(spot['freq_offset']),                                        # dHz
            str(spot['distance']),                                           # Dist
            spot['md'],                                                      # Mode
            spot['b'],                                                       # Band
            _SRC_ABBR.get(src, src[:1].upper() if src else '?'),            # Src
            spot['rc'],                                                      # Reporter
            spot['rl'][:6],                                                  # Rptr Grid
            f"{range_km}",                                                   # Range
            qsl_text,                                                        # QSL
        ]

        wsjt = src == 'wsjt'
        for col, val in enumerate(values):
            item = _AgeItem(val) if col == SPOT_AGE_COL else QTableWidgetItem(val)
            item.setBackground(bg)
            item.setForeground(fg)
            if wsjt:
                item.setFont(self._italic_font)
            if col == SPOT_AGE_COL:
                item.setData(Qt.ItemDataRole.UserRole, spot['unix_time'])
            elif col == CALL_COL:
                item.setData(Qt.ItemDataRole.UserRole, {'dxcc': dxcc, 'band': band, 'mode': mode})
            elif col == TIME_COL:
                item.setData(Qt.ItemDataRole.UserRole, ts)
            elif col == COUNTRY_COL:
                item.setData(Qt.ItemDataRole.UserRole, orig_country)
            self.table.setItem(row, col, item)

        # store spot-action dict on CALL_COL using _SPOT_ROLE (UserRole is
        # already used for the dxcc/band/mode dict set in the loop above)
        counter_item = self.table.item(row, CALL_COL)
        if counter_item is not None:
            counter_item.setData(_SPOT_ROLE, {
                'call':        spot['call'],
                'md':          mode,
                'b':           spot.get('b', ''),
                'freq_offset': spot.get('freq_offset', 0),
                'unix_time':   spot.get('unix_time', 0.0),
                'rp':          spot.get('rp', '0'),
                'msg':         spot.get('msg', ''),
                'delta_t':     spot.get('delta_t', 0.0),
                'loc':         spot.get('loc', ''),
                'source':      src,
            })

        # Re-apply bold if this call was selected before being re-inserted
        if spot['call'] == self._selected_call:
            self._set_row_font(row, bold=True)

        self.table.setRowHidden(row, self._row_is_hidden(row))

    # -- single-click: bold selection -----------------------------------------

    def _set_row_font(self, row: int, bold: bool) -> None:
        counter_item = self.table.item(row, CALL_COL)
        is_wsjt = False
        if counter_item is not None:
            data = counter_item.data(_SPOT_ROLE)
            if isinstance(data, dict):
                is_wsjt = data.get('source') == 'wsjt'
        if bold:
            font = self._bold_italic_font if is_wsjt else self._bold_font
        else:
            font = self._italic_font if is_wsjt else self._fixed_font
        for col in range(self.table.columnCount()):
            cell = self.table.item(row, col)
            if cell is not None:
                cell.setFont(font)

    def _on_cell_clicked(self, row: int, _col: int) -> None:
        call_item = self.table.item(row, CALL_COL)
        new_call = call_item.text() if call_item else ''
        if new_call == self._selected_call:
            return
        if self._selected_call:
            for r in range(self.table.rowCount()):
                item = self.table.item(r, CALL_COL)
                if item is not None and item.text() == self._selected_call:
                    self._set_row_font(r, bold=False)
                    break
        self._selected_call = new_call
        self._set_row_font(row, bold=True)

    # -- double-click handler -------------------------------------------------

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        counter_item = self.table.item(item.row(), CALL_COL)
        if counter_item is None:
            return
        spot_data = counter_item.data(_SPOT_ROLE)
        if isinstance(spot_data, dict):
            self.spot_activated.emit(spot_data)

    # -- restyle on criterion change ------------------------------------------

    def _restyle_all(self) -> None:
        adif = self._adif_log
        criterion = self._criterion
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        for row in range(self.table.rowCount()):
            call_item = self.table.item(row, CALL_COL)
            if call_item is None:
                continue
            data = call_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            dxcc = data.get('dxcc', -1)
            band = data.get('band', '')
            mode = data.get('mode', '')
            call = call_item.text()

            state = ''
            if criterion == 'was':
                conf_list, wkd_list = [], []
                if adif is None:
                    status = 'new'
                else:
                    state = adif.resolve_was_state(call, dxcc)
                    status = adif.was_status(state, band)
            elif adif is not None:
                if adif.mode_matches_criterion(mode, criterion):
                    status = _effective_status(adif.award_status(dxcc, band, criterion),
                                               criterion, band, adif)
                    conf_list, wkd_list = adif.criterion_qso_details(dxcc, band, criterion)
                else:
                    status, conf_list, wkd_list = 'n/a', [], []
            else:
                status, conf_list, wkd_list = 'new', [], []

            color_key = 'was_new' if criterion == 'was' and status == 'new' else status
            bg_hex, fg_hex = AWARD_COLORS.get(color_key, AWARD_COLORS['new'])
            bg = QColor(bg_hex)
            fg = QColor(fg_hex)

            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if item is not None:
                    item.setBackground(bg)
                    item.setForeground(fg)

            qsl_item = self.table.item(row, QSL_COL)
            if qsl_item is not None:
                if criterion == 'was':
                    qsl_item.setText(_was_qsl_label(status, state, band))
                else:
                    qsl_item.setText(_award_qsl_label(status, criterion, band, conf_list, wkd_list))

            # Update Country column: show "US <state>" for WAS, restore original otherwise.
            country_item = self.table.item(row, COUNTRY_COL)
            if country_item is not None:
                orig_country: str = country_item.data(Qt.ItemDataRole.UserRole) or country_item.text()
                if criterion == 'was' and dxcc == 291:
                    was_state = state if state else ('?' if '/' in call else '')
                    country_item.setText(f"US {was_state}" if was_state else "US")
                else:
                    country_item.setText(orig_country)

        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)

    # -- age update -----------------------------------------------------------

    def _update_ages(self) -> None:
        now = time.time()
        expired_rows: list[tuple[int, str]] = []  # (row, source)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, SPOT_AGE_COL)
            if item is None:
                continue
            unix_time = item.data(Qt.ItemDataRole.UserRole)
            if unix_time is None:
                continue
            age = now - unix_time
            if self._max_age_secs > 0 and age > self._max_age_secs:
                counter_item = self.table.item(row, CALL_COL)
                source = 'psk'
                if counter_item is not None:
                    d = counter_item.data(_SPOT_ROLE)
                    if isinstance(d, dict):
                        source = d.get('source', 'psk')
                expired_rows.append((row, source))
            else:
                item.setText(_format_age(int(age)))

        psk_removed = wsjt_removed = telnet1_removed = telnet2_removed = 0
        telnet3_removed = telnet4_removed = 0
        for row, source in reversed(expired_rows):
            self.table.removeRow(row)
            if source == 'wsjt':
                wsjt_removed += 1
            elif source == 'telnet1':
                telnet1_removed += 1
            elif source == 'telnet2':
                telnet2_removed += 1
            elif source == 'telnet3':
                telnet3_removed += 1
            elif source == 'telnet4':
                telnet4_removed += 1
            else:
                psk_removed += 1
        if (psk_removed or wsjt_removed or telnet1_removed or telnet2_removed
                or telnet3_removed or telnet4_removed):
            self.spots_expired.emit(
                psk_removed, wsjt_removed, telnet1_removed, telnet2_removed,
                telnet3_removed, telnet4_removed,
            )

    # -- context menu ---------------------------------------------------------

    @staticmethod
    def _band_sort_key(band: str) -> int:
        m = re.match(r'(\d+)', band)
        return -int(m.group(1)) if m else 0

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return

        col = item.column()

        # ── Reporter column: FCC licensee lookup ──────────────────────────────
        if col == REPORTER_COL:
            self._show_reporter_info(pos, item.row())
            return

        # ── QSL column: worked/confirmed QSO details ──────────────────────────
        if col != QSL_COL:
            return
        if self._adif_log is None:
            return

        call_item = self.table.item(item.row(), CALL_COL)
        if call_item is None:
            return
        row_data = call_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row_data, dict):
            return

        dxcc = row_data.get('dxcc', -1)
        band = row_data.get('band', '')
        call = call_item.text()
        criterion = self._criterion

        if criterion == 'was':
            was_state = self._adif_log.resolve_was_state(call, dxcc)
            if not was_state:
                return
            conf_list, wkd_list = self._adif_log.was_qso_details(was_state, band)
        else:
            was_state = ''
            conf_list, wkd_list = self._adif_log.criterion_qso_details(dxcc, band, criterion)

        if not conf_list and not wkd_list:
            return

        fixed_family = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: #1a1a2e;
                color: #ffffff;
                border: 1px solid #006080;
                font-family: {fixed_family};
                font-size: 12pt;
            }}
            QMenu::item {{ color: #ffffff; }}
            QMenu::item:disabled {{ color: #cccccc; }}
            QMenu::separator {{ background: #006080; height: 1px; margin: 4px 8px; }}
        """)

        def _add(s: str) -> None:
            a = menu.addAction(s)
            if a is not None:
                a.setEnabled(False)

        crit_label = _CRIT_ABBR.get(criterion, criterion.upper())
        header = f"Call: {call}  [{crit_label}]"
        if was_state:
            header += f"  State: {was_state}"
        _add(header)
        menu.addSeparator()

        def _entry_line(entry: dict[str, str]) -> str:
            grid_part = f"  {entry['grid']}" if entry.get('grid') else ''
            return f"  {entry['band'].lower()}/{entry['mode']}: {entry['call']}  {_fmt_date(entry['date'])}{grid_part}"

        if conf_list:
            _add("Confirmed:")
            sorted_conf = sorted(conf_list, key=lambda e: (
                self._band_sort_key(e['band']), self._MODE_SORT.get(e['mode'], 99)
            ))
            for entry in sorted_conf:
                _add(_entry_line(entry))

        if wkd_list and conf_list:
            menu.addSeparator()

        if wkd_list:
            _add("Worked (unconfirmed):")
            sorted_wkd = sorted(wkd_list, key=lambda e: (
                self._band_sort_key(e['band']), self._MODE_SORT.get(e['mode'], 99)
            ))
            for entry in sorted_wkd:
                _add(_entry_line(entry))

        menu.exec(self.table.viewport().mapToGlobal(pos))  # type: ignore[union-attr]

    def _show_reporter_info(self, pos, row: int) -> None:
        # Show FCC licensee details for the reporter callsign in a right-click popup.
        rc_item = self.table.item(row, REPORTER_COL)
        if rc_item is None:
            return
        reporter = rc_item.text().strip()
        if not reporter:
            return

        fixed_family = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: #1a1a2e;
                color: #ffffff;
                border: 1px solid #006080;
                font-family: {fixed_family};
                font-size: 11pt;
            }}
            QMenu::item {{ color: #ffffff; }}
            QMenu::item:disabled {{ color: #cccccc; }}
            QMenu::separator {{ background: #006080; height: 1px; margin: 4px 8px; }}
        """)

        def _add(s: str) -> None:
            a = menu.addAction(s)
            if a is not None:
                a.setEnabled(False)

        _add(f"Reporter: {reporter}")
        menu.addSeparator()

        if not fcc_db.fcc_db_path().exists():
            _add("FCC database not downloaded.")
            _add("Use Settings → Update FCC Database.")
        else:
            info = fcc_db.lookup_callsign_info(reporter)
            if info is None:
                _add("Not found in FCC database.")
            else:
                _add(f"Name:    {info['name']}")
                _add(f"Class:   {info['license_class']}")
                _add(f"City:    {info['city']}, {info['state']}")

        menu.exec(self.table.viewport().mapToGlobal(pos))  # type: ignore[union-attr]
