"""Main controller for DX Spotter.

This module wires together the MQTT connection to PSK Reporter, the optional
WSJT-X UDP listener, two telnet cluster connections, the Qt GUI (:class:`~main_window.MainWindow`), and the
ADIF / RumLogNG contact log.  Application entry point is :func:`main`.
"""
import argparse
import signal
import sys
import threading
import time
from typing import Callable

from colorama import Fore, Style
from pyhamtools import LookupLib, Callinfo
from pyhamtools.locator import calculate_distance as qth_distance

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from adif_log import ADIFLog
from appconfig import AppConfig, config_path, load_config, save_config
from callsign_corrections import correct_entity
from commander_client import CommanderClient, is_available as commander_available
import cty_cache
import fcc_db
from main_window import MainWindow, make_app_icon
from mqtt_listener import MqttListener
from rigctld_client import RigctldClient, is_available as rigctld_available
from settings_dialog import SettingsDialog
from telnet_cluster import TelnetCluster
from wsjtx_listener import WsjtxListener, freq_to_band

# Ham band frequency boundaries in kHz; used to map the rig-control VFO to band string.
_BAND_RANGES: list[tuple[float, float, str]] = [
    (1800.0,   2000.0,  '160m'),
    (3500.0,   4000.0,  '80m'),
    (5330.0,   5410.0,  '60m'),
    (7000.0,   7300.0,  '40m'),
    (10100.0, 10150.0,  '30m'),
    (14000.0, 14350.0,  '20m'),
    (18068.0, 18168.0,  '17m'),
    (21000.0, 21450.0,  '15m'),
    (24890.0, 24990.0,  '12m'),
    (28000.0, 29700.0,  '10m'),
    (50000.0, 54000.0,   '6m'),
    (144000.0, 148000.0, '2m'),
]

# Network Status Symbols
CONNECTED = "\U0001F310"     # 🌐
CONNECTING = "\U0001F4E1"    # 📡
DISCONNECTED = "\U0001F6AB" # 🚫

