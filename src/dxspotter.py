"""Main controller for DX Spotter.

This module wires together the MQTT connection to PSK Reporter, the optional
WSJT-X UDP listener, the Qt GUI (:class:`~main_window.MainWindow`), and the
ADIF / RumLogNG contact log.  Application entry point is :func:`main`.
"""
import argparse
import json
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt
from colorama import Fore, Style
from pyhamtools import LookupLib, Callinfo
from pyhamtools.locator import calculate_distance as qth_distance

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from adif_log import ADIFLog
from appconfig import AppConfig, config_path, load_config, save_config
from commander_client import CommanderClient, is_available as commander_available
from main_window import MainWindow, make_app_icon
from settings_dialog import SettingsDialog
from wsjtx_listener import WsjtxListener


class DXSpotter:
    """Top-level application controller for DX Spotter.

    Owns the MQTT client (PSK Reporter), an optional :class:`~wsjtx_listener.WsjtxListener`
    (WSJT-X UDP), the :class:`~main_window.MainWindow` Qt window, and the loaded
    :class:`~adif_log.ADIFLog`.  Coordinates data flow between all components:

    * Incoming PSK Reporter MQTT messages are decoded in :meth:`on_message` and
      forwarded to the spot table via :attr:`~main_window.MainWindow.new_spot`.
    * Incoming WSJT-X decodes arrive on a background thread via
      :meth:`_on_wsjt_spot` and are forwarded to the same signal.
    * Settings changes from the GUI are applied live in :meth:`_apply_settings`.
    * Double-clicks on the spot table trigger :meth:`_on_spot_activated`, which
      sends a Reply / Configure message back to WSJT-X.

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
        Number of PSK Reporter spots received in the current session.
    wsjt_counter : int
        Number of WSJT-X spots received in the current session.
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
        """Initialise instance variables; call :meth:`run` to start the application."""
        self.args: argparse.Namespace | None = None
        self.cinfo: Callinfo | None = None
        self.psk_counter: int = 0
        self.wsjt_counter: int = 0
        self.topic: str | None = None
        self.my_grid: str = "FM05kw"
        self.window: MainWindow | None = None
        self.adif_log: ADIFLog | None = None
        self.wsjt_listener: WsjtxListener | None = None
        self._mqtt_client: mqtt.Client | None = None
        self._current_adif_path: str = ''
        self._criterion: str = 'mixed'
        self._config: AppConfig = AppConfig()
        self._last_wsjt_heartbeat: float = 0.0  # epoch of most recent HB from WSJT-X
        self._mqtt_connected: bool = False

    # -- radio control --------------------------------------------------------

    def _qsy_rigctld(self, band: str) -> None:
        """Set radio frequency via rigctld (localhost:4532) for the given band."""
        band_freqs = self.freqs.get(band, {})
        if not band_freqs:
            print(f"rigctld: no frequency mapping for {band!r}")
            return
        mode = (getattr(self.args, 'mode', None) or 'FT8').upper()
        if mode in ('FC', 'FCS'):
            mode = 'FT8'
        elif mode == 'CS':
            mode = 'CW'

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
        try:
            return self.cinfo.get_country_name(call)  # type: ignore[union-attr]
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
        try:
            return self.cinfo.get_all(call)['adif']  # type: ignore[union-attr]
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
        # Resubscribes MQTT if topic changed, reloads ADIF if path changed,
        # clears the table if band/mode/range changed, restarts or reconfigures
        # the WSJT-X listener as needed.
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
        if new_topic != self.topic and self._mqtt_client is not None:
            if self.topic:
                self._mqtt_client.unsubscribe(self.topic)
            self.topic = new_topic
            self._mqtt_client.subscribe(self.topic)
            print(f"Resubscribed to: {self.topic}")

        # Reload ADIF if the path changed
        new_adif = settings['adif_path']
        if new_adif != self._current_adif_path:
            self._current_adif_path = new_adif
            print(f"Reloading ADIF: {new_adif}")
            self.adif_log = ADIFLog(new_adif)

        # Clear table and reset counters when display-affecting params change
        if (self.args.band  != old_band  or
                self.args.mode  != old_mode  or
                self.args.range != old_range):
            self.psk_counter = 0
            self.wsjt_counter = 0
            if self.window is not None:
                self.window.clear_table()

        # When the band changes, QSY the radio via rigctld
        if self.args.band != old_band and self.args.band is not None:
            self._qsy_rigctld(self.args.band)

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
        self.psk_counter  = 0
        self.wsjt_counter = 0
        if self.window is not None:
            self.window.clear_table()
        if self._mqtt_client is not None and self.topic:
            self._mqtt_client.unsubscribe(self.topic)
            self._mqtt_client.subscribe(self.topic)
            print(f"Restarted — subscribed to: {self.topic}")

    # -- MQTT callbacks -------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc, properties) -> None:
        """MQTT callback fired when the broker connection is established.

        Subscribes to the current topic and sets the connected flag so the
        status bar indicator turns green on the next timer tick.

        Parameters
        ----------
        client : mqtt.Client
            The Paho MQTT client instance.
        userdata : object
            User data (unused).
        flags : dict
            Connection flags from the broker.
        rc : int
            Connection result code (0 = success).
        properties : mqtt.Properties
            MQTT v5 properties (unused for v3.1.1).
        """
        self._mqtt_connected = True
        print("Connected with result code " + str(rc))
        print(f"Subscribing to topic: {self.topic}")
        client.subscribe(self.topic)

    def on_disconnect(self, client, userdata, flags, rc, properties) -> None:
        """MQTT callback fired when the broker connection is lost or closed.

        Clears the connected flag so the status bar indicator changes colour
        on the next timer tick.

        Parameters
        ----------
        client : mqtt.Client
            The Paho MQTT client instance.
        userdata : object
            User data (unused).
        flags : dict
            Disconnect flags.
        rc : int
            Disconnect result code (0 = clean disconnect).
        properties : mqtt.Properties
            MQTT v5 properties (unused for v3.1.1).
        """
        self._mqtt_connected = False
        print(f"MQTT disconnected (rc={rc})")

    def on_message(self, client, userdata, msg) -> None:
        """MQTT callback fired for each incoming PSK Reporter spot message.

        Decodes the JSON payload, applies mode / range / geographic grid
        filters, performs callsign lookup, and emits the spot to the Qt spot
        table via :attr:`~main_window.MainWindow.new_spot`.

        The geographic filter (``rx_grids``) currently restricts spots to
        those reported by stations whose Maidenhead grid square starts with one
        of ``['FM', 'FN', 'FL', 'EL', 'EN', 'EM']`` — US East Coast and
        Southeast grid prefixes.

        PSK Reporter MQTT payload fields used:

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
        client : mqtt.Client
            The Paho MQTT client instance (unused inside the callback).
        userdata : object
            User data (unused).
        msg : mqtt.MQTTMessage
            Incoming MQTT message; ``msg.payload`` is UTF-8 JSON.
        """
        assert self.args is not None
        try:
            payload = json.loads(msg.payload)
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
                    case _:
                        return

            if payload['md'] == 'CW':
                colorline = Fore.GREEN
            elif payload['md'] in ['FT4', 'FT8', 'FT2']:
                colorline = Fore.CYAN
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
                    "counter":     total,
                    "direction":   direction,
                    "timestamp":   timestamp,
                    "call":        call,
                    "loc":         loc,
                    "rp":          payload['rp'],
                    "country":     country,
                    "freq_offset": freq_offset,
                    "distance":    distance,
                    "md":          payload['md'],
                    "b":           payload['b'],
                    "rc":          payload['rc'],
                    "rl":          payload['rl'],
                    "range":       range_km,
                    "unix_time":   payload['t'],
                    "dxcc":        dxcc,
                    "source":      "psk",
                })

        except json.JSONDecodeError as e:
            print("Error processing message:", str(e))

    # -- WSJT-X callback ------------------------------------------------------

    def _on_wsjt_spot(self, dx_call: str, dx_grid: str, snr: int,
                      df: int, mode: str, band: str,
                      unix_time: float, msg: str = '',
                      delta_t: float = 0.0) -> None:
        # Called from the WsjtxListener background thread when a new decode
        # passes the RESHOW_SECS gate. No range filter is applied: we decoded
        # the signal directly so the station is reachable by definition.
        # Emits the spot dict to MainWindow.new_spot (thread-safe Qt signal).
        assert self.args is not None
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
                "counter":     total,
                "direction":   "RX",
                "timestamp":   timestamp,
                "call":        dx_call,
                "loc":         dx_grid,
                "rp":          f"{snr:+d}",
                "country":     country,
                "freq_offset": df,
                "distance":    dist_km,
                "md":          mode,
                "b":           band,
                "rc":          self.args.call or "WSJT-X",
                "rl":          self.my_grid,
                "range":       0,
                "unix_time":   unix_time,
                "dxcc":        dxcc,
                "source":      "wsjt",
                "msg":         msg,
                "delta_t":     delta_t,
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

    def _on_spots_expired(self, psk_removed: int, wsjt_removed: int) -> None:
        self.psk_counter  = max(0, self.psk_counter  - psk_removed)
        self.wsjt_counter = max(0, self.wsjt_counter - wsjt_removed)

    def _update_pskr_status(self) -> None:
        if self.window is None:
            return
        if self._mqtt_connected:
            self.window.set_pskr_status("PSKR: connected", ok=True)
        else:
            self.window.set_pskr_status("PSKR: connecting…", ok=None)

    def _on_wsjt_heartbeat(self) -> None:
        """Called from the WSJT-X listener thread on first packet and each incoming Heartbeat."""
        self._last_wsjt_heartbeat = time.time()

    def _update_wsjt_status(self) -> None:
        """Polled from the main-thread QTimer; updates the status bar WSJT-X indicator."""
        if self.window is None or self.wsjt_listener is None:
            return
        elapsed = time.time() - self._last_wsjt_heartbeat
        if self._last_wsjt_heartbeat == 0.0:
            self.window.set_wsjt_status("WSJT-X: waiting…", ok=None)
        elif elapsed < 45:
            self.window.set_wsjt_status("WSJT-X: connected", ok=True)
        else:
            self.window.set_wsjt_status(
                f"WSJT-X: no signal ({int(elapsed)}s)", ok=False
            )

    def _on_call_busy(self, call: str) -> None:
        """Called from WSJT-X listener thread when a CQ caller enters a QSO."""
        if self.window is not None:
            self.window.call_busy.emit(call)

    def _on_call_active(self, call: str) -> None:
        """Called from WSJT-X listener thread when a station resumes calling CQ."""
        if self.window is not None:
            self.window.call_active.emit(call)

    def _activate_rig_spot(self, spot_data: dict) -> None:
        # QSY the rig to a CW/SSB spot via DX Lab Commander.
        # Runs in a daemon thread so the post-set verify delay (~0.75 s) does
        # not block the Qt main thread.
        # For PSK/telnet CW and SSB spots, freq_offset is the absolute
        # frequency in Hz (self.freqs has no entry for these modes, so
        # get_freq_offset returns payload['f'] - 0 = payload['f']).
        if not self._config.commander_enabled:
            print(f"Double-click: Commander not enabled for "
                  f"{spot_data.get('call')} ({spot_data.get('md')})")
            return
        psk_mode = spot_data.get('md', '').upper()
        freq_khz = spot_data.get('freq_offset', 0) / 1000.0
        cmd_mode = self._psk_mode_to_commander(psk_mode, freq_khz)
        if cmd_mode is None:
            print(f"Double-click: no Commander mode mapping for {psk_mode!r}")
            return
        host = self._config.commander_host
        port = self._config.commander_port
        timeout = self._config.commander_timeout
        verify_delay = self._config.commander_verify_delay
        call = spot_data.get('call', '')

        def _run() -> None:
            if not commander_available(host, port):
                print(f"Commander not reachable at {host}:{port}")
                return
            client = CommanderClient(host=host, port=port, timeout=timeout)
            result = client.set_freq_and_mode(freq_khz, cmd_mode,
                                              verify_delay=verify_delay)
            if result.success:
                print(f"Commander QSY: {call} → {freq_khz:.3f} kHz {cmd_mode}")
            else:
                print(f"Commander QSY failed for {call}: {result.errors}")

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _psk_mode_to_commander(psk_mode: str, freq_khz: float) -> str | None:
        # Map a spot-table mode string to a Commander mode string.
        # SSB is split into LSB (below 10 MHz / 40 m and lower) and USB above.
        # Digital modes return None — they are handled via WSJT-X, not Commander.
        if psk_mode == 'CW':
            return 'CW'
        if psk_mode == 'SSB':
            return 'LSB' if freq_khz < 10_000.0 else 'USB'
        if psk_mode in ('AM', 'FM', 'LSB', 'USB'):
            return psk_mode
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
        3. Initialise the pyhamtools callsign lookup library.
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
                            choices=["FT8", "FT4", "FT2", "CW", "SSB", "FC", "FCS", "CS"],
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
        print(self.args)

        print("Loading lookup directory")
        lookuplib = LookupLib(lookuptype="countryfile", filename=self.args.cty_plist)
        self.cinfo = Callinfo(lookuplib)

        if self.args.call is not None:
            if not self.cinfo.is_valid_callsign(self.args.call.replace(".", "/")):
                print(f"Error: Callsign {self.args.call} is not valid!")
                sys.exit(1)

        self.topic = self.build_topic()

        adif_path = cfg.adif_path
        self._current_adif_path = adif_path
        self.adif_log = self._load_log(cfg)

        app = QApplication(sys.argv)
        app.setStyle('Fusion')
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        app.setWindowIcon(make_app_icon())

        self.window = MainWindow(
            initial_args=self.args,
            initial_adif_path=adif_path,
            initial_criterion=cfg.criterion,
            initial_display_filter=cfg.display_filter,
        )
        self.window.settings_changed.connect(self._apply_settings)
        self.window.restart_requested.connect(self._restart)
        self.window.criterion_changed.connect(self._on_criterion_changed)
        self.window.spot_activated.connect(self._on_spot_activated)
        self.window.settings_requested.connect(self._open_settings)
        self.window._spot_table.spots_expired.connect(self._on_spots_expired)
        self.window.restyle_spots(self.adif_log, self._criterion)
        self.window.set_max_spot_age(cfg.max_spot_age)
        self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
        self.window.show()

        if _first_run:
            QTimer.singleShot(0, self._open_settings)

        self._count_timer = QTimer()
        def _tick():
            assert self.window is not None
            self.window.update_counts(self.psk_counter, self.wsjt_counter)
            self._update_wsjt_status()
            self._update_pskr_status()
        self._count_timer.timeout.connect(_tick)
        self._count_timer.start(250)

        print("Connecting to MQTT server")
        self._mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt_client.on_connect    = self.on_connect
        self._mqtt_client.on_disconnect = self.on_disconnect
        self._mqtt_client.on_message    = self.on_message
        self._mqtt_client.connect("mqtt.pskreporter.info", 1883, 60)

        print("Starting MQTT loop")
        self._mqtt_client.loop_start()

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

        def _save_and_cleanup():
            self._save_config()
            self._mqtt_client.loop_stop()   # type: ignore[union-attr]
            self._mqtt_client.disconnect()  # type: ignore[union-attr]
            if self.wsjt_listener is not None:
                self.wsjt_listener.stop()

        app.aboutToQuit.connect(_save_and_cleanup)
        sys.exit(app.exec())

    @staticmethod
    def _load_log(cfg) -> 'ADIFLog | None':
        """Load the log from whichever source the config specifies (read-only)."""
        if cfg.log_source == 'rumlogng':
            print("Loading RumLogNG CloudKit database (read-only)")
            return ADIFLog.from_rumlogng()
        if cfg.adif_path:
            print("Loading ADIF log")
            return ADIFLog(cfg.adif_path)
        return None

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
        cfg.adif_path = s.get('adif_path', cfg.adif_path)
        cfg.band = s.get('band') or cfg.band
        cfg.mode = s.get('mode', cfg.mode)
        cfg.decode_filter = s.get('wsjt_filter', cfg.decode_filter)
        cfg.max_range = s.get('range') or 0
        cfg.max_spot_age = s.get('max_spot_age', cfg.max_spot_age)
        cfg.criterion = self.window.get_criterion()
        cfg.display_filter = self.window.get_display_filter()
        save_config(cfg)
        print(f"Config saved to {config_path()}")

    def _open_settings(self) -> None:
        """Open the Settings dialog; apply changes immediately where possible."""
        if self.window is None:
            return
        cfg = self._config
        dlg = SettingsDialog(
            log_source=cfg.log_source,
            adif_path=self._current_adif_path,
            udp_address=cfg.udp_address,
            udp_port=cfg.udp_port,
            my_grid=self.my_grid,
            rx_grid_prefixes=cfg.rx_grid_prefixes,
            wsjt_reshow_secs=cfg.wsjt_reshow_secs,
            commander_enabled=cfg.commander_enabled,
            commander_port=cfg.commander_port,
            commander_timeout=cfg.commander_timeout,
            commander_verify_delay=cfg.commander_verify_delay,
            parent=self.window,
        )
        if dlg.exec() != SettingsDialog.DialogCode.Accepted:
            return

        cfg.udp_address = dlg.udp_address
        cfg.udp_port = dlg.udp_port
        self.my_grid = dlg.my_grid
        cfg.my_grid = dlg.my_grid
        cfg.rx_grid_prefixes = dlg.rx_grid_prefixes
        cfg.wsjt_reshow_secs = dlg.wsjt_reshow_secs
        cfg.commander_enabled = dlg.commander_enabled
        cfg.commander_port = dlg.commander_port
        cfg.commander_timeout = dlg.commander_timeout
        cfg.commander_verify_delay = dlg.commander_verify_delay

        new_source = dlg.log_source
        new_adif = dlg.adif_path
        source_changed = new_source != cfg.log_source
        adif_changed = new_adif != self._current_adif_path

        if source_changed or adif_changed:
            cfg.log_source = new_source
            cfg.adif_path = new_adif
            self._current_adif_path = new_adif
            self.adif_log = self._load_log(cfg)
            self.window.set_adif_path(new_adif)
            self.window.restyle_spots(self.adif_log, self._criterion)
            self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
            src_label = 'RumLogNG' if new_source == 'rumlogng' else f'ADIF: {new_adif}'
            print(f"Log source changed → {src_label}")


def main() -> None:
    """Application entry point registered in ``pyproject.toml``.

    Creates a :class:`DXSpotter` instance and calls :meth:`~DXSpotter.run`.
    """
    DXSpotter().run()


if __name__ == "__main__":
    main()
