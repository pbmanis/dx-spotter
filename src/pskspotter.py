import argparse
import json
import signal
import socket
import sys
import time

import paho.mqtt.client as mqtt
from colorama import Fore, Style
from pyhamtools import LookupLib, Callinfo
from pyhamtools.locator import calculate_distance as qth_distance

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from adif_log import ADIFLog
from main_window import MainWindow, make_app_icon
from wsjtx_listener import WsjtxListener


ADIF_PATH = "/Users/pbmanis/Documents/personal/radio/Logs/NC3G_all_2026.05.11.adif"


class PSKSpotter:
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
        self._current_adif_path: str = ADIF_PATH

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
        try:
            with socket.create_connection(('localhost', 4532), timeout=0.5) as s:
                s.sendall(f'\\set_freq {freq_hz}\n'.encode())
                resp = s.recv(64).decode(errors='replace').strip()
            if resp == 'RPRT 0':
                print(f"rigctld: {band} → {freq_hz:,} Hz")
            else:
                print(f"rigctld: unexpected response {resp!r}")
        except OSError as e:
            print(f"rigctld: could not reach localhost:4532 — {e}")

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
        old_show  = getattr(self.args, 'show', None)

        self.args.call     = settings['call']
        self.args.band     = settings['band']
        self.args.mode     = settings['mode']
        self.args.range    = settings['range']
        self.args.terminal = settings['terminal']
        self.args.show     = settings['spot_filter']

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
                self.args.range != old_range or
                self.args.show  != old_show):
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
        want_wsjt  = settings['wsjt']
        new_filter = settings['wsjt_filter']
        new_port   = settings['wsjt_port']
        new_call   = settings['call']
        if want_wsjt:
            if self.wsjt_listener is None or self.wsjt_listener.port != new_port:
                if self.wsjt_listener is not None:
                    self.wsjt_listener.stop()
                self.wsjt_listener = WsjtxListener(
                    port=new_port,
                    decode_filter=new_filter,
                    my_call=new_call,
                    on_spot=self._on_wsjt_spot,
                )
                self.wsjt_listener.start()
            else:
                self.wsjt_listener.decode_filter = new_filter.upper()
                self.wsjt_listener.my_call = new_call.upper() if new_call else None
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
            if country in ["United States", "Canada", "Unknown"]:
                return

            freq_offset = self.get_freq_offset(payload['f'], payload['b'], payload['md'])
            try:
                distance = int(qth_distance(payload['sl'], payload['rl']))
            except Exception:
                raise ValueError(f"Invalid grid squares: {payload['sl']} and {payload['rl']}")

            range_km = int(qth_distance(self.my_grid, payload['rl']))
            if self.args.range is not None and range_km > self.args.range:
                return

            if payload['rp'] is None:
                payload['rp'] = "N/A"

            rx_grids = ["FM", "FN", "FL", "EL", "EN", "EM"]
            if not any(payload['rl'].startswith(g) for g in rx_grids):
                return

            dxcc = self.get_dxcc(call)

            if self.adif_log is not None:
                show = self.args.show
                if show == 'New anywhere':
                    if self.adif_log.ever_worked(dxcc):
                        return
                elif show == 'New on band':
                    if self.adif_log.ever_confirmed_on_band(dxcc, payload['b']):
                        return

            total = self.psk_counter + self.wsjt_counter

            if self.adif_log is not None:
                adif_status = self.adif_log.status(dxcc, payload['b'], payload['md'])
                confirmed_pairs: dict[tuple[str, str], list[str]] = (
                    self.adif_log.confirmed_band_modes(dxcc)
                    if adif_status == 'confirmed_other' else {}
                )
                confirmed_details: list[dict[str, str]] = (
                    self.adif_log.confirmed_details(dxcc, payload['b'], payload['md'])
                    if adif_status == 'confirmed' else []
                )
            else:
                adif_status = 'confirmed'
                confirmed_pairs: dict[tuple[str, str], list[str]] = {}
                confirmed_details: list[dict[str, str]] = []

            if self.args.terminal:
                print(
                    f"{total:06d} | {color} | {timestamp:19} | {call:10} | {loc:10} | {payload['rp']:3} dB | "
                    f"{country:20} | {freq_offset:4} Hz | {distance:5} km | {payload['md']:5} | {payload['b']:4} | "
                    f" {payload['rc']:10} | {payload['rl']:10} | {range_km:5} km ({range_km * 0.621371:5.1f} mi)"
                    f" [{adif_status}]{Style.RESET_ALL}"
                )

            if self.window is not None:
                self.window.new_spot.emit({
                    "counter":           total,
                    "direction":         direction,
                    "timestamp":         timestamp,
                    "call":              call,
                    "loc":               loc,
                    "rp":                payload['rp'],
                    "country":           country,
                    "freq_offset":       freq_offset,
                    "distance":          distance,
                    "md":                payload['md'],
                    "b":                 payload['b'],
                    "rc":                payload['rc'],
                    "rl":                payload['rl'],
                    "range":             range_km,
                    "unix_time":         payload['t'],
                    "adif_status":       adif_status,
                    "confirmed_pairs":   confirmed_pairs,
                    "confirmed_details": confirmed_details,
                })

        except json.JSONDecodeError as e:
            print("Error processing message:", str(e))

    # -- WSJT-X callback ------------------------------------------------------

    def _on_wsjt_spot(self, dx_call: str, dx_grid: str, snr: int,
                      df: int, mode: str, band: str,
                      unix_time: float) -> None:
        assert self.args is not None
        self.wsjt_counter += 1

        country = self.get_country_text(dx_call)
        if country in ["United States", "Canada"]:
            return

        dist_km = 0
        if dx_grid:
            try:
                dist_km = int(qth_distance(self.my_grid, dx_grid))
            except Exception:
                dist_km = 0

        if self.args.range is not None and dist_km > 0 and dist_km > self.args.range:
            return

        dxcc = self.get_dxcc(dx_call)

        if self.adif_log is not None:
            show = self.args.show
            if show == 'New anywhere':
                if self.adif_log.ever_worked(dxcc):
                    return
            elif show == 'New on band':
                if self.adif_log.ever_confirmed_on_band(dxcc, band):
                    return

        if self.adif_log is not None:
            adif_status = self.adif_log.status(dxcc, band, mode)
            confirmed_pairs: dict[tuple[str, str], list[str]] = (
                self.adif_log.confirmed_band_modes(dxcc)
                if adif_status == 'confirmed_other' else {}
            )
            confirmed_details: list[dict[str, str]] = (
                self.adif_log.confirmed_details(dxcc, band, mode)
                if adif_status == 'confirmed' else []
            )
        else:
            adif_status = 'confirmed'
            confirmed_pairs = {}
            confirmed_details = []

        total = self.psk_counter + self.wsjt_counter
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(unix_time))

        if self.args.terminal:
            print(
                f"{total:06d} | WSJT | {timestamp} | {dx_call:10} | "
                f"{dx_grid:6} | {snr:+3d} dB | {country:20} | {df:4} Hz | "
                f"{dist_km:5} km | {mode:5} | {band:4} [{adif_status}]"
            )

        if self.window is not None:
            self.window.new_spot.emit({
                "counter":           total,
                "direction":         "RX",
                "timestamp":         timestamp,
                "call":              dx_call,
                "loc":               dx_grid,
                "rp":                f"{snr:+d}",
                "country":           country,
                "freq_offset":       df,
                "distance":          dist_km,
                "md":                mode,
                "b":                 band,
                "rc":                self.args.call or "WSJT-X",
                "rl":                self.my_grid,
                "range":             0,
                "unix_time":         unix_time,
                "adif_status":       adif_status,
                "confirmed_pairs":   confirmed_pairs,
                "confirmed_details": confirmed_details,
                "source":            "wsjt",
            })

    # -- entry point ----------------------------------------------------------

    def run(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("-c", "--call", required=False, default=None,
                            help="Call sign")
        parser.add_argument("-b", "--band", required=False, default='10m',
                            help="Band (e.g. 20m)")
        parser.add_argument("-m", "--mode", required=False, default='FC',
                            choices=["FT8", "FT4", "FT2", "CW", "SSB", "FC", "FCS", "CS"],
                            help="Mode (e.g. FT8)")
        parser.add_argument("-r", "--range", required=False, type=int, default=250,
                            help="Maximum rx station range from my grid in km (default: 250)")
        parser.add_argument("-t", "--terminal", action="store_true", default=False,
                            help="Print spots to the terminal (default: off)")
        parser.add_argument("-W", "--wsjt", action="store_true", default=True,
                            help="Enable WSJT-X UDP listener (port 2237, on by default)")
        parser.add_argument("-wf", "--wsjt-filter", default="CQ",
                            choices=["CQ", "all", "me"],
                            help="WSJT-X decode filter: CQ=CQ calls only, all=all decodes, "
                                 "me=CQ calls + calls directed to --call (default: CQ)")
        parser.add_argument("--wsjt-port", type=int, default=2237,
                            help="WSJT-X UDP port (default: 2237)")
        parser.add_argument("--show", default='All spots',
                            choices=['All spots', 'New on band', 'New anywhere'],
                            help="Which spots to display (default: All spots)")
        parser.add_argument("--cty-plist", required=False, default=None,
                            help="Local filename of CTY Plist from country-code")
        self.args = parser.parse_args()
        print(self.args)

        print("Loading lookup directory")
        lookuplib = LookupLib(lookuptype="countryfile", filename=self.args.cty_plist)
        self.cinfo = Callinfo(lookuplib)

        if self.args.call is not None:
            if not self.cinfo.is_valid_callsign(self.args.call.replace(".", "/")):
                print(f"Error: Callsign {self.args.call} is not valid!")
                sys.exit(1)

        self.topic = self.build_topic()

        print("Loading ADIF log")
        self.adif_log = ADIFLog(ADIF_PATH)
        self._current_adif_path = ADIF_PATH

        app = QApplication(sys.argv)
        app.setStyle('Fusion')
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        app.setWindowIcon(make_app_icon())

        self.window = MainWindow(
            initial_args=self.args,
            initial_adif_path=ADIF_PATH,
        )
        self.window.settings_changed.connect(self._apply_settings)
        self.window.restart_requested.connect(self._restart)
        self.window.show()

        self._count_timer = QTimer()
        self._count_timer.timeout.connect(
            lambda: self.window.update_counts(  # type: ignore[union-attr]
                self.psk_counter, self.wsjt_counter
            )
        )
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
            )
            self.wsjt_listener.start()

        def _cleanup():
            self._mqtt_client.loop_stop()   # type: ignore[union-attr]
            self._mqtt_client.disconnect()  # type: ignore[union-attr]
            if self.wsjt_listener is not None:
                self.wsjt_listener.stop()

        app.aboutToQuit.connect(_cleanup)
        sys.exit(app.exec())


def main():
    PSKSpotter().run()


if __name__ == "__main__":
    main()
