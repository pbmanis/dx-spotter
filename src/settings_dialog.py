"""Settings dialog for DX Spotter.

Provides :class:`SettingsDialog`, a modal ``QDialog`` that lets the user
configure the log source (ADIF file vs. RumLogNG), the operator's grid square,
and the WSJT-X UDP network settings.

Result values are exposed as read-only properties and consumed by
:meth:`~dxspotter.DXSpotter._open_settings` after the dialog is accepted.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox, QTabWidget,
    QVBoxLayout, QButtonGroup, QWidget,
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

    #: Emitted when the user requests an FCC database rebuild.
    fcc_update_requested = pyqtSignal()

    def __init__(self, log_source: str, adif_path: str,
                 udp_address: str, udp_port: int,
                 my_grid: str = 'FM05kw',
                 rx_grid_prefixes: list[str] | None = None,
                 wsjt_reshow_secs: int = 300,
                 wsjt_no_spot_mins: int = 2,
                 wsjt_show_decodes: bool = True,
                 rig_control_enabled: bool = False,
                 rig_track_band: bool = True,
                 rig_backend: str = 'commander',
                 commander_port: int = 52002,
                 commander_timeout: float = 0.2,
                 commander_verify_delay: float = 0.75,
                 rigctld_port: int = 4532,
                 rigctld_timeout: float = 0.2,
                 rigctld_verify_delay: float = 0.75,
                 telnet1_enabled: bool = False,
                 telnet1_host: str = '',
                 telnet1_port: int = 7300,
                 telnet1_callsign: str = '',
                 telnet2_enabled: bool = False,
                 telnet2_host: str = '',
                 telnet2_port: int = 7300,
                 telnet2_callsign: str = '',
                 telnet3_enabled: bool = False,
                 telnet3_host: str = '',
                 telnet3_port: int = 7300,
                 telnet3_callsign: str = '',
                 telnet4_enabled: bool = False,
                 telnet4_host: str = '',
                 telnet4_port: int = 7300,
                 telnet4_callsign: str = '',
                 telnet_us_ca_spotters_only: bool = True,
                 pskr_enabled: bool = True,
                 pskr_host: str = 'mqtt.pskreporter.info',
                 pskr_port: int = 1883,
                 pskr_service_name: str = 'PSK Reporter',
                 pskr_reshow_secs: int = 300,
                 cty_path: str = '',
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
        wsjt_no_spot_mins : int, optional
            Minutes without any Decode packet before the WSJT-X status turns
            yellow.  Default is ``2``.
        wsjt_show_decodes : bool, optional
            Whether WSJT-X decodes are added to the spot table and (when
            ``-t``/``--terminal`` is active) printed to the terminal.
            Default ``True``.
        rig_control_enabled : bool, optional
            Whether rig control (double-click QSY) is active.  Applies to
            whichever backend ``rig_backend`` selects.  Default ``False``.
        rig_track_band : bool, optional
            Whether the active backend's reported VFO frequency is allowed to
            override the Band filter.  Independent of ``rig_control_enabled``
            so it can be turned off if the backend misreports frequency,
            without losing double-click QSY.  Default ``True``.
        rig_backend : str, optional
            Which rig-control backend to use: ``'commander'`` or
            ``'rigctld'``.  Default ``'commander'``.
        commander_port : int, optional
            TCP port Commander listens on.  Default ``52002``.
        commander_timeout : float, optional
            Per-query TCP receive timeout in seconds.  Default ``0.2``.
        commander_verify_delay : float, optional
            Seconds to wait after a set command before reading back rig state.
            Default ``0.75``.
        rigctld_port : int, optional
            TCP port rigctld listens on.  Default ``4532``.
        rigctld_timeout : float, optional
            Per-query TCP receive timeout in seconds.  Default ``0.2``.
        rigctld_verify_delay : float, optional
            Seconds to wait after a set command before reading back rig state.
            Default ``0.75``.
        telnet1_enabled : bool, optional
            Whether DX Cluster 1 is enabled.  Default ``False``.
        telnet1_host : str, optional
            Hostname or IP address for cluster 1.  Default ``''``.
        telnet1_port : int, optional
            TCP port for cluster 1.  Default ``7300``.
        telnet1_callsign : str, optional
            Login callsign for cluster 1.  Default ``''``.
        telnet2_enabled : bool, optional
            Whether DX Cluster 2 is enabled.  Default ``False``.
        telnet2_host : str, optional
            Hostname or IP address for cluster 2.  Default ``''``.
        telnet2_port : int, optional
            TCP port for cluster 2.  Default ``7300``.
        telnet2_callsign : str, optional
            Login callsign for cluster 2.  Default ``''``.
        telnet3_enabled : bool, optional
            Whether DX Cluster 3 is enabled.  Default ``False``.
        telnet3_host : str, optional
            Hostname or IP address for cluster 3.  Default ``''``.
        telnet3_port : int, optional
            TCP port for cluster 3.  Default ``7300``.
        telnet3_callsign : str, optional
            Login callsign for cluster 3.  Default ``''``.
        telnet4_enabled : bool, optional
            Whether DX Cluster 4 is enabled.  Default ``False``.
        telnet4_host : str, optional
            Hostname or IP address for cluster 4.  Default ``''``.
        telnet4_port : int, optional
            TCP port for cluster 4.  Default ``7300``.
        telnet4_callsign : str, optional
            Login callsign for cluster 4.  Default ``''``.
        telnet_us_ca_spotters_only : bool, optional
            Whether to discard DX Cluster spots whose reporting station is
            outside the US/Canada.  Default ``True``.
        pskr_enabled : bool, optional
            Whether the PSK Reporter MQTT connection is enabled.  Default ``True``.
        pskr_host : str, optional
            PSK Reporter MQTT broker hostname.  Default ``'mqtt.pskreporter.info'``.
        pskr_port : int, optional
            PSK Reporter MQTT broker port.  Default ``1883``.
        pskr_service_name : str, optional
            Free-text label for the PSK Reporter connection, for reference only.
            Default ``'PSK Reporter'``.
        pskr_reshow_secs : int, optional
            Minimum seconds between successive table entries for the same
            PSK Reporter callsign.  Default is ``300``.
        cty_path : str, optional
            Path to the current cached CTY plist file (used only for display).
            Default ``''``.
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

        self._no_spot_spin = QSpinBox()
        self._no_spot_spin.setRange(1, 60)
        self._no_spot_spin.setSuffix(" min")
        self._no_spot_spin.setValue(wsjt_no_spot_mins)

        self._wsjt_show_cb = QCheckBox("Show WSJT-X decodes (table + terminal)")
        self._wsjt_show_cb.setChecked(wsjt_show_decodes)

        udp_form.addRow("UDP Server Address:", self._addr_edit)
        udp_form.addRow("UDP Port:", self._port_spin)
        udp_form.addRow("Reporter Grid Prefixes:", self._rx_grid_edit)
        udp_form.addRow("Call Re-show Interval:", self._reshow_spin)
        udp_form.addRow("No-decode warning after:", self._no_spot_spin)
        udp_form.addRow("", self._wsjt_show_cb)

        udp_note = QLabel(
            "Use 224.0.0.1 (multicast) so multiple apps (RUMlogNG, GridTracker…) "
            "each receive their own copy.  UDP changes take effect on next launch."
        )
        udp_note.setWordWrap(True)
        udp_note.setStyleSheet("color: #999999; font-size: 10pt;")

        # ── Communication (rig control) ───────────────────────────────────────
        cmd_box = QGroupBox("Communication  (Rig Control)")
        cmd_layout = QVBoxLayout(cmd_box)

        common_form = QFormLayout()
        self._cmd_enabled = QCheckBox("Enable rig control for CW / SSB spots")
        self._cmd_enabled.setChecked(rig_control_enabled)

        self._cmd_track_band = QCheckBox("Track Band filter from VFO")
        self._cmd_track_band.setChecked(rig_track_band)

        self._backend_group = QButtonGroup(self)
        self._rb_backend_commander = QRadioButton("DX Lab Commander")
        self._rb_backend_rigctld = QRadioButton("rigctld (Hamlib / K2K3Controller)")
        self._backend_group.addButton(self._rb_backend_commander, 0)
        self._backend_group.addButton(self._rb_backend_rigctld, 1)
        if rig_backend == 'rigctld':
            self._rb_backend_rigctld.setChecked(True)
        else:
            self._rb_backend_commander.setChecked(True)
        backend_row = QHBoxLayout()
        backend_row.addWidget(self._rb_backend_commander)
        backend_row.addWidget(self._rb_backend_rigctld)

        common_form.addRow("", self._cmd_enabled)
        common_form.addRow("", self._cmd_track_band)
        common_form.addRow("Backend:", backend_row)
        cmd_layout.addLayout(common_form)

        # -- Commander-specific settings --
        self._cmd_settings_box = QGroupBox("DX Lab Commander Settings")
        cmd_form = QFormLayout(self._cmd_settings_box)

        self._cmd_port_spin = QSpinBox()
        self._cmd_port_spin.setRange(1024, 65535)
        self._cmd_port_spin.setValue(commander_port)

        self._cmd_timeout_spin = QDoubleSpinBox()
        self._cmd_timeout_spin.setRange(0.05, 5.0)
        self._cmd_timeout_spin.setSingleStep(0.05)
        self._cmd_timeout_spin.setDecimals(2)
        self._cmd_timeout_spin.setSuffix(" s")
        self._cmd_timeout_spin.setValue(commander_timeout)

        self._cmd_delay_spin = QDoubleSpinBox()
        self._cmd_delay_spin.setRange(0.1, 10.0)
        self._cmd_delay_spin.setSingleStep(0.05)
        self._cmd_delay_spin.setDecimals(2)
        self._cmd_delay_spin.setSuffix(" s")
        self._cmd_delay_spin.setValue(commander_verify_delay)

        cmd_form.addRow("TCP Port:", self._cmd_port_spin)
        cmd_form.addRow("Query timeout:", self._cmd_timeout_spin)
        cmd_form.addRow("Verify delay:", self._cmd_delay_spin)
        cmd_layout.addWidget(self._cmd_settings_box)

        # -- rigctld-specific settings --
        self._rigctld_settings_box = QGroupBox("rigctld Settings")
        rigctld_form = QFormLayout(self._rigctld_settings_box)

        self._rigctld_port_spin = QSpinBox()
        self._rigctld_port_spin.setRange(1024, 65535)
        self._rigctld_port_spin.setValue(rigctld_port)

        self._rigctld_timeout_spin = QDoubleSpinBox()
        self._rigctld_timeout_spin.setRange(0.05, 5.0)
        self._rigctld_timeout_spin.setSingleStep(0.05)
        self._rigctld_timeout_spin.setDecimals(2)
        self._rigctld_timeout_spin.setSuffix(" s")
        self._rigctld_timeout_spin.setValue(rigctld_timeout)

        self._rigctld_delay_spin = QDoubleSpinBox()
        self._rigctld_delay_spin.setRange(0.1, 10.0)
        self._rigctld_delay_spin.setSingleStep(0.05)
        self._rigctld_delay_spin.setDecimals(2)
        self._rigctld_delay_spin.setSuffix(" s")
        self._rigctld_delay_spin.setValue(rigctld_verify_delay)

        rigctld_form.addRow("TCP Port:", self._rigctld_port_spin)
        rigctld_form.addRow("Query timeout:", self._rigctld_timeout_spin)
        rigctld_form.addRow("Verify delay:", self._rigctld_delay_spin)
        cmd_layout.addWidget(self._rigctld_settings_box)

        self._update_rig_backend_ui()
        self._backend_group.idToggled.connect(lambda _id, checked: (
            self._update_rig_backend_ui() if checked else None
        ))

        cmd_note = QLabel(
            "Only one backend talks to the rig at a time — pick the one "
            "actually connected to it.  Verify delay must cover at least one "
            "backend poll cycle (~0.7 s minimum).  Uncheck \"Track Band "
            "filter\" if the backend reports a stale or incorrect VFO "
            "frequency — it was overriding manual band selection."
        )
        cmd_note.setWordWrap(True)
        cmd_note.setStyleSheet("color: #999999; font-size: 10pt;")

        # ── Country Lookup File (CTY) ─────────────────────────────────────────
        self._cty_refreshed: bool = False
        cty_box = QGroupBox("Country Lookup File (CTY)")
        cty_layout = QVBoxLayout(cty_box)
        self._cty_status_label = QLabel()
        self._cty_refresh_btn = QPushButton("Refresh CTY…")
        self._cty_refresh_btn.clicked.connect(self._refresh_cty)
        cty_layout.addWidget(self._cty_status_label)
        cty_layout.addWidget(self._cty_refresh_btn)
        self._update_cty_label()

        # ── FCC Amateur Call Database ─────────────────────────────────────────
        fcc_box = QGroupBox("FCC Amateur Call Database")
        fcc_layout = QVBoxLayout(fcc_box)
        self._fcc_status_label = QLabel()
        self._fcc_update_btn = QPushButton("Update FCC Database…")
        self._fcc_update_btn.clicked.connect(self._request_fcc_update)
        fcc_layout.addWidget(self._fcc_status_label)
        fcc_layout.addWidget(self._fcc_update_btn)
        fcc_note = QLabel(
            "Downloads and indexes the FCC ULS amateur licence database "
            "(~30 MB).  Used to compute reporter distance for DX Cluster "
            "spots.  The download runs in the background; progress appears "
            "in the main window status bar."
        )
        fcc_note.setWordWrap(True)
        fcc_note.setStyleSheet("color: #999999; font-size: 10pt;")
        fcc_layout.addWidget(fcc_note)
        self._update_fcc_label()

        # ── DX Cluster (Telnet) ───────────────────────────────────────────────
        telnet_filter_box = QGroupBox("DX Cluster Filtering")
        telnet_filter_layout = QVBoxLayout(telnet_filter_box)
        self._telnet_us_ca_only = QCheckBox("Only show spots from US/Canada spotters")
        self._telnet_us_ca_only.setChecked(telnet_us_ca_spotters_only)
        telnet_filter_layout.addWidget(self._telnet_us_ca_only)
        telnet_filter_note = QLabel(
            "When checked, a spot is shown only if the reporting/spotting "
            "station (not the DX station) is in the US or Canada.  Uncheck "
            "to see spots from any spotter worldwide."
        )
        telnet_filter_note.setWordWrap(True)
        telnet_filter_note.setStyleSheet("color: #999999; font-size: 10pt;")
        telnet_filter_layout.addWidget(telnet_filter_note)

        telnet_box = QGroupBox("DX Cluster (Telnet)")
        telnet_layout = QGridLayout(telnet_box)

        def _build_cluster_box(
            title: str, enabled: bool, host: str, port: int, callsign: str,
            host_placeholder: str,
        ) -> tuple[QGroupBox, QCheckBox, QLineEdit, QSpinBox, QLineEdit]:
            # Build one "Cluster N" group box; returns the box plus its widgets.
            box = QGroupBox(title)
            form = QFormLayout(box)
            enabled_cb = QCheckBox("Enable")
            enabled_cb.setChecked(enabled)
            host_edit = QLineEdit(host)
            host_edit.setPlaceholderText(host_placeholder)
            port_spin = QSpinBox()
            port_spin.setRange(1, 65535)
            port_spin.setValue(port)
            call_edit = QLineEdit(callsign.upper())
            call_edit.setPlaceholderText("Your callsign")
            form.addRow("", enabled_cb)
            form.addRow("Host:", host_edit)
            form.addRow("Port:", port_spin)
            form.addRow("Callsign:", call_edit)
            return box, enabled_cb, host_edit, port_spin, call_edit

        c1_box, self._t1_enabled, self._t1_host, self._t1_port, self._t1_call = (
            _build_cluster_box("Cluster 1", telnet1_enabled, telnet1_host,
                               telnet1_port, telnet1_callsign, "e.g. dxc.k0xm.net")
        )
        c2_box, self._t2_enabled, self._t2_host, self._t2_port, self._t2_call = (
            _build_cluster_box("Cluster 2", telnet2_enabled, telnet2_host,
                               telnet2_port, telnet2_callsign, "e.g. dxc.w3lpl.net")
        )
        c3_box, self._t3_enabled, self._t3_host, self._t3_port, self._t3_call = (
            _build_cluster_box("Cluster 3", telnet3_enabled, telnet3_host,
                               telnet3_port, telnet3_callsign, "e.g. dxc.ve7cc.net")
        )
        c4_box, self._t4_enabled, self._t4_host, self._t4_port, self._t4_call = (
            _build_cluster_box("Cluster 4", telnet4_enabled, telnet4_host,
                               telnet4_port, telnet4_callsign, "e.g. dxc.nc7j.com")
        )

        # 2x2 grid: cluster 1/2 on the top row, 3/4 on the bottom row.
        telnet_layout.addWidget(c1_box, 0, 0)
        telnet_layout.addWidget(c2_box, 0, 1)
        telnet_layout.addWidget(c3_box, 1, 0)
        telnet_layout.addWidget(c4_box, 1, 1)

        # ── PSK Reporter ──────────────────────────────────────────────────────
        pskr_box = QGroupBox("PSK Reporter (MQTT)")
        pskr_form = QFormLayout(pskr_box)
        self._pskr_enabled = QCheckBox("Enable")
        self._pskr_enabled.setChecked(pskr_enabled)
        self._pskr_host = QLineEdit(pskr_host)
        self._pskr_host.setPlaceholderText("e.g. mqtt.pskreporter.info")
        self._pskr_port = QSpinBox()
        self._pskr_port.setRange(1, 65535)
        self._pskr_port.setValue(pskr_port)
        self._pskr_service_name = QLineEdit(pskr_service_name)
        self._pskr_service_name.setPlaceholderText("e.g. PSK Reporter")
        self._pskr_reshow_spin = QSpinBox()
        self._pskr_reshow_spin.setRange(0, 3600)
        self._pskr_reshow_spin.setSuffix(" s")
        self._pskr_reshow_spin.setValue(pskr_reshow_secs)
        pskr_form.addRow("", self._pskr_enabled)
        pskr_form.addRow("MQTT Host:", self._pskr_host)
        pskr_form.addRow("MQTT Port:", self._pskr_port)
        pskr_form.addRow("Service Name:", self._pskr_service_name)
        pskr_form.addRow("Call Re-show Interval:", self._pskr_reshow_spin)

        pskr_note = QLabel(
            "Service Name is for reference only (shown in this dialog); it has "
            "no effect on the MQTT connection itself.  Call Re-show Interval "
            "limits how often the same callsign can jump back to the top of "
            "the spot table (0 = no limit)."
        )
        pskr_note.setWordWrap(True)
        pskr_note.setStyleSheet("color: #999999; font-size: 10pt;")

        # ── buttons ───────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        tabs = QTabWidget()

        tab_log = QWidget()
        tab_log_layout = QVBoxLayout(tab_log)
        tab_log_layout.addWidget(src_box)
        tab_log_layout.addWidget(station_box)
        tab_log_layout.addStretch()
        tabs.addTab(tab_log, "Log & Station")

        tab_wsjt = QWidget()
        tab_wsjt_layout = QVBoxLayout(tab_wsjt)
        tab_wsjt_layout.addWidget(udp_box)
        tab_wsjt_layout.addWidget(udp_note)
        tab_wsjt_layout.addStretch()
        tabs.addTab(tab_wsjt, "WSJT-X")

        tab_pskr = QWidget()
        tab_pskr_layout = QVBoxLayout(tab_pskr)
        tab_pskr_layout.addWidget(pskr_box)
        tab_pskr_layout.addWidget(pskr_note)
        tab_pskr_layout.addStretch()
        tabs.addTab(tab_pskr, "PSK Reporter")

        tab_cmd = QWidget()
        tab_cmd_layout = QVBoxLayout(tab_cmd)
        tab_cmd_layout.addWidget(cmd_box)
        tab_cmd_layout.addWidget(cmd_note)
        tab_cmd_layout.addStretch()
        tabs.addTab(tab_cmd, "Communication")

        tab_cluster = QWidget()
        tab_cluster_layout = QVBoxLayout(tab_cluster)
        tab_cluster_layout.addWidget(telnet_filter_box)
        tab_cluster_layout.addWidget(telnet_box)
        tab_cluster_layout.addStretch()
        tabs.addTab(tab_cluster, "DX Cluster")

        tab_db = QWidget()
        tab_db_layout = QVBoxLayout(tab_db)
        tab_db_layout.addWidget(cty_box)
        tab_db_layout.addWidget(fcc_box)
        tab_db_layout.addStretch()
        tabs.addTab(tab_db, "Databases")

        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- helpers ---------------------------------------------------------------

    def _update_log_source_ui(self) -> None:
        adif_selected = self._rb_adif.isChecked()
        self._adif_row_widget.setVisible(adif_selected)
        self._rum_info.setVisible(not adif_selected)

    def _update_rig_backend_ui(self) -> None:
        commander_selected = self._rb_backend_commander.isChecked()
        self._cmd_settings_box.setEnabled(commander_selected)
        self._rigctld_settings_box.setEnabled(not commander_selected)

    def _browse_adif(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ADIF Log File", self._adif_edit.text(),
            "ADIF Files (*.adif *.adi);;All Files (*)"
        )
        if path:
            self._adif_edit.setText(path)

    def _update_cty_label(self) -> None:
        import cty_cache as _cty
        age = _cty.cty_age_days()
        if age is None:
            text = "Not downloaded yet."
        else:
            days = int(age)
            text = f"Age: {days} day{'s' if days != 1 else ''}  —  {_cty.cty_plist_path()}"
        self._cty_status_label.setText(text)

    def _update_fcc_label(self) -> None:
        import fcc_db as _fcc
        age = _fcc.fcc_db_age_days()
        count = _fcc.fcc_db_entry_count()
        if age is None:
            text = "Not downloaded."
        else:
            days = int(age)
            text = (
                f"Age: {days} day{'s' if days != 1 else ''}  —  "
                f"{count:,} callsigns  —  {_fcc.fcc_db_path()}"
            )
        self._fcc_status_label.setText(text)

    def _request_fcc_update(self) -> None:
        self._fcc_update_btn.setEnabled(False)
        self._fcc_update_btn.setText("Updating (background)…")
        self.fcc_update_requested.emit()

    def _refresh_cty(self) -> None:
        from PyQt6.QtCore import Qt
        import cty_cache as _cty
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            _cty.download_cty()
            self._cty_refreshed = True
        except Exception as exc:
            QMessageBox.warning(self, "CTY Refresh", f"Download failed:\n{exc}")
        finally:
            QApplication.restoreOverrideCursor()
        self._update_cty_label()

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

    @property
    def wsjt_no_spot_mins(self) -> int:
        """Minutes without a Decode packet before the WSJT-X status indicator turns yellow."""
        return self._no_spot_spin.value()

    @property
    def wsjt_show_decodes(self) -> bool:
        """Whether WSJT-X decodes should be shown in the table and terminal."""
        return self._wsjt_show_cb.isChecked()

    @property
    def rig_control_enabled(self) -> bool:
        """Whether rig control (double-click QSY) is enabled."""
        return self._cmd_enabled.isChecked()

    @property
    def rig_track_band(self) -> bool:
        """Whether the Band filter should follow the active backend's reported VFO frequency."""
        return self._cmd_track_band.isChecked()

    @property
    def rig_backend(self) -> str:
        """Selected rig-control backend: ``'commander'`` or ``'rigctld'``."""
        return 'rigctld' if self._rb_backend_rigctld.isChecked() else 'commander'

    @property
    def commander_port(self) -> int:
        """TCP port Commander listens on."""
        return self._cmd_port_spin.value()

    @property
    def commander_timeout(self) -> float:
        """Per-query TCP receive timeout in seconds."""
        return self._cmd_timeout_spin.value()

    @property
    def commander_verify_delay(self) -> float:
        """Seconds to wait after a set command before reading back rig state."""
        return self._cmd_delay_spin.value()

    @property
    def rigctld_port(self) -> int:
        """TCP port rigctld listens on."""
        return self._rigctld_port_spin.value()

    @property
    def rigctld_timeout(self) -> float:
        """Per-query TCP receive timeout in seconds."""
        return self._rigctld_timeout_spin.value()

    @property
    def rigctld_verify_delay(self) -> float:
        """Seconds to wait after a set command before reading back rig state."""
        return self._rigctld_delay_spin.value()

    @property
    def telnet1_enabled(self) -> bool:
        """Whether DX Cluster 1 is enabled."""
        return self._t1_enabled.isChecked()

    @property
    def telnet1_host(self) -> str:
        """Hostname or IP address for DX Cluster 1."""
        return self._t1_host.text().strip()

    @property
    def telnet1_port(self) -> int:
        """TCP port for DX Cluster 1."""
        return self._t1_port.value()

    @property
    def telnet1_callsign(self) -> str:
        """Login callsign for DX Cluster 1 (stripped, upper-case)."""
        return self._t1_call.text().strip().upper()

    @property
    def telnet2_enabled(self) -> bool:
        """Whether DX Cluster 2 is enabled."""
        return self._t2_enabled.isChecked()

    @property
    def telnet2_host(self) -> str:
        """Hostname or IP address for DX Cluster 2."""
        return self._t2_host.text().strip()

    @property
    def telnet2_port(self) -> int:
        """TCP port for DX Cluster 2."""
        return self._t2_port.value()

    @property
    def telnet2_callsign(self) -> str:
        """Login callsign for DX Cluster 2 (stripped, upper-case)."""
        return self._t2_call.text().strip().upper()

    @property
    def telnet3_enabled(self) -> bool:
        """Whether DX Cluster 3 is enabled."""
        return self._t3_enabled.isChecked()

    @property
    def telnet3_host(self) -> str:
        """Hostname or IP address for DX Cluster 3."""
        return self._t3_host.text().strip()

    @property
    def telnet3_port(self) -> int:
        """TCP port for DX Cluster 3."""
        return self._t3_port.value()

    @property
    def telnet3_callsign(self) -> str:
        """Login callsign for DX Cluster 3 (stripped, upper-case)."""
        return self._t3_call.text().strip().upper()

    @property
    def telnet4_enabled(self) -> bool:
        """Whether DX Cluster 4 is enabled."""
        return self._t4_enabled.isChecked()

    @property
    def telnet4_host(self) -> str:
        """Hostname or IP address for DX Cluster 4."""
        return self._t4_host.text().strip()

    @property
    def telnet4_port(self) -> int:
        """TCP port for DX Cluster 4."""
        return self._t4_port.value()

    @property
    def telnet4_callsign(self) -> str:
        """Login callsign for DX Cluster 4 (stripped, upper-case)."""
        return self._t4_call.text().strip().upper()

    @property
    def telnet_us_ca_spotters_only(self) -> bool:
        """Whether to discard DX Cluster spots from non-US/Canada spotters."""
        return self._telnet_us_ca_only.isChecked()

    @property
    def pskr_enabled(self) -> bool:
        """Whether the PSK Reporter MQTT connection is enabled."""
        return self._pskr_enabled.isChecked()

    @property
    def pskr_host(self) -> str:
        """PSK Reporter MQTT broker hostname (stripped)."""
        return self._pskr_host.text().strip()

    @property
    def pskr_port(self) -> int:
        """PSK Reporter MQTT broker port."""
        return self._pskr_port.value()

    @property
    def pskr_service_name(self) -> str:
        """Free-text reference label for the PSK Reporter connection (stripped)."""
        return self._pskr_service_name.text().strip()

    @property
    def pskr_reshow_secs(self) -> int:
        """Minimum seconds between successive table entries for the same PSK Reporter call."""
        return self._pskr_reshow_spin.value()

    @property
    def cty_refreshed(self) -> bool:
        """True if the user clicked Refresh CTY during this dialog session."""
        return self._cty_refreshed
