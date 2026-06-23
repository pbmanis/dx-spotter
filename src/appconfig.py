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
        (PSK Reporter only).  ``0`` disables the range filter.
    max_spot_age : int
        Remove spot table rows older than this many minutes.  ``0`` keeps spots
        forever.
    wsjt_enabled : bool
        Whether to start the WSJT-X UDP listener on launch.
    wsjt_port : int
        UDP port on which to listen for WSJT-X packets (default ``2237``).
    criterion : str
        The DXCC award criterion used to colour the QSL column.  One of
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
    commander_enabled : bool
        Whether to use DX Lab Commander for rig control when a CW or SSB spot
        is double-clicked.  Set to ``True`` in ``config.toml`` to enable.
    commander_host : str
        Hostname or IP address of the Commander process.  Almost always
        ``'127.0.0.1'`` (same machine).
    commander_port : int
        TCP port Commander listens on.  Commander's documented default is
        ``52002`` (configured port block base + 2).  Some installations use a
        different port; check Commander's configuration.
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
    criterion: str = 'mixed'
    display_filter: str = 'all'
    rx_grid_prefixes: list[str] = field(
        default_factory=lambda: ["FM", "FN", "FL", "EL", "EN", "EM"]
    )
    wsjt_reshow_secs: int = 300
    commander_enabled: bool = False
    commander_host: str = '127.0.0.1'
    commander_port: int = 52002
    commander_timeout: float = 0.2
    commander_verify_delay: float = 0.75


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
    is returned without raising an exception.  Unrecognised keys are silently
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
    raw_prefixes = filt.get('rx_grid_prefixes', None)
    if isinstance(raw_prefixes, list):
        cfg.rx_grid_prefixes = [str(p).upper() for p in raw_prefixes]
    cfg.wsjt_reshow_secs = int(filt.get('wsjt_reshow_secs', cfg.wsjt_reshow_secs))

    ui = data.get('ui', {})
    cfg.criterion = str(ui.get('criterion', cfg.criterion))
    cfg.display_filter = str(ui.get('display_filter', cfg.display_filter))

    rig = data.get('rig', {})
    cfg.commander_enabled = bool(rig.get('commander_enabled', cfg.commander_enabled))
    cfg.commander_host = str(rig.get('commander_host', cfg.commander_host))
    cfg.commander_port = int(rig.get('commander_port', cfg.commander_port))
    cfg.commander_timeout = float(rig.get('commander_timeout', cfg.commander_timeout))
    cfg.commander_verify_delay = float(rig.get('commander_verify_delay', cfg.commander_verify_delay))

    return cfg


def save_config(cfg: AppConfig) -> None:
    """Write a :class:`AppConfig` to the platform config file as TOML.

    The parent directory is created if it does not already exist.  The file is
    always written from scratch (not patched in place), so unknown keys are not
    preserved.

    Parameters
    ----------
    cfg : AppConfig
        Current application configuration to serialise.
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
rx_grid_prefixes   = [{", ".join(f'"{p}"' for p in cfg.rx_grid_prefixes)}]
wsjt_reshow_secs   = {cfg.wsjt_reshow_secs}

[ui]
criterion      = "{cfg.criterion}"
display_filter = "{cfg.display_filter}"

[rig]
commander_enabled      = {"true" if cfg.commander_enabled else "false"}
commander_host         = "{cfg.commander_host}"
commander_port         = {cfg.commander_port}
commander_timeout      = {cfg.commander_timeout}
commander_verify_delay = {cfg.commander_verify_delay}
"""
    path.write_text(content, encoding='utf-8')
