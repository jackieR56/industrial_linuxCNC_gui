#!/usr/bin/env python3
# settings.py — operator-tunable settings, read from the LinuxCNC machine ini.
#
# Everything lives in the [GUI] and [GUI_COLORS] sections of the ini that
# LinuxCNC hands the DISPLAY program (-ini /path/file.ini). Every getter takes
# a default, so a key that is missing or malformed falls back to the built-in
# value — an ini with no GUI sections at all looks exactly like stock.
# See gui_settings.sample.ini for the full list.
#
# Resolved at import time so other modules can build module-level constants.

import os
import sys

import linuxcnc
from sdl2 import SDL_Color

# Font used when [GUI]FONT_PATH is not set. A Debian/Ubuntu-installed font so
# a stock machine starts; set FONT_PATH in the ini to your panel font.
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def _parse_ini_arg(argv):
    """LinuxCNC launches DISPLAY programs as: prog -ini /path/to/file.ini"""
    it = iter(argv[1:])
    for a in it:
        if a in ("-ini", "--ini"):
            return next(it, None)
    return None


INI_PATH = _parse_ini_arg(sys.argv) or os.environ.get("INI_FILE_NAME")

_ini = None
if INI_PATH:
    try:
        _ini = linuxcnc.ini(INI_PATH)
    except Exception as e:
        print(f"settings: could not read {INI_PATH}: {e}")


def _raw(section, key):
    if _ini is None:
        return None
    v = _ini.find(section, key)
    return v.strip() if v is not None else None


def _warn(section, key, raw, default):
    print(f"settings: bad [{section}]{key} = {raw!r}, using {default!r}")


def get_str(section, key, default):
    v = _raw(section, key)
    return v if v else default


def get_int(section, key, default):
    v = _raw(section, key)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        _warn(section, key, v, default)
        return default


def get_bool(section, key, default):
    v = _raw(section, key)
    if not v:
        return default
    s = v.lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    _warn(section, key, v, default)
    return default


def get_list(section, key, default):
    """Comma-separated list of strings."""
    v = _raw(section, key)
    if not v:
        return list(default)
    return [p.strip() for p in v.split(",") if p.strip()]


def get_path(section, key, default):
    return os.path.expanduser(get_str(section, key, default))


def _parse_rgb(v, n=3):
    parts = [int(p) for p in v.split(",")]
    if len(parts) != n or not all(0 <= p <= 255 for p in parts):
        raise ValueError
    return tuple(parts)


def get_color(section, key, default):
    """"R, G, B" -> tuple of ints ("R, G, B, A" where the default has alpha)."""
    v = _raw(section, key)
    if not v:
        return default
    try:
        return _parse_rgb(v, len(default))
    except ValueError:
        _warn(section, key, v, default)
        return default


def get_sdl_color(section, key, default):
    return SDL_Color(*get_color(section, key, default))


def get_palette(section, key, default):
    """"R, G, B; R, G, B; ..." -> list of tuples."""
    v = _raw(section, key)
    if not v:
        return list(default)
    try:
        return [_parse_rgb(p) for p in v.split(";") if p.strip()]
    except ValueError:
        _warn(section, key, v, default)
        return list(default)


# Shorthand for the colors section, which every module draws from.
def color(key, default):
    return get_color("GUI_COLORS", key, default)


def sdl_color(key, default):
    return get_sdl_color("GUI_COLORS", key, default)
