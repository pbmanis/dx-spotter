import argparse

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QApplication,
    QGroupBox, QFormLayout, QLabel, QRadioButton, QButtonGroup,
)
from PyQt6.QtCore import pyqtSignal

from pyqtgraph.dockarea import DockArea, Dock
from pyqtgraph.parametertree import Parameter, ParameterTree

from spot_window import SpotTable, make_app_icon  # re-export make_app_icon


_BANDS = ['All', '160m', '80m', '60m', '40m', '30m', '20m',
          '17m', '15m', '12m', '10m', '6m', '2m']
_MODES = ['FT8', 'FT4', 'FT2', 'CW', 'SSB', 'FC', 'FCS', 'CS']
_WSJT_FILTERS = ['CQ', 'all']

# Award criteria: internal key → display label (controls QSL column coloring only)
_CRITERIA: list[tuple[str, str]] = [
    ('5bd',     '5 Band DXCC  (80/40/20/15/10m)'),
    ('cw',      'DXCC CW'),
    ('mixed',   'DXCC Mixed'),
    ('digital', 'DXCC Digital'),
    ('ssb',     'DXCC SSB'),
    ('6m',      'DXCC 6M'),
]
_DEFAULT_CRITERION = 'mixed'

# Display filter: controls which rows are visible in the spot table
_DISPLAY_FILTERS: list[tuple[str, str]] = [
    ('all',         'All'),
    ('dxcc_only',   'DXCC only  (no US, Canada)'),
    ('unconfirmed', 'Unconfirmed only'),
]
_DEFAULT_DISPLAY_FILTER = 'all'


