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


COLUMNS = ["#", "Src", "Dir", "Time", "DX Call", "DX Grid", "SNR", "Country",
           "dHz", "Dist", "Mode", "Band", "Reporter", "Rptr Grid", "Range", "Age", "QSL"]

# DXCC entity numbers for mainland US and Canada (excluded by 'dxcc_only' filter)
_US_CANADA_DXCC: frozenset[int] = frozenset({1, 291})

AGE_COL  = COLUMNS.index("Age")
QSL_COL  = COLUMNS.index("QSL")
CALL_COL = COLUMNS.index("DX Call")

# 3-state award colour scheme
# confirmed = grey (already in the log for this award)
# worked    = orange (QSO in log, need confirmation)
# new       = red (never worked, needed for award)
AWARD_COLORS: dict[str, tuple[str, str]] = {
    'confirmed': ("#505050", "#d0d0d0"),
    'worked':    ("#b85000", "#ffffff"),
    'new':       ("#8b0000", "#ffffff"),
    'n/a':       ("#505050", "#808080"),  # same bg as confirmed, dimmer text
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
    return f"New [{abbr}]"


class _AgeItem(QTableWidgetItem):
    """Age column item: displays human-readable age, sorts by unix_time (UserRole)."""
    def __lt__(self, other: QTableWidgetItem) -> bool:
        my_ts    = self.data(Qt.ItemDataRole.UserRole)
        other_ts = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(my_ts, (int, float)) and isinstance(other_ts, (int, float)):
            return my_ts > other_ts   # larger timestamp = more recent = smaller age
        return super().__lt__(other)


class SpotTable(QWidget):
    """Scrollable spot table — designed to be embedded in a DockArea dock."""

    spot_activated = pyqtSignal(dict)  # double-click on a row → DXSpotter

    _MODE_SORT: dict[str, int] = {"CW": 0, "SSB": 1, "FT4": 2, "FT8": 3, "FT2": 4}

    def __init__(self, parent=None):
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

    # -- public interface -----------------------------------------------------

    def add_spot(self, spot: dict) -> None:
        self._pending_spots.append(spot)

    def clear(self) -> None:
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
        adif      = self._adif_log
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
                    status = adif.award_status(dxcc, band, criterion)
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

    def set_adif_log(self, adif_log: ADIFLog | None) -> None:
        self._adif_log = adif_log

    def set_criterion(self, criterion: str) -> None:
        self._criterion = criterion
        self._restyle_all()
        self._apply_display_filter()  # 'unconfirmed' depends on criterion

    def set_display_filter(self, filter_str: str) -> None:
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
        spots, self._pending_spots = self._pending_spots, []
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

        adif      = self._adif_log
        criterion = self._criterion
        if adif is not None:
            if adif.mode_matches_criterion(mode, criterion):
                status = adif.award_status(dxcc, band, criterion)
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
            f"{spot['counter']:06d}",
            spot.get('source', 'psk'),           # Src
            spot['direction'],
            spot['timestamp'],
            spot['call'],
            spot['loc'][:6],                     # DX Grid — max 6 chars
            f"{spot['rp']} dB",
            spot['country'],
            str(spot['freq_offset']),            # Offset Hz — unit in header
            str(spot['distance']),               # Dist km — unit in header
            spot['md'],
            spot['b'],
            spot['rc'],
            spot['rl'][:6],                      # Rptr Grid — max 6 chars
            f"{range_km}", #  km ({range_km * 0.621371:.1f} mi)",
            _format_age(int(time.time() - spot['unix_time'])),
            qsl_text,
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

        # store fields needed by the double-click handler on column 0
        counter_item = self.table.item(row, 0)
        if counter_item is not None:
            counter_item.setData(Qt.ItemDataRole.UserRole, {
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
        counter_item = self.table.item(row, 0)
        is_wsjt = False
        if counter_item is not None:
            data = counter_item.data(Qt.ItemDataRole.UserRole)
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
        counter_item = self.table.item(item.row(), 0)
        if counter_item is None:
            return
        spot_data = counter_item.data(Qt.ItemDataRole.UserRole)
        if isinstance(spot_data, dict):
            self.spot_activated.emit(spot_data)

    # -- restyle on criterion change ------------------------------------------

    def _restyle_all(self) -> None:
        adif      = self._adif_log
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
                    status = adif.award_status(dxcc, band, criterion)
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
        for row in range(self.table.rowCount()):
            item = self.table.item(row, AGE_COL)
            if item is None:
                continue
            unix_time = item.data(Qt.ItemDataRole.UserRole)
            if unix_time is not None:
                item.setText(_format_age(int(now - unix_time)))

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
