from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppConfig:
    udp_address:    str  = '224.0.0.1'
    udp_port:       int  = 2237
    log_source:     str  = 'adif'   # 'adif' or 'rumlogng'
    adif_path:      str  = ''
    my_grid:        str  = 'FM05kw'
    band:           str  = '10m'
    mode:           str  = 'FC'
    decode_filter:  str  = 'CQ'
    max_range:      int  = 0
    wsjt_enabled:   bool = True
    wsjt_port:      int  = 2237
    criterion:      str  = 'mixed'
    display_filter: str  = 'all'


def config_path() -> Path:
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / 'DXSpotter'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', str(Path.home()))) / 'DXSpotter'
    else:
        xdg = os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))
        base = Path(xdg) / 'dxspotter'
    return base / 'config.toml'


def load_config() -> AppConfig:
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
    cfg.udp_port    = int(net.get('udp_port',    cfg.udp_port))

    adif = data.get('adif', {})
    cfg.log_source = str(adif.get('log_source', cfg.log_source))
    cfg.adif_path  = str(adif.get('path',       cfg.adif_path))

    filt = data.get('filters', {})
    cfg.my_grid       = str(filt.get('my_grid',       cfg.my_grid))
    cfg.band          = str(filt.get('band',          cfg.band))
    cfg.mode          = str(filt.get('mode',          cfg.mode))
    cfg.decode_filter = str(filt.get('decode_filter', cfg.decode_filter))
    cfg.max_range     = int(filt.get('max_range',     cfg.max_range))
    cfg.wsjt_enabled  = bool(filt.get('wsjt_enabled', cfg.wsjt_enabled))
    cfg.wsjt_port     = int(filt.get('wsjt_port',     cfg.wsjt_port))

    ui = data.get('ui', {})
    cfg.criterion      = str(ui.get('criterion',      cfg.criterion))
    cfg.display_filter = str(ui.get('display_filter', cfg.display_filter))

    return cfg


def save_config(cfg: AppConfig) -> None:
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
wsjt_enabled  = {"true" if cfg.wsjt_enabled else "false"}
wsjt_port     = {cfg.wsjt_port}

[ui]
criterion      = "{cfg.criterion}"
display_filter = "{cfg.display_filter}"
"""
    path.write_text(content, encoding='utf-8')
