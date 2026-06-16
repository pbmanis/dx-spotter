import argparse

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QApplication,
    QGroupBox, QFormLayout, QLabel,
)
from PyQt6.QtCore import pyqtSignal

from pyqtgraph.dockarea import DockArea, Dock
from pyqtgraph.parametertree import Parameter, ParameterTree

from spot_window import SpotTable, make_app_icon  # re-export make_app_icon


_BANDS = ['All', '160m', '80m', '60m', '40m', '30m', '20m',
          '17m', '15m', '12m', '10m', '6m', '2m']
_MODES = ['FT8', 'FT4', 'FT2', 'CW', 'SSB', 'FC', 'FCS', 'CS']
_WSJT_FILTERS = ['CQ', 'all', 'me']
_SHOW_OPTS = ['All spots', 'New on band', 'New anywhere']


class MainWindow(QMainWindow):
    """Main application window: ParameterTree settings dock (left) + spot table dock (right)."""

    new_spot          = pyqtSignal(dict)  # MQTT/WSJT-X thread → table (thread-safe)
    restart_requested = pyqtSignal()       # Restart button → PSKSpotter
    settings_changed  = pyqtSignal(dict)  # any param change → PSKSpotter

    def __init__(self, initial_args: argparse.Namespace, initial_adif_path: str):
        super().__init__()
        self.setWindowTitle("PSK Spotter")
        self.resize(1400, 700)

        area = DockArea()
        self.setCentralWidget(area)

        # Left dock ~20%, right dock ~80%
        left_dock  = Dock("Settings", size=(280, 700))
        right_dock = Dock("Spots",    size=(1120, 700))
        area.addDock(left_dock,  'left')
        area.addDock(right_dock, 'right', relativeTo=left_dock)

        # ── Parameter tree ────────────────────────────────────────────────────
        self._params = self._build_params(initial_args, initial_adif_path)

        pt = ParameterTree(showHeader=False)
        pt.setParameters(self._params, showTop=False)

        # Action parameters show their name in column 0 AND as button text in
        # column 1.  Walk the live tree items and blank the column-0 label on
        # every action so only the button remains visible.
        self._clear_action_labels(pt)

        self._params.sigTreeStateChanged.connect(self._on_params_changed)

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

        # ── Restart / Quit buttons below the tree ─────────────────────────────
        btn_restart = QPushButton("Restart")
        btn_quit    = QPushButton("Quit")
        btn_restart.clicked.connect(lambda: self.restart_requested.emit())
        btn_quit.clicked.connect(lambda: QApplication.instance().quit())  # type: ignore[union-attr]

        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_restart)
        btn_row.addWidget(btn_quit)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(4)
        left_layout.addWidget(pt)
        left_layout.addWidget(reports_box)
        left_layout.addLayout(btn_row)
        left_dock.addWidget(left_widget)

        # ── Spot table ────────────────────────────────────────────────────────
        self._spot_table = SpotTable()
        right_dock.addWidget(self._spot_table)
        self.new_spot.connect(self._spot_table.add_spot)

    # -- parameter tree -------------------------------------------------------

    @staticmethod
    def _clear_action_labels(pt: ParameterTree) -> None:
        """Blank the left-column name on every action item so the button isn't double-labeled."""
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
            ]),
            dict(name='Filtering', type='group', children=[
                dict(name='Max Range (km)', type='int',
                     value=args.range or 0, min=0,
                     tip='0 = no range limit'),
                dict(name='Show', type='list', limits=_SHOW_OPTS,
                     value=getattr(args, 'show', 'All spots')),
            ]),
            dict(name='ADIF Log', type='group', children=[
                dict(name='File', type='file', value=adif_path,
                     fileMode='ExistingFile',
                     nameFilter='ADIF Files (*.adif *.adi);;All Files (*)'),
            ]),
            dict(name='Display', type='group', children=[
                dict(name='Terminal Output', type='bool', value=args.terminal),
            ]),
            dict(name='WSJT-X', type='group', children=[
                dict(name='Enable',   type='bool',
                     value=getattr(args, 'wsjt', True)),
                dict(name='Filter',   type='list', limits=_WSJT_FILTERS,
                     value=getattr(args, 'wsjt_filter', 'CQ')),
                dict(name='UDP Port', type='int',
                     value=getattr(args, 'wsjt_port', 2237),
                     min=1024, max=65535),
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
            'spot_filter': p.child('Filtering').child('Show').value(),
            'terminal':    p.child('Display').child('Terminal Output').value(),
            'wsjt':        p.child('WSJT-X').child('Enable').value(),
            'wsjt_filter': p.child('WSJT-X').child('Filter').value(),
            'wsjt_port':   p.child('WSJT-X').child('UDP Port').value(),
            'adif_path':   p.child('ADIF Log').child('File').value().strip(),
        }

    def _on_params_changed(self, _root, changes) -> None:
        for _param, change, _data in changes:
            if change == 'value':
                self.settings_changed.emit(self._collect_settings())
                return  # emit once even if multiple values changed together

    # -- table interface ------------------------------------------------------

    def update_counts(self, psk_count: int, wsjt_count: int) -> None:
        self._lbl_psk.setText(str(psk_count))
        self._lbl_wsjt.setText(str(wsjt_count))
        self._lbl_total.setText(str(psk_count + wsjt_count))

    def clear_table(self) -> None:
        self._spot_table.clear()