class DXSpotter:
    """Top-level application controller for DX Spotter.

    Owns the MQTT client (PSK Reporter), an optional :class:`~wsjtx_listener.WsjtxListener`
    (WSJT-X UDP), two telnet cluster connections, the :class:`~main_window.MainWindow` Qt window, and the loaded
    :class:`~adif_log.ADIFLog`.  Coordinates data flow between all components:

    * Incoming PSK Reporter MQTT messages are decoded in :meth:`on_message` and
      forwarded to the spot table via :attr:`~main_window.MainWindow.new_spot`.
    * Incoming WSJT-X decodes arrive on a background thread via
      :meth:`_on_wsjt_spot` and are forwarded to the same signal.
    * Settings changes from the GUI are applied live in :meth:`_apply_settings`.
    * Double-clicks on the spot table trigger :meth:`_on_spot_activated`, depending
      on the mode, these are sent to:
        - FT8/FT4/FT2: sends a Reply / Configure message back to WSJT-X.
        - CW, SSB: sends a command to the active rig-control backend
          (DX Lab Commander or rigctld — see :attr:`~appconfig.AppConfig.rig_backend`)
          to set the radio frequency and mode.

    Attributes
    ----------
    freqs : dict[str, dict[str, int]]
        Standard digital-mode dial frequencies (Hz) keyed by band then mode.
        Used to compute the audio frequency offset (DF) shown in the ``dHz``
        column and passed to :meth:`_qsy_rigctld`.
    args : argparse.Namespace or None
        Parsed command-line arguments merged with config-file values.
    cinfo : Callinfo or None
        pyhamtools callsign lookup object (country file backend).
    psk_counter : int
        Number of PSK Reporter spots currently in the table.
    wsjt_counter : int
        Number of WSJT-X spots currently in the table.
    telnet1_counter : int
        Number of DX Cluster 1 spots currently in the table.
    telnet2_counter : int
        Number of DX Cluster 2 spots currently in the table.
    telnet3_counter : int
        Number of DX Cluster 3 spots currently in the table.
    telnet4_counter : int
        Number of DX Cluster 4 spots currently in the table.
    topic : str or None
        Active PSK Reporter MQTT subscription topic string.
    my_grid : str
        Operator's Maidenhead grid square (e.g. ``'FM05kw'``).
    window : MainWindow or None
        The main Qt window; ``None`` until :meth:`run` creates it.
    adif_log : ADIFLog or None
        Loaded contact log; ``None`` when no log file is configured.
    wsjt_listener : WsjtxListener or None
        Active WSJT-X UDP listener; ``None`` when WSJT-X is disabled.
    """

    freqs = {
        "2m":  {"FT2": 144_177_000, "FT8": 144_174_000},
        "6m":  {"FT2": 50_316_000, "FT4": 50_318_000, "FT8": 50_313_000},
        "10m": {"FT2": 28_184_000, "FT4": 28_180_000, "FT8": 28_074_000},
        "15m": {"FT2": 21_144_000, "FT4": 21_140_000, "FT8": 21_074_000},
        "17m": {"FT2": 18_108_000, "FT4": 18_104_000, "FT8": 18_100_000},
        "20m": {"FT2": 14_084_000, "FT4": 14_080_000, "FT8": 14_074_000},
        "30m": {"FT2": 10_144_000, "FT4": 10_140_000, "FT8": 10_136_000},
        "40m": {"FT2": 7_062_000, "FT4": 7_047_500, "FT8": 7_074_000},
        "80m": {"FT2": 3_578_000, "FT4": 3_575_000, "FT8": 3_573_000},
    }

    def __init__(self) -> None:
        """Initialize instance variables; call :meth:`run` to start the application."""
        self.args: argparse.Namespace | None = None
        self.cinfo: Callinfo | None = None
        self.psk_counter: int = 0
        self.wsjt_counter: int = 0
        self.telnet1_counter: int = 0
        self.telnet2_counter: int = 0
        self.telnet3_counter: int = 0
        self.telnet4_counter: int = 0
        self.topic: str | None = None
        self.my_grid: str = "FM05kw"
        self.window: MainWindow | None = None
        self.adif_log: ADIFLog | None = None
        self.wsjt_listener: WsjtxListener | None = None
        self._telnet1: TelnetCluster | None = None
        self._telnet2: TelnetCluster | None = None
        self._telnet3: TelnetCluster | None = None
        self._telnet4: TelnetCluster | None = None
        self._mqtt_listener: MqttListener | None = None
        self._psk_call_times: dict[str, float] = {}  # call -> last-shown epoch (reshow gate)
        self._current_adif_path: str = ''
        self._criterion: str = 'mixed'
        self._config: AppConfig = AppConfig()
        self._last_wsjt_heartbeat: float = 0.0  # epoch of most recent HB from WSJT-X
        self._rig_freq_khz: float = 0.0   # latest RX freq from the active rig-control backend (GIL-safe)
        self._rig_poll_stop_event: threading.Event = threading.Event()
        self._rig_poll_thread: threading.Thread | None = None
        self._fcc_build_thread: threading.Thread | None = None
        self._fcc_status_message: str = ''  # written by worker, read by timer tick

    # -- radio control --------------------------------------------------------

    def _qsy_rigctld(self, band: str) -> None:
        """Set radio frequency via rigctld (localhost:port) for the given band."""
        band_freqs = self.freqs.get(band, {})
        if not band_freqs:
            print(f"rigctld: no frequency mapping for {band!r}")
            return
        mode = (getattr(self.args, 'mode', None) or 'FT8').upper()
        if mode in ('FC', 'FCS'):
            mode = 'FT8'
        elif mode == 'CS':
            mode = 'CW'

    def _rig_backend_settings(
        self,
    ) -> tuple[type, Callable[[str, int], bool], str, int, float, float]:
        """Return connection settings for the active rig-control backend.

        Returns
        -------
        tuple
            ``(client_cls, is_available_fn, host, port, timeout, verify_delay)``
            for whichever backend :attr:`~appconfig.AppConfig.rig_backend`
            selects.  ``client_cls`` exposes ``get_rx_freq_khz()`` and
            ``set_freq_and_mode(freq_khz, mode, verify_delay=...)``, matching
            interfaces shared by :class:`~commander_client.CommanderClient`
            and :class:`~rigctld_client.RigctldClient`.
        """
        if self._config.rig_backend == 'rigctld':
            return (
                RigctldClient, rigctld_available,
                self._config.rigctld_host, self._config.rigctld_port,
                self._config.rigctld_timeout, self._config.rigctld_verify_delay,
            )
        return (
            CommanderClient, commander_available,
            self._config.commander_host, self._config.commander_port,
            self._config.commander_timeout, self._config.commander_verify_delay,
        )

    def _digital_mode_str(self) -> str:
        """Return the active backend's mode string for USB-data (FT8/FT4/FT2) QSY."""
        return 'PKTUSB' if self._config.rig_backend == 'rigctld' else 'DATA-U'

    # -- helpers --------------------------------------------------------------

    def get_base_freq(self, band: str, mode: str) -> int:
        """Return the standard dial frequency in Hz for a band/mode combination.

        Parameters
        ----------
        band : str
            Band string (e.g. ``'20m'``).
        mode : str
            Mode string (e.g. ``'FT8'``).

        Returns
        -------
        int
            Dial frequency in Hz, or ``0`` when the combination is not in
            :attr:`freqs`.
        """
        if band not in self.freqs:
            return 0
        if mode not in self.freqs[band]:
            return 0
        return self.freqs[band][mode]

    def get_freq_offset(self, freq: int, band: str, mode: str) -> int:
        """Return the audio frequency offset (DF) relative to the standard dial frequency.

        Parameters
        ----------
        freq : int
            Absolute frequency in Hz (the ``f`` field from the PSK Reporter
            MQTT payload).
        band : str
            Band string (e.g. ``'20m'``).
        mode : str
            Mode string (e.g. ``'FT8'``).

        Returns
        -------
        int
            DF in Hz — positive means above the standard frequency.  Returns
            ``freq`` unchanged when ``band``/``mode`` are not in :attr:`freqs`
            (base frequency is 0).
        """
        return freq - self.get_base_freq(band, mode)

    def get_country_text(self, call: str) -> str:
        """Look up the country/territory name for a callsign.

        Parameters
        ----------
        call : str
            Amateur radio callsign.

        Returns
        -------
        str
            Country or territory name (e.g. ``'United States'``), or
            ``'Unknown'`` when the callsign cannot be resolved.
        """
        if self.cinfo is None:
            return "Unknown"
        try:
            if self.cinfo.check_if_mm(call):
                return "Maritime Mobile"
            if self.cinfo.check_if_am(call):
                return "Aeronautical Mobile"
            info = self.cinfo.get_all(call)
            return correct_entity(call, info).get("country", "Unknown")
        except Exception:
            return "Unknown"

    def get_dxcc(self, call: str) -> int:
        """Return the ADIF DXCC entity number for a callsign.

        Parameters
        ----------
        call : str
            Amateur radio callsign.

        Returns
        -------
        int
            ADIF DXCC entity number, or ``-1`` when the lookup fails.
        """
        if self.cinfo is None:
            return -1
        try:
            if self.cinfo.check_if_mm(call) or self.cinfo.check_if_am(call):
                return -1
            info = self.cinfo.get_all(call)
            return correct_entity(call, info)["adif"]
        except Exception:
            return -1

    def build_topic(self) -> str:
        """Build the PSK Reporter MQTT subscription topic string from current filter settings.

        The topic uses MQTT wildcards (``+`` = any single level, ``#`` = any
        subtree) so the broker delivers only spots that match the configured
        band, mode, and callsign.

        Returns
        -------
        str
            MQTT topic in the PSK Reporter v2 filter format:
            ``pskr/filter/v2/{band}/{mode}/+/+/+/+/+/{call}/#``.
        """
        assert self.args is not None
        band = self.args.band if self.args.band else "+"
        mode = self.args.mode.upper() if self.args.mode else "+"
        if mode in ["FC", "CS", "FCS"]:
            mode = "+"
        call = self.args.call.upper() if self.args.call else "+"
        return f"pskr/filter/v2/{band}/{mode}/+/+/+/+/+/{call}/#"

    # -- settings / restart (called from Qt main thread via signals) ----------

    def _apply_settings(self, settings: dict) -> None:
        # Apply a settings dict emitted by MainWindow.settings_changed.
        # Resubscribes MQTT if topic changed, clears the table if band/mode/range
        # changed, restarts or reconfigures the WSJT-X listener as needed.
        assert self.args is not None

        old_band = self.args.band
        old_mode = self.args.mode
        old_range = self.args.range

        self.args.band = settings['band']
        self.args.mode = settings['mode']
        self.args.range = settings['range']

        # Apply max spot age to table immediately
        if self.window is not None:
            self.window.set_max_spot_age(settings.get('max_spot_age', 30))

        # Rebuild MQTT topic and resubscribe if it changed
        new_topic = self.build_topic()
        if new_topic != self.topic and self._mqtt_listener is not None:
            self.topic = new_topic
            self._mqtt_listener.resubscribe(self.topic)

        # Clear table and reset counters when display-affecting params change
        if (self.args.band  != old_band  or
                self.args.mode  != old_mode  or
                self.args.range != old_range):
            self.psk_counter = 0
            self.wsjt_counter = 0
            self.telnet1_counter = 0
            self.telnet2_counter = 0
            self.telnet3_counter = 0
            self.telnet4_counter = 0
            self._psk_call_times.clear()
            if self.window is not None:
                self.window.clear_table()

        # When the band changes, QSY the radio via rigctld
        if self.args.band != old_band and self.args.band is not None:
            self._qsy_rigctld(self.args.band)

        # Auto-select criterion when switching to 6m
        if self.args.band == '6m' and self.args.band != old_band and self.window is not None:
            df = self.window.get_display_filter()
            self.window.set_criterion('was' if df == 'all' else '6m')
            

        # Start / stop WSJT-X listener.  Only do a full restart (socket rebind)
        # when the port changes; for filter/call changes, update in place to
        # avoid a race where the old socket still holds the port for up to 1 s.
        want_wsjt = self._config.wsjt_enabled
        new_filter = settings['wsjt_filter']
        new_port = self._config.wsjt_port
        new_call = self.args.call
        if want_wsjt:
            if self.wsjt_listener is None or self.wsjt_listener.port != new_port:
                if self.wsjt_listener is not None:
                    self.wsjt_listener.stop()
                self._last_wsjt_heartbeat = 0.0
                self.wsjt_listener = WsjtxListener(
                    port=new_port,
                    decode_filter=new_filter,
                    my_call=new_call,
                    on_spot=self._on_wsjt_spot,
                    on_call_busy=self._on_call_busy,
                    on_call_active=self._on_call_active,
                    on_heartbeat=self._on_wsjt_heartbeat,
                    reshow_secs=self._config.wsjt_reshow_secs,
                )
                self.wsjt_listener.start()
            else:
                old_filter = self.wsjt_listener.decode_filter
                self.wsjt_listener.decode_filter = new_filter.upper()
                self.wsjt_listener.my_call = new_call.upper() if new_call else None
                if old_filter != self.wsjt_listener.decode_filter:
                    # Filter change: reset the rate-limiting gate so spots that
                    # match the new filter re-appear even if recently seen.
                    self.wsjt_listener.reset_call_times()
                    self.wsjt_counter = 0
                    if self.window is not None:
                        self.window.clear_table()
        else:
            if self.wsjt_listener is not None:
                self.wsjt_listener.stop()
                self.wsjt_listener = None

    def _restart(self) -> None:
        """Clear the table and re-subscribe to the current topic."""
        self.psk_counter = 0
        self.wsjt_counter = 0
        self.telnet1_counter = 0
        self.telnet2_counter = 0
        self.telnet3_counter = 0
        self.telnet4_counter = 0
        self._psk_call_times.clear()
        if self.window is not None:
            self.window.clear_table()
        if self._mqtt_listener is not None and self.topic:
            self._mqtt_listener.resubscribe(self.topic)
            print(f"Restarted — subscribed to: {self.topic}")

    # -- PSK Reporter spot callback -------------------------------------------

    def _on_psk_spot(self, payload: dict) -> None:
        """Process one raw PSK Reporter JSON payload from :class:`~mqtt_listener.MqttListener`.

        Applies mode / range / geographic grid filters, performs callsign and
        DXCC lookups, and emits an enriched spot dict to the Qt spot table via
        :attr:`~main_window.MainWindow.new_spot`.

        PSK Reporter payload fields used:

        * ``t``  — Unix timestamp of the spot.
        * ``f``  — absolute frequency in Hz.
        * ``b``  — band string (e.g. ``'20m'``).
        * ``md`` — mode (e.g. ``'FT8'``).
        * ``rp`` — reported SNR in dB.
        * ``sc`` — sender callsign (DX station being heard).
        * ``sl`` — sender locator (DX station's grid square).
        * ``rc`` — reporter callsign (receiving station).
        * ``rl`` — reporter locator (receiving station's grid square).

        Parameters
        ----------
        payload : dict
            Parsed JSON dict from :class:`~mqtt_listener.MqttListener`.
            Invoked from the Paho network thread; emits via a queued Qt signal.
        """
        assert self.args is not None
        self.psk_counter += 1
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(payload['t']))
        scall = payload['sc'].replace(".", "/")

        if self.args.mode is not None:
            match self.args.mode.upper():
                case "FT8":
                    if payload['md'] != "FT8": return
                case "FT4":
                    if payload['md'] != "FT4": return
                case "FT2":
                    if payload['md'] != "FT2": return
                case "CW":
                    if payload['md'] != "CW":  return
                case "SSB":
                    if payload['md'] != "SSB": return
                case "FT":
                    if payload['md'] not in ["FT4", "FT8", "FT2"]: return
                case "FC":
                    if payload['md'] not in ["CW", "FT4", "FT8", "FT2"]: return
                case "FCS":
                    if payload['md'] not in ["CW", "FT4", "FT8", "FT2", "SSB"]: return
                case "CS":
                    if payload['md'] not in ["CW", "SSB"]: return
                case 'RTTY':
                    if payload['md'] != 'RTTY': return
                case _:
                    return

        if payload['md'] == 'CW':
            colorline = Fore.GREEN
        elif payload['md'] in ['FT4', 'FT8', 'FT2']:
            colorline = Fore.CYAN
        elif payload['md'] == 'RTTY':
            colorline = Fore.BLUE
        elif payload['md'] == 'SSB':
            colorline = Fore.MAGENTA
        else:
            colorline = Fore.YELLOW

        if self.args.call is not None and scall == self.args.call.replace(".", "/").upper():
            call = payload['rc'].replace(".", "/")
            loc = payload['rl']
            direction = "TX"
            color = colorline + "| TX"
        else:
            call = scall
            loc = payload['sl']
            direction = "RX"
            color = colorline + "| RX"

        country = self.get_country_text(call)

        freq_offset = self.get_freq_offset(payload['f'], payload['b'], payload['md'])
        try:
            distance = int(qth_distance(payload['sl'], payload['rl']))
            range_km = int(qth_distance(self.my_grid, payload['rl']))
        except Exception:
            return
        if self.args.range is not None and range_km > self.args.range:
            return

        if payload['rp'] is None:
            payload['rp'] = "N/A"

        rx_grids = self._config.rx_grid_prefixes
        if rx_grids and not any(payload['rl'].startswith(g) for g in rx_grids):
            return

        now = time.time()
        if now - self._psk_call_times.get(call, 0.0) < self._config.pskr_reshow_secs:
            return   # suppress table update; call was shown too recently
        self._psk_call_times[call] = now

        dxcc = self.get_dxcc(call)

        total = self.psk_counter + self.wsjt_counter

        if self.args.terminal:
            print(
                f"{total:06d} | {color} | {timestamp:19} | {call:10} | {loc:10} | {payload['rp']:3} dB | "
                f"{country:20} | {freq_offset:4} Hz | {distance:5} km | {payload['md']:5} | {payload['b']:4} | "
                f" {payload['rc']:10} | {payload['rl']:10} | {range_km:5}"
                f"{Style.RESET_ALL}"
            )

        if self.window is not None:
            self.window.new_spot.emit({
                "counter":      total,
                "direction":    direction,
                "timestamp":    timestamp,
                "call":         call,
                "loc":          loc,
                "rp":           payload['rp'],
                "country":      country,
                "freq_offset":  freq_offset,
                "distance":     distance,
                "md":           payload['md'],
                "b":            payload['b'],
                "rc":           payload['rc'],
                "rl":           payload['rl'],
                "range":        range_km,
                "unix_time":    payload['t'],
                "dxcc":         dxcc,
                "source":       "psk",
                "abs_freq_hz":  payload['f'],
            })

    # -- WSJT-X callback ------------------------------------------------------

    def _on_wsjt_spot(self, dx_call: str, dx_grid: str, snr: int,
                      df: int, mode: str, band: str,
                      unix_time: float, msg: str = '',
                      delta_t: float = 0.0,
                      abs_freq_hz: int = 0) -> None:
        # Called from the WsjtxListener background thread when a new decode
        # passes the RESHOW_SECS gate. No range filter is applied: we decoded
        # the signal directly so the station is reachable by definition.
        # Emits the spot dict to MainWindow.new_spot (thread-safe Qt signal).
        assert self.args is not None
        if not self._config.wsjt_show_decodes:
            return
        self.wsjt_counter += 1

        country = self.get_country_text(dx_call)

        dist_km = 0
        if dx_grid:
            try:
                dist_km = int(qth_distance(self.my_grid, dx_grid))
            except Exception:
                dist_km = 0
        # No range filter for WSJT-X spots: we decoded the signal directly,
        # so the station is reachable by definition regardless of km distance.

        dxcc  = self.get_dxcc(dx_call)
        total = self.psk_counter + self.wsjt_counter
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(unix_time))

        if self.args.terminal:
            print(
                f"{total:06d} | WSJT | {timestamp} | {dx_call:10} | "
                f"{dx_grid:6} | {snr:+3d} dB | {country:20} | {df:4} Hz | "
                f"{dist_km:5} km | {mode:5} | {band:4}"
            )

        if self.window is not None:
            self.window.new_spot.emit({
                "counter":      total,
                "direction":    "RX",
                "timestamp":    timestamp,
                "call":         dx_call,
                "loc":          dx_grid,
                "rp":           f"{snr:+d}",
                "country":      country,
                "freq_offset":  df,
                "distance":     dist_km,
                "md":           mode,
                "b":            band,
                "rc":           self.args.call or "WSJT-X",
                "rl":           self.my_grid,
                "range":        0,
                "unix_time":    unix_time,
                "dxcc":         dxcc,
                "source":       "wsjt",
                "msg":          msg,
                "delta_t":      delta_t,
                "abs_freq_hz":  abs_freq_hz,  # absolute Hz — used by BandMap
            })

    # Mode → allowed set, used in both PSK and telnet mode filters
    _MODE_FILTER: dict[str, frozenset[str]] = {
        'FT8': frozenset({'FT8'}),
        'FT4': frozenset({'FT4'}),
        'FT2': frozenset({'FT2'}),
        'CW':  frozenset({'CW'}),
        'SSB': frozenset({'SSB'}),
        'FT':  frozenset({'FT4', 'FT8', 'FT2'}),
        'FC':  frozenset({'CW', 'FT4', 'FT8', 'FT2'}),
        'FCS': frozenset({'CW', 'FT4', 'FT8', 'FT2', 'SSB'}),
        'CS':  frozenset({'CW', 'SSB'}),
        'RTTY': frozenset({'RTTY'}),
    }

    def _on_telnet_spot(self, raw: dict, index: int) -> None:
        # Massage a raw TelnetCluster spot dict and forward it to the spot table.
        # Called from a TelnetCluster background thread — only uses Qt signals
        # (thread-safe) for all GUI interactions.
        assert self.args is not None
        call = raw['call']
        freq_hz = raw['abs_freq_hz']
        mode = raw.get('md', '')

        # Apply mode filter
        if self.args.mode is not None:
            allowed = self._MODE_FILTER.get(self.args.mode.upper())
            if allowed is not None and mode not in allowed:
                if self.args.terminal:
                    print(f"telnet spot dropped: mode {mode!r} not in {self.args.mode!r} filter")
                return

        band = freq_to_band(freq_hz)
        if not band:
            if self.args.terminal:
                print(f"telnet spot dropped: out of band ({freq_hz} Hz)")
            return  # frequency outside recognized ham bands

        # Optionally hide telnet-derived spots from outside the US or Canada
        # (Settings → DX Cluster → "Only show spots from US/Canada spotters").
        spotter = raw['rc']
        spotter = spotter[:-2] if spotter.endswith('-#') else spotter
        if self._config.telnet_us_ca_spotters_only and self.get_dxcc(spotter) not in (291, 1):
            if self.args.terminal:
                print(f"telnet spot dropped: spotter {spotter} outside US/Canada")
            return

        # Apply band filter when a specific band is selected
        if self.args.band is not None and band != self.args.band:
            if self.args.terminal:
                print(f"telnet spot dropped: out of selected band ({band})")
            return

        # Look up spotter lat/lon from FCC DB and compute distance from operator.
        # Falls back to 0 (filter bypassed) when the call is not in the DB yet.
        spotter_pos = fcc_db.lookup_location(spotter)
        if spotter_pos is not None:
            op_lat, op_lon = fcc_db.grid_to_latlon(self.my_grid)
            range_km = int(fcc_db.haversine_km(op_lat, op_lon, *spotter_pos))
        else:
            range_km = 0
        if self.args.range is not None and range_km > self.args.range > 0:
            if self.args.terminal:
                print(f"telnet spot dropped: spotter {spotter} out of range ({range_km} km)")
            return

        dxcc = self.get_dxcc(call)
        country = self.get_country_text(call)
        freq_offset = self.get_freq_offset(freq_hz, band, mode)
        unix_time = raw['unix_time']
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(unix_time))

        if index == 1:
            self.telnet1_counter += 1
        elif index == 2:
            self.telnet2_counter += 1
        elif index == 3:
            self.telnet3_counter += 1
        else:
            self.telnet4_counter += 1
        total = (self.psk_counter + self.wsjt_counter
                 + self.telnet1_counter + self.telnet2_counter
                 + self.telnet3_counter + self.telnet4_counter)

        if self.args.terminal:
            print(
                f"{total:06d} | TLN{index} | {timestamp} | {call:10} | "
                f"{raw['rp']} dB | {country:20} | "
                f"{freq_hz / 1000:.1f} kHz | {mode:5} | {band:4} | "
                f"{raw['rc']:10}"
            )

        if self.window is not None:
            self.window.new_spot.emit({
                'counter':     total,
                'direction':   'RX',
                'timestamp':   timestamp,
                'call':        call,
                'loc':         '',
                'rp':          raw['rp'],
                'country':     country,
                'freq_offset': freq_offset,
                'distance':    0,
                'md':          mode,
                'b':           band,
                'rc':          raw['rc'],
                'rl':          raw.get('rl', ''),
                'range':       range_km,
                'unix_time':   unix_time,
                'dxcc':        dxcc,
                'source':      f'telnet{index}',
                'abs_freq_hz': freq_hz,
                'msg':         raw.get('comment', ''),
                'delta_t':     0.0,
            })

    _WSJT_DIGITAL = frozenset({'FT8', 'FT4', 'FT2'})

    def _on_spot_activated(self, spot_data: dict) -> None:
        # Route a double-clicked spot to the appropriate radio-control path.
        # Mode drives routing; source tag (psk / wsjt / telnet / …) does not.
        mode = spot_data.get('md', '').upper()
        if mode in self._WSJT_DIGITAL:
            self._activate_digital_spot(spot_data)
        else:
            self._activate_rig_spot(spot_data)

    def _activate_digital_spot(self, spot_data: dict) -> None:
        # Route a digital spot to WSJT-X.  For non-wsjt sources, check whether
        # the station is currently visible in WSJT-X and prompt if not.
        if self.wsjt_listener is None:
            print("Double-click: WSJT-X listener not active")
            return
        call = spot_data.get('call', '')
        if spot_data.get('source') != 'wsjt':
            if self.wsjt_listener.get_latest_decode(call) is None:
                self._prompt_wsjt_not_visible(spot_data)
                return
        self._send_to_wsjt(spot_data)

    def _send_to_wsjt(self, spot_data: dict) -> None:
        # Send a spot to WSJT-X via Configure + Reply.
        # Prefers a fresh WSJT-X decode; falls back to stored spot-row values.
        if self.wsjt_listener is None:
            return
        call   = spot_data.get('call', '')
        source = spot_data.get('source', 'psk')

        # Prefer the freshest WSJT-X decode — updated every 15 s regardless
        # of the 5-minute display gate, so Reply always has exact fields.
        latest = self.wsjt_listener.get_latest_decode(call)
        low_confidence = False
        if latest:
            time_ms = latest['ms']
            snr     = latest['snr']
            df      = latest['df']
            delta_t = latest['delta_t']
            mode    = latest['mode']
            msg     = latest['msg']
            low_confidence = latest.get('low_confidence', False)
            print(f"Double-click: {call} ({source}) — fresh decode "
                  f"ms={time_ms} df={df} dt={delta_t:.2f}s "
                  f"snr={snr:+d} lc={low_confidence} msg={msg!r}")
        else:
            unix_time = spot_data.get('unix_time', 0.0)
            time_ms   = int(round((unix_time % 86400) * 1000))
            try:
                snr = int(str(spot_data.get('rp', '0')).lstrip('+'))
            except ValueError:
                snr = 0
            df      = spot_data.get('freq_offset', 0)
            delta_t = spot_data.get('delta_t', 0.0)
            mode    = spot_data.get('md', 'FT8').upper()
            msg     = spot_data.get('msg', '')
            if not msg:
                loc = spot_data.get('loc', '')
                msg = f"CQ {call} {loc[:4]}".strip() if call else ''
            print(f"Double-click: {call} ({source}) — no cached WSJT-X decode, "
                  f"using spot-row values")

        loc = spot_data.get('loc', '')

        # QSY the rig to the standard FT8/FT4/FT2 dial frequency so the rig-
        # control backend does not leave the radio on the previous CW/SSB
        # frequency.
        if self._config.rig_control_enabled:
            band = spot_data.get('b', '')
            dial_hz = self.get_base_freq(band, mode)
            if dial_hz > 0:
                dial_khz = dial_hz / 1000.0
                client_cls, avail_fn, _host, _port, _timeout, _verify_delay = (
                    self._rig_backend_settings()
                )
                digital_mode = self._digital_mode_str()
                _call = call

                def _qsy_digital() -> None:
                    try:
                        if not avail_fn(_host, _port):
                            print(f"Rig control not reachable at {_host}:{_port}")
                            return
                        client = client_cls(host=_host, port=_port, timeout=_timeout)
                        result = client.set_freq_and_mode(
                            dial_khz, digital_mode, verify_delay=_verify_delay
                        )
                        if result.success:
                            print(
                                f"Rig QSY: {_call} → {dial_khz:.3f} kHz {digital_mode} "
                                f"({mode})"
                            )
                        else:
                            print(
                                f"Rig QSY failed for {_call}: {result.errors}"
                            )
                    except Exception as exc:
                        print(f"Rig QSY exception for {_call}: {exc}")

                threading.Thread(target=_qsy_digital, daemon=True).start()

        self.wsjt_listener.highlight_call(call, bg=(255, 200, 0), fg=(0, 0, 0))
        # Configure sets DX call, Rx DF, and generates standard messages
        # without needing a band-activity match (unlike Reply).
        self.wsjt_listener.configure(
            rx_df=df, dx_call=call, dx_grid=loc,
            generate_messages=True,
        )
        # Reply additionally selects the matching row in band activity
        # (visual feedback); keep it in case it works on this WSJT-X build.
        self.wsjt_listener.reply_to_decode(
            time_ms=time_ms, snr=snr, df=df, mode=mode,
            message=msg, delta_t=delta_t, low_confidence=low_confidence,
        )

    def _prompt_wsjt_not_visible(self, spot_data: dict) -> None:
        # Ask the user whether to send a spot that isn't in the WSJT-X decoded list.
        call = spot_data.get('call', '')
        box = QMessageBox(self.window)
        box.setWindowTitle("Spot not visible in WSJT-X")
        box.setText(f"<b>{call}</b> is not currently visible in WSJT-X.")
        box.setInformativeText(
            "The station may not be audible on this band.  "
            "Send the spot to WSJT-X anyway?"
        )
        send_btn = box.addButton("Send to WSJT-X", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is send_btn:
            self._send_to_wsjt(spot_data)

    def _on_spots_expired(
        self,
        psk_removed: int,
        wsjt_removed: int,
        telnet1_removed: int,
        telnet2_removed: int,
        telnet3_removed: int,
        telnet4_removed: int,
    ) -> None:
        self.psk_counter = max(0, self.psk_counter - psk_removed)
        self.wsjt_counter = max(0, self.wsjt_counter - wsjt_removed)
        self.telnet1_counter = max(0, self.telnet1_counter - telnet1_removed)
        self.telnet2_counter = max(0, self.telnet2_counter - telnet2_removed)
        self.telnet3_counter = max(0, self.telnet3_counter - telnet3_removed)
        self.telnet4_counter = max(0, self.telnet4_counter - telnet4_removed)

    def _update_pskr_status(self) -> None:
        if self.window is None:
            return
        if not self._config.pskr_enabled:
            self.window.set_pskr_status("PSKR: disabled", ok=None)
            return
        connected = self._mqtt_listener.connected if self._mqtt_listener is not None else False
        if connected:
            self.window.set_pskr_status(f"PSKR: {CONNECTED}", ok=True)
        else:
            self.window.set_pskr_status(f"PSKR: {DISCONNECTED}", ok=None)

    def _update_telnet_status(self) -> None:
        # Poll each cluster's connected property and update the T1-T4 status bar labels.
        if self.window is None:
            return
        for index, cluster in (
            (1, self._telnet1), (2, self._telnet2),
            (3, self._telnet3), (4, self._telnet4),
        ):
            if cluster is None:
                self.window.set_telnet_status(index, f"T{index}: {DISCONNECTED}", ok=None)
            elif cluster.connected:
                self.window.set_telnet_status(index, f"T{index}: {CONNECTED}", ok=True)
            else:
                self.window.set_telnet_status(index, f"T{index}: {CONNECTING}", ok=False)

    def _on_wsjt_heartbeat(self) -> None:
        """Called from the WSJT-X listener thread on first packet and each incoming Heartbeat."""
        self._last_wsjt_heartbeat = time.time()

    def _update_wsjt_status(self) -> None:
        """Polled from the main-thread QTimer; updates the status bar WSJT-X indicator."""
        if self.window is None or self.wsjt_listener is None:
            return
        now = time.time()
        elapsed_hb = now - self._last_wsjt_heartbeat
        if self._last_wsjt_heartbeat == 0.0:
            self.window.set_wsjt_status(f"WSJT-X: {CONNECTING}", ok=None)
        elif elapsed_hb >= 45:
            self.window.set_wsjt_status(
                f"WSJT-X: no signal ({int(elapsed_hb)}s)", ok=False
            )
        else:
            # Connected — check whether any Decode packets have arrived recently.
            no_spot_secs = self._config.wsjt_no_spot_mins * 60
            last_dec = self.wsjt_listener.last_decode_time
            if last_dec == 0.0 or (now - last_dec) > no_spot_secs:
                self.window.set_wsjt_status(f"WSJT-X: {CONNECTED}", ok='warn')
            else:
                self.window.set_wsjt_status(f"WSJT-X: {CONNECTED}", ok=True)

    def _on_call_busy(self, call: str) -> None:
        """Called from WSJT-X listener thread when a CQ caller enters a QSO."""
        if self.window is not None:
            self.window.call_busy.emit(call)

    def _on_call_active(self, call: str) -> None:
        """Called from WSJT-X listener thread when a station resumes calling CQ."""
        if self.window is not None:
            self.window.call_active.emit(call)

    def _activate_rig_spot(self, spot_data: dict) -> None:
        # QSY the rig to a CW/SSB spot via the active rig-control backend.
        # Runs in a daemon thread so the post-set verify delay (~0.75 s) does
        # not block the Qt main thread.
        # For PSK/telnet CW and SSB spots, freq_offset is the absolute
        # frequency in Hz (self.freqs has no entry for these modes, so
        # get_freq_offset returns payload['f'] - 0 = payload['f']).
        if not self._config.rig_control_enabled:
            print(f"Double-click: rig control not enabled for "
                  f"{spot_data.get('call')} ({spot_data.get('md')})")
            return
        psk_mode = spot_data.get('md', '').upper()
        freq_khz = spot_data.get('freq_offset', 0) / 1000.0
        rig_mode = self._psk_mode_to_rig_mode(psk_mode, freq_khz)
        if rig_mode is None:
            print(f"Double-click: no rig mode mapping for {psk_mode!r}")
            return
        client_cls, avail_fn, host, port, timeout, verify_delay = (
            self._rig_backend_settings()
        )
        call = spot_data.get('call', '')

        def _run() -> None:
            if not avail_fn(host, port):
                print(f"Rig control not reachable at {host}:{port}")
                return
            client = client_cls(host=host, port=port, timeout=timeout)
            result = client.set_freq_and_mode(freq_khz, rig_mode,
                                              verify_delay=verify_delay)
            if result.success:
                print(f"Rig QSY: {call} → {freq_khz:.3f} kHz {rig_mode}")
            else:
                print(f"Rig QSY failed for {call}: {result.errors}")

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _psk_mode_to_rig_mode(psk_mode: str, freq_khz: float) -> str | None:
        # Map a spot-table mode string to a rig-control mode string. Shared by
        # both backends: Commander and rigctld (Hamlib) both accept the same
        # names for CW/AM/FM/LSB/USB. SSB is split into LSB (below 10 MHz /
        # 40 m and lower) and USB above. Digital modes return None — they are
        # handled via WSJT-X, not rig control.
        if psk_mode == 'CW':
            return 'CW'
        if psk_mode == 'SSB':
            return 'LSB' if freq_khz < 10_000.0 else 'USB'
        if psk_mode in ('AM', 'FM', 'LSB', 'USB'):
            return psk_mode
        return None

    @staticmethod
    def _freq_to_band(freq_khz: float) -> str | None:
        for lo, hi, band in _BAND_RANGES:
            if lo <= freq_khz <= hi:
                return band
        return None

    def _rig_poll_loop(self) -> None:
        # Background daemon thread: query the active rig-control backend for
        # RX frequency every 1 s. Writes result to _rig_freq_khz (GIL-safe
        # float); 0.0 on error.
        while not self._rig_poll_stop_event.is_set():
            if self._config.rig_control_enabled:
                client_cls, _avail, host, port, _timeout, _delay = (
                    self._rig_backend_settings()
                )
                try:
                    client = client_cls(host=host, port=port, timeout=0.15)
                    self._rig_freq_khz = client.get_rx_freq_khz()
                except OSError:
                    self._rig_freq_khz = 0.0
            else:
                self._rig_freq_khz = 0.0
            self._rig_poll_stop_event.wait(1.0)

    def _update_rig_band(self) -> None:
        # Called from the 250 ms Qt timer.  If the active rig-control backend
        # has seen a new band, update the Band parameter — which triggers the
        # normal settings-change chain (MQTT resubscribe, table clear, WAS
        # label update).
        if (not self._config.rig_control_enabled
                or not self._config.rig_track_band
                or self.window is None):
            return
        freq = self._rig_freq_khz
        if freq <= 0.0:
            return
        band = self._freq_to_band(freq)
        if band is None or band == self.args.band:
            return
        if self.args.terminal:
            print(f"Rig VFO override: reported {freq:.1f} kHz -> band {band!r} "
                  f"(replacing filter {self.args.band!r})")
        self.window._params.child('Data Filters').child('Band').setValue(band)  # noqa: SLF001

    def _effective_bandmap_band(self) -> str | None:
        # Determine the band the bandmap should display, in priority order:
        # 1. Explicit band filter (args.band) — set by user or rig-control tracking.
        # 2. Active rig-control backend's VFO frequency (if connected).
        # 3. WSJT-X dial frequency (if listener is active and tuned).
        # 4. None — bandmap shows nothing.
        if self.args is None:
            return None
        if self.args.band is not None:
            return self.args.band
        if self._rig_freq_khz > 0:
            return self._freq_to_band(self._rig_freq_khz)
        if self.wsjt_listener is not None and self.wsjt_listener.dial_freq > 0:
            return freq_to_band(self.wsjt_listener.dial_freq)
        return None

    def _on_criterion_changed(self, criterion: str) -> None:
        # Slot connected to MainWindow.criterion_changed.
        # Updates the stored criterion and asks the spot table to restyle all rows.
        self._criterion = criterion
        if self.window is not None:
            self.window.restyle_spots(self.adif_log, criterion)

    # -- entry point ----------------------------------------------------------

    def run(self) -> None:
        """Load configuration, build the Qt window, connect to MQTT, and enter the event loop.

        This is the single entry point for the application.  It performs, in
        order:

        1. Load persisted :class:`~appconfig.AppConfig` from the config file.
        2. Parse command-line arguments (CLI values override config values).
        3. Initialize the pyhamtools callsign lookup library.
        4. Load the contact log (ADIF or RumLogNG).
        5. Create the Qt application and :class:`~main_window.MainWindow`.
        6. Connect signals between the window and this controller.
        7. Connect to the PSK Reporter MQTT broker and start the network loop.
        8. Optionally start the :class:`~wsjtx_listener.WsjtxListener`.
        9. Register a cleanup callback that saves config and tears down sockets
           when the Qt event loop exits.
        10. Enter ``app.exec()`` (blocks until the window is closed).
        """
        # Load persisted config first so argparse defaults reflect saved state.
        _first_run = not config_path().exists()
        self._config = load_config()
        cfg = self._config

        parser = argparse.ArgumentParser()
        parser.add_argument("-c", "--call", required=False, default=None,
                            help="Call sign")
        parser.add_argument("-b", "--band", required=False,
                            choices=["2m", "6m", "10m", "15m", "17m", "20m", "30m", "40m", "80m", "160m"],
                            help="Band (e.g. 20m)")
        parser.add_argument("-m", "--mode", required=False,
                            choices=["FT8", "FT4", "FT2", "CW", "SSB", "FC", "FCS", "CS", "FT", "RTTY"],
                            help="Mode (e.g. FT8)")
        parser.add_argument("-r", "--range", required=False, type=int,
                            help="Maximum rx station range from my grid in km (0 = no limit)")
        parser.add_argument("-t", "--terminal", action="store_true", default=False,
                            help="Print spots to the terminal (default: off)")
        parser.add_argument("-W", "--wsjt", action="store_true", default=None,
                            help="Enable WSJT-X UDP listener")
        parser.add_argument("-wf", "--wsjt-filter", choices=["CQ", "all", "me"],
                            help="WSJT-X decode filter")
        parser.add_argument("--wsjt-port", type=int,
                            help="WSJT-X UDP port")
        parser.add_argument("--cty-plist", required=False, default=None,
                            help="Local filename of CTY Plist from country-code")
        # macOS passes -psn_XXXXXXXX when launching as a .app bundle; strip it.
        argv = [a for a in sys.argv[1:] if not a.startswith('-psn')]
        cli = parser.parse_args(argv)

        # Merge: CLI wins over config when explicitly provided.
        cli.my_grid = cfg.my_grid
        cli.max_spot_age = cfg.max_spot_age
        cli.band = cli.band or cfg.band
        cli.mode = cli.mode or cfg.mode
        cli.range = cli.range if cli.range is not None else cfg.max_range
        cli.wsjt = cli.wsjt if cli.wsjt is not None else cfg.wsjt_enabled
        cli.wsjt_filter = cli.wsjt_filter or cfg.decode_filter
        cli.wsjt_port = cli.wsjt_port if cli.wsjt_port is not None else cfg.wsjt_port
        self.args = cli
        self.my_grid    = cfg.my_grid
        self._criterion = cfg.criterion
        # print(self.args)

        print("Loading lookup directory")
        cty_path = self.args.cty_plist or str(cty_cache.ensure_cty())
        lookuplib = LookupLib(lookuptype="countryfile", filename=cty_path)
        self.cinfo = Callinfo(lookuplib)

        if self.args.call is not None:
            if not self.cinfo.is_valid_callsign(self.args.call.replace(".", "/")):
                print(f"Error: Callsign {self.args.call} is not valid!")
                sys.exit(1)

        age = fcc_db.fcc_db_age_days()
        count = fcc_db.fcc_db_entry_count()
        if age is None:
            print("FCC DB: not found — use Settings → Update FCC Database to download")
        else:
            print(f"FCC DB: {count:,} callsigns, age {age:.1f} days")

        self.topic = self.build_topic()

        adif_path = cfg.adif_path
        self._current_adif_path = adif_path
        self.adif_log = self._load_log(cfg)

        app = QApplication(sys.argv)
        app.setStyle('Fusion')
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        # For PyInstaller bundles (sys.frozen=True) the dock icon comes from the
        # bundle's ICNS via Info.plist.  Calling setWindowIcon() here would
        # override it with a PyQt6-created NSImage sized incorrectly by macOS.
        # Only set the icon programmatically for source-tree runs.
        if not getattr(sys, "frozen", False):
            app.setWindowIcon(make_app_icon())

        self.window = MainWindow(
            initial_args=self.args,
            initial_criterion=cfg.criterion,
            initial_display_filter=cfg.display_filter,
        )
        self.window.settings_changed.connect(self._apply_settings)
        self.window.restart_requested.connect(self._restart)
        self.window.criterion_changed.connect(self._on_criterion_changed)
        self.window.spot_activated.connect(self._on_spot_activated)
        self.window.settings_requested.connect(self._open_settings)
        self.window.reload_log_requested.connect(self._reload_log)
        self.window._spot_table.spots_expired.connect(self._on_spots_expired)
        self.window.restyle_spots(self.adif_log, self._criterion)
        self.window.set_max_spot_age(cfg.max_spot_age)
        self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
        self.window.set_station_info(self.args.call or '', self.my_grid)
        self.window.update_paper_only_list(self._build_paper_only_list(self.adif_log))
        self.window.show()

        if _first_run:
            QTimer.singleShot(0, self._open_settings)

        self._count_timer = QTimer()

        def _tick():
            assert self.window is not None
            self.window.update_counts(
                self.psk_counter, self.wsjt_counter,
                self.telnet1_counter, self.telnet2_counter,
                self.telnet3_counter, self.telnet4_counter,
            )
            self._update_wsjt_status()
            self._update_pskr_status()
            self._update_telnet_status()
            self._update_rig_band()
            self.window.set_bandmap_band(self._effective_bandmap_band())
            if self._fcc_status_message:
                self.window.statusBar().showMessage(self._fcc_status_message, 6000)
                self._fcc_status_message = ''

        self._count_timer.timeout.connect(_tick)
        self._count_timer.start(250)

        self._rig_poll_thread = threading.Thread(
            target=self._rig_poll_loop,
            daemon=True,
            name='rig-band-poll',
        )
        self._rig_poll_thread.start()

        if cfg.pskr_enabled:
            self._mqtt_listener = MqttListener(
                host=cfg.pskr_host,
                port=cfg.pskr_port,
                topic=self.topic,
                on_spot=self._on_psk_spot,
            )
            self._mqtt_listener.start()

        if self.args.wsjt:
            self.wsjt_listener = WsjtxListener(
                port=self.args.wsjt_port,
                decode_filter=self.args.wsjt_filter,
                my_call=self.args.call,
                on_spot=self._on_wsjt_spot,
                on_call_busy=self._on_call_busy,
                on_call_active=self._on_call_active,
                on_heartbeat=self._on_wsjt_heartbeat,
                mcast_addr=cfg.udp_address,
                reshow_secs=cfg.wsjt_reshow_secs,
            )
            self.wsjt_listener.start()
            self.window.set_wsjt_status("WSJT-X: waiting…", ok=None)
        else:
            self.window.set_wsjt_status("WSJT-X: disabled", ok=None)

        # Start DX cluster telnet connections if configured
        if cfg.telnet1_enabled and cfg.telnet1_host and cfg.telnet1_callsign:
            self._telnet1 = TelnetCluster(
                cfg.telnet1_host, cfg.telnet1_port, cfg.telnet1_callsign,
                on_spot=lambda raw: self._on_telnet_spot(raw, 1),
                index=1,
            )
            self._telnet1.start()
        if cfg.telnet2_enabled and cfg.telnet2_host and cfg.telnet2_callsign:
            self._telnet2 = TelnetCluster(
                cfg.telnet2_host, cfg.telnet2_port, cfg.telnet2_callsign,
                on_spot=lambda raw: self._on_telnet_spot(raw, 2),
                index=2,
            )
            self._telnet2.start()
        if cfg.telnet3_enabled and cfg.telnet3_host and cfg.telnet3_callsign:
            self._telnet3 = TelnetCluster(
                cfg.telnet3_host, cfg.telnet3_port, cfg.telnet3_callsign,
                on_spot=lambda raw: self._on_telnet_spot(raw, 3),
                index=3,
            )
            self._telnet3.start()
        if cfg.telnet4_enabled and cfg.telnet4_host and cfg.telnet4_callsign:
            self._telnet4 = TelnetCluster(
                cfg.telnet4_host, cfg.telnet4_port, cfg.telnet4_callsign,
                on_spot=lambda raw: self._on_telnet_spot(raw, 4),
                index=4,
            )
            self._telnet4.start()

        def _save_and_cleanup():
            self._save_config()
            self._rig_poll_stop_event.set()
            if self._mqtt_listener is not None:
                self._mqtt_listener.stop()
            if self.wsjt_listener is not None:
                self.wsjt_listener.stop()
            if self._telnet1 is not None:
                self._telnet1.stop()
            if self._telnet2 is not None:
                self._telnet2.stop()
            if self._telnet3 is not None:
                self._telnet3.stop()
            if self._telnet4 is not None:
                self._telnet4.stop()

        app.aboutToQuit.connect(_save_and_cleanup)
        sys.exit(app.exec())

    @staticmethod
    def _load_log(cfg) -> 'ADIFLog | None':
        """Load the log from whichever source the config specifies (read-only)."""
        if cfg.log_source == 'rumlogng':
            print("Loading RumLogNG CloudKit database (read-only)")
            return ADIFLog.from_rumlogng(config=cfg)
        if cfg.adif_path:
            print("Loading ADIF log")
            return ADIFLog(cfg.adif_path)
        return None

    def _build_paper_only_list(self, log: 'ADIFLog | None') -> list[dict]:
        # Return country-enriched, country-sorted confirmed QSOs for paper-only DXCC entities.
        if log is None:
            return []
        entries = log.paper_only_confirmed_entries()
        result = []
        for entry in entries:
            country = self.get_country_text(entry['call'])
            result.append({**entry, 'country': country})
        result.sort(key=lambda x: (x['country'].lower(), x['call'].upper()))
        return result

    @staticmethod
    def _log_info_text(cfg, log: 'ADIFLog | None') -> str:
        if log is None:
            return "No log loaded"
        if cfg.log_source == 'rumlogng':
            source = "RUMlogNG"
        else:
            from pathlib import Path
            source = Path(cfg.adif_path).name if cfg.adif_path else "ADIF"
        total    = log.confirmed_dxcc_count
        lotw     = log.confirmed_lotw_dxcc_count
        paper    = log.confirmed_paper_only_dxcc_count
        conf_str = f"{total} DXCC confirmed  ({lotw} LoTW,  {paper} paper only)"
        return f"Log: {source}   |   {log.total_qsos:,} QSOs   |   {conf_str}"

    def _save_config(self) -> None:
        """Collect current UI state into self._config and write to disk."""
        if self.window is None or self.args is None:
            return
        s = self.window._collect_settings()  # noqa: SLF001
        cfg = self._config
        cfg.my_grid = self.my_grid
        cfg.band = s.get('band') or cfg.band
        cfg.mode = s.get('mode', cfg.mode)
        cfg.decode_filter = s.get('wsjt_filter', cfg.decode_filter)
        cfg.max_range = s.get('range') or 0
        cfg.max_spot_age = s.get('max_spot_age', cfg.max_spot_age)
        cfg.criterion = self.window.get_criterion()
        cfg.display_filter = self.window.get_display_filter()
        save_config(cfg)
        print(f"Config saved to {config_path()}")

    def _reload_log(self) -> None:
        """Re-parse the current log source and refresh all spot colours."""
        if self.window is None:
            return
        cfg = self._config
        self.adif_log = self._load_log(cfg)
        self.window.restyle_spots(self.adif_log, self._criterion)
        self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
        self.window.update_paper_only_list(self._build_paper_only_list(self.adif_log))
        src = 'RumLogNG' if cfg.log_source == 'rumlogng' else cfg.adif_path
        print(f"Log reloaded: {src}")

    def _reinit_cinfo(self) -> None:
        # Reload LookupLib from the (just-refreshed) cache file.
        cty_path = str(cty_cache.cty_plist_path())
        lookuplib = LookupLib(lookuptype="countryfile", filename=cty_path)
        self.cinfo = Callinfo(lookuplib)
        print(f"CTY reloaded: {cty_path}")

    def _start_fcc_update(self) -> None:
        # Start a background FCC database build if one is not already running.
        if self._fcc_build_thread is not None and self._fcc_build_thread.is_alive():
            return
        if self.window is not None:
            self.window.statusBar().showMessage(
                "FCC database download started — check console for progress …"
            )
        self._fcc_build_thread = threading.Thread(
            target=self._fcc_build_worker,
            daemon=True,
            name='fcc-db-build',
        )
        self._fcc_build_thread.start()

    def _fcc_build_worker(self) -> None:
        # Run in a daemon thread; sets _fcc_status_message when done (GIL-safe).
        try:
            fcc_db.build_fcc_db(progress_cb=lambda msg: print(msg))
            count = fcc_db.fcc_db_entry_count() or 0
            self._fcc_status_message = f"FCC database updated: {count:,} callsigns."
        except Exception as exc:
            self._fcc_status_message = f"FCC database update failed: {exc}"

    def _open_settings(self) -> None:
        """Open the Settings dialog; apply changes immediately where possible."""
        if self.window is None:
            return
        assert self.args is not None
        cfg = self._config
        dlg = SettingsDialog(
            log_source=cfg.log_source,
            adif_path=self._current_adif_path,
            udp_address=cfg.udp_address,
            udp_port=cfg.udp_port,
            my_grid=self.my_grid,
            rx_grid_prefixes=cfg.rx_grid_prefixes,
            wsjt_reshow_secs=cfg.wsjt_reshow_secs,
            wsjt_no_spot_mins=cfg.wsjt_no_spot_mins,
            wsjt_show_decodes=cfg.wsjt_show_decodes,
            rig_control_enabled=cfg.rig_control_enabled,
            rig_track_band=cfg.rig_track_band,
            rig_backend=cfg.rig_backend,
            commander_port=cfg.commander_port,
            commander_timeout=cfg.commander_timeout,
            commander_verify_delay=cfg.commander_verify_delay,
            rigctld_port=cfg.rigctld_port,
            rigctld_timeout=cfg.rigctld_timeout,
            rigctld_verify_delay=cfg.rigctld_verify_delay,
            telnet1_enabled=cfg.telnet1_enabled,
            telnet1_host=cfg.telnet1_host,
            telnet1_port=cfg.telnet1_port,
            telnet1_callsign=cfg.telnet1_callsign,
            telnet2_enabled=cfg.telnet2_enabled,
            telnet2_host=cfg.telnet2_host,
            telnet2_port=cfg.telnet2_port,
            telnet2_callsign=cfg.telnet2_callsign,
            telnet3_enabled=cfg.telnet3_enabled,
            telnet3_host=cfg.telnet3_host,
            telnet3_port=cfg.telnet3_port,
            telnet3_callsign=cfg.telnet3_callsign,
            telnet4_enabled=cfg.telnet4_enabled,
            telnet4_host=cfg.telnet4_host,
            telnet4_port=cfg.telnet4_port,
            telnet4_callsign=cfg.telnet4_callsign,
            telnet_us_ca_spotters_only=cfg.telnet_us_ca_spotters_only,
            pskr_enabled=cfg.pskr_enabled,
            pskr_host=cfg.pskr_host,
            pskr_port=cfg.pskr_port,
            pskr_service_name=cfg.pskr_service_name,
            pskr_reshow_secs=cfg.pskr_reshow_secs,
            cty_path=str(cty_cache.cty_plist_path()),
            parent=self.window,
        )
        dlg.fcc_update_requested.connect(self._start_fcc_update)
        if dlg.exec() != SettingsDialog.DialogCode.Accepted:
            return

        if dlg.cty_refreshed:
            self._reinit_cinfo()

        cfg.udp_address = dlg.udp_address
        cfg.udp_port = dlg.udp_port
        self.my_grid = dlg.my_grid
        cfg.my_grid = dlg.my_grid
        self.window.set_station_info(self.args.call or '', self.my_grid)
        cfg.rx_grid_prefixes = dlg.rx_grid_prefixes
        cfg.wsjt_reshow_secs = dlg.wsjt_reshow_secs
        cfg.wsjt_no_spot_mins = dlg.wsjt_no_spot_mins
        cfg.wsjt_show_decodes = dlg.wsjt_show_decodes
        cfg.rig_control_enabled = dlg.rig_control_enabled
        cfg.rig_track_band = dlg.rig_track_band
        cfg.rig_backend = dlg.rig_backend
        cfg.commander_port = dlg.commander_port
        cfg.commander_timeout = dlg.commander_timeout
        cfg.commander_verify_delay = dlg.commander_verify_delay
        cfg.rigctld_port = dlg.rigctld_port
        cfg.rigctld_timeout = dlg.rigctld_timeout
        cfg.rigctld_verify_delay = dlg.rigctld_verify_delay

        # Restart telnet clusters if any of their settings changed
        t1_changed = (
            cfg.telnet1_enabled != dlg.telnet1_enabled
            or cfg.telnet1_host != dlg.telnet1_host
            or cfg.telnet1_port != dlg.telnet1_port
            or cfg.telnet1_callsign != dlg.telnet1_callsign
        )
        cfg.telnet1_enabled = dlg.telnet1_enabled
        cfg.telnet1_host = dlg.telnet1_host
        cfg.telnet1_port = dlg.telnet1_port
        cfg.telnet1_callsign = dlg.telnet1_callsign
        if t1_changed:
            if self._telnet1 is not None:
                self._telnet1.stop()
            self._telnet1 = None
            if cfg.telnet1_enabled and cfg.telnet1_host and cfg.telnet1_callsign:
                self._telnet1 = TelnetCluster(
                    cfg.telnet1_host, cfg.telnet1_port, cfg.telnet1_callsign,
                    on_spot=lambda raw: self._on_telnet_spot(raw, 1),
                    index=1,
                )
                self._telnet1.start()

        t2_changed = (
            cfg.telnet2_enabled != dlg.telnet2_enabled
            or cfg.telnet2_host != dlg.telnet2_host
            or cfg.telnet2_port != dlg.telnet2_port
            or cfg.telnet2_callsign != dlg.telnet2_callsign
        )
        cfg.telnet2_enabled = dlg.telnet2_enabled
        cfg.telnet2_host = dlg.telnet2_host
        cfg.telnet2_port = dlg.telnet2_port
        cfg.telnet2_callsign = dlg.telnet2_callsign
        if t2_changed:
            if self._telnet2 is not None:
                self._telnet2.stop()
            self._telnet2 = None
            if cfg.telnet2_enabled and cfg.telnet2_host and cfg.telnet2_callsign:
                self._telnet2 = TelnetCluster(
                    cfg.telnet2_host, cfg.telnet2_port, cfg.telnet2_callsign,
                    on_spot=lambda raw: self._on_telnet_spot(raw, 2),
                    index=2,
                )
                self._telnet2.start()

        t3_changed = (
            cfg.telnet3_enabled != dlg.telnet3_enabled
            or cfg.telnet3_host != dlg.telnet3_host
            or cfg.telnet3_port != dlg.telnet3_port
            or cfg.telnet3_callsign != dlg.telnet3_callsign
        )
        cfg.telnet3_enabled = dlg.telnet3_enabled
        cfg.telnet3_host = dlg.telnet3_host
        cfg.telnet3_port = dlg.telnet3_port
        cfg.telnet3_callsign = dlg.telnet3_callsign
        if t3_changed:
            if self._telnet3 is not None:
                self._telnet3.stop()
            self._telnet3 = None
            if cfg.telnet3_enabled and cfg.telnet3_host and cfg.telnet3_callsign:
                self._telnet3 = TelnetCluster(
                    cfg.telnet3_host, cfg.telnet3_port, cfg.telnet3_callsign,
                    on_spot=lambda raw: self._on_telnet_spot(raw, 3),
                    index=3,
                )
                self._telnet3.start()

        t4_changed = (
            cfg.telnet4_enabled != dlg.telnet4_enabled
            or cfg.telnet4_host != dlg.telnet4_host
            or cfg.telnet4_port != dlg.telnet4_port
            or cfg.telnet4_callsign != dlg.telnet4_callsign
        )
        cfg.telnet4_enabled = dlg.telnet4_enabled
        cfg.telnet4_host = dlg.telnet4_host
        cfg.telnet4_port = dlg.telnet4_port
        cfg.telnet4_callsign = dlg.telnet4_callsign
        if t4_changed:
            if self._telnet4 is not None:
                self._telnet4.stop()
            self._telnet4 = None
            if cfg.telnet4_enabled and cfg.telnet4_host and cfg.telnet4_callsign:
                self._telnet4 = TelnetCluster(
                    cfg.telnet4_host, cfg.telnet4_port, cfg.telnet4_callsign,
                    on_spot=lambda raw: self._on_telnet_spot(raw, 4),
                    index=4,
                )
                self._telnet4.start()

        # No reconnect needed — this only affects per-spot filtering in _on_telnet_spot.
        cfg.telnet_us_ca_spotters_only = dlg.telnet_us_ca_spotters_only

        # Restart the PSK Reporter MQTT connection if any of its settings changed
        pskr_changed = (
            cfg.pskr_enabled != dlg.pskr_enabled
            or cfg.pskr_host != dlg.pskr_host
            or cfg.pskr_port != dlg.pskr_port
        )
        cfg.pskr_enabled = dlg.pskr_enabled
        cfg.pskr_host = dlg.pskr_host
        cfg.pskr_port = dlg.pskr_port
        cfg.pskr_service_name = dlg.pskr_service_name
        cfg.pskr_reshow_secs = dlg.pskr_reshow_secs
        if pskr_changed:
            if self._mqtt_listener is not None:
                self._mqtt_listener.stop()
            self._mqtt_listener = None
            if cfg.pskr_enabled:
                assert self.topic is not None
                self._mqtt_listener = MqttListener(
                    host=cfg.pskr_host,
                    port=cfg.pskr_port,
                    topic=self.topic,
                    on_spot=self._on_psk_spot,
                )
                self._mqtt_listener.start()

        new_source = dlg.log_source
        new_adif = dlg.adif_path
        source_changed = new_source != cfg.log_source
        adif_changed = new_adif != self._current_adif_path

        if source_changed or adif_changed:
            cfg.log_source = new_source
            cfg.adif_path = new_adif
            self._current_adif_path = new_adif
            self.adif_log = self._load_log(cfg)
            self.window.restyle_spots(self.adif_log, self._criterion)
            self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
            self.window.update_paper_only_list(self._build_paper_only_list(self.adif_log))
            src_label = 'RumLogNG' if new_source == 'rumlogng' else f'ADIF: {new_adif}'
            print(f"Log source changed → {src_label}")


def main() -> None:
    """Application entry point registered in ``pyproject.toml``.

    Creates a :class:`DXSpotter` instance and calls :meth:`~DXSpotter.run`.
    """
    DXSpotter().run()


if __name__ == "__main__":
    main()
