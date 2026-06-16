import os
import re
import time

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QMenu, QTableWidget, QTableWidgetItem, QAbstractItemView,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QFontDatabase, QIcon


COLUMNS = ["#", "Dir", "Time", "DX Call", "DX Grid", "SNR", "Country",
           "Offset Hz", "Dist km", "Mode", "Band", "Reporter", "Rptr Grid", "Range", "Age", "QSL"]

AGE_COL  = COLUMNS.index("Age")
QSL_COL  = COLUMNS.index("QSL")
CALL_COL = COLUMNS.index("DX Call")

STATUS_LABELS: dict[str, str] = {
    'confirmed':       'Conf',
    'confirmed_other': 'Other',
    'worked':          'Worked',
    'new':             'New',
}

STATUS_COLORS: dict[str, tuple[str, str]] = {
    'confirmed_other': ("#807000", "#ffffff"),
    'worked':          ("#1a4f72", "#ffffff"),
    'new':             ("#7d3c00", "#ffffff"),
}

MODE_COLORS: dict[str, tuple[str, str]] = {
    "CW":  ("#006400", "#ffffff"),
    "FT8": ("#006080", "#ffffff"),
    "FT4": ("#006080", "#ffffff"),
    "FT2": ("#006080", "#ffffff"),
    "SSB": ("#600060", "#ffffff"),
}
DEFAULT_MODE_COLOR: tuple[str, str] = ("#806000", "#000000")


def make_app_icon() -> QIcon:
    path = os.path.join(os.path.dirname(__file__), "icons", "pskspot.png")
    return QIcon(path)


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"


