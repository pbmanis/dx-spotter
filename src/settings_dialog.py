from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QSpinBox, QVBoxLayout, QButtonGroup,
)

from adif_log import RUMLOGNG_DB_PATH


class SettingsDialog(QDialog):
    def __init__(self, log_source: str, adif_path: str,
                 udp_address: str, udp_port: int,
                 my_grid: str = 'FM05kw',
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Log source ────────────────────────────────────────────────────────
        src_box = QGroupBox("Log Source  (read-only)")
        src_layout = QVBoxLayout(src_box)

        self._src_group = QButtonGroup(self)
        self._rb_adif   = QRadioButton("ADIF file")
        self._rb_rum    = QRadioButton("RumLogNG (CloudKit database)")
        self._src_group.addButton(self._rb_adif, 0)
        self._src_group.addButton(self._rb_rum,  1)
        src_layout.addWidget(self._rb_adif)
        src_layout.addWidget(self._rb_rum)

        rumlogng_exists = Path(RUMLOGNG_DB_PATH).exists()
        self._rb_rum.setEnabled(rumlogng_exists)
        if not rumlogng_exists:
            self._rb_rum.setText(
                "RumLogNG (CloudKit database)  — not found on this machine"
            )

        # ADIF path row (shown only when ADIF source is selected)
        self._adif_row_widget = QGroupBox("ADIF Log File")
        adif_row = QHBoxLayout(self._adif_row_widget)
        self._adif_edit = QLineEdit(adif_path)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_adif)
        adif_row.addWidget(self._adif_edit)
        adif_row.addWidget(browse_btn)

        src_layout.addWidget(self._adif_row_widget)

        # RumLogNG path info label (shown only when RumLogNG selected)
        self._rum_info = QLabel(
            f"Database path (read-only):\n{RUMLOGNG_DB_PATH}"
        )
        self._rum_info.setWordWrap(True)
        self._rum_info.setStyleSheet("color: #aaaaaa; font-size: 10pt;")
        src_layout.addWidget(self._rum_info)

        # set initial state
        if log_source == 'rumlogng' and rumlogng_exists:
            self._rb_rum.setChecked(True)
        else:
            self._rb_adif.setChecked(True)
        self._update_log_source_ui()

        self._src_group.idToggled.connect(lambda _id, checked: (
            self._update_log_source_ui() if checked else None
        ))

        # ── Station ───────────────────────────────────────────────────────────
        station_box = QGroupBox("Station")
        station_form = QFormLayout(station_box)
        self._grid_edit = QLineEdit(my_grid.upper())
        self._grid_edit.setMaxLength(6)
        self._grid_edit.setPlaceholderText("e.g. FM05kw")
        station_form.addRow("My Grid Square:", self._grid_edit)

        # ── WSJT-X network ───────────────────────────────────────────────────
        udp_box = QGroupBox("WSJT-X Network")
        udp_form = QFormLayout(udp_box)
        self._addr_edit = QLineEdit(udp_address)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(udp_port)
        udp_form.addRow("UDP Server Address:", self._addr_edit)
        udp_form.addRow("UDP Port:", self._port_spin)

        udp_note = QLabel(
            "Use 224.0.0.1 (multicast) so multiple apps (RUMlogNG, GridTracker…) "
            "each receive their own copy.  UDP changes take effect on next launch."
        )
        udp_note.setWordWrap(True)
        udp_note.setStyleSheet("color: #999999; font-size: 10pt;")

        # ── buttons ───────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(src_box)
        layout.addWidget(station_box)
        layout.addWidget(udp_box)
        layout.addWidget(udp_note)
        layout.addStretch()
        layout.addWidget(buttons)

    # -- helpers ---------------------------------------------------------------

    def _update_log_source_ui(self) -> None:
        adif_selected = self._rb_adif.isChecked()
        self._adif_row_widget.setVisible(adif_selected)
        self._rum_info.setVisible(not adif_selected)

    def _browse_adif(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ADIF Log File", self._adif_edit.text(),
            "ADIF Files (*.adif *.adi);;All Files (*)"
        )
        if path:
            self._adif_edit.setText(path)

    # -- result properties -----------------------------------------------------

    @property
    def my_grid(self) -> str:
        return self._grid_edit.text().strip().upper()

    @property
    def log_source(self) -> str:
        return 'rumlogng' if self._rb_rum.isChecked() else 'adif'

    @property
    def adif_path(self) -> str:
        return self._adif_edit.text().strip()

    @property
    def udp_address(self) -> str:
        return self._addr_edit.text().strip()

    @property
    def udp_port(self) -> int:
        return self._port_spin.value()
