import argparse
import json
import signal
import sys
import time

import paho.mqtt.client as mqtt
from colorama import Fore, Style
from pyhamtools import LookupLib, Callinfo
from pyhamtools.locator import calculate_distance as qth_distance

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from adif_log import ADIFLog
from appconfig import AppConfig, load_config, save_config
from main_window import MainWindow, make_app_icon
from settings_dialog import SettingsDialog
from wsjtx_listener import WsjtxListener


class DXSpotter:
    freqs = {
        "2m"  : { "FT2": 144_177_000, "FT8": 144_174_000 },
        "6m"  : { "FT2":  50_316_000, "FT4":  50_318_000, "FT8":  50_313_000 },
        "10m" : { "FT2":  28_184_000, "FT4":  28_180_000, "FT8":  28_074_000 },
        "15m" : { "FT2":  21_144_000, "FT4":  21_140_000, "FT8":  21_074_000 },
        "17m" : { "FT2":  18_108_000, "FT4":  18_104_000, "FT8":  18_100_000 },
        "20m" : { "FT2":  14_084_000, "FT4":  14_080_000, "FT8":  14_074_000 },
        "30m" : { "FT2":  10_144_000, "FT4":  10_140_000, "FT8":  10_136_000 },
        "40m" : { "FT2":   7_062_000, "FT4":   7_047_500, "FT8":   7_074_000 },
        "80m" : { "FT2":   3_578_000, "FT4":   3_575_000, "FT8":   3_573_000 },
    }

    def __init__(self):
        self.args: argparse.Namespace | None = None
        self.cinfo: Callinfo | None = None
        self.psk_counter:  int = 0
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
        freq_hz = (band_freqs.get(mode)
                   or band_freqs.get('FT8')
                   or next(iter(band_freqs.values())))
        # try:
        #     with socket.create_connection(('localhost', 4532), timeout=0.5) as s:
        #         s.sendall(f'\\set_freq {freq_hz}\n'.encode())
        #         resp = s.recv(64).decode(errors='replace').strip()
        #     if resp == 'RPRT 0':
        #         print(f"rigctld: {band} → {freq_hz:,} Hz")
        #     else:
        #         print(f"rigctld: unexpected response {resp!r}")
        # except OSError as e:
        #     print(f"rigctld: could not reach localhost:4532 — {e}")

    # -- helpers --------------------------------------------------------------

    def get_base_freq(self, band, mode):
        if band not in self.freqs:
            return 0
        if mode not in self.freqs[band]:
            return 0
        return self.freqs[band][mode]

    def get_freq_offset(self, freq, band, mode):
        return freq - self.get_base_freq(band, mode)

    def get_country_text(self, call) -> str:
        try:
            return self.cinfo.get_country_name(call)  # type: ignore[union-attr]
        except Exception:
            return "Unknown"

    def get_dxcc(self, call: str) -> int:
        try:
            return self.cinfo.get_all(call)['adif']  # type: ignore[union-attr]
        except Exception:
            return -1

    def build_topic(self) -> str:
        assert self.args is not None
        band = self.args.band if self.args.band else "+"
        mode = self.args.mode.upper() if self.args.mode else "+"
        if mode in ["FC", "CS", "FCS"]:
            mode = "+"
        call = self.args.call.upper() if self.args.call else "+"
        return f"pskr/filter/v2/{band}/{mode}/+/+/+/+/+/{call}/#"

    # -- settings / restart (called from Qt main thread via signals) ----------

    def _apply_settings(self, settings: dict) -> None:
        assert self.args is not None

        old_band  = self.args.band
        old_mode  = self.args.mode
        old_range = self.args.range

        self.args.call     = settings['call']
        self.args.band     = settings['band']
        self.args.mode     = settings['mode']
        self.args.range    = settings['range']
        self.args.terminal = settings['terminal']

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
            self.psk_counter  = 0
            self.wsjt_counter = 0
            if self.window is not None:
                self.window.clear_table()

        # When the band changes, QSY the radio via rigctld
        if self.args.band != old_band and self.args.band is not None:
            self._qsy_rigctld(self.args.band)

        # Start / stop WSJT-X listener.  Only do a full restart (socket rebind)
        # when the port changes; for filter/call changes, update in place to
        # avoid a race where the old socket still holds the port for up to 1 s.
        want_wsjt  = self._config.wsjt_enabled
        new_filter = settings['wsjt_filter']
        new_port   = self._config.wsjt_port
        new_call   = settings['call']
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

    def on_connect(self, client, userdata, flags, rc, properties):
        print("Connected with result code " + str(rc))
        print(f"Subscribing to topic: {self.topic}")
        client.subscribe(self.topic)

    def on_message(self, client, userdata, msg):
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

            rx_grids = ["FM", "FN", "FL", "EL", "EN", "EM"]
            if not any(payload['rl'].startswith(g) for g in rx_grids):
                return

            dxcc = self.get_dxcc(call)

            total = self.psk_counter + self.wsjt_counter

            if self.args.terminal:
                print(
                    f"{total:06d} | {color} | {timestamp:19} | {call:10} | {loc:10} | {payload['rp']:3} dB | "
                    f"{country:20} | {freq_offset:4} Hz | {distance:5} km | {payload['md']:5} | {payload['b']:4} | "
                    f" {payload['rc']:10} | {payload['rl']:10} | {range_km:5}" #  ({range_km * 0.621371:5.1f} mi)"
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
                })

        except json.JSONDecodeError as e:
            print("Error processing message:", str(e))

    # -- WSJT-X callback ------------------------------------------------------

    def _on_wsjt_spot(self, dx_call: str, dx_grid: str, snr: int,
                      df: int, mode: str, band: str,
                      unix_time: float, msg: str = '',
                      delta_t: float = 0.0) -> None:
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
        """Called when the user double-clicks a spot row."""
        mode = spot_data.get('md', '').upper()
        if mode in self._WSJT_DIGITAL:
            if self.wsjt_listener is None:
                print("Double-click: WSJT-X listener not active")
                return
            call   = spot_data.get('call', '')
            source = spot_data.get('source', 'psk')

            # Prefer the freshest WSJT-X decode — updated every 15 s regardless
            # of the 5-minute display gate, so Reply always has exact fields.
            latest = self.wsjt_listener.get_latest_decode(call)
            low_confidence = False
            if latest:
                time_ms        = latest['ms']
                snr            = latest['snr']
                df             = latest['df']
                delta_t        = latest['delta_t']
                mode           = latest['mode']
                msg            = latest['msg']
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
                msg     = spot_data.get('msg', '')
                if not msg:
                    loc = spot_data.get('loc', '')
                    msg = f"CQ {call} {loc[:4]}".strip() if call else ''
                print(f"Double-click: {call} ({source}) — no cached WSJT-X decode, "
                      f"using spot-row values (PSK Reporter spot or stale)")

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
        else:
            self._on_spot_activated_other(spot_data)

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

    def _on_spot_activated_other(self, _spot_data: dict) -> None:
        """Hook for future non-WSJT-X rig control (e.g. Elecraft K3)."""
        pass

    def _on_criterion_changed(self, criterion: str) -> None:
        self._criterion = criterion
        if self.window is not None:
            self.window.restyle_spots(self.adif_log, criterion)

    # -- entry point ----------------------------------------------------------

    def run(self):
        # Load persisted config first so argparse defaults reflect saved state.
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
        cli.my_grid     = cfg.my_grid
        cli.band        = cli.band        or cfg.band
        cli.mode        = cli.mode        or cfg.mode
        cli.range       = cli.range       if cli.range is not None   else cfg.max_range
        cli.wsjt        = cli.wsjt        if cli.wsjt is not None    else cfg.wsjt_enabled
        cli.wsjt_filter = cli.wsjt_filter or cfg.decode_filter
        cli.wsjt_port   = cli.wsjt_port   if cli.wsjt_port is not None else cfg.wsjt_port
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
        self.window.restyle_spots(self.adif_log, self._criterion)
        self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
        self.window.show()

        self._count_timer = QTimer()
        def _tick():
            assert self.window is not None
            self.window.update_counts(self.psk_counter, self.wsjt_counter)
            self._update_wsjt_status()
        self._count_timer.timeout.connect(_tick)
        self._count_timer.start(250)

        print("Connecting to MQTT server")
        self._mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt_client.on_connect = self.on_connect
        self._mqtt_client.on_message = self.on_message
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
            source = "RumLogNG"
        else:
            source = cfg.adif_path or "ADIF"
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
        cfg.my_grid        = self.my_grid
        cfg.adif_path      = s.get('adif_path', cfg.adif_path)
        cfg.band           = s.get('band') or cfg.band
        cfg.mode           = s.get('mode', cfg.mode)
        cfg.decode_filter  = s.get('wsjt_filter', cfg.decode_filter)
        cfg.max_range      = s.get('range') or 0
        cfg.criterion      = self.window.get_criterion()
        cfg.display_filter = self.window.get_display_filter()
        from appconfig import config_path  # avoid circular at module level
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
            parent=self.window,
        )
        if dlg.exec() != SettingsDialog.DialogCode.Accepted:
            return

        cfg.udp_address = dlg.udp_address
        cfg.udp_port    = dlg.udp_port
        self.my_grid    = dlg.my_grid
        cfg.my_grid     = dlg.my_grid

        new_source = dlg.log_source
        new_adif   = dlg.adif_path
        source_changed = new_source != cfg.log_source
        adif_changed   = new_adif != self._current_adif_path

        if source_changed or adif_changed:
            cfg.log_source = new_source
            cfg.adif_path  = new_adif
            self._current_adif_path = new_adif
            self.adif_log = self._load_log(cfg)
            self.window.set_adif_path(new_adif)
            self.window.restyle_spots(self.adif_log, self._criterion)
            self.window.set_log_info(self._log_info_text(cfg, self.adif_log))
            src_label = 'RumLogNG' if new_source == 'rumlogng' else f'ADIF: {new_adif}'
            print(f"Log source changed → {src_label}")


def main():
    DXSpotter().run()


if __name__ == "__main__":
    main()
