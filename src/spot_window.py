"""Spot table widget and supporting helpers for DX Spotter.

The central UI component is :class:`SpotTable`, a ``QWidget`` wrapping a
``QTableWidget`` that displays incoming DX spots from PSK Reporter and WSJT-X,
colours rows by DXCC award status, and provides right-click QSO-detail context
menus.

Module-level constants
----------------------
COLUMNS : list[str]
    Ordered list of column header strings for the spot table.
AGE_COL, QSL_COL, CALL_COL : int
    Pre-computed column indices for the Age, QSL, and DX Call columns.
AWARD_COLORS : dict[str, tuple[str, str]]
    Maps award status → ``(background_hex, foreground_hex)`` colour pairs.
_US_CANADA_DXCC : frozenset[int]
    ADIF DXCC entity numbers for mainland US (291) and Canada (1), excluded
    by the ``'dxcc_only'`` display filter.
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
from PyQt6.QtGui import QColor, QFontDatabase, QIcon

if TYPE_CHECKING:
    from adif_log import ADIFLog


COLUMNS = ["DX Call", "Dir", "Country", "DX Grid", "Time", "Age",
           "dHz", "Dist", "Mode", "SNR", "Band", "Src", "Reporter", "Rptr Grid",
           "Range", "QSL"]

# DXCC entity numbers for mainland US and Canada (excluded by 'dxcc_only' filter)
_US_CANADA_DXCC: frozenset[int] = frozenset({1, 291})

AGE_COL = COLUMNS.index("Age")
QSL_COL = COLUMNS.index("QSL")
CALL_COL = COLUMNS.index("DX Call")
# Separate role for the spot-action dict on CALL_COL (avoids collision with
# the dxcc/band/mode dict stored in UserRole on the same cell).
_SPOT_ROLE = Qt.ItemDataRole.UserRole + 1

# 3-state award colour scheme
# confirmed = grey (already in the log for this award)
# worked    = orange (QSO in log, need confirmation)
# new       = red (never worked, needed for award)
AWARD_COLORS: dict[str, tuple[str, str]] = {
    'confirmed': ("#505050", "#d0d0d0"),
    'worked':    ("#b85000", "#ffffff"),
    'new':       ("#8b0000", "#ffffff"),
    'n/a':       ("#505050", "#808080"),  # same bg as confirmed, dimmer text
    'over100':   ("#004040", "#00e0e0"),  # 5BD-only: band already ≥100 confirmed
}

_CRIT_ABBR: dict[str, str] = {
    '5bd':     '5BD',
    'cw':      'CW',
    'mixed':   'Mix',
    'digital': 'Dig',
    'ssb':     'SSB',
    '6m':      '6M',
}


def make_app_icon() -> QIcon:
    """Load and return the DX Spotter application icon.

    Returns
    -------
    QIcon
        Icon loaded from ``src/icons/dxspot.png`` relative to this module.
    """
    path = os.path.join(os.path.dirname(__file__), "icons", "dxspot.png")
    return QIcon(path)


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

    Each row is coloured according to the active DXCC award criterion and the
    spot's mode, using the :data:`AWARD_COLORS` palette.  When the criterion
    or ADIF log changes, :meth:`set_criterion` and :meth:`set_adif_log` trigger
    a full re-style pass.

    Signals
    -------
    spot_activated : pyqtSignal(dict)
        Emitted when the user double-clicks a spot row.  The dict contains
        the spot-action fields stored in ``_SPOT_ROLE`` on the DX Call cell.
    spots_expired : pyqtSignal(int, int)
        Emitted after the age-expiry pass with ``(psk_removed, wsjt_removed)``
        counts so :class:`~dxspotter.DXSpotter` can decrement its counters.
    """

    spot_activated = pyqtSignal(dict)   # double-click on a row → DXSpotter
    spots_expired  = pyqtSignal(int, int)  # (psk_removed, wsjt_removed) when rows age out

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
        batch_timer.start(250)

        self._adif_log: ADIFLog | None = None
        self._criterion: str = 'mixed'
        self._display_filter: str = 'all'
        self._dimmed_calls: set[str] = set()
        self._selected_call: str = ''
        self._max_age_secs: int = 30 * 60  # 0 = no expiry

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
                if adif.mode_matches_criterion(mode, criterion):
                    status = _effective_status(adif.award_status(dxcc, band, criterion),
                                               criterion, band, adif)
                else:
                    status = 'n/a'
            else:
                status = 'new'
            bg_hex, fg_hex = AWARD_COLORS.get(status, AWARD_COLORS['new'])
            bg = QColor(bg_hex)
            fg = QColor(fg_hex)
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is not None:
                    cell.setBackground(bg)
                    cell.setForeground(fg)

    def set_adif_log(self, adif_log: 'ADIFLog | None') -> None:
        """Replace the contact log used for award-status colouring.

        Parameters
        ----------
        adif_log : ADIFLog or None
            New log instance, or ``None`` to clear (all spots coloured as
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
        if self._display_filter == 'dxcc_only':
            return dxcc in _US_CANADA_DXCC
        if self._display_filter == 'unconfirmed':
            adif = self._adif_log
            if adif is None:
                return False
            if adif.mode_matches_criterion(mode, self._criterion):
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
        deduped: dict[str, dict] = {}
        for spot in spots:
            deduped[spot['call']] = spot
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        for spot in deduped.values():
            self._insert_spot(spot)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.table.scrollToTop()

    def _insert_spot(self, spot: dict) -> None:
        dxcc = spot.get('dxcc', -1)
        band = spot.get('b', '')
        mode = spot.get('md', '')

        adif = self._adif_log
        criterion = self._criterion
        if adif is not None:
            if adif.mode_matches_criterion(mode, criterion):
                status = _effective_status(adif.award_status(dxcc, band, criterion),
                                           criterion, band, adif)
                conf_list, wkd_list = adif.criterion_qso_details(dxcc, band, criterion)
            else:
                status, conf_list, wkd_list = 'n/a', [], []
        else:
            status, conf_list, wkd_list = 'new', [], []

        bg_hex, fg_hex = AWARD_COLORS.get(status, AWARD_COLORS['new'])
        bg = QColor(bg_hex)
        fg = QColor(fg_hex)

        # Remove existing row for this call so it reappears at the top
        for r in range(self.table.rowCount() - 1, -1, -1):
            existing = self.table.item(r, CALL_COL)
            if existing is not None and existing.text() == spot['call']:
                self.table.removeRow(r)

        self.table.insertRow(0)
        row = 0

        range_km = spot.get('range', 0)
        qsl_text = _award_qsl_label(status, criterion, band, conf_list, wkd_list)

        values = [
            spot['call'],                        # DX Call
            spot['direction'],                   # Dir
            spot['country'],                     # Country
            spot['loc'][:6],                     # DX Grid — max 6 chars
            spot['timestamp'],                   # Time
            _format_age(int(time.time() - spot['unix_time'])),  # Age
            str(spot['freq_offset']),            # dHz
            str(spot['distance']),               # Dist
            spot['md'],                          # Mode
            f"{spot['rp']} dB",                  # SNR
            spot['b'],                           # Band
            spot.get('source', 'psk'),           # Src
            spot['rc'],                          # Reporter
            spot['rl'][:6],                      # Rptr Grid — max 6 chars
            f"{range_km}",                       # Range
            qsl_text,                            # QSL
        ]

        wsjt = spot.get('source') == 'wsjt'
        for col, val in enumerate(values):
            item = _AgeItem(val) if col == AGE_COL else QTableWidgetItem(val)
            item.setBackground(bg)
            item.setForeground(fg)
            if wsjt:
                item.setFont(self._italic_font)
            if col == AGE_COL:
                item.setData(Qt.ItemDataRole.UserRole, spot['unix_time'])
            elif col == CALL_COL:
                item.setData(Qt.ItemDataRole.UserRole, {'dxcc': dxcc, 'band': band, 'mode': mode})
            self.table.setItem(row, col, item)

        # store spot-action dict on CALL_COL using _SPOT_ROLE (UserRole is
        # already used for the dxcc/band/mode dict set in the loop above)
        counter_item = self.table.item(row, CALL_COL)
        if counter_item is not None:
            counter_item.setData(_SPOT_ROLE, {
                'call':        spot['call'],
                'md':          mode,
                'freq_offset': spot.get('freq_offset', 0),
                'unix_time':   spot.get('unix_time', 0.0),
                'rp':          spot.get('rp', '0'),
                'msg':         spot.get('msg', ''),
                'delta_t':     spot.get('delta_t', 0.0),
                'loc':         spot.get('loc', ''),
                'source':      spot.get('source', 'psk'),
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

            if adif is not None:
                if adif.mode_matches_criterion(mode, criterion):
                    status = _effective_status(adif.award_status(dxcc, band, criterion),
                                               criterion, band, adif)
                    conf_list, wkd_list = adif.criterion_qso_details(dxcc, band, criterion)
                else:
                    status, conf_list, wkd_list = 'n/a', [], []
            else:
                status, conf_list, wkd_list = 'new', [], []

            bg_hex, fg_hex = AWARD_COLORS.get(status, AWARD_COLORS['new'])
            bg = QColor(bg_hex)
            fg = QColor(fg_hex)

            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if item is not None:
                    item.setBackground(bg)
                    item.setForeground(fg)

            qsl_item = self.table.item(row, QSL_COL)
            if qsl_item is not None:
                qsl_item.setText(_award_qsl_label(status, criterion, band, conf_list, wkd_list))

        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)

    # -- age update -----------------------------------------------------------

    def _update_ages(self) -> None:
        now = time.time()
        expired_rows: list[tuple[int, str]] = []  # (row, source)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, AGE_COL)
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

        psk_removed = wsjt_removed = 0
        for row, source in reversed(expired_rows):
            self.table.removeRow(row)
            if source == 'wsjt':
                wsjt_removed += 1
            else:
                psk_removed += 1
        if psk_removed or wsjt_removed:
            self.spots_expired.emit(psk_removed, wsjt_removed)

    # -- context menu ---------------------------------------------------------

    @staticmethod
    def _band_sort_key(band: str) -> int:
        m = re.match(r'(\d+)', band)
        return -int(m.group(1)) if m else 0

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None or item.column() != QSL_COL:
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

        conf_list, wkd_list = self._adif_log.criterion_qso_details(dxcc, band, self._criterion)
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

        crit_label = _CRIT_ABBR.get(self._criterion, self._criterion.upper())
        _add(f"Call: {call}  [{crit_label}]")
        menu.addSeparator()

        if conf_list:
            _add("Confirmed:")
            sorted_conf = sorted(conf_list, key=lambda e: (
                self._band_sort_key(e['band']), self._MODE_SORT.get(e['mode'], 99)
            ))
            for entry in sorted_conf:
                _add(f"  {entry['band'].lower()}/{entry['mode']}: {entry['call']}  {_fmt_date(entry['date'])}")

        if wkd_list and conf_list:
            menu.addSeparator()

        if wkd_list:
            _add("Worked (unconfirmed):")
            sorted_wkd = sorted(wkd_list, key=lambda e: (
                self._band_sort_key(e['band']), self._MODE_SORT.get(e['mode'], 99)
            ))
            for entry in sorted_wkd:
                _add(f"  {entry['band'].lower()}/{entry['mode']}: {entry['call']}  {_fmt_date(entry['date'])}")

        menu.exec(self.table.viewport().mapToGlobal(pos))  # type: ignore[union-attr]
