"""Settings dialog for DX Spotter.

Provides :class:`SettingsDialog`, a modal ``QDialog`` that lets the user
configure the log source (ADIF file vs. RumLogNG), the operator's grid square,
and the WSJT-X UDP network settings.

Result values are exposed as read-only properties and consumed by
:meth:`~dxspotter.DXSpotter._open_settings` after the dialog is accepted.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QSpinBox, QVBoxLayout, QButtonGroup,
)

from adif_log import RUMLOGNG_DB_PATH


class SettingsDialog(QDialog):
    """Modal dialog for editing persistent DX Spotter settings.

    Presents three groups of controls:

    * **Log Source** — radio buttons to choose between an ADIF file and the
      RumLogNG CloudKit database.  The RumLogNG option is disabled when the
      database file cannot be found on disk.
    * **Station** — operator's Maidenhead grid square (6 characters).
    * **WSJT-X Network** — UDP multicast address and port number.

    Result values are read back through the read-only properties
    :attr:`my_grid`, :attr:`log_source`, :attr:`adif_path`,
    :attr:`udp_address`, and :attr:`udp_port` after :meth:`exec` returns
    ``Accepted``.
    """

    def __init__(self, log_source: str, adif_path: str,
                 udp_address: str, udp_port: int,
                 my_grid: str = 'FM05kw',
                 rx_grid_prefixes: list[str] | None = None,
                 wsjt_reshow_secs: int = 300,
                 parent=None) -> None:
        """Build and populate the settings dialog.

        Parameters
        ----------
        log_source : str
            Current log source key: ``'adif'`` or ``'rumlogng'``.
        adif_path : str
            Current path to the ADIF log file (pre-fills the file field).
        udp_address : str
            Current WSJT-X UDP multicast address (pre-fills the address field).
        udp_port : int
            Current WSJT-X UDP port (pre-fills the port spinner).
        my_grid : str, optional
            Operator's Maidenhead grid square (default ``'FM05kw'``).
        rx_grid_prefixes : list[str] or None, optional
            Two-character Maidenhead grid prefixes used to restrict PSK Reporter
            spots by reporter location.  Shown as a space-separated string.
            ``None`` uses the default list.
        wsjt_reshow_secs : int, optional
            Minimum seconds between successive table entries for the same WSJT-X
            callsign.  Default is ``300``.
        parent : QWidget or None, optional
            Optional Qt parent widget.
        """
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

        _prefixes = rx_grid_prefixes if rx_grid_prefixes is not None else ["FM", "FN", "FL", "EL", "EN", "EM"]
        self._rx_grid_edit = QLineEdit(" ".join(_prefixes))
        self._rx_grid_edit.setPlaceholderText("e.g. FM FN FL  (blank = accept all reporters)")

        self._reshow_spin = QSpinBox()
        self._reshow_spin.setRange(0, 3600)
        self._reshow_spin.setSuffix(" s")
        self._reshow_spin.setValue(wsjt_reshow_secs)

        udp_form.addRow("UDP Server Address:", self._addr_edit)
        udp_form.addRow("UDP Port:", self._port_spin)
        udp_form.addRow("Reporter Grid Prefixes:", self._rx_grid_edit)
        udp_form.addRow("Call Re-show Interval:", self._reshow_spin)

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
        """Operator's Maidenhead grid square entered in the dialog (stripped, upper-case)."""
        return self._grid_edit.text().strip().upper()

    @property
    def log_source(self) -> str:
        """Selected log source: ``'rumlogng'`` or ``'adif'``."""
        return 'rumlogng' if self._rb_rum.isChecked() else 'adif'

    @property
    def adif_path(self) -> str:
        """Filesystem path to the ADIF log file (stripped)."""
        return self._adif_edit.text().strip()

    @property
    def udp_address(self) -> str:
        """UDP server address entered in the dialog (stripped)."""
        return self._addr_edit.text().strip()

    @property
    def udp_port(self) -> int:
        """UDP port number selected in the dialog."""
        return self._port_spin.value()

    @property
    def rx_grid_prefixes(self) -> list[str]:
        """List of two-character Maidenhead grid prefixes from the dialog (upper-cased)."""
        raw = self._rx_grid_edit.text().strip().upper()
        if not raw:
            return []
        return [t for t in raw.split() if t]

    @property
    def wsjt_reshow_secs(self) -> int:
        """Minimum seconds between successive WSJT-X table entries for the same call."""
        return self._reshow_spin.value()