class SpotTable(QWidget):
    """Scrollable spot table — designed to be embedded in a DockArea dock."""

    _MODE_SORT: dict[str, int] = {"CW": 0, "SSB": 1, "FT4": 2, "FT8": 3, "FT2": 4}

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)  # type: ignore[union-attr]
        self.table.horizontalHeader().setStretchLastSection(True)  # type: ignore[union-attr]

        fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed_font.setPointSize(12)
        self.table.setFont(fixed_font)

        italic_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        italic_font.setPointSize(12)
        italic_font.setItalic(True)
        self._italic_font = italic_font

        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.table)

        age_timer = QTimer(self)
        age_timer.timeout.connect(self._update_ages)
        age_timer.start(15_000)

        self._pending_spots: list[dict] = []
        batch_timer = QTimer(self)
        batch_timer.timeout.connect(self._flush_pending)
        batch_timer.start(250)  # drain queue 4× per second

    # -- public interface -----------------------------------------------------

    def add_spot(self, spot: dict) -> None:
        self._pending_spots.append(spot)

    def clear(self) -> None:
        self._pending_spots.clear()
        self.table.setRowCount(0)

    # -- private --------------------------------------------------------------

    def _flush_pending(self) -> None:
        if not self._pending_spots:
            return
        spots, self._pending_spots = self._pending_spots, []
        # Coalesce: keep only the latest spot per call within this batch
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
        self.table.scrollToBottom()

    def _insert_spot(self, spot: dict) -> None:
        status = spot.get("adif_status", "confirmed")
        if status == 'confirmed':
            bg_hex, fg_hex = MODE_COLORS.get(spot["md"], DEFAULT_MODE_COLOR)
        else:
            bg_hex, fg_hex = STATUS_COLORS.get(status, DEFAULT_MODE_COLOR)

        bg = QColor(bg_hex)
        fg = QColor(fg_hex)

        # Remove any existing row for this call so it reappears at the bottom
        for r in range(self.table.rowCount() - 1, -1, -1):
            existing = self.table.item(r, CALL_COL)
            if existing is not None and existing.text() == spot["call"]:
                self.table.removeRow(r)

        row = self.table.rowCount()
        self.table.insertRow(row)

        range_km = spot["range"]
        values = [
            f"{spot['counter']:06d}",
            spot["direction"],
            spot["timestamp"],
            spot["call"],
            spot["loc"],
            f"{spot['rp']} dB",
            spot["country"],
            f"{spot['freq_offset']} Hz",
            f"{spot['distance']} km",
            spot["md"],
            spot["b"],
            spot["rc"],
            spot["rl"],
            f"{range_km} km ({range_km * 0.621371:.1f} mi)",
            _format_age(int(time.time() - spot["unix_time"])),
            self._qsl_label(status,
                            spot.get("confirmed_pairs", {}),
                            spot.get("confirmed_details", []),
                            spot["b"], spot["md"]),
        ]

        qsl_userdata = {
            'status':            status,
            'confirmed_pairs':   spot.get("confirmed_pairs", {}),
            'confirmed_details': spot.get("confirmed_details", []),
            'band':              spot["b"],
            'mode':              spot["md"],
        }

        wsjt = spot.get("source") == "wsjt"
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            item.setBackground(bg)
            item.setForeground(fg)
            if wsjt:
                item.setFont(self._italic_font)
            if col == AGE_COL:
                item.setData(Qt.ItemDataRole.UserRole, spot["unix_time"])
            elif col == QSL_COL:
                item.setData(Qt.ItemDataRole.UserRole, qsl_userdata)
            self.table.setItem(row, col, item)

    # -- private --------------------------------------------------------------

    def _update_ages(self) -> None:
        now = time.time()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, AGE_COL)
            if item is None:
                continue
            unix_time = item.data(Qt.ItemDataRole.UserRole)
            if unix_time is not None:
                item.setText(_format_age(int(now - unix_time)))

    @staticmethod
    def _band_sort_key(band: str) -> int:
        m = re.match(r'(\d+)', band)
        return -int(m.group(1)) if m else 0

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None or item.column() != QSL_COL:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        status = data.get('status', '')
        if status not in ('confirmed', 'confirmed_other'):
            return

        call_item = self.table.item(item.row(), CALL_COL)
        call = call_item.text() if call_item is not None else "?"

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

        _add(f"Call: {call}")
        menu.addSeparator()

        if status == 'confirmed':
            band = data.get('band', '')
            mode = data.get('mode', '')
            _add(f"Confirmed on {band.lower()}/{mode}:")
            for entry in data.get('confirmed_details', []):
                ecall = entry.get('call', '?')
                date = self._fmt_date(entry.get('date', ''))
                _add(f"  {ecall}  {date}")
        else:
            _add("Other – confirmed on:")
            sorted_pairs = sorted(
                data.get('confirmed_pairs', {}).items(),
                key=lambda kv: (self._band_sort_key(kv[0][0]),
                                self._MODE_SORT.get(kv[0][1], 99))
            )
            for (band, mode), calls in sorted_pairs:
                _add(f"  {band.lower()}/{mode}: {' '.join(calls)}")

        menu.exec(self.table.viewport().mapToGlobal(pos))  # type: ignore[union-attr]

    @staticmethod
    def _fmt_date(d: str) -> str:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d

    @staticmethod
    def _qsl_label(status: str,
                   confirmed_pairs: dict[tuple[str, str], list[str]],
                   confirmed_details: list[dict[str, str]],
                   band: str, mode: str) -> str:
        base = STATUS_LABELS.get(status, status)
        if status == 'confirmed' and confirmed_details:
            first = confirmed_details[0]
            call = first.get('call', '?')
            date = SpotTable._fmt_date(first.get('date', ''))
            n = len(confirmed_details)
            count = f"({n})" if n > 1 else ""
            return f"{base}{count}: {call} {band.lower()}/{mode} {date}"
        if status == 'confirmed_other' and confirmed_pairs:
            parts = [
                f"{b.lower()}/{m}: {' '.join(calls)}"
                for (b, m), calls in confirmed_pairs.items()
            ]
            return f"{base} ({', '.join(parts)})"
        return base