class MainWindow(QMainWindow):
    """Main application window: ParameterTree settings dock (left) + spot table dock (right)."""

    new_spot          = pyqtSignal(dict)   # MQTT/WSJT-X thread → table (thread-safe)
    call_busy         = pyqtSignal(str)    # WSJT-X thread → dim call in table
    call_active       = pyqtSignal(str)    # WSJT-X thread → undim call in table
    restart_requested = pyqtSignal()       # Restart button → DXSpotter
    settings_changed  = pyqtSignal(dict)  # any param change → DXSpotter
    criterion_changed = pyqtSignal(str)   # award criteria radio button → DXSpotter
    spot_activated    = pyqtSignal(dict)  # double-click on spot row → DXSpotter
    settings_requested = pyqtSignal()     # Settings button → DXSpotter

    def __init__(self, initial_args: argparse.Namespace, initial_adif_path: str,
                 initial_criterion: str = 'mixed',
                 initial_display_filter: str = 'all'):
        super().__init__()
        self.setWindowTitle("DX Spotter")
        self.resize(1400, 700)

        area = DockArea()
        self.setCentralWidget(area)

        # ── Status bar ────────────────────────────────────────────────────────
        self._sb_log  = QLabel("No log loaded")
        self._sb_wsjt = QLabel("WSJT-X: —")
        self._sb_log.setStyleSheet("padding: 0 6px;")
        self._sb_wsjt.setStyleSheet("padding: 0 6px;")
        self.statusBar().addWidget(self._sb_log, 1)       # left, stretches
        self.statusBar().addPermanentWidget(self._sb_wsjt) # right, fixed

        left_dock  = Dock("Settings", size=(280, 700))
        right_dock = Dock("Spots",    size=(1120, 700))
        area.addDock(left_dock,  'left')
        area.addDock(right_dock, 'right', relativeTo=left_dock)

        # ── Parameter tree ────────────────────────────────────────────────────
        self._params = self._build_params(initial_args, initial_adif_path)

        pt = ParameterTree(showHeader=False)
        pt.setParameters(self._params, showTop=False)
        self._clear_action_labels(pt)
        self._params.sigTreeStateChanged.connect(self._on_params_changed)

        # ── Award criteria radio buttons ──────────────────────────────────────
        criteria_box = QGroupBox("Award Criteria")
        crit_layout  = QVBoxLayout(criteria_box)
        crit_layout.setContentsMargins(6, 4, 6, 4)
        crit_layout.setSpacing(2)

        self._crit_group = QButtonGroup(self)
        for key, label in _CRITERIA:
            rb = QRadioButton(label)
            if key == initial_criterion:
                rb.setChecked(True)
            self._crit_group.addButton(rb)
            rb.setProperty('criterion', key)
            crit_layout.addWidget(rb)

        self._crit_group.buttonClicked.connect(self._on_criterion_clicked)

        # ── Display filter radio buttons ──────────────────────────────────────
        display_filter_box = QGroupBox("Display Filter")
        df_layout = QVBoxLayout(display_filter_box)
        df_layout.setContentsMargins(6, 4, 6, 4)
        df_layout.setSpacing(2)

        self._display_filter_group = QButtonGroup(self)
        for key, label in _DISPLAY_FILTERS:
            rb = QRadioButton(label)
            if key == initial_display_filter:
                rb.setChecked(True)
            self._display_filter_group.addButton(rb)
            rb.setProperty('display_filter', key)
            df_layout.addWidget(rb)

        self._display_filter_group.buttonClicked.connect(self._on_display_filter_clicked)

        # ── Reports panel ────────────────────────────────────────────────────
        reports_box = QGroupBox("Reports")
        rpt_layout = QFormLayout(reports_box)
        rpt_layout.setContentsMargins(4, 4, 4, 4)
        rpt_layout.setSpacing(2)
        self._lbl_psk   = QLabel("0")
        self._lbl_wsjt  = QLabel("0")
        self._lbl_total = QLabel("0")
        rpt_layout.addRow("PSK Reporter:", self._lbl_psk)
        rpt_layout.addRow("WSJT-X:",       self._lbl_wsjt)
        rpt_layout.addRow("Total:",         self._lbl_total)

        # ── Restart / Settings / Quit buttons ────────────────────────────────
        btn_restart  = QPushButton("Restart")
        btn_settings = QPushButton("Settings")
        btn_quit     = QPushButton("Quit")
        btn_restart.clicked.connect(lambda: self.restart_requested.emit())
        btn_settings.clicked.connect(lambda: self.settings_requested.emit())
        btn_quit.clicked.connect(lambda: QApplication.instance().quit())  # type: ignore[union-attr]

        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_restart)
        btn_row.addWidget(btn_settings)
        btn_row.addWidget(btn_quit)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(4)
        left_layout.addWidget(pt)
        left_layout.addWidget(criteria_box)
        left_layout.addWidget(display_filter_box)
        left_layout.addWidget(reports_box)
        left_layout.addLayout(btn_row)
        left_dock.addWidget(left_widget)

        # ── Spot table ────────────────────────────────────────────────────────
        self._spot_table = SpotTable()
        right_dock.addWidget(self._spot_table)
        self.new_spot.connect(self._spot_table.add_spot)
        self.call_busy.connect(self._spot_table.dim_call)
        self.call_active.connect(self._spot_table.undim_call)
        self._spot_table.spot_activated.connect(self.spot_activated)

    # -- parameter tree -------------------------------------------------------

    @staticmethod
    def _clear_action_labels(pt: ParameterTree) -> None:
        def _visit(item) -> None:
            if getattr(getattr(item, 'param', None), 'type', lambda: None)() == 'action':
                item.setText(0, '')
            for i in range(item.childCount()):
                _visit(item.child(i))
        _visit(pt.invisibleRootItem())

    @staticmethod
    def _build_params(args: argparse.Namespace, adif_path: str) -> Parameter:
        return Parameter.create(name='root', type='group', children=[
            dict(name='Connection', type='group', children=[
                dict(name='Call Sign', type='str',
                     value=args.call or ''),
                dict(name='Band',      type='list', limits=_BANDS,
                     value=args.band or '10m'),
                dict(name='Mode',      type='list', limits=_MODES,
                     value=args.mode or 'FC'),
                dict(name='Decode Filter', type='list', limits=_WSJT_FILTERS,
                     value=getattr(args, 'wsjt_filter', 'CQ'),
                     tip='WSJT-X: CQ=CQ calls only, all=all decodes, me=calls to my call'),
            ]),
            dict(name='Filtering', type='group', children=[
                dict(name='Max Range (km)', type='int',
                     value=args.range or 0, min=0,
                     tip='0 = no range limit'),
            ]),
            dict(name='ADIF Log', type='group', children=[
                dict(name='File', type='file', value=adif_path,
                     fileMode='ExistingFile',
                     nameFilter='ADIF Files (*.adif *.adi);;All Files (*)'),
            ]),
            dict(name='Display', type='group', children=[
                dict(name='Terminal Output', type='bool', value=args.terminal),
            ]),
        ])

    def _collect_settings(self) -> dict:
        p = self._params
        band_str = p.child('Connection').child('Band').value()
        range_km = p.child('Filtering').child('Max Range (km)').value()
        return {
            'call':        p.child('Connection').child('Call Sign').value().strip() or None,
            'band':        None if band_str == 'All' else band_str,
            'mode':        p.child('Connection').child('Mode').value(),
            'range':       range_km if range_km > 0 else None,
            'terminal':    p.child('Display').child('Terminal Output').value(),
            'wsjt_filter': p.child('Connection').child('Decode Filter').value(),
            'adif_path':   p.child('ADIF Log').child('File').value().strip(),
        }

    def _on_params_changed(self, _root, changes) -> None:
        for _param, change, _data in changes:
            if change == 'value':
                self.settings_changed.emit(self._collect_settings())
                return

    def _on_criterion_clicked(self, button: QRadioButton) -> None:
        key = button.property('criterion')
        if key:
            self.criterion_changed.emit(key)

    def _on_display_filter_clicked(self, button: QRadioButton) -> None:
        key = button.property('display_filter')
        if key:
            self._spot_table.set_display_filter(key)

    # -- public interface -----------------------------------------------------

    def update_counts(self, psk_count: int, wsjt_count: int) -> None:
        self._lbl_psk.setText(str(psk_count))
        self._lbl_wsjt.setText(str(wsjt_count))
        self._lbl_total.setText(str(psk_count + wsjt_count))

    def clear_table(self) -> None:
        self._spot_table.clear()

    def restyle_spots(self, adif_log, criterion: str) -> None:
        self._spot_table.set_adif_log(adif_log)
        self._spot_table.set_criterion(criterion)

    def get_criterion(self) -> str:
        for btn in self._crit_group.buttons():
            if btn.isChecked():
                return btn.property('criterion')
        return _DEFAULT_CRITERION

    def get_display_filter(self) -> str:
        for btn in self._display_filter_group.buttons():
            if btn.isChecked():
                return btn.property('display_filter')
        return _DEFAULT_DISPLAY_FILTER

    def set_adif_path(self, path: str) -> None:
        """Sync the ADIF File field in the parameter tree (called after Settings dialog)."""
        self._params.child('ADIF Log').child('File').setValue(path)

    def set_log_info(self, text: str) -> None:
        self._sb_log.setText(text)

    def set_wsjt_status(self, text: str, ok: bool | None = None) -> None:
        """Update the WSJT-X status indicator in the status bar.

        ok=True  → green text   (connected / heartbeat)
        ok=False → orange text  (no signal / lost)
        ok=None  → default colour (disabled / unknown)
        """
        if ok is True:
            colour = "#55cc55"
        elif ok is False:
            colour = "#cc8800"
        else:
            colour = "#888888"
        self._sb_wsjt.setStyleSheet(f"padding: 0 6px; color: {colour};")
        self._sb_wsjt.setText(text)
