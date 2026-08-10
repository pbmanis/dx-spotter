"""Application configuration: TOML load/save and the :class:`AppConfig` dataclass.

The configuration file is stored in a platform-appropriate location:

* **macOS** — ``~/Library/Application Support/DXSpotter/config.toml``
* **Windows** — ``%APPDATA%/DXSpotter/config.toml``
* **Linux/other** — ``$XDG_CONFIG_HOME/dxspotter/config.toml``
  (falls back to ``~/.config/dxspotter/config.toml``)

The file is created automatically on first save.  All values are optional;
missing keys fall back to the dataclass defaults.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AppConfig:
    """Flat dataclass holding every user-configurable setting for DX Spotter.

    An instance is created by :func:`load_config` (populated from the TOML
    file) or constructed directly with defaults.  :func:`save_config` writes
    the current state back to disk.

    Attributes
    ----------
    udp_address : str
        Multicast or unicast UDP address that WSJT-X broadcasts to.
        The default ``'224.0.0.1'`` is the WSJT-X standard multicast address,
        which lets multiple apps each receive an independent copy of every
        packet.
    udp_port : int
        UDP port number that WSJT-X sends to (default ``2237``).
    log_source : str
        Which contact log backend to load: ``'adif'`` (plain ADIF file) or
        ``'rumlogng'`` (RumLogNG CloudKit SQLite database).
    adif_path : str
        Filesystem path to the ADIF log file.  Ignored when
        ``log_source == 'rumlogng'``.
    my_grid : str
        Operator's Maidenhead grid square (e.g. ``'FM05kw'``).  Used to
        compute the distance from the operator's QTH to each DX station.
    band : str
        Active band filter (e.g. ``'20m'``).  Passed to the PSK Reporter MQTT
        topic and used to filter WSJT-X decodes.
    mode : str
        Active mode filter.  Compound values: ``'FC'`` = FT8 + FT4 + FT2 + CW;
        ``'FCS'`` adds SSB; ``'CS'`` = CW + SSB.
    decode_filter : str
        WSJT-X decode filter: ``'CQ'`` = CQ calls only; ``'all'`` = every
        decode; ``'me'`` = only decodes addressed to ``my_call``.
    max_range : int
        Maximum distance in km from the operator's grid to a reporting station
        ("PSK Report"er only).  ``0`` disables the range filter.
    max_spot_age : int
        Remove spot table rows older than this many minutes.  ``0`` keeps spots
        forever.
    wsjt_enabled : bool
        Whether to start the WSJT-X UDP listener on launch.
    wsjt_port : int
        UDP port on which to listen for WSJT-X packets (default ``2237``).
    wsjt_show_decodes : bool
        Whether WSJT-X decodes are shown at all: added to the spot table and
        (when ``-t``/``--terminal`` is active) printed to the terminal.  When
        ``False``, the WSJT-X listener keeps running in the background (so the
        decode-drought status indicator still works) but individual decodes
        are dropped rather than displayed.  Lets WSJT-X output be silenced
        independently of PSK Reporter and DX Cluster output.  Default ``True``.
    criterion : str
        The DXCC award criterion used to color the QSL column.  One of
        ``'5bd'``, ``'cw'``, ``'mixed'``, ``'digital'``, ``'ssb'``, ``'6m'``.
    display_filter : str
        Row visibility filter: ``'all'``, ``'dxcc_only'``, or
        ``'unconfirmed'``.
    rx_grid_prefixes : list[str]
        Two-character Maidenhead grid prefixes used to restrict PSK Reporter
        spots to those reported by stations in the operator's region.  Only
        spots whose reporter grid square (``rl``) starts with one of these
        prefixes are shown.  Empty list disables the filter (all reporters
        accepted).
    wsjt_reshow_secs : int
        Minimum number of seconds between successive table entries for the
        same callsign from WSJT-X.  A callsign heard again within this window
        is silently dropped from the spot table (though its decode is still
        cached for Reply).  Default is ``300`` (5 minutes).
    rig_control_enabled : bool
        Whether to use rig control (QSY when a CW or SSB spot is
        double-clicked, and the digital dial-frequency QSY before handing a
        spot to WSJT-X).  Applies to whichever backend :attr:`rig_backend`
        selects.  Set to ``True`` in ``config.toml`` to enable.
    rig_track_band : bool
        Whether the active backend's reported VFO frequency is allowed to
        override the Band filter every poll cycle.  Independent of
        :attr:`rig_control_enabled` so QSY can stay on while auto
        band-tracking is turned off (e.g. when the backend is misreporting
        the rig's frequency).  Default ``True``.
    rig_backend : str
        Which rig-control backend to use: ``'commander'`` (DX Lab Suite
        Commander) or ``'rigctld'`` (Hamlib network rig control, e.g. via
        K2K3Controller's CAT/PTT bridge).  Default ``'commander'``.
    commander_host : str
        Hostname or IP address of the Commander process.  Almost always
        ``'127.0.0.1'`` (same machine).
    commander_port : int
        TCP port Commander listens on.  Commander's documented default is
        ``52002`` (configured port block base + 2).  Some installations use a
        different port; check Commander's configuration.
    commander_timeout : float
        Per-query TCP receive timeout in seconds for Commander.  Default
        ``0.2``.
    commander_verify_delay : float
        Seconds to wait after a Commander set command before reading back rig
        state to verify it took effect.  Default ``0.75``.
    rigctld_host : str
        Hostname or IP address of the rigctld process.  Almost always
        ``'127.0.0.1'`` (same machine).
    rigctld_port : int
        TCP port rigctld listens on.  Hamlib's standard default is ``4532``;
        K2K3Controller's CAT/PTT bridge also defaults to ``4532`` and is
        adjustable in its own settings.
    rigctld_timeout : float
        Per-query TCP receive timeout in seconds for rigctld.  Default
        ``0.2``.
    rigctld_verify_delay : float
        Seconds to wait after a rigctld set command before reading back rig
        state to verify it took effect.  Default ``0.75``.
    telnet1_enabled : bool
        Whether DX Cluster connection 1 is enabled on startup.
    telnet1_host : str
        Hostname or IP address of DX Cluster node 1.
    telnet1_port : int
        TCP port for DX Cluster node 1 (common value: ``7300``).
    telnet1_callsign : str
        Operator callsign sent as a login credential to cluster 1.
    telnet2_enabled : bool
        Whether DX Cluster connection 2 is enabled on startup.
    telnet2_host : str
        Hostname or IP address of DX Cluster node 2.
    telnet2_port : int
        TCP port for DX Cluster node 2 (common value: ``7300``).
    telnet2_callsign : str
        Operator callsign sent as a login credential to cluster 2.
    telnet3_enabled : bool
        Whether DX Cluster connection 3 is enabled on startup.
    telnet3_host : str
        Hostname or IP address of DX Cluster node 3.
    telnet3_port : int
        TCP port for DX Cluster node 3 (common value: ``7300``).
    telnet3_callsign : str
        Operator callsign sent as a login credential to cluster 3.
    telnet4_enabled : bool
        Whether DX Cluster connection 4 is enabled on startup.
    telnet4_host : str
        Hostname or IP address of DX Cluster node 4.
    telnet4_port : int
        TCP port for DX Cluster node 4 (common value: ``7300``).
    telnet4_callsign : str
        Operator callsign sent as a login credential to cluster 4.
    telnet_us_ca_spotters_only : bool
        When ``True`` (default), DX Cluster spots are shown only when the
        reporting/spotting station is located in the US or Canada (ADIF DXCC
        291 or 1).  Spots from all other spotters are silently discarded.
        Set to ``False`` to show telnet-cluster spots from any spotter.
    pskr_enabled : bool
        Whether to connect to PSK Reporter via MQTT on startup.
    pskr_host : str
        MQTT broker hostname for PSK Reporter (default
        ``'mqtt.pskreporter.info'``).
    pskr_port : int
        MQTT broker port for PSK Reporter (default ``1883``).
    pskr_service_name : str
        Free-text label for the PSK Reporter connection, shown in the
        Settings dialog for reference only; not used by the MQTT protocol.
    pskr_reshow_secs : int
        Minimum number of seconds between successive table entries for the
        same callsign from PSK Reporter.  A callsign heard again within this
        window is silently dropped, mirroring :attr:`wsjt_reshow_secs`.
        Default is ``300`` (5 minutes).
    """

    udp_address: str = '224.0.0.1'
    udp_port: int = 2237
    log_source: str = 'adif'
    adif_path: str = ''
    my_grid: str = 'FM05kw'
    band: str = '10m'
    mode: str = 'FC'
    decode_filter: str = 'CQ'
    max_range: int = 0
    max_spot_age: int = 30
    wsjt_enabled: bool = True
    wsjt_port: int = 2237
    wsjt_show_decodes: bool = True
    criterion: str = 'mixed'
    display_filter: str = 'all'
    rx_grid_prefixes: list[str] = field(
        default_factory=lambda: ["FM", "FN", "FL", "EL", "EN", "EM"]
    )
    wsjt_reshow_secs: int = 300
    wsjt_no_spot_mins: int = 2
    rig_control_enabled: bool = False
    rig_track_band: bool = True
    rig_backend: str = 'commander'
    commander_host: str = '127.0.0.1'
    commander_port: int = 52002
    commander_timeout: float = 0.2
    commander_verify_delay: float = 0.75
    rigctld_host: str = '127.0.0.1'
    rigctld_port: int = 4532
    rigctld_timeout: float = 0.2
    rigctld_verify_delay: float = 0.75
    telnet1_enabled: bool = False
    telnet1_host: str = ''
    telnet1_port: int = 7300
    telnet1_callsign: str = ''
    telnet2_enabled: bool = False
    telnet2_host: str = ''
    telnet2_port: int = 7300
    telnet2_callsign: str = ''
    telnet3_enabled: bool = False
    telnet3_host: str = ''
    telnet3_port: int = 7300
    telnet3_callsign: str = ''
    telnet4_enabled: bool = False
    telnet4_host: str = ''
    telnet4_port: int = 7300
    telnet4_callsign: str = ''
    telnet_us_ca_spotters_only: bool = True
    pskr_enabled: bool = True
    pskr_host: str = 'mqtt.pskreporter.info'
    pskr_port: int = 1883
    pskr_service_name: str = 'PSK Reporter'
    pskr_reshow_secs: int = 300


def config_path() -> Path:
    """Return the platform-appropriate path to the DXSpotter configuration file.

    The file is not guaranteed to exist; call :func:`load_config` to read it
    (with safe fallback to defaults) or :func:`save_config` to create/update it.

    Returns
    -------
    Path
        Absolute path to ``config.toml`` inside the platform config directory.
    """
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / 'DXSpotter'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', str(Path.home()))) / 'DXSpotter'
    else:
        xdg = os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))
        base = Path(xdg) / 'dxspotter'
    return base / 'config.toml'


def load_config() -> AppConfig:
    """Load the DXSpotter configuration from the platform config file.

    If the file does not exist or cannot be parsed, a default :class:`AppConfig`
    is returned without raising an exception.  Unrecognized keys are silently
    ignored; missing keys fall back to the dataclass defaults.

    Returns
    -------
    AppConfig
        Populated configuration object.
    """
    path = config_path()
    if not path.exists():
        return AppConfig()
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"Warning: could not read config {path}: {e}")
        return AppConfig()

    cfg = AppConfig()

    net = data.get('network', {})
    cfg.udp_address = str(net.get('udp_address', cfg.udp_address))
    cfg.udp_port = int(net.get('udp_port', cfg.udp_port))

    adif = data.get('adif', {})
    cfg.log_source = str(adif.get('log_source', cfg.log_source))
    cfg.adif_path = str(adif.get('path', cfg.adif_path))

    filt = data.get('filters', {})
    cfg.my_grid = str(filt.get('my_grid', cfg.my_grid))
    cfg.band = str(filt.get('band', cfg.band))
    cfg.mode = str(filt.get('mode', cfg.mode))
    cfg.decode_filter = str(filt.get('decode_filter', cfg.decode_filter))
    cfg.max_range = int(filt.get('max_range', cfg.max_range))
    cfg.max_spot_age = int(filt.get('max_spot_age', cfg.max_spot_age))
    cfg.wsjt_enabled = bool(filt.get('wsjt_enabled', cfg.wsjt_enabled))
    cfg.wsjt_port = int(filt.get('wsjt_port', cfg.wsjt_port))
    cfg.wsjt_show_decodes = bool(
        filt.get('wsjt_show_decodes', cfg.wsjt_show_decodes)
    )
    raw_prefixes = filt.get('rx_grid_prefixes', None)
    if isinstance(raw_prefixes, list):
        cfg.rx_grid_prefixes = [str(p).upper() for p in raw_prefixes]
    cfg.wsjt_reshow_secs = int(filt.get('wsjt_reshow_secs', cfg.wsjt_reshow_secs))
    cfg.wsjt_no_spot_mins = int(filt.get('wsjt_no_spot_mins', cfg.wsjt_no_spot_mins))

    ui = data.get('ui', {})
    cfg.criterion = str(ui.get('criterion', cfg.criterion))
    cfg.display_filter = str(ui.get('display_filter', cfg.display_filter))

    rig = data.get('rig', {})
    cfg.rig_control_enabled = bool(rig.get('rig_control_enabled', cfg.rig_control_enabled))
    cfg.rig_track_band = bool(rig.get('rig_track_band', cfg.rig_track_band))
    cfg.rig_backend = str(rig.get('rig_backend', cfg.rig_backend))
    cfg.commander_host = str(rig.get('commander_host', cfg.commander_host))
    cfg.commander_port = int(rig.get('commander_port', cfg.commander_port))
    cfg.commander_timeout = float(rig.get('commander_timeout', cfg.commander_timeout))
    cfg.commander_verify_delay = float(rig.get('commander_verify_delay', cfg.commander_verify_delay))
    cfg.rigctld_host = str(rig.get('rigctld_host', cfg.rigctld_host))
    cfg.rigctld_port = int(rig.get('rigctld_port', cfg.rigctld_port))
    cfg.rigctld_timeout = float(rig.get('rigctld_timeout', cfg.rigctld_timeout))
    cfg.rigctld_verify_delay = float(rig.get('rigctld_verify_delay', cfg.rigctld_verify_delay))

    telnet = data.get('telnet', {})
    t1 = telnet.get('cluster1', {})
    cfg.telnet1_enabled = bool(t1.get('enabled', cfg.telnet1_enabled))
    cfg.telnet1_host = str(t1.get('host', cfg.telnet1_host))
    cfg.telnet1_port = int(t1.get('port', cfg.telnet1_port))
    cfg.telnet1_callsign = str(t1.get('callsign', cfg.telnet1_callsign))
    t2 = telnet.get('cluster2', {})
    cfg.telnet2_enabled = bool(t2.get('enabled', cfg.telnet2_enabled))
    cfg.telnet2_host = str(t2.get('host', cfg.telnet2_host))
    cfg.telnet2_port = int(t2.get('port', cfg.telnet2_port))
    cfg.telnet2_callsign = str(t2.get('callsign', cfg.telnet2_callsign))
    t3 = telnet.get('cluster3', {})
    cfg.telnet3_enabled = bool(t3.get('enabled', cfg.telnet3_enabled))
    cfg.telnet3_host = str(t3.get('host', cfg.telnet3_host))
    cfg.telnet3_port = int(t3.get('port', cfg.telnet3_port))
    cfg.telnet3_callsign = str(t3.get('callsign', cfg.telnet3_callsign))
    t4 = telnet.get('cluster4', {})
    cfg.telnet4_enabled = bool(t4.get('enabled', cfg.telnet4_enabled))
    cfg.telnet4_host = str(t4.get('host', cfg.telnet4_host))
    cfg.telnet4_port = int(t4.get('port', cfg.telnet4_port))
    cfg.telnet4_callsign = str(t4.get('callsign', cfg.telnet4_callsign))
    cfg.telnet_us_ca_spotters_only = bool(
        telnet.get('us_ca_spotters_only', cfg.telnet_us_ca_spotters_only)
    )

    pskr = data.get('pskr', {})
    cfg.pskr_enabled = bool(pskr.get('enabled', cfg.pskr_enabled))
    cfg.pskr_host = str(pskr.get('host', cfg.pskr_host))
    cfg.pskr_port = int(pskr.get('port', cfg.pskr_port))
    cfg.pskr_service_name = str(pskr.get('service_name', cfg.pskr_service_name))
    cfg.pskr_reshow_secs = int(pskr.get('reshow_secs', cfg.pskr_reshow_secs))

    return cfg


def save_config(cfg: AppConfig) -> None:
    """Write a :class:`AppConfig` to the platform config file as TOML.

    The parent directory is created if it does not already exist.  The file is
    always written from scratch (not patched in place), so unknown keys are not
    preserved.

    Parameters
    ----------
    cfg : AppConfig
        Current application configuration to serialize.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"""\
# DXSpotter configuration — safe to edit by hand.

[network]
udp_address = "{cfg.udp_address}"
udp_port    = {cfg.udp_port}

[adif]
log_source = "{cfg.log_source}"
path       = "{cfg.adif_path}"

[filters]
my_grid       = "{cfg.my_grid}"
band          = "{cfg.band}"
mode          = "{cfg.mode}"
decode_filter = "{cfg.decode_filter}"
max_range     = {cfg.max_range}
max_spot_age  = {cfg.max_spot_age}
wsjt_enabled       = {"true" if cfg.wsjt_enabled else "false"}
wsjt_port          = {cfg.wsjt_port}
wsjt_show_decodes = {"true" if cfg.wsjt_show_decodes else "false"}
rx_grid_prefixes   = [{", ".join(f'"{p}"' for p in cfg.rx_grid_prefixes)}]
wsjt_reshow_secs   = {cfg.wsjt_reshow_secs}
wsjt_no_spot_mins  = {cfg.wsjt_no_spot_mins}

[ui]
criterion      = "{cfg.criterion}"
display_filter = "{cfg.display_filter}"

[rig]
rig_control_enabled    = {"true" if cfg.rig_control_enabled else "false"}
rig_track_band         = {"true" if cfg.rig_track_band else "false"}
rig_backend            = "{cfg.rig_backend}"
commander_host         = "{cfg.commander_host}"
commander_port         = {cfg.commander_port}
commander_timeout      = {cfg.commander_timeout}
commander_verify_delay = {cfg.commander_verify_delay}
rigctld_host           = "{cfg.rigctld_host}"
rigctld_port           = {cfg.rigctld_port}
rigctld_timeout        = {cfg.rigctld_timeout}
rigctld_verify_delay   = {cfg.rigctld_verify_delay}

[telnet]
us_ca_spotters_only = {"true" if cfg.telnet_us_ca_spotters_only else "false"}

[telnet.cluster1]
enabled  = {"true" if cfg.telnet1_enabled else "false"}
host     = "{cfg.telnet1_host}"
port     = {cfg.telnet1_port}
callsign = "{cfg.telnet1_callsign}"

[telnet.cluster2]
enabled  = {"true" if cfg.telnet2_enabled else "false"}
host     = "{cfg.telnet2_host}"
port     = {cfg.telnet2_port}
callsign = "{cfg.telnet2_callsign}"

[telnet.cluster3]
enabled  = {"true" if cfg.telnet3_enabled else "false"}
host     = "{cfg.telnet3_host}"
port     = {cfg.telnet3_port}
callsign = "{cfg.telnet3_callsign}"

[telnet.cluster4]
enabled  = {"true" if cfg.telnet4_enabled else "false"}
host     = "{cfg.telnet4_host}"
port     = {cfg.telnet4_port}
callsign = "{cfg.telnet4_callsign}"

[pskr]
enabled      = {"true" if cfg.pskr_enabled else "false"}
host         = "{cfg.pskr_host}"
port         = {cfg.pskr_port}
service_name = "{cfg.pskr_service_name}"
reshow_secs  = {cfg.pskr_reshow_secs}
"""
    path.write_text(content, encoding='utf-8')
