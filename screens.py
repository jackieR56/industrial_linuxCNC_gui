#!/usr/bin/env python3
# screens.py — render helpers, Screen base with multi-level softkeys,
#              all screens, and the ScreenManager.

import ctypes
import math
import re
import time
from collections import deque
from sdl2 import *
from sdl2.sdlttf import *
from backplot import Backplot2D, AXIS_COLORS
import linuxcnc
import json
import os
import settings
import configfile
import tooltbl

WHITE = settings.sdl_color("TEXT", (255, 255, 255))
RED   = settings.sdl_color("ALARM", (250, 0, 0))
BLACK = settings.sdl_color("TEXT_INVERSE", (0, 0, 0))   # text on a lit fill
DIM    = settings.sdl_color("DIM", (150, 150, 150))
ACCENT = settings.sdl_color("ACCENT", (255, 150, 40))

BG_C       = settings.color("BACKGROUND", (0, 0, 0))
FRAME_C    = settings.color("FRAME", (0, 0, 255))        # softkey frame
CURSOR_C   = settings.color("CURSOR_CELL", (60, 60, 60)) # probe grid cursor
GRID_ON_C  = settings.color("GRID_ON", (27, 95, 165))    # probe cell, populated
GRID_OFF_C = settings.color("GRID_OFF", (45, 45, 45))    # probe cell, empty

STATUS_H  = 60          # content starts below this
SOFTKEY_Y = 960         # softkey frame line
KEY_X0, KEY_W = 60, 180 # [<] 0-60, ten keys, [>] 1860-1920
BUFFER_H = 54           # input line band above the softkey frame
PANE_W   = 460          # status pane width (statuspane imports this)

# Tool wear now lives in tool.tbl (see tooltbl.py). The old side file is
# only looked for to import it once (OffsetScreen._migrate_legacy_wear).
LEGACY_WEAR_FILE = os.path.join(os.path.dirname(__file__), "tool_wear.json")

# Probe macros. LinuxCNC resolves these by SUBROUTINE_PATH when it runs them;
# we only read them here, to show the operator the routine's own placement
# instructions rather than a generic "position the probe" line.
MACRO_DIR = os.path.join(os.path.dirname(__file__), "macros")

_MACRO_META  = re.compile(r"^(author|version|date)\s*:", re.I)
_MACRO_BOILER = "ensure all settings"
_macro_help_cache = {}


def macro_help(name):
    """The leading comment block of macros/<name>.ngc, minus the metadata and
    the boilerplate trailer. [0] is what the routine does, [1:] is where to
    put the probe. Empty when the macro has no header (tool_length) or cannot
    be read — this is called from the draw loop, so it must not raise."""
    if name in _macro_help_cache:
        return _macro_help_cache[name]
    out = []
    try:
        with open(os.path.join(MACRO_DIR, name + ".ngc")) as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                if not (line.startswith("(") and line.endswith(")")):
                    break               # first real g-code ends the header
                body = line[1:-1].strip()
                if not body or _MACRO_META.match(body):
                    continue
                # The "ensure all settings..." trailer is boilerplate. Six
                # macros wrap it mid-line, so keep whatever real text precedes
                # it and stop — no macro has anything useful after it.
                cut = body.lower().find(_MACRO_BOILER)
                if cut >= 0:
                    body = body[:cut].strip().rstrip(",")
                    if body:
                        out.append(body.upper())
                    break
                out.append(body.upper())
    except OSError:
        out = []
    res = tuple(out)
    _macro_help_cache[name] = res
    return res

WCS_PARAM_BASE = 5221   # G54 X = 5221; each system +20; axes X..W
# 10-entry, 1-based by g5x_index (status bar):
WCS = ("None", "G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3")
# 9-entry, 0-based by system number (WORK table):
WCS_NAMES = ("G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3")

# LinuxCNC 9-tuple axis order — C is index 5 (B occupies 4 even if unused)
AXIS_IDX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}

PANE_SCREENS = {0: "LOADS", 1: "POSMODE", 2: "POSMODE", 5: "POSMODE"}
#               POS         PROG          OFFSET        GRAPHICS

# ---- probing (Probe Basic macros) ---------------------------------------

# Shared arg list, in the macros' fixed positional order.
# (key, label, default, kind). kind: LEN = length, FEED = length/min,
# NUM = unitless (tool number, mode flag, axis select).
# Values in persist["probe"] and these defaults are MACHINE units; LEN/FEED
# convert at the P.SET display/entry and at the macro call string (the macros
# read their args in the interpreter's current G20/G21 mode). Values persisted
# before the kind column existed were already machine units (the reference
# machine is mm and they are the metric defaults), so there is no migration.
PROBE_PARAMS = [
    ("probe_tool",   "PROBE TOOL NO.",   99,    "NUM"),
    ("max_z",        "MAX Z DIST",       25.0,  "LEN"),
    ("max_xy",       "MAX XY DIST",      25.0,  "LEN"),
    ("xy_clear",     "XY CLEARANCE",     6.0,   "LEN"),
    ("z_clear",      "Z CLEARANCE",      3.0,   "LEN"),
    ("step_off",     "STEP-OFF WIDTH",   12.0,  "LEN"),
    ("extra_depth",  "EXTRA Z DEPTH",    2.0,   "LEN"),
    ("slow_fr",      "PROBE SLOW FR",    30.0,  "FEED"),
    ("fast_fr",      "PROBE FAST FR",    300.0, "FEED"),
    ("cal_offset",   "CAL OFFSET",       0.0,   "LEN"),
    ("x_hint",       "X HINT",           0.0,   "LEN"),
    ("y_hint",       "Y HINT",           0.0,   "LEN"),
    ("dia_hint",     "DIA HINT",         10.0,  "LEN"),
    ("edge_width",   "EDGE WIDTH",       10.0,  "LEN"),
    ("probe_mode",   "MODE (0=SET WCS)", 0,     "NUM"),
]

# Args past #15 are family-specific and #16 means different things to each,
# so they are appended per macro rather than folded into PROBE_PARAMS (which
# is the positional contract every macro shares and must stay 15 long).
PROBE_EXTRA = [
    ("wco_rot",     "SET WCS ROTATION (1=YES)", 0,    "NUM"),   # edge angle  #16
    ("cal_dia",     "CAL GAUGE DIA",            10.0, "LEN"),   # cal         #16
    ("cal_x_width", "CAL GAUGE X WIDTH",        10.0, "LEN"),   # cal         #17
    ("cal_y_width", "CAL GAUGE Y WIDTH",        10.0, "LEN"),   # cal         #18
    ("cal_axis",    "CAL AXIS (0=AVG 1=X 2=Y)", 0,    "NUM"),   # cal         #19
]
PROBE_FIELDS = PROBE_PARAMS + PROBE_EXTRA
PROBE_KIND = {k: kind for k, _l, _d, kind in PROBE_FIELDS}

# page -> {(row, col): (macro, icon_kind)}
# row 0 = back (+Y), row 2 = front (-Y); col 0 = left (-X), col 2 = right (+X)
PZ = ("probe_z_minus_wco", "z")

PROBE_PAGES = {
    "POUT": {
        (0, 0): ("probe_back_left_outside",   "cnr_out"),
        (0, 1): ("probe_back_outside",        "edge_out"),
        (0, 2): ("probe_back_right_outside",  "cnr_out"),
        (1, 0): ("probe_left_outside",        "edge_out"),
        (1, 1): PZ,
        (1, 2): ("probe_right_outside",       "edge_out"),
        (2, 0): ("probe_front_left_outside",  "cnr_out"),
        (2, 1): ("probe_front_outside",       "edge_out"),
        (2, 2): ("probe_front_right_outside", "cnr_out"),
    },
    "PIN": {
        (0, 0): ("probe_back_left_inside",   "cnr_in"),
        (0, 1): ("probe_y_plus_wco",         "edge_in"),
        (0, 2): ("probe_back_right_inside",  "cnr_in"),
        (1, 0): ("probe_x_minus_wco",        "edge_in"),
        (1, 1): PZ,
        (1, 2): ("probe_x_plus_wco",         "edge_in"),
        (2, 0): ("probe_front_left_inside",  "cnr_in"),
        (2, 1): ("probe_y_minus_wco",        "edge_in"),
        (2, 2): ("probe_front_right_inside", "cnr_in"),
    },
    # The corner macros are named for the direction of their doubled probe,
    # not for the corner they sit on, which is how they came to be wired a
    # quarter-turn out. Placement here follows the motion in the .ngc:
    # x_plus probes the back face once then the LEFT face twice = back left.
    "PANG": {
        (0, 0): ("probe_corner_x_plus_edge_angle",  "ang_cnr"),
        (0, 1): ("probe_back_edge_angle",           "ang_edge"),
        (0, 2): ("probe_corner_y_minus_edge_angle", "ang_cnr"),
        (1, 0): ("probe_left_edge_angle",           "ang_edge"),
        (1, 1): PZ,
        (1, 2): ("probe_right_edge_angle",          "ang_edge"),
        (2, 0): ("probe_corner_y_plus_edge_angle",  "ang_cnr"),
        (2, 1): ("probe_front_edge_angle",          "ang_edge"),
        (2, 2): ("probe_corner_x_minus_edge_angle", "ang_cnr"),
    },
    "PBOSS": {
        (0, 0): ("probe_rect_boss",    "boss_rect"),
        (0, 2): ("probe_rect_pocket",  "pkt_rect"),
        (1, 1): PZ,
        (2, 0): ("probe_round_boss",   "boss_round"),
        (2, 2): ("probe_round_pocket", "pkt_round"),
    },
    "PRIDGE": {
        (0, 0): ("probe_ridge_x",  "ridge_x"),
        (0, 2): ("probe_valley_x", "valley_x"),
        (1, 1): PZ,
        (2, 0): ("probe_ridge_y",  "ridge_y"),
        (2, 2): ("probe_valley_y", "valley_y"),
    },
    # Calibration: probe a gauge of KNOWN size to derive the ball/stylus
    # runout correction. No centre Z — a Z touch tells you nothing about
    # stylus diameter. Gauge size is DIA HINT on the [P.SET] page.
    "PCAL": {
        (0, 0): ("probe_cal_square_boss",   "boss_rect"),
        (0, 2): ("probe_cal_square_pocket", "pkt_rect"),
        (2, 0): ("probe_cal_round_boss",    "boss_round"),
        (2, 2): ("probe_cal_round_pocket",  "pkt_round"),
    },
}
PROBE_PAGE_KEYS = (("POUT", "OUT"), ("PIN", "IN"), ("PANG", "ANGLE"),
                   ("PBOSS", "BOSS"), ("PRIDGE", "RIDGE"), ("PCAL", "CAL"))

# Which macros take the extra args. Derived from the pages so they cannot
# drift; the centre Z cell rides on the ANGLE page but is a plain 15-arg call.
CAL_MACROS = frozenset(m for m, _k in PROBE_PAGES["PCAL"].values())
ANG_MACROS = frozenset(m for m, _k in PROBE_PAGES["PANG"].values()) - {PZ[0]}

# Which P.SET fields each cal cell actually reads: the nominal it positions
# off, and the known-true size it compares against. Not always the same field.
CAL_FIELDS = {
    "probe_cal_round_boss":   ("cal_dia",),
    "probe_cal_round_pocket": ("dia_hint", "cal_dia"),
    "probe_cal_square_boss":  ("x_hint", "y_hint", "cal_x_width", "cal_y_width"),
    "probe_cal_square_pocket": ("x_hint", "y_hint", "cal_x_width", "cal_y_width"),
}

FEAT_C  = settings.color("PROBE_FEATURE", (60, 200, 90))   # workpiece faces
START_C = settings.color("PROBE_START", (250, 60, 60))     # probe start position
PATH_C  = settings.color("PROBE_PATH", (180, 120, 230))    # probe motion

# tool length probe diagram

TOOL_SENSOR_MACRO = "tool_length"

# POS-screen home indicators (fed by the status-pane HAL bit pins)
HOME_C = settings.color("HOME_MARK", (255, 255, 255))   # datum mark: axis at its reference position


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------
def render_text(renderer, font, message, color=WHITE):
    """Make a texture from a string. Caller owns it and must destroy it."""
    surf = TTF_RenderText_Blended(font, message.encode(), color)
    tex = SDL_CreateTextureFromSurface(renderer, surf)
    SDL_FreeSurface(surf)
    return tex


def blit_text(renderer, tex, x, y):
    w = ctypes.c_int(0)
    h = ctypes.c_int(0)
    SDL_QueryTexture(tex, None, None, ctypes.byref(w), ctypes.byref(h))
    dst = SDL_Rect(x, y, w.value, h.value)
    SDL_RenderCopy(renderer, tex, None, dst)


def draw_line(renderer, font, text, x, y, color=WHITE):
    """Convenience: render+blit+destroy in one call for uncached text."""
    tex = render_text(renderer, font, text, color)
    blit_text(renderer, tex, x, y)
    SDL_DestroyTexture(tex)


def text_width(font, s):
    """Pixel width of a string in the given font (for word highlighting)."""
    w = ctypes.c_int(0)
    h = ctypes.c_int(0)
    TTF_SizeUTF8(font, s.encode(), ctypes.byref(w), ctypes.byref(h))
    return w.value


def space_words(text):
    """M3S1000 -> M3 S1000. Insert a space before each address letter that
    starts a new word. Leaves comments, existing spacing, and decimals alone."""
    parts = re.split(r'(\([^)]*\))', text)
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:                       # a comment, pass through
            out.append(seg)
            continue
        seg = re.sub(r'(?<=[\d.])\s*(?=[A-Za-z])', ' ', seg)
        seg = re.sub(r'[ \t]+', ' ', seg)
        out.append(seg)
    return "".join(out).strip()


# ---------------------------------------------------------------------------
# Probe scenario icons — grid cells and the big diagram share one routine,
# so they can never disagree. green = feature faces, red = probe start,
# purple = probe motion.
# ---------------------------------------------------------------------------
def _pc(renderer, rgb):
    SDL_SetRenderDrawColor(renderer, rgb[0], rgb[1], rgb[2], 255)


def draw_circle(renderer, cx, cy, r, seg=14):
    px = py = None
    for i in range(seg + 1):
        a = 2 * math.pi * i / seg
        x, y = int(cx + r * math.cos(a)), int(cy + r * math.sin(a))
        if px is not None:
            SDL_RenderDrawLine(renderer, px, py, x, y)
        px, py = x, y


def draw_home_symbol(renderer, cx, cy, r):
    """Datum mark: circle with two opposed quadrants filled (top-left and
    bottom-right). Drawn only while the axis is at its reference position —
    absence of the mark is the 'not referenced' state, so there is nothing
    to draw for false or unknown."""
    cx, cy, r = int(cx), int(cy), int(r)
    _pc(renderer, HOME_C)
    for dy in range(-r, 0):                     # top-left quadrant
        dx = int(math.sqrt(max(r * r - dy * dy, 0)))
        SDL_RenderDrawLine(renderer, cx - dx, cy + dy, cx, cy + dy)
    for dy in range(0, r + 1):                  # bottom-right quadrant
        dx = int(math.sqrt(max(r * r - dy * dy, 0)))
        SDL_RenderDrawLine(renderer, cx, cy + dy, cx + dx, cy + dy)
    draw_circle(renderer, cx, cy, r, seg=48)


def draw_arrow(renderer, x0, y0, x1, y1, head=7):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    SDL_RenderDrawLine(renderer, x0, y0, x1, y1)
    ang = math.atan2(y1 - y0, x1 - x0)
    for da in (2.6, -2.6):
        SDL_RenderDrawLine(renderer, x1, y1,
                           int(x1 + head * math.cos(ang + da)),
                           int(y1 + head * math.sin(ang + da)))


def draw_probe_icon(renderer, kind, r, c, x, y, s):
    """One probe scenario drawn to fit the square at (x, y, s)."""
    q = s / 5.0
    cx, cy = x + s / 2.0, y + s / 2.0
    hd = max(4, int(q * 0.45))          # arrowhead size
    rad = max(3, int(q * 0.42))         # start-circle radius

    def feat(x0, y0, x1, y1):
        _pc(renderer, FEAT_C)
        SDL_RenderDrawLine(renderer, int(x0), int(y0), int(x1), int(y1))

    def start(px, py):
        _pc(renderer, START_C)
        draw_circle(renderer, px, py, rad)

    def path(x0, y0, x1, y1):
        _pc(renderer, PATH_C)
        draw_arrow(renderer, x0, y0, x1, y1, hd)

    def leg(x0, y0, x1, y1):            # non-arrow leg of an L path
        _pc(renderer, PATH_C)
        SDL_RenderDrawLine(renderer, int(x0), int(y0), int(x1), int(y1))

    if kind == "z":
        feat(cx - 1.3 * q, cy + 1.2 * q, cx + 1.3 * q, cy + 1.2 * q)
        start(cx, cy - 1.3 * q)
        path(cx, cy - 0.7 * q, cx, cy + 0.85 * q)
        return

    # Inside walls: the *_wco macros make no Z move and no step-off, so the
    # tip is already down in the pocket and simply drives out to one wall.
    if kind == "edge_in":
        if r in (0, 2):                            # horizontal wall
            fy = (y + 1.0 * q) if r == 0 else (y + s - 1.0 * q)
            d = -1.0 if r == 0 else 1.0
            feat(cx - 1.7 * q, fy, cx + 1.7 * q, fy)
            start(cx, cy)
            path(cx, cy + d * 0.7 * q, cx, fy - d * 0.35 * q)
        else:                                      # vertical wall
            fx = (x + 1.0 * q) if c == 0 else (x + s - 1.0 * q)
            d = -1.0 if c == 0 else 1.0
            feat(fx, cy - 1.7 * q, fx, cy + 1.7 * q)
            start(cx, cy)
            path(cx + d * 0.7 * q, cy, fx - d * 0.35 * q, cy)
        return

    # Outside edge: start sits ON the edge. The step-off is the first move,
    # then it drops beside the stock and probes back into the face.
    if kind == "edge_out":
        if r in (0, 2):                            # horizontal face
            d = -1.0 if r == 0 else 1.0            # outward, away from stock
            feat(cx - 1.8 * q, cy, cx + 1.8 * q, cy)
            start(cx, cy)
            leg(cx, cy, cx, cy + d * 1.7 * q)
            path(cx, cy + d * 1.7 * q, cx, cy + d * 0.35 * q)
        else:                                      # vertical face
            d = -1.0 if c == 0 else 1.0
            feat(cx, cy - 1.8 * q, cx, cy + 1.8 * q)
            start(cx, cy)
            leg(cx, cy, cx + d * 1.7 * q, cy)
            path(cx + d * 1.7 * q, cy, cx + d * 0.35 * q, cy)
        return

    # Edge angle: two touches on one face, edge_width apart. Which way the
    # second point travels differs per macro — front runs right, back left,
    # left toward the front, right toward the back.
    if kind == "ang_edge":
        tilt = 0.4 * q
        if r in (0, 2):
            d  = -1.0 if r == 0 else 1.0           # outward
            s2 = -1.0 if r == 0 else 1.0           # along the edge
            feat(cx - 1.9 * q, cy - tilt, cx + 1.9 * q, cy + tilt)
            for i, t in enumerate((-0.8, 0.8)):
                px = cx + s2 * t * q
                fy = cy + tilt * (px - cx) / (1.9 * q)
                if i == 0:
                    start(px, fy)
                path(px, fy + d * 1.6 * q, px, fy + d * 0.35 * q)
        else:
            d  = -1.0 if c == 0 else 1.0
            s2 = 1.0 if c == 0 else -1.0
            feat(cx - tilt, cy - 1.9 * q, cx + tilt, cy + 1.9 * q)
            for i, t in enumerate((-0.8, 0.8)):
                py = cy + s2 * t * q
                fx = cx + tilt * (py - cy) / (1.9 * q)
                if i == 0:
                    start(fx, py)
                path(fx + d * 1.6 * q, py, fx + d * 0.35 * q, py)
        return

    # Outside corner: start is over the corner itself. It steps off in X,
    # drops, probes the X face; then lifts, moves diagonally and probes Y.
    if kind == "cnr_out":
        hx = 1.0 if c == 0 else -1.0        # horizontal face extends this way
        vy = 1.0 if r == 0 else -1.0        # vertical face extends this way
        feat(cx, cy, cx + hx * 2.1 * q, cy)
        feat(cx, cy, cx, cy + vy * 2.1 * q)
        start(cx, cy)
        ay = cy + vy * 0.9 * q                           # touch the V face
        leg(cx, cy, cx - hx * 1.7 * q, ay)
        path(cx - hx * 1.7 * q, ay, cx - hx * 0.35 * q, ay)
        bx = cx + hx * 0.9 * q                           # touch the H face
        leg(cx, cy, bx, cy - vy * 1.7 * q)
        path(bx, cy - vy * 1.7 * q, bx, cy - vy * 0.35 * q)
        return

    # Corner + angle: three touches. One face once, the adjacent face twice
    # so the edge angle falls out of the pair.
    if kind == "ang_cnr":
        hx = 1.0 if c == 0 else -1.0
        vy = 1.0 if r == 0 else -1.0
        feat(cx, cy, cx + hx * 2.1 * q, cy)
        feat(cx, cy, cx, cy + vy * 2.1 * q)
        start(cx, cy)
        if (r == 0) == (c == 0):            # doubled face is the vertical one
            path(cx + hx * 0.9 * q, cy - vy * 1.7 * q,
                 cx + hx * 0.9 * q, cy - vy * 0.35 * q)
            for t in (0.7, 1.75):
                path(cx - hx * 1.7 * q, cy + vy * t * q,
                     cx - hx * 0.35 * q, cy + vy * t * q)
        else:                               # doubled face is the horizontal
            path(cx - hx * 1.7 * q, cy + vy * 0.9 * q,
                 cx - hx * 0.35 * q, cy + vy * 0.9 * q)
            for t in (0.7, 1.75):
                path(cx + hx * t * q, cy - vy * 1.7 * q,
                     cx + hx * t * q, cy - vy * 0.35 * q)
        return

    # Inside corner: start over the inside corner, step diagonally into the
    # pocket, one plunge, then probe out to each wall in turn.
    if kind == "cnr_in":
        hx = 1.0 if c == 0 else -1.0
        vy = 1.0 if r == 0 else -1.0
        vx = x + 1.1 * q if c == 0 else x + s - 1.1 * q
        vyp = y + 1.1 * q if r == 0 else y + s - 1.1 * q
        feat(vx, vyp, vx + hx * 2.2 * q, vyp)
        feat(vx, vyp, vx, vyp + vy * 2.2 * q)
        px, py = vx + hx * 1.5 * q, vyp + vy * 1.5 * q   # after the step-off
        start(vx, vyp)
        leg(vx, vyp, px, py)
        path(px - hx * 0.2 * q, py, vx + hx * 0.35 * q, py)
        path(px, py - vy * 0.2 * q, px, vyp + vy * 0.35 * q)
        return

    if kind in ("boss_rect", "boss_round", "pkt_rect", "pkt_round"):
        boss = kind.startswith("boss")
        _pc(renderer, FEAT_C)
        if kind.endswith("round"):
            draw_circle(renderer, cx, cy, 1.35 * q)
        else:
            SDL_RenderDrawRect(renderer, SDL_Rect(int(cx - 1.5 * q),
                                                  int(cy - 1.15 * q),
                                                  int(3.0 * q), int(2.3 * q)))
        # Both start centred over the feature. The boss steps out past each
        # face and probes back in; the pocket plunges and probes outward.
        hw = 1.35 * q if kind.endswith("round") else 1.5 * q
        hh = 1.35 * q if kind.endswith("round") else 1.15 * q
        start(cx, cy)
        if boss:                                   # approach inward
            path(cx - 2.2 * q, cy, cx - (hw + 0.3 * q), cy)
            path(cx + 2.2 * q, cy, cx + (hw + 0.3 * q), cy)
            path(cx, cy + 2.2 * q, cx, cy + (hh + 0.3 * q))
            path(cx, cy - 2.2 * q, cx, cy - (hh + 0.3 * q))
        else:                                      # from centre outward
            path(cx - 0.35 * q, cy, cx - (hw - 0.25 * q), cy)
            path(cx + 0.35 * q, cy, cx + (hw - 0.25 * q), cy)
            path(cx, cy - 0.35 * q, cx, cy - (hh - 0.25 * q))
            path(cx, cy + 0.35 * q, cx, cy + (hh - 0.25 * q))
        return

    if kind in ("ridge_x", "ridge_y", "valley_x", "valley_y"):
        ridge = kind.startswith("ridge")
        horiz = kind.endswith("_x")                # motion along X
        _pc(renderer, FEAT_C)
        if ridge:
            if horiz:
                SDL_RenderDrawRect(renderer, SDL_Rect(int(cx - 0.9 * q),
                                                      int(cy - 1.6 * q),
                                                      int(1.8 * q), int(3.2 * q)))
                start(cx, cy)
                path(cx - 2.0 * q, cy, cx - 1.15 * q, cy)
                path(cx + 2.0 * q, cy, cx + 1.15 * q, cy)
            else:
                SDL_RenderDrawRect(renderer, SDL_Rect(int(cx - 1.6 * q),
                                                      int(cy - 0.9 * q),
                                                      int(3.2 * q), int(1.8 * q)))
                start(cx, cy)
                path(cx, cy - 2.0 * q, cx, cy - 1.15 * q)
                path(cx, cy + 2.0 * q, cx, cy + 1.15 * q)
        else:                                      # valley: gap in the middle
            if horiz:
                feat(cx - 2.0 * q, cy - 1.4 * q, cx - 0.9 * q, cy - 1.4 * q)
                feat(cx - 0.9 * q, cy - 1.4 * q, cx - 0.9 * q, cy + 1.4 * q)
                feat(cx + 2.0 * q, cy - 1.4 * q, cx + 0.9 * q, cy - 1.4 * q)
                feat(cx + 0.9 * q, cy - 1.4 * q, cx + 0.9 * q, cy + 1.4 * q)
                start(cx, cy)
                path(cx - 0.3 * q, cy, cx - 0.7 * q, cy)
                path(cx + 0.3 * q, cy, cx + 0.7 * q, cy)
            else:
                feat(cx - 1.4 * q, cy - 2.0 * q, cx - 1.4 * q, cy - 0.9 * q)
                feat(cx - 1.4 * q, cy - 0.9 * q, cx + 1.4 * q, cy - 0.9 * q)
                feat(cx - 1.4 * q, cy + 2.0 * q, cx - 1.4 * q, cy + 0.9 * q)
                feat(cx - 1.4 * q, cy + 0.9 * q, cx + 1.4 * q, cy + 0.9 * q)
                start(cx, cy)
                path(cx, cy - 0.3 * q, cx, cy - 0.7 * q)
                path(cx, cy + 0.3 * q, cx, cy + 0.7 * q)
        return


# ---------------------------------------------------------------------------
# Input buffer + field cursor (single definitions — do not duplicate)
# ---------------------------------------------------------------------------
class InputBuffer:
    """Global key buffer. Fed by SDL_TEXTINPUT, always uppercase."""
    ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789()?,@#=*-.[]&+/; ")
    MAXLEN = 64
    MAXLEN_RAW = 240                  # loadrt names= lists run long

    def __init__(self):
        self.text = ""
        # raw: keep case and accept every printable ASCII char. Turned on by
        # the SYSTEM editor pages (hal pin names and paths are case-sensitive)
        # and off again when that screen is left.
        self.raw = False

    def feed(self, s):                # printable chars from SDL_TEXTINPUT
        if self.raw:
            for ch in s:
                if 32 <= ord(ch) < 127 and len(self.text) < self.MAXLEN_RAW:
                    self.text += ch
            return
        for ch in s.upper():
            if ch in self.ALLOWED and len(self.text) < self.MAXLEN:
                self.text += ch

    def backspace(self):
        self.text = self.text[:-1]

    def clear(self):                  # bind to a CAN softkey
        self.text = ""

    def take(self):                   # commit: returns contents and empties
        t, self.text = self.text, ""
        return t


class Field:
    __slots__ = ("x", "y", "w", "h", "setter", "getter")
    def __init__(self, x, y, w, h, setter=None, getter=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.setter = setter          # called with display-units text on commit
        self.getter = getter          # must return DISPLAY units (+INPUT adds
                                      # typed display value to it, then setter
                                      # divides back to machine)


class FieldCursor:
    """Grid of selectable fields: up/down move by rows, left/right by one."""
    def __init__(self, cols=1):
        self.fields = []
        self.idx = 0
        self.cols = cols

    def add(self, x, y, w, h, setter=None, getter=None):
        self.fields.append(Field(x, y, w, h, setter, getter))
        return len(self.fields) - 1

    def move(self, d):                    # rows
        if self.fields:
            self.idx = (self.idx + d * self.cols) % len(self.fields)

    def move_h(self, d):                  # columns
        if self.fields:
            self.idx = (self.idx + d) % len(self.fields)

    def goto_row(self, row):
        if self.fields:
            self.idx = max(0, min(row * self.cols, len(self.fields) - 1))

    def row(self):
        return self.idx // self.cols

    def is_current(self, i):
        return bool(self.fields) and i == self.idx

    def current(self):
        return self.fields[self.idx] if self.fields else None

    def commit(self, text):
        f = self.current()
        if f and text and f.setter:
            f.setter(text)
            return True
        return False

    def commit_add(self, text):           # +INPUT
        f = self.current()
        if f and text and f.setter and f.getter:
            try:
                f.setter(str(f.getter() + float(text)))
                return True
            except ValueError:
                pass
        return False


def draw_field(renderer, font, text, i, cursor, pad=6):
    """Draw one field; reverse-video if the cursor is on it."""
    f = cursor.fields[i]
    if cursor.is_current(i):
        SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
        SDL_RenderFillRect(renderer, SDL_Rect(f.x, f.y, f.w, f.h))
        draw_line(renderer, font, text, f.x + pad, f.y + 2, BLACK)
    else:
        draw_line(renderer, font, text, f.x + pad, f.y + 2)


# ---------------------------------------------------------------------------
# Softkey machinery
# ---------------------------------------------------------------------------
# Softkey label colouring. The kind is looked up from the label, so menu
# definitions stay uncluttered; pass kind= explicitly where the same label
# means something different (e.g. CLEAR).
PAGE, ENTRY, ACT, MENU = "page", "entry", "act", "menu"

KEY_KIND = {
    # page / view selectors — change what the screen shows
    "PRGRM": PAGE, "EDIT": PAGE, "DIR": PAGE, "USB": PAGE, "CHECK": PAGE,
    "CURRNT": PAGE, "NEXT": PAGE, "RSTR": PAGE, "MDI": PAGE,
    "OFFSET": PAGE, "SETING": PAGE, "WORK": PAGE, "PROBE": PAGE,
    "WEAR": PAGE, "GEOM": PAGE, "ABS": PAGE, "REL": PAGE, "MACH": PAGE,
    "ALL": PAGE, "TOGO": PAGE, "P.SET": PAGE,
    "OUT": PAGE, "IN": PAGE, "ANGLE": PAGE, "BOSS": PAGE, "RIDGE": PAGE,
    "CAL": PAGE,
    "XY": PAGE, "XZ": PAGE, "YZ": PAGE, "ISO": PAGE,
    "ZOOM+": PAGE, "ZOOM-": PAGE, "<-": PAGE, "->": PAGE, "UP": PAGE,
    "DOWN": PAGE, "FIT": PAGE, "REDRAW": PAGE,
    "TEXT": PAGE, "FIELDS": PAGE, "INFO": PAGE, "PINS": PAGE,
    "SEC \\/": PAGE, "SEC /\\": PAGE,
    "PHYS": PAGE, "HAL": PAGE, "GRP \\/": PAGE, "GRP /\\": PAGE,
    "HOLD": PAGE, "LOG": PAGE,
    # data entry / search / edit — consume the typed buffer or move a cursor
    "INPUT": ENTRY, "+INPUT": ENTRY, "INP.C.": ENTRY,
    "O SRH": ENTRY, "N SRH": ENTRY, "NO.SRH": ENTRY, "SEARCH": ENTRY,
    "SRH \\/": ENTRY, "SRH /\\": ENTRY,
    "INSERT": ENTRY, "ALTER": ENTRY, "ALT.LIN": ENTRY,
    "DEL.WRD": ENTRY, "DEL.LIN": ENTRY, "PRESET": ENTRY,
    # machine / file actions
    "EXEC": ACT, "MEASUR": ACT, "Z.PRB": ACT, "SELECT": ACT, "SAVE": ACT,
    "READ": ACT, "PUNCH": ACT, "DELETE": ACT, "NEW": ACT, "REFRSH": ACT,
    "REWIND": ACT, "ORIGIN": ACT, "PTSPRE": ACT, "EXIT": ACT,
    "APPLY": ACT, "COPY": ACT, "RESTOR": ACT, "RESTRT": ACT,
    "REGEN": ACT,
    # menus and back-outs
    "(OPRT)": MENU, "CLEAR": MENU, "CAN": MENU, "CANCEL": MENU,
    "RETURN": MENU, "<": MENU, ">": MENU,
    "CLR.CNT": MENU,
}

KEY_COLORS = {
    PAGE:  settings.sdl_color("KEY_PAGE", (0, 100, 255)),   # changes what's displayed
    ENTRY: settings.sdl_color("KEY_ENTRY", (0, 255, 0)),   # consumes the typed buffer
    ACT:   settings.sdl_color("KEY_ACT", (255, 0, 0)),     # commands machine / files
    MENU:  settings.sdl_color("KEY_MENU", (255, 255, 255)), # menus, cancel, back
}
# active-key fill; also every reverse-video row/field/word cursor
KEY_HILITE = settings.color("HILITE", (230, 230, 230))

HELP_DIM_C    = settings.color("HELP_DIM", (0, 0, 0, 200))   # veil behind help
HELP_BORDER_C = settings.color("HELP_BORDER", (60, 120, 220))
HELP_TITLE    = settings.sdl_color("HELP_TITLE", (120, 190, 255))


class K:
    """One softkey: label + zero-arg action. Blank label = dead key."""
    __slots__ = ("label", "action", "kind")
    def __init__(self, label="", action=None, kind=None):
        self.label = label
        self.action = action
        self.kind = kind or KEY_KIND.get(label, MENU)


def _pad10(items):
    items = list(items)[:10]
    return items + [K()] * (10 - len(items))


class Screen:
    """Base screen. Softkey menus are a stack of levels; each level is a
    list of pages; each page is exactly 10 K items (slots F2..F11).
    Slot 0 ([<], F1) and slot 11 ([>], F12) are auto-managed:
      [<] shows only when there's a level to return to,
      [>] shows only when the current level has multiple pages."""

    def __init__(self, app):
        self.app = app
        self._stack = []
        self.cursor = None
        self.set_root([K()] * 10)

    # ---- menu machinery ----
    @staticmethod
    def _as_pages(x):
        return x if (x and isinstance(x[0], list)) else [x]

    def set_root(self, pages):
        self._stack = [[[_pad10(p) for p in self._as_pages(pages)], 0]]

    def push(self, pages):
        self._stack.append([[_pad10(p) for p in self._as_pages(pages)], 0])

    def pop(self):
        if len(self._stack) > 1:
            self._stack.pop()

    def next_page(self):
        top = self._stack[-1]
        top[1] = (top[1] + 1) % len(top[0])

    def _page(self):
        pages, idx = self._stack[-1]
        return pages[idx]

    # ---- called by the manager ----
    def active_keys(self):
        """Labels naming the chapter/sub-mode currently on display. Those
        softkeys are drawn boxed. Override per screen."""
        return ()

    def softkey_items(self):
        """12 (label, kind, active) slots: [<], ten keys, [>]."""
        pages, _ = self._stack[-1]
        left  = "<" if len(self._stack) > 1 else ""
        right = ">" if len(pages) > 1 else ""
        # Only the root level names chapters; a pushed level may reuse one of
        # those labels for something else (CLEAR -> GEOM), which must not box.
        act = self.active_keys() if len(self._stack) == 1 else ()
        items = [(left, MENU, False)]
        items += [(it.label, it.kind, bool(it.label) and it.label in act)
                  for it in self._page()]
        items.append((right, MENU, False))
        return items

    def on_softkey(self, i):
        if i == 0:
            self.pop()
        elif i == 11:
            self.next_page()
        else:
            item = self._page()[i - 1]
            if item.action:
                item.action()

    # ---- override per screen ----
    def on_enter(self):
        pass

    def tick(self):
        """Per-frame work that must run even while another screen is shown
        (ScreenManager.draw calls it on every screen). Default: nothing."""

    def on_leave(self):
        """Called by the manager before another screen is shown."""
        pass

    def on_touch(self, x, y):
        """A press inside the content area. Return True if consumed."""
        return False

    def on_key(self, sc):
        if self.cursor:
            if sc == SDL_SCANCODE_UP:       self.cursor.move(-1);   return True
            if sc == SDL_SCANCODE_DOWN:     self.cursor.move(+1);   return True
            if sc == SDL_SCANCODE_LEFT:     self.cursor.move_h(-1); return True
            if sc == SDL_SCANCODE_RIGHT:    self.cursor.move_h(+1); return True
            if sc == SDL_SCANCODE_PAGEUP:   self.cursor.move(-8);   return True
            if sc == SDL_SCANCODE_PAGEDOWN: self.cursor.move(+8);   return True
            if sc == SDL_SCANCODE_RETURN:
                return self.cursor.commit(self.app.input.take())
        return False

    def draw(self, renderer, area):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
class PosScreen(Screen):

    # home-indicator column, clear of the widest large-font DRO row
    HOME_X = 1200
    HOME_R = 34

    def _dro_pitch(self):
        return 150 if len(self.app.axes) > 3 else 200

    def _at_home(self, ax):
        pane = getattr(self.app, "pane", None)
        return pane.at_home(ax) if pane is not None else None

    def on_enter(self):
        self.sub = getattr(self, "sub", "MACH")     # keep last sub-page
        self.set_root([
            K("ABS",  lambda: setattr(self, "sub", "ABS")),
            K("REL",  lambda: setattr(self, "sub", "REL")),
            K("MACH", lambda: setattr(self, "sub", "MACH")),
            K("ALL",  lambda: setattr(self, "sub", "ALL")),
            K("TOGO", lambda: setattr(self, "sub", "TOGO")),
            K(), K(), K(), K(),
            K("(OPRT)", self._oprt),
        ])

    def active_keys(self):
        return (self.sub,)

    def _oprt(self):
        self.push([
            K("ORIGIN", self._zero_rel),
            K("PRESET", self._preset),
            K("PTSPRE", lambda: self.push([          # parts counter reset
                K("CAN", self.pop),
                K("EXEC", lambda: (self.app.pane.reset_parts(), self.pop())),
            ])),
        ])

    def _preset(self):
        """Set the relative counter: type axis+value (e.g. X5.0), press PRESET.
        Typed value is in display units."""
        txt = self.app.input.take().strip()
        if not txt or txt[0] not in self.app.axes:
            return
        try:
            val = float(txt[1:])
        except ValueError:
            return
        ax = txt[0]
        idx = AXIS_IDX[ax]
        val_m = self.app.disp_to_machine(ax, val)
        self.app.rel_origin[idx] = self.app.stat.actual_position[idx] - val_m

    def _zero_rel(self):
        st = self.app.stat
        for ax in self.app.axes:
            idx = AXIS_IDX[ax]
            self.app.rel_origin[idx] = st.actual_position[idx]

    def _rel(self, st, idx):
        return st.actual_position[idx] - self.app.rel_origin[idx]

    def _work(self, st, idx):
        return (st.actual_position[idx] - st.g5x_offset[idx]
                - st.g92_offset[idx] - st.tool_offset[idx])

    def draw(self, renderer, area):
        st, lf, f = self.app.stat, self.app.large_font, self.app.font
        app = self.app
        axes = app.axes
        pitch = self._dro_pitch()

        def rows(valfn, title, home=False):
            draw_line(renderer, f, title, 10, 80)
            for r, ax in enumerate(axes):
                idx = AXIS_IDX[ax]
                y = 120 + r * pitch
                draw_line(renderer, lf,
                          "{}  {}".format(ax, app.fmt_axis(ax, valfn(idx))),
                          10, y)
                # reference-position mark: machine coordinates only, and only
                # while the axis is actually at home
                if home and self._at_home(ax):
                    draw_home_symbol(renderer, self.HOME_X,
                                     y + TTF_FontHeight(lf) // 2, self.HOME_R)

        if self.sub == "MACH":
            rows(lambda i: st.position[i], "MACHINE", home=True)
        elif self.sub == "ABS":
            rows(lambda i: self._work(st, i), "ABSOLUTE")
        elif self.sub == "REL":
            rows(lambda i: self._rel(st, i), "RELATIVE")
        elif self.sub == "TOGO":
            rows(lambda i: st.dtg[i], "TO GO")
        else:  # ALL — lower section y derived from axis count
            n = len(axes)
            y2 = 150 + n * 70 + 60
            draw_line(renderer, f, "MACHINE", 10, 80)
            draw_line(renderer, f, "ABSOLUTE", 960, 80)
            draw_line(renderer, f, "RELATIVE", 10, y2 - 70)
            draw_line(renderer, f, "TO GO", 960, y2 - 70)
            for r, ax in enumerate(axes):
                idx = AXIS_IDX[ax]
                draw_line(renderer, f, "{}  {}".format(
                    ax, app.fmt_axis(ax, st.position[idx])), 10, 150 + r * 70)
                draw_line(renderer, f, "{}  {}".format(
                    ax, app.fmt_axis(ax, self._work(st, idx))), 960, 150 + r * 70)
                draw_line(renderer, f, "{}  {}".format(
                    ax, app.fmt_axis(ax, self._rel(st, idx))), 10, y2 + r * 70)
                draw_line(renderer, f, "{}  {}".format(
                    ax, app.fmt_axis(ax, st.dtg[idx])), 960, y2 + r * 70)


def scan_modals(lines, upto):
    """Text-scan program lines[0:upto] and collect last-seen modal words.
    Pure text scan (not the interpreter) — for the RSTR/CURRNT displays.
    Returns dict; 'oword' flags any o-word sub/call before the target,
    where run-from-line is unreliable."""
    out = {"T": None, "S": None, "F": None, "spindle": "M5", "coolant": "M9",
           "units": None, "wcs": None, "tlo": None, "plane": None,
           "dist": None, "oword": False}
    word_re = re.compile(r'([A-Za-z])\s*([+-]?\d*\.?\d+)')
    for raw in lines[:upto]:
        s = re.sub(r'\([^)]*\)', '', raw).split(';')[0]
        if re.match(r'\s*[oO][\w<>]+\s+(sub|call|do|while|repeat|if)\b', s,
                    re.IGNORECASE):
            out["oword"] = True
        for letter, num in word_re.findall(s):
            L = letter.upper()
            try:
                v = float(num)
            except ValueError:
                continue
            if L == "T":
                out["T"] = int(v)
            elif L == "S":
                out["S"] = v
            elif L == "F":
                out["F"] = v
            elif L == "M":
                m = int(v)
                if m in (3, 4, 5):
                    out["spindle"] = f"M{m}"
                elif m in (7, 8, 9):
                    out["coolant"] = f"M{m}"
            elif L == "G":
                g = round(v, 1)
                if g in (20, 21):
                    out["units"] = f"G{int(g)}"
                elif 54 <= g <= 59.3:
                    out["wcs"] = f"G{g:g}"
                elif g in (43, 49):
                    out["tlo"] = f"G{int(g)}"
                elif g in (17, 18, 19):
                    out["plane"] = f"G{int(g)}"
                elif g in (90, 91):
                    out["dist"] = f"G{int(g)}"
    return out


class ProgScreen(Screen):
    """PROG screen. Chapters depend on task_mode:
      MANUAL: PRGRM  EDIT  DIR  USB        (EDIT-mode tree)
      AUTO:   PRGRM  CHECK CURRNT NEXT RSTR (MEM-mode tree)
      MDI:    PRGRM  MDI   CURRNT NEXT      (MDI-mode tree)
    BG-EDT, C.A.P, SCHDUL and EX-EDT block ops intentionally omitted."""

    LIST_ROWS = 13          # program text rows on PRGRM page
    FILE_ROWS = 10          # rows on DIR/USB pages
    EDIT_ROWS = 11          # rows in the editor

    _CHAPTERS_BY_MODE = {
        linuxcnc.MODE_MANUAL: ("PRGRM", "EDIT", "DIR", "USB"),
        linuxcnc.MODE_AUTO:   ("PRGRM", "CHECK", "CURRNT", "NEXT", "RSTR"),
        linuxcnc.MODE_MDI:    ("PRGRM", "MDI", "CURRNT", "NEXT"),
    }

    # ------------------------------------------------------------- lifecycle
    def on_enter(self):
        self.chapter = getattr(self, "chapter", "PRGRM")
        self.view_line = None            # None = follow execution
        self.check_sub = getattr(self, "check_sub", "ABS")
        self.note = ""                   # transient feedback line
        # editor state
        self.edit_lines = None           # working copy while editing
        self.edit_cur = 0
        self.edit_scroll = 0
        # file browser state
        self.dir_path = getattr(self, "dir_path", None) or self.app.prog_dir
        self.usb_path = None
        self.entries = []
        self.file_cur = 0
        self.file_scroll = 0
        # MDI history
        if not hasattr(self, "mdi_hist"):
            self.mdi_hist = deque(maxlen=30)
        # RSTR state
        self.rstr_line = None            # 1-based target line
        self.rstr_scan = None
        self._last_mode = self.app.stat.task_mode
        self._enter_chapter(self.chapter)

    def _valid_chapters(self):
        return self._CHAPTERS_BY_MODE.get(self.app.stat.task_mode,
                                          ("PRGRM",))

    def active_keys(self):
        if self.chapter == "CHECK":
            return (self.chapter, self.check_sub)
        return (self.chapter,)

    def _enter_chapter(self, name):
        if name not in self._valid_chapters():
            name = "PRGRM"
        self.chapter = name
        self.note = ""
        self.cursor = None               # no FieldCursor pages here
        if name == "EDIT":
            self._edit_begin()
            return                       # _edit_begin sets its own softkeys
        if name in ("DIR", "USB"):
            self._refresh_files()
        self.set_root(self._root_row())

    # ------------------------------------------------------------- root menu
    def _root_row(self):
        row = []
        for name in self._valid_chapters():
            row.append(K(name, lambda n=name: self._enter_chapter(n)))
        # chapter-specific extra root keys
        if self.chapter == "CHECK":
            row.append(K("ABS", lambda: setattr(self, "check_sub", "ABS")))
            row.append(K("REL", lambda: setattr(self, "check_sub", "REL")))
        elif self.chapter == "RSTR":
            row.append(K("SEARCH", self._rstr_search))
            row.append(K("EXEC",   self._rstr_exec))
            row.append(K("CAN",    self._rstr_cancel))
        while len(row) < 9:
            row.append(K())
        row.append(K("(OPRT)", self._oprt))
        return row

    # ------------------------------------------------------------- OPRT
    def _oprt(self):
        if self.chapter == "PRGRM":
            self.push([
                K("O SRH", self._o_srh),
                K("N SRH", self._n_srh),
                K("SRH \\/", lambda: self._srh(+1)),
                K("SRH /\\", lambda: self._srh(-1)),
                K("REWIND", self._rewind),
            ])
        elif self.chapter == "MDI":
            self.push([
                K("EXEC",  self._mdi_exec),
                K("CLEAR", self._mdi_clear, kind=ENTRY),
            ])
        elif self.chapter in ("DIR", "USB"):
            keys = [
                K("SELECT", self._file_select),
                K("O SRH",  self._file_o_srh),
                K("NEW",    self._file_new),
                K("DELETE", lambda: self._confirm(self._file_delete)),
                K("REFRSH", self._refresh_files),
            ]
            if self.chapter == "USB":
                keys.insert(3, K("READ", self._file_read_menu))
            else:
                keys.insert(3, K("PUNCH", lambda: self._confirm(self._file_punch)))
            self.push(keys)
        # CHECK/CURRNT/NEXT: nothing beyond the dropped BG-EDT

    def _confirm(self, exec_fn):
        self.push([K("CAN", self.pop),
                   K("EXEC", lambda: (exec_fn(), self.pop()))])

    # ------------------------------------------------------------- PRGRM verbs
    def _rewind(self):
        self.view_line = 0

    def _o_srh(self):
        """Load a program from prog_dir by O-number (matches file stem)."""
        txt = self.app.input.take().strip().lstrip("O")
        try:
            n = int(txt)
        except ValueError:
            return
        stems = {f"O{n}", f"O{n:04d}", f"O{n:05d}"}
        try:
            names = sorted(os.listdir(self.app.prog_dir))
        except OSError:
            self.note = "PROGRAM DIR NOT FOUND"
            return
        for name in names:
            stem = os.path.splitext(name)[0].upper()
            if stem in stems:
                self._load(os.path.join(self.app.prog_dir, name))
                return
        self.note = f"O{n} NOT FOUND"

    # ---- search helpers: the same verbs serve PRGRM (view cursor over the
    # loaded program) and EDIT (line cursor over the unsaved working copy)
    def _in_editor(self):
        return self.chapter == "EDIT" and self.edit_lines is not None

    def _search_lines(self):
        return self.edit_lines if self._in_editor() else self.app.ndisp.lines()

    def _search_start(self):
        if self._in_editor():
            return self.edit_cur
        st = self.app.stat
        return (self.view_line if self.view_line is not None
                else max(0, (st.motion_line or st.current_line) - 1))

    def _search_goto(self, i):
        if self._in_editor():
            self.edit_cur = i
            self.edit_word = 0          # land on the first word of the hit
            self._clamp_word()
        else:
            self.view_line = i

    def _n_srh(self):
        txt = self.app.input.take().strip().lstrip("N")
        try:
            n = int(txt)
        except ValueError:
            return
        pat = re.compile(rf'[Nn]0*{n}\b')
        for i, line in enumerate(self._search_lines()):
            if pat.search(line):
                self._search_goto(i)
                return
        self.note = f"N{n} NOT FOUND"

    def _srh(self, direction):
        """Address search: find buffer text below/above the current line."""
        pat = self.app.input.take().strip()
        if not pat:
            return
        lines = self._search_lines()
        start = self._search_start()
        rng = (range(start + 1, len(lines)) if direction > 0
               else range(start - 1, -1, -1))
        for i in rng:
            if pat in lines[i].upper():
                self._search_goto(i)
                return
        self.note = f"{pat} NOT FOUND"

    def _load(self, path):
        try:
            self.app.reload_program(path)
            self.view_line = 0
            self.note = f"LOADED {os.path.basename(path)}"
        except linuxcnc.error as e:
            self.note = str(e)

    # ------------------------------------------------------------- editor
    def _edit_begin(self):
        st = self.app.stat
        if st.interp_state != linuxcnc.INTERP_IDLE:
            self.note = "CANNOT EDIT WHILE RUNNING"
            self.chapter = "PRGRM"
            self.set_root(self._root_row())
            return
        if not st.file:
            self.note = "NO PROGRAM LOADED"
            self.chapter = "PRGRM"
            self.set_root(self._root_row())
            return
        self.edit_path = st.file         # the file actually opened
        try:
            with open(st.file) as f:
                self.edit_lines = [l.rstrip("\n") for l in f.readlines()]
        except OSError:
            self.edit_lines = []
        if not self.edit_lines:
            self.edit_lines = [""]
        self.edit_cur = 0
        self.edit_word = 0
        self.edit_scroll = 0
        self.set_root([
            K("INSERT",  self._edit_insert_word),
            K("ALTER",   self._edit_alter),
            K("DEL.WRD", self._edit_delete_word),
            K("ALT.LIN", self._edit_alter_line),
            K("DEL.LIN", self._edit_delete_line),
            K("N SRH",   self._n_srh),
            K("SRH \\/", lambda: self._srh(+1)),
            K("SRH /\\", lambda: self._srh(-1)),
            K("SAVE",   lambda: self._confirm(self._edit_save)),
            K("CANCEL", self._edit_cancel),
        ])

    def _edit_words(self):
        """[(start, end)] spans of whitespace-separated words on the
        cursored line."""
        line = self.edit_lines[self.edit_cur]
        return [(m.start(), m.end()) for m in re.finditer(r'\S+', line)]

    def _clamp_word(self):
        n = len(self._edit_words())
        self.edit_word = max(0, min(self.edit_word, max(0, n - 1)))

    def _edit_insert_word(self):
        """INSERT: buffer text becomes new word(s) AFTER the selected word
        (start of line if the line is empty)."""
        text = space_words(self.app.input.take()).strip()
        if not text:
            return
        line = self.edit_lines[self.edit_cur]
        words = self._edit_words()
        if not words:
            self.edit_lines[self.edit_cur] = text
            self.edit_word = 0
        else:
            _s, end = words[self.edit_word]
            new = line[:end] + " " + text + line[end:]
            self.edit_lines[self.edit_cur] = re.sub(r'[ \t]+', ' ', new).strip()
            self.edit_word += 1
        self._clamp_word()

    def _edit_alter(self):
        """ALTER: replace the selected word with the buffer text."""
        text = space_words(self.app.input.take()).strip()
        words = self._edit_words()
        if not text or not words:
            return
        s, e = words[self.edit_word]
        line = self.edit_lines[self.edit_cur]
        self.edit_lines[self.edit_cur] = line[:s] + text + line[e:]

    def _edit_delete_word(self):
        """DEL.WRD: remove the selected word."""
        words = self._edit_words()
        if not words:
            return
        s, e = words[self.edit_word]
        line = self.edit_lines[self.edit_cur]
        self.edit_lines[self.edit_cur] = re.sub(r'[ \t]+', ' ',
                                                line[:s] + line[e:]).strip()
        self._clamp_word()

    def _edit_alter_line(self):
        """ALT.LIN: replace the whole cursored line with the buffer text."""
        self.edit_lines[self.edit_cur] = space_words(self.app.input.take())
        self.edit_word = 0

    def _edit_insert(self):
        """RETURN key: buffer text becomes a new LINE after the cursored one."""
        text = space_words(self.app.input.take())
        self.edit_lines.insert(self.edit_cur + 1, text)
        self.edit_cur += 1
        self.edit_word = 0

    def _edit_delete_line(self):
        if self.edit_lines:
            self.edit_lines.pop(self.edit_cur)
        if not self.edit_lines:
            self.edit_lines = [""]
        self.edit_cur = min(self.edit_cur, len(self.edit_lines) - 1)
        self._clamp_word()

    def _edit_save(self):
        st = self.app.stat
        if st.interp_state != linuxcnc.INTERP_IDLE:
            self.note = "CANNOT SAVE - PROGRAM RUNNING"
            return
        path = getattr(self, "edit_path", None)
        if not path or st.file != path:
            self.note = "PROGRAM CHANGED - SAVE CANCELLED"
            return
        try:
            configfile.atomic_write(path, "\n".join(self.edit_lines) + "\n",
                                    backup=True)
        except OSError as e:
            self.note = f"SAVE FAILED: {e}"
            return
        self.edit_lines = None
        self.app.reload_program(path)
        self.note = "SAVED"
        self._enter_chapter("PRGRM")

    def _edit_cancel(self):
        self.edit_lines = None
        self._enter_chapter("PRGRM")

    def edit_key(self, action):
        """Physical ALTER/INSERT/DELETE keys — active only in EDIT."""
        if self.chapter != "EDIT" or self.edit_lines is None:
            return
        if action == "ALTER":
            self._edit_alter()
        elif action == "INSERT":
            self._edit_insert_word()
        elif action == "DELETE":
            self._edit_delete_word()

    # ------------------------------------------------------------- MDI
    def _mdi_exec(self):
        cmd = space_words(self.app.input.take().strip())
        if not cmd:
            return
        st = self.app.stat
        if st.estop or st.task_state != linuxcnc.STATE_ON:
            self.note = "MACHINE NOT READY"
            return
        try:
            # async: a long MDI move must not freeze the screen; task
            # queues further blocks behind it
            self.app.mdi_async(cmd)
            self.mdi_hist.appendleft(cmd)
        except linuxcnc.error as e:
            self.note = str(e)

    def _mdi_clear(self):
        self.mdi_hist.clear()

    # ------------------------------------------------------------- RSTR
    def _rstr_search(self):
        """Type N-number (N100) or raw line number (100); scan modals up to it."""
        txt = self.app.input.take().strip()
        lines = self.app.ndisp.lines()
        if not lines:
            self.note = "NO PROGRAM LOADED"
            return
        target = None
        if txt[:1].upper() == "N":
            try:
                n = int(txt[1:])
            except ValueError:
                return
            pat = re.compile(rf'[Nn]0*{n}\b')
            for i, line in enumerate(lines):
                if pat.search(line):
                    target = i + 1
                    break
            if target is None:
                self.note = f"N{n} NOT FOUND"
                return
        else:
            try:
                target = int(txt)
            except ValueError:
                return
            if not (1 <= target <= len(lines)):
                self.note = "LINE OUT OF RANGE"
                return
        self.rstr_line = target
        self.rstr_scan = scan_modals(lines, target - 1)

    def _rstr_exec(self):
        """Run from the scanned line. NOTE: LinuxCNC does not execute M/S/T
        on the way — re-establish tool/spindle/coolant via MDI first."""
        if self.rstr_line is None:
            self.note = "SEARCH A LINE FIRST"
            return
        if self.rstr_scan and self.rstr_scan["oword"]:
            self.note = "RESTART INTO SUB - NOT SUPPORTED"
            return
        st = self.app.stat
        if st.estop or st.task_state != linuxcnc.STATE_ON:
            self.note = "MACHINE NOT READY"
            return
        try:
            self.app.run_from_line(self.rstr_line)
        except linuxcnc.error as e:
            self.note = str(e)

    def _rstr_cancel(self):
        self.rstr_line = None
        self.rstr_scan = None

    # ------------------------------------------------------------- files
    def _roots(self):
        if self.chapter == "USB":
            for m in self.app.usb_mounts:
                if os.path.isdir(m):
                    return m
            return None
        return self.app.prog_dir

    def _refresh_files(self):
        root = self._roots()
        self.entries = []
        self.file_cur = 0
        self.file_scroll = 0
        if root is None:
            self.note = "NO USB MOUNTED"
            self.set_root(self._root_row())
            return
        base = self.usb_path if self.chapter == "USB" else self.dir_path
        if not base or not (base == root or base.startswith(root + os.sep)):
            base = root
        try:
            names = sorted(os.listdir(base), key=str.upper)
        except OSError:
            base = root
            try:
                names = sorted(os.listdir(base), key=str.upper)
            except OSError:
                names = []
        if base != root:
            self.entries.append(("..", True, os.path.dirname(base)))
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(base, n)
            self.entries.append((n, os.path.isdir(p), p))
        # dirs first, both halves alphabetical
        self.entries.sort(key=lambda e: (e[0] != "..", not e[1], e[0].upper()))
        if self.chapter == "USB":
            self.usb_path = base
        else:
            self.dir_path = base
        self.set_root(self._root_row())

    def _cur_entry(self):
        return self.entries[self.file_cur] if self.entries else None

    def _file_select(self):
        e = self._cur_entry()
        if not e:
            return
        name, is_dir, path = e
        if is_dir:
            if self.chapter == "USB":
                self.usb_path = path
            else:
                self.dir_path = path
            self._refresh_files()
        else:
            self._load(path)

    def _file_o_srh(self):
        txt = self.app.input.take().strip().lstrip("O")
        try:
            n = int(txt)
        except ValueError:
            return
        stems = {f"O{n}", f"O{n:04d}", f"O{n:05d}"}
        for i, (name, is_dir, _p) in enumerate(self.entries):
            if not is_dir and os.path.splitext(name)[0].upper() in stems:
                self.file_cur = i
                return
        self.note = f"O{n} NOT FOUND"

    def _file_new(self):
        """Buffer = program name (O0100 or alphanumeric); creates <name>.ngc."""
        name = self.app.input.take().strip()
        if not name:
            return
        name = re.sub(r'[^\w.-]', '_', name)
        if not name.upper().endswith(".NGC"):
            name += ".ngc"
        base = self.usb_path if self.chapter == "USB" else self.dir_path
        path = os.path.join(base, name)
        if os.path.exists(path):
            self.note = "ALREADY EXISTS"
            return
        try:
            configfile.atomic_write(path, "( NEW PROGRAM )\nM2\n",
                                    backup=False)
        except OSError as e:
            self.note = f"CREATE FAILED: {e}"
            return
        self._refresh_files()
        self.note = f"CREATED {name}"

    def _file_delete(self):
        e = self._cur_entry()
        if not e or e[1]:                # never delete directories
            return
        # the loaded-but-idle program may go (LinuxCNC keeps its copy)
        if self._is_running_file(e[2]):
            self.note = "CANNOT DELETE RUNNING PROGRAM"
            return
        try:
            os.remove(e[2])
            self.note = f"DELETED {e[0]}"
        except OSError as err:
            self.note = f"DELETE FAILED: {err}"
        self._refresh_files()

    def _is_running_file(self, path):
        """True when `path` is the loaded program and the interpreter is
        not idle."""
        st = self.app.stat
        return (bool(st.file) and path == st.file
                and st.interp_state != linuxcnc.INTERP_IDLE)

    def _file_read_menu(self):
        """USB page READ softkey: warn about overwrite, then confirm."""
        e = self._cur_entry()
        if not e or e[1]:
            return
        dst = os.path.join(self.app.prog_dir, e[0])
        if os.path.exists(dst):
            self.note = f"READ {e[0]} -> DIR, OVERWRITES EXISTING"
        else:
            self.note = f"READ {e[0]} -> DIR"
        self._confirm(self._file_read)

    def _file_read(self):
        """USB page: copy cursored file USB -> program directory."""
        e = self._cur_entry()
        if not e or e[1]:
            return
        dst = os.path.join(self.app.prog_dir, e[0])
        if self._is_running_file(dst):
            self.note = "CANNOT OVERWRITE RUNNING PROGRAM"
            return
        if os.path.exists(dst):
            try:
                configfile.rotate_backups(dst)
            except OSError as err:
                self.note = f"READ FAILED: BACKUP: {err}"
                return
        try:
            configfile.copy_and_sync(e[2], dst)
            self.note = f"READ {e[0]} -> DIR"
        except OSError as err:
            self.note = f"READ FAILED: {err}"
            return
        if dst == self.app.stat.file:
            # idle and loaded: reopen so LinuxCNC's copy matches the file
            try:
                self.app.reload_program(dst)
            except linuxcnc.error as err:
                self.note = f"READ {e[0]} -> DIR, RELOAD FAILED: {err}"

    def _file_punch(self):
        """DIR page: copy cursored file program directory -> first USB."""
        e = self._cur_entry()
        if not e or e[1]:
            return
        usb = None
        for m in self.app.usb_mounts:
            if os.path.isdir(m):
                usb = m
                break
        if usb is None:
            self.note = "NO USB MOUNTED"
            return
        try:
            configfile.copy_and_sync(e[2], os.path.join(usb, e[0]))
            os.sync()
            self.note = f"PUNCH {e[0]} -> USB"
        except OSError as err:
            self.note = f"PUNCH FAILED: {err}"

    # ------------------------------------------------------------- keys
    def on_key(self, sc):
        ch = self.chapter
        if ch == "EDIT" and self.edit_lines is not None:
            if sc == SDL_SCANCODE_UP:
                self.edit_cur = max(0, self.edit_cur - 1)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_DOWN:
                self.edit_cur = min(len(self.edit_lines) - 1,
                                    self.edit_cur + 1)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_LEFT:
                self.edit_word = max(0, self.edit_word - 1); return True
            if sc == SDL_SCANCODE_RIGHT:
                self.edit_word += 1
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_PAGEUP:
                self.edit_cur = max(0, self.edit_cur - self.EDIT_ROWS)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_PAGEDOWN:
                self.edit_cur = min(len(self.edit_lines) - 1,
                                    self.edit_cur + self.EDIT_ROWS)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_RETURN:
                self._edit_insert(); return True
        elif ch in ("DIR", "USB"):
            if sc == SDL_SCANCODE_UP:
                self.file_cur = max(0, self.file_cur - 1); return True
            if sc == SDL_SCANCODE_DOWN:
                self.file_cur = min(max(0, len(self.entries) - 1),
                                    self.file_cur + 1); return True
            if sc == SDL_SCANCODE_PAGEUP:
                self.file_cur = max(0, self.file_cur - self.FILE_ROWS)
                return True
            if sc == SDL_SCANCODE_PAGEDOWN:
                self.file_cur = min(max(0, len(self.entries) - 1),
                                    self.file_cur + self.FILE_ROWS)
                return True
            if sc == SDL_SCANCODE_RETURN:
                self._file_select(); return True
        elif ch == "MDI":
            if sc == SDL_SCANCODE_RETURN:
                self._mdi_exec(); return True
        elif ch == "PRGRM":
            lines = self.app.ndisp.lines()
            st = self.app.stat
            def base():
                if self.view_line is not None:
                    return self.view_line
                return max(0, (st.motion_line or st.current_line) - 3)
            if sc == SDL_SCANCODE_UP and self.view_line is not None:
                self.view_line = max(0, self.view_line - 1); return True
            if sc == SDL_SCANCODE_DOWN and self.view_line is not None:
                self.view_line = min(max(0, len(lines) - 1),
                                     self.view_line + 1); return True
            if sc == SDL_SCANCODE_PAGEUP:
                self.view_line = max(0, base() - self.LIST_ROWS); return True
            if sc == SDL_SCANCODE_PAGEDOWN:
                self.view_line = min(max(0, len(lines) - 1),
                                     base() + self.LIST_ROWS); return True
        return super().on_key(sc)

    # ------------------------------------------------------------- draw
    def draw(self, renderer, area):
        # rebuild chapter row if the mode dial moved (physical switch)
        mode = self.app.stat.task_mode
        if mode != self._last_mode:
            self._last_mode = mode
            if self.chapter == "EDIT" and self.edit_lines is not None:
                self.edit_lines = None           # discard edits on mode change
            self._enter_chapter(self.chapter)    # revalidates against new mode

        f = self.app.font
        ch = self.chapter
        if ch == "PRGRM":
            self._draw_prgrm(renderer, f)
        elif ch == "CHECK":
            self._draw_check(renderer, f)
        elif ch == "CURRNT":
            self._draw_block(renderer, f, 0, "CURRENT BLOCK")
        elif ch == "NEXT":
            self._draw_block(renderer, f, 1, "NEXT BLOCK")
        elif ch == "MDI":
            self._draw_mdi(renderer, f)
        elif ch == "RSTR":
            self._draw_rstr(renderer, f)
        elif ch == "EDIT":
            self._draw_edit(renderer, f)
        elif ch in ("DIR", "USB"):
            self._draw_files(renderer, f)
        if self.note:
            draw_line(renderer, f, self.note, 10, SOFTKEY_Y - BUFFER_H - 120, RED)

    def _hilite_row(self, renderer, f, text, x, y, w, active):
        if active:
            SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
            SDL_RenderFillRect(renderer, SDL_Rect(x - 4, y - 2, w, 56))
            draw_line(renderer, f, text, x, y, BLACK)
        else:
            draw_line(renderer, f, text, x, y)

    def _draw_prgrm(self, renderer, f):
        st = self.app.stat
        draw_line(renderer, f, os.path.basename(st.file) if st.file
                  else "(no program loaded)", 10, 70)
        lines = self.app.ndisp.lines()
        if not lines:
            return
        cur = st.motion_line or st.current_line
        executing = cur > 0 and st.interp_state != linuxcnc.INTERP_IDLE
        if executing or self.view_line is None:
            first = max(0, cur - 3)
        else:
            first = self.view_line
        first = max(0, min(first, max(0, len(lines) - self.LIST_ROWS)))
        for row, idx in enumerate(range(first,
                                        min(len(lines), first + self.LIST_ROWS))):
            color = RED if (idx + 1) == cur and cur > 0 else WHITE
            draw_line(renderer, f, f"{idx + 1:4d} {lines[idx].rstrip()}",
                      10, 130 + row * 55, color)

    def _draw_check(self, renderer, f):
        st = self.app.stat
        app = self.app
        draw_line(renderer, f, f"PROGRAM CHECK  ({self.check_sub})", 10, 70)
        lines = self.app.ndisp.lines()
        cur = st.motion_line or st.current_line
        if lines:
            first = max(0, cur - 2)
            for row, idx in enumerate(range(first, min(len(lines), first + 5))):
                color = RED if (idx + 1) == cur and cur > 0 else WHITE
                draw_line(renderer, f, lines[idx].rstrip(), 10, 130 + row * 55, color)
        y = 430
        # position (ABS/REL per toggle) left, DTG right
        draw_line(renderer, f, self.check_sub, 10, y)
        draw_line(renderer, f, "DIST TO GO", 740, y)
        y += 60
        for r, ax in enumerate(app.axes):
            idx = AXIS_IDX[ax]
            if self.check_sub == "ABS":
                v = (st.actual_position[idx] - st.g5x_offset[idx]
                     - st.g92_offset[idx] - st.tool_offset[idx])
            else:
                v = st.actual_position[idx] - app.rel_origin[idx]
            draw_line(renderer, f, f"{ax} {app.fmt_axis(ax, v)}", 10, y + r * 52)
            draw_line(renderer, f, f"{ax} {app.fmt_axis(ax, st.dtg[idx])}",
                      740, y + r * 52)

    def _draw_block(self, renderer, f, offset, title):
        """offset 0 = executing block, 1 = next physical block."""
        st = self.app.stat
        draw_line(renderer, f, title, 10, 70)
        lines = self.app.ndisp.lines()
        cur = st.motion_line or st.current_line
        idx = cur - 1 + offset
        if lines and 0 <= idx < len(lines):
            # the block's words, one per row
            words = re.findall(r'[A-Za-z][+-]?[\d.]*', lines[idx])
            for r, w in enumerate(words[:10]):
                draw_line(renderer, f, w, 10, 140 + r * 55)
            draw_line(renderer, f, lines[idx].strip(), 400, 140, RED)
        # live modal state (from stat, not the text scan)
        codes = []
        for v in st.gcodes[1:]:
            if v != -1:
                codes.append(f"G{v // 10}" + (f".{v % 10}" if v % 10 else ""))
        for v in st.mcodes[1:]:
            if v != -1:
                codes.append(f"M{v}")
        draw_line(renderer, f, "MODAL", 740, 140)
        for j in range(0, len(codes), 4):
            draw_line(renderer, f, " ".join(codes[j:j + 4]),
                      740, 200 + (j // 4) * 50, DIM)

    def _draw_mdi(self, renderer, f):
        draw_line(renderer, f, "MDI  (type block, EXEC or ENTER to run)", 10, 70)
        st = self.app.stat
        if st.interp_state != linuxcnc.INTERP_IDLE:
            draw_line(renderer, f, "EXECUTING...", 10, 140, RED)
        if self.app.show_mdi_history:
            y = 200
            for cmd in list(self.mdi_hist)[:12]:
                draw_line(renderer, f, cmd, 10, y, DIM)
                y += 52

    def _draw_rstr(self, renderer, f):
        draw_line(renderer, f, "PROGRAM RESTART", 10, 70)
        draw_line(renderer, f,
                  "TYPE N-NUMBER (N100) OR LINE (100), THEN [SEARCH]", 10, 130)
        if self.rstr_line is None:
            return
        lines = self.app.ndisp.lines()
        y = 210
        draw_line(renderer, f, f"TARGET LINE {self.rstr_line}:", 10, y)
        if 0 < self.rstr_line <= len(lines):
            draw_line(renderer, f, lines[self.rstr_line - 1].strip(),
                      420, y, RED)
        y += 70
        s = self.rstr_scan
        rows = [
            ("TOOL",    f"T{s['T']}" if s['T'] is not None else "-"),
            ("SPINDLE", f"{s['spindle']}"
                        + (f"  S{s['S']:g}" if s['S'] else "")),
            ("FEED",    f"F{s['F']:g}" if s['F'] else "-"),
            ("COOLANT", s["coolant"]),
            ("MODALS",  " ".join(x for x in (s["units"], s["wcs"], s["tlo"],
                                             s["plane"], s["dist"]) if x)),
        ]
        for label, val in rows:
            draw_line(renderer, f, f"{label:<9} {val}", 10, y)
            y += 55
        y += 20
        if s["oword"]:
            draw_line(renderer, f, "!! SUB/LOOP BEFORE TARGET - EXEC BLOCKED",
                      10, y, RED)
        else:
            draw_line(renderer, f,
                      "RE-ESTABLISH T/S/COOLANT VIA MDI, THEN [EXEC]", 10, y, RED)

    def _draw_edit(self, renderer, f):
        st = self.app.stat
        draw_line(renderer, f,
                  f"EDIT  {os.path.basename(st.file)}", 10, 70)
        if self.edit_lines is None:
            return
        # keep cursor visible
        if self.edit_cur < self.edit_scroll:
            self.edit_scroll = self.edit_cur
        elif self.edit_cur >= self.edit_scroll + self.EDIT_ROWS:
            self.edit_scroll = self.edit_cur - self.EDIT_ROWS + 1
        for row, idx in enumerate(range(self.edit_scroll,
                                        min(len(self.edit_lines),
                                            self.edit_scroll + self.EDIT_ROWS))):
            line = self.edit_lines[idx]
            prefix = f"{idx + 1:4d} "
            y = 130 + row * 55
            draw_line(renderer, f, prefix + line, 10, y)
            if idx != self.edit_cur:
                continue
            # highlight the selected WORD on the cursored line
            spans = [(m.start(), m.end())
                     for m in re.finditer(r'\S+', line)]
            if spans:
                self._clamp_word()
                s, e = spans[self.edit_word]
                x0 = 10 + text_width(f, prefix + line[:s])
                wpx = text_width(f, line[s:e])
                SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
                SDL_RenderFillRect(renderer,
                                   SDL_Rect(x0 - 3, y - 2, wpx + 6, 56))
                draw_line(renderer, f, line[s:e], x0, y, BLACK)
            else:
                # empty line: block cursor at the insert position
                SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
                SDL_RenderFillRect(renderer,
                                   SDL_Rect(10 + text_width(f, prefix),
                                            y - 2, 26, 56))

    def _draw_files(self, renderer, f):
        base = self.usb_path if self.chapter == "USB" else self.dir_path
        draw_line(renderer, f, f"{self.chapter}  {base or '-'}", 10, 70)
        if not self.entries:
            draw_line(renderer, f, "(empty)", 10, 130)
            return
        if self.file_cur < self.file_scroll:
            self.file_scroll = self.file_cur
        elif self.file_cur >= self.file_scroll + self.FILE_ROWS:
            self.file_scroll = self.file_cur - self.FILE_ROWS + 1
        for row, idx in enumerate(range(self.file_scroll,
                                        min(len(self.entries),
                                            self.file_scroll + self.FILE_ROWS))):
            name, is_dir, path = self.entries[idx]
            if is_dir:
                text = f"<DIR>  {name}"
            else:
                try:
                    size = os.path.getsize(path)
                    text = f"{size // 1024:5d}K  {name}" if size >= 1024 \
                           else f"{size:5d}B  {name}"
                except OSError:
                    text = f"    ?  {name}"
            self._hilite_row(renderer, f, text, 10, 130 + row * 60, 1300,
                             idx == self.file_cur)


class OffsetScreen(Screen):
    """OFFSET/SETTING chapter: [OFFSET] [SETING] [WORK] (+PROBE pages)."""

    VISIBLE_ROWS = 9
    ROW_H = 74
    TABLE_Y = 200
    WCOL_X0, WCOL_W, WCOL_PITCH = 170, 240, 256
    UNIT_CYCLE = ("MACHINE", "MM", "INCH", "PROGRAM")

    # ------------------------------------------------------------- lifecycle
    def __init__(self, app):
        super().__init__(app)
        # running probe cycle: None or
        # {"kind": "WORK"|"TOOL"|"CAL", "state": "ARMED"|"RUNNING", "t0": s}
        self._probe = None
        self._probe_note = ""
        self._tsetter = {}
        self.wcs_vals = None             # machine units, seeded on entry

    def on_enter(self):
        self.chapter = getattr(self, "chapter", "OFFSET")
        self.sub = getattr(self, "sub", "GEOM")          # WEAR | GEOM
        self.scroll = 0
        self._read_table()
        self._refresh_wcs()
        self._enter_chapter(self.chapter)

    def active_keys(self):
        ch = self.chapter
        if ch == "OFFSET":                       # WEAR/GEOM share the row
            return (ch, self.sub)
        if ch in PROBE_PAGES:                    # chapter POUT -> label OUT
            return tuple(lbl for pg, lbl in PROBE_PAGE_KEYS if pg == ch)
        return (ch,)

    # ------------------------------------------------------------- root menu
    def _root_row(self, extra=None):
        row = [
            K("OFFSET", lambda: self._enter_chapter("OFFSET")),
            K("SETING", lambda: self._enter_chapter("SETING")),
            K("WORK",   lambda: self._enter_chapter("WORK")),
        ]
        row += extra or []
        while len(row) < 9:
            row.append(K())
        row.append(K("(OPRT)", self._oprt))
        return row

    def _enter_chapter(self, name):
        self.chapter = name
        if name == "OFFSET":
            self.set_root(self._root_row([
                K("WEAR", lambda: self._set_sub("WEAR")),
                K("GEOM", lambda: self._set_sub("GEOM")),
                K("PROBE", lambda: self._enter_chapter("TPROBE")),
            ]))
            self._build_tool_cursor()
        elif name == "SETING":
            self.set_root(self._root_row())
            self._build_setting_cursor()
        elif name == "WORK":
            self._refresh_wcs()
            self.set_root(self._root_row([
                K("PROBE", lambda: self._enter_chapter("WPROBE")),
            ]))
            self._build_work_cursor()
        elif name == "TPROBE":
            self.cursor = FieldCursor(cols=1)      # nothing to edit here
            self._tsetter = self._read_toolsetter()
            self.set_root([
                K("Z.PRB", lambda: self._confirm(self._tprobe_exec)),
                K(), K(), K(), K(), K(), K(), K(),
                K("RETURN", lambda: self._enter_chapter("OFFSET")),
                K(),
            ])
        elif name == "WPROBE":
            self._enter_chapter("POUT")
        elif name in PROBE_PAGES:
            self._build_probe_grid()
            row = [K(lbl, lambda n=pg: self._enter_chapter(n))
                   for pg, lbl in PROBE_PAGE_KEYS]      # six page keys
            row += [K("P.SET", lambda: self._enter_chapter("PSET")),
                    K("EXEC", lambda: self._confirm(self._wprobe_exec)),
                    K(),
                    K("RETURN", lambda: self._enter_chapter("WORK"))]
            self.set_root(row)
        elif name == "PSET":
            self._build_pset_cursor()
            self.set_root([
                K("INPUT", lambda: self.cursor.commit(self.app.input.take())),
                K(), K(), K(), K(), K(), K(), K(),
                K("RETURN", lambda: self._enter_chapter("POUT")),
                K(),
            ])

    def _set_sub(self, sub):
        self.sub = sub
        self._build_tool_cursor()

    # ------------------------------------------------------------- OPRT menus
    def _oprt(self):
        if self.chapter == "OFFSET":
            page1 = [
                K("NO.SRH", self._no_srh),
                K("MEASUR", self._tool_measure),
                K("INP.C.", self._input_counter),
                K("+INPUT", lambda: self.cursor.commit_add(self.app.input.take())),
                K("INPUT",  lambda: self.cursor.commit(self.app.input.take())),
            ]
            page2 = [
                K("CLEAR", self._clear_menu),
                K("READ",  lambda: self._confirm(self._read_exec)),
                K("PUNCH", lambda: self._confirm(self._punch_exec)),
            ]
            self.push([page1, page2])
        elif self.chapter == "SETING":
            self.push([
                K("INPUT", lambda: self.cursor.commit(self.app.input.take() or " ")),
            ])
        elif self.chapter == "WORK":
            self.push([
                K("NO.SRH", self._no_srh),
                K("MEASUR", self._work_measure),
                K("+INPUT", lambda: self.cursor.commit_add(self.app.input.take())),
                K("INPUT",  lambda: self.cursor.commit(self.app.input.take())),
            ])

    def _clear_menu(self):
        self.push([
            K("ALL",  lambda: self._clear("ALL"),  kind=ACT),
            K("WEAR", lambda: self._clear("WEAR"), kind=ACT),
            K("GEOM", lambda: self._clear("GEOM"), kind=ACT),
        ])

    def _confirm(self, exec_fn):
        self.push([K("CAN", self.pop), K("EXEC", lambda: (exec_fn(), self.pop()))])

    # ------------------------------------------------------------- tool data
    # Internal wear/geom values are MACHINE units; conversion only at the
    # MDI strings (interp units) and the display/entry membrane.

    def _tools(self):
        # real entries only: skip index 0 spindle slot, id -1 empties
        return [t for t in self.app.stat.tool_table[1:] if t.id > 0]

    def _tool_tbl_path(self):
        try:
            ini = linuxcnc.ini(self.app.stat.ini_filename)
            tbl = ini.find("EMCIO", "TOOL_TABLE") or "tool.tbl"
        except linuxcnc.error:
            tbl = "tool.tbl"
        return os.path.join(os.path.dirname(self.app.stat.ini_filename), tbl)

    # tool.tbl is the single source of truth: LinuxCNC's Z/diameter is
    # geometry + wear combined, and the wear share is a ";W:Z.. R.." token
    # at the start of the tool's comment (machine units, tooltbl.py). A G10
    # L1 from anywhere keeps the comment, so the split cannot drift apart.
    def _read_table(self):
        """Refresh self.wear / self.comments (user text only, token
        stripped) from the .tbl. Runs the one-time JSON import first."""
        path = self._tool_tbl_path()
        try:
            tbl = tooltbl.read_table(path)
        except OSError:
            tbl = {}
        # an unreadable/empty table is not "no tokens": leave the JSON alone
        if (tbl and os.path.exists(LEGACY_WEAR_FILE)
                and self._migrate_legacy_wear(path, tbl)):
            try:
                tbl = tooltbl.read_table(path)
            except OSError:
                tbl = {}
        self.wear = {tid: dict(e["wear"]) for tid, e in tbl.items()}
        self.comments = {tid: e["comment"] for tid, e in tbl.items()}

    def _migrate_legacy_wear(self, path, tbl):
        """Old tool_wear.json -> tokens, once. Imported only when no tool
        in the table carries a token yet; either way the JSON is renamed
        to .migrated so this never runs twice. True if the .tbl changed."""
        changed = False
        try:
            if any(e["tagged"] for e in tbl.values()):
                os.replace(LEGACY_WEAR_FILE, LEGACY_WEAR_FILE + ".migrated")
                self.app.alarm("OLD TOOL_WEAR.JSON IGNORED (RENAMED)")
                return False
            with open(LEGACY_WEAR_FILE) as f:
                old = {int(k): v for k, v in json.load(f).items()}
            for tid, w in old.items():
                z, r = float(w.get("Z", 0.0)), float(w.get("R", 0.0))
                if tid in tbl and (z or r):
                    tooltbl.write_comment(path, tid, z, r, tbl[tid]["comment"])
                    changed = True
            os.replace(LEGACY_WEAR_FILE, LEGACY_WEAR_FILE + ".migrated")
            if changed:
                self.app.command.load_tool_table()
            self.app.alarm("TOOL WEAR MOVED INTO TOOL.TBL")
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.app.alarm(f"TOOL WEAR MIGRATION FAILED: {e}")
        return changed

    def _write_wear_token(self, tid, z, r, text=None):
        """File half of every wear/comment change. False (alarm raised) on
        failure. Callers then load_tool_table so LinuxCNC's in-memory
        comment -- which its own G10 rewrites put back -- carries it."""
        if text is None:
            text = self.comments.get(tid, "")
        try:
            tooltbl.write_comment(self._tool_tbl_path(), tid, z, r, text)
        except OSError as e:
            self.app.alarm(f"TOOL.TBL WRITE FAILED: {e}")
            return False
        except KeyError:
            self.app.alarm(f"T{tid} NOT FOUND IN TOOL.TBL")
            return False
        return True

    def _reload_table(self):
        """load_tool_table and wait for task to take it, so a following
        G10 L1 rewrites the file from the fresh copy (comment included)."""
        self.app.command.load_tool_table()
        self.app.command.wait_complete()

    def _set_tool_comment(self, tid, text):
        """Rewrite the user text of tool tid's comment, keeping its wear
        token, then reload so LinuxCNC's in-memory copy carries it."""
        w = self._wear_of(tid)
        if not self._write_wear_token(tid, w["Z"], w["R"], text):
            return
        self.app.command.load_tool_table()
        self._read_table()

    def _wear_of(self, tid):
        """In-memory copy of the .tbl token; not persisted from here."""
        return self.wear.setdefault(tid, {"Z": 0.0, "R": 0.0})

    def _geom_of(self, tid):
        # LinuxCNC stores geom+wear combined (we wrote it), so geom = stored - wear
        for t in self.app.stat.tool_table:
            if t.id == tid:
                w = self._wear_of(tid)
                return {"Z": t.zoffset - w["Z"], "R": t.diameter / 2 - w["R"]}
        return {"Z": 0.0, "R": 0.0}

    def _g10(self, code):
        """Blocking offset write. False (alarm raised) when the mode switch
        fails or task has not acknowledged the block after 5 s; callers
        must not update their local copy then."""
        try:
            ok = self.app.mdi(code)
        except linuxcnc.error as e:
            self.app.alarm(str(e))
            return False
        if not ok:
            self.app.alarm("G10 NOT ACKNOWLEDGED")
        return ok

    def _write_tool(self, tid, geom=None):
        """G10 L1 with geom + wear. Pass geom when wear has just changed:
        _geom_of derives it from the stored value minus the CURRENT wear,
        so it must be taken before the wear is edited."""
        g, w = geom or self._geom_of(tid), self._wear_of(tid)
        z = self.app.machine_to_interp(g['Z'] + w['Z'])
        r = self.app.machine_to_interp(g['R'] + w['R'])
        if not self._g10(f"G10 L1 P{tid} Z{z:.4f} R{r:.4f}"):
            return False
        self.app.command.load_tool_table()
        return True

    def _set_tool_val(self, tid, col, text, table):
        """Cursor/INPUT path: text is display units."""
        try:
            typed = float(text)
        except ValueError:
            return
        self._set_tool_machine(tid, col,
                               self.app.disp_to_machine("Z", typed), table)

    def _set_tool_machine(self, tid, col, val, table):
        """Machine-units entry point (MEASUR and probe cycles)."""
        axis = "Z" if col == 0 else "R"
        if table == "WEAR":
            # order: token into the file, reload (LinuxCNC's in-memory
            # comment now has it), then the G10 -- whose table rewrite
            # keeps that comment.
            geom = self._geom_of(tid)            # before the wear changes
            w = self._wear_of(tid)
            old = dict(w)
            new = dict(w, **{axis: val})
            if not self._write_wear_token(tid, new["Z"], new["R"]):
                return
            self._reload_table()
            w.update(new)
            if not self._write_tool(tid, geom):
                # G10 refused: put the old token back so stored - wear
                # still gives the old geometry
                w.update(old)
                if self._write_wear_token(tid, old["Z"], old["R"]):
                    self.app.command.load_tool_table()
            self._read_table()
        else:                                   # GEOM: adjust so geom == val
            w = self._wear_of(tid)[axis]
            mdi_val = self.app.machine_to_interp(val + w)
            if not self._g10(f"G10 L1 P{tid} {axis}{mdi_val:.4f}"):
                return
            self.app.command.load_tool_table()

    def _get_tool_val(self, tid, col, table):
        """Machine units."""
        axis = "Z" if col == 0 else "R"
        return (self._wear_of(tid) if table == "WEAR" else self._geom_of(tid))[axis]

    def _build_tool_cursor(self):
        self.cursor = FieldCursor(cols=3)
        for t in self._tools():
            for col in (0, 1):
                self.cursor.add(0, 0, 300, 64,     # rects positioned at draw time
                    setter=lambda s, tid=t.id, c=col: self._set_tool_val(
                        tid, c, s, self.sub),
                    getter=lambda tid=t.id, c=col: self.app.machine_to_disp(
                        "Z", self._get_tool_val(tid, c, self.sub)))
            # comment column: setter only (no getter -> +INPUT safely no-ops)
            self.cursor.add(0, 0, 420, 64,
                setter=lambda s, tid=t.id: self._set_tool_comment(tid, s))

    # ------------------------------------------------------------- settings
    def _build_setting_cursor(self):
        self.cursor = FieldCursor(cols=1)
        self.cursor.add(0, 0, 900, 60, setter=self._set_units)

    def _set_units(self, text):
        t = text.strip().upper()
        if t in self.UNIT_CYCLE:
            self.app.display_units = t
        else:                                    # any other commit = cycle
            i = self.UNIT_CYCLE.index(self.app.display_units)
            self.app.display_units = self.UNIT_CYCLE[(i + 1) % 4]
        self.app.save_persist()

    # ------------------------------------------------------------- work data
    def _refresh_wcs(self):
        """Page-entry seed. While a program/MDI runs the var file and the
        offsets are in motion, so the local copy is kept (read once if
        there is none yet)."""
        if (self.wcs_vals is None
                or self.app.stat.interp_state == linuxcnc.INTERP_IDLE):
            self.wcs_vals = self._read_var_wcs()

    def _wcs_from_stat(self, sys_idx):
        """Copy the ACTIVE system's offsets from stat (poll first). The var
        file lags a G10 until task's next command, stat does not."""
        st = self.app.stat
        if sys_idx + 1 != st.g5x_index:
            return False
        for ax in "XYZABC":
            self.wcs_vals[sys_idx][ax] = st.g5x_offset[AXIS_IDX[ax]]
        return True

    def _read_var_wcs(self):
        """All nine systems from the var file: {sys_index: {axis: val}}.
        Machine units. Read on page entry only; edits update locally.
        The var file is flushed only on task's NEXT command, so a G10 just
        made (e.g. by a probe) is not in it yet: the active system is taken
        from stat.g5x_offset, which is current."""
        out = {i: {ax: 0.0 for ax in "XYZABC"} for i in range(9)}
        try:
            ini = linuxcnc.ini(self.app.stat.ini_filename)
            var = ini.find("RS274NGC", "PARAMETER_FILE") or "linuxcnc.var"
            var = os.path.join(os.path.dirname(self.app.stat.ini_filename), var)
            params = {}
            with open(var) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        params[int(parts[0])] = float(parts[1])
            for s in range(9):
                base = WCS_PARAM_BASE + 20 * s
                for ax, off in (("X", 0), ("Y", 1), ("Z", 2),
                                ("A", 3), ("B", 4), ("C", 5)):
                    out[s][ax] = params.get(base + off, 0.0)
        except (OSError, ValueError):
            pass
        st = self.app.stat
        a = st.g5x_index - 1
        if 0 <= a < 9:
            for ax in "XYZABC":
                out[a][ax] = st.g5x_offset[AXIS_IDX[ax]]
        return out

    def _set_wcs(self, sys_idx, ax, text):
        try:
            typed = float(text)
        except ValueError:
            return
        machine_val = self.app.disp_to_machine(ax, typed)
        mdi_val = (self.app.machine_to_interp(machine_val)
                   if ax in self.app.LINEAR_AXES else machine_val)
        if not self._g10(f"G10 L2 P{sys_idx + 1} {ax}{mdi_val:.4f}"):
            return
        self.wcs_vals[sys_idx][ax] = machine_val       # store machine units
        self.app.stat.poll()                           # fresh after wait_complete
        self._wcs_from_stat(sys_idx)

    def _build_work_cursor(self):
        axes = self.app.axes
        self.cursor = FieldCursor(cols=len(axes))
        for s in range(9):
            for ax in axes:
                self.cursor.add(0, 0, self.WCOL_W, 56,
                    setter=lambda t, si=s, a=ax: self._set_wcs(si, a, t),
                    getter=lambda si=s, a=ax: self.app.machine_to_disp(
                        a, self.wcs_vals[si][a]))

    # ------------------------------------------------------------- OPRT leaves
    def _no_srh(self):
        txt = self.app.input.take().strip()
        try:
            n = int(float(txt))
        except ValueError:
            return
        if self.chapter == "OFFSET":
            for row, t in enumerate(self._tools()):
                if t.id == n:
                    self.cursor.goto_row(row)
                    return
        elif self.chapter == "WORK":
            if 1 <= n <= 9:
                self.cursor.goto_row(n - 1)

    def _parse_axis_val(self, txt):
        txt = txt.strip()
        if txt and txt[0] in self.app.axes:
            try:
                return txt[0], float(txt[1:])
            except ValueError:
                pass
        return None, None

    def _tool_measure(self):
        """MEASUR on tool page: Z<ref> -> length = machine Z - ref.
        Typed ref is display units. Probe cycles call
        _set_tool_machine directly."""
        ax, val = self._parse_axis_val(self.app.input.take())
        if ax != "Z":
            return
        row = self.cursor.row()
        tools = self._tools()
        if row < len(tools):
            tid = tools[row].id
            ref_m = self.app.disp_to_machine("Z", val)
            length = self.app.stat.actual_position[2] - ref_m
            self._set_tool_machine(tid, 0, length, "GEOM")

    def _work_measure(self):
        """MEASUR on work page: G10 L20 — current position becomes <val>.
        Typed value is display units."""
        ax, val = self._parse_axis_val(self.app.input.take())
        if ax is None:
            return
        sys_idx = self.cursor.row()
        val_m = self.app.disp_to_machine(ax, val)
        mdi_val = (self.app.machine_to_interp(val_m)
                   if ax in self.app.LINEAR_AXES else val_m)
        if not self._g10(f"G10 L20 P{sys_idx + 1} {ax}{mdi_val:.4f}"):
            return

        idx = AXIS_IDX[ax]
        st = self.app.stat
        if sys_idx + 1 == st.g5x_index:
            st.poll()                                    # fresh after wait_complete
            self.wcs_vals[sys_idx][ax] = st.g5x_offset[idx]
        else:
            # L20 semantics: offset = machine pos - g92 - desired reading
            self.wcs_vals[sys_idx][ax] = (st.actual_position[idx]
                                          - st.g92_offset[idx] - val_m)

    def _input_counter(self):
        """INP.C.: axis letter -> write the RELATIVE counter value into the
        cursored field. Setters expect display units, so convert."""
        txt = self.app.input.take().strip()
        if txt not in tuple(self.app.axes):
            return
        idx = AXIS_IDX[txt]
        rel = self.app.stat.actual_position[idx] - self.app.rel_origin[idx]
        self.cursor.commit(f"{self.app.machine_to_disp(txt, rel):.4f}")

    def _clear(self, what):
        tools = self._tools()
        geoms = {t.id: self._geom_of(t.id) for t in tools}   # before any change
        if what in ("ALL", "WEAR"):
            for t in tools:                      # one pass of zero tokens
                w = self._wear_of(t.id)
                if w["Z"] or w["R"]:
                    if not self._write_wear_token(t.id, 0.0, 0.0):
                        self.app.command.load_tool_table()
                        self._read_table()
                        return
                self.wear[t.id] = {"Z": 0.0, "R": 0.0}
            self._reload_table()
        if what == "ALL":
            for t in tools:
                if not self._g10(f"G10 L1 P{t.id} Z0 R0"):
                    break
        elif what == "GEOM":                     # geometry 0: stored = wear
            for t in tools:
                w = self._wear_of(t.id)
                z = self.app.machine_to_interp(w["Z"])
                r = self.app.machine_to_interp(w["R"])
                if not self._g10(f"G10 L1 P{t.id} Z{z:.4f} R{r:.4f}"):
                    break
        elif what == "WEAR":                     # re-apply geom, wear now 0
            for t in tools:
                if not self._write_tool(t.id, geoms[t.id]):
                    break
        self.app.command.load_tool_table()
        self._read_table()
        self.pop()

    def _read_exec(self):
        self.app.command.load_tool_table()
        self._read_table()

    def _punch_exec(self):
        # Nothing to save: every edit already landed in tool.tbl (G10 L1
        # and the wear token). Kept as a reload, same as READ.
        self._read_exec()

    # ------------------------------------------------------------- draw
    def draw(self, renderer, area):
        f = self.app.font
        self._poll_probe(renderer, f)
        if self.chapter == "OFFSET":
            self._draw_tools(renderer, f)
        elif self.chapter == "SETING":
            self._draw_settings(renderer, f)
        elif self.chapter == "WORK":
            self._draw_work(renderer, f)
        elif self.chapter == "TPROBE":
            self._draw_tprobe(renderer, f)
        elif self.chapter in PROBE_PAGES:
            self._draw_wprobe(renderer, f)
        elif self.chapter == "PSET":
            self._draw_pset(renderer, f)

    def _scroll_to_cursor(self, nrows):
        row = self.cursor.row()
        if row < self.scroll:
            self.scroll = row
        elif row >= self.scroll + self.VISIBLE_ROWS:
            self.scroll = row - self.VISIBLE_ROWS + 1
        self.scroll = max(0, min(self.scroll, max(0, nrows - self.VISIBLE_ROWS)))

    def _draw_tools(self, renderer, f):
        app = self.app
        title = ("TOOL OFFSET / "
                 + ("WEAR" if self.sub == "WEAR" else "GEOMETRY")
                 + f"  ({app.unit_tag()})")
        draw_line(renderer, f, title, 10, 70)
        draw_line(renderer, f,
                  "NO.       LENGTH(Z)          RADIUS        COMMENT", 10, 118)
        tools = self._tools()
        self._scroll_to_cursor(len(tools))
        for vis, row in enumerate(range(self.scroll,
                                        min(len(tools),
                                            self.scroll + self.VISIBLE_ROWS))):
            t = tools[row]
            y = self.TABLE_Y + vis * self.ROW_H
            draw_line(renderer, f, f"{t.id:03d}", 10, y)
            for col, x in ((0, 160), (1, 560)):
                i = row * 3 + col
                fld = self.cursor.fields[i]
                fld.x, fld.y, fld.w, fld.h = x, y - 2, 340, 60
                val = self._get_tool_val(t.id, col, self.sub)
                draw_field(renderer, f, app.fmt_axis("Z", val), i, self.cursor)
            i = row * 3 + 2
            fld = self.cursor.fields[i]
            fld.x, fld.y, fld.w, fld.h = 960, y - 2, 470, 60
            draw_field(renderer, f, self.comments.get(t.id, ""), i, self.cursor)

    def _draw_settings(self, renderer, f):
        draw_line(renderer, f, "SETTING", 10, 70)
        fld = self.cursor.fields[0]
        fld.x, fld.y, fld.w, fld.h = 10, self.TABLE_Y - 2, 900, 60
        draw_field(renderer, f,
                   f"DISPLAY UNITS   = {self.app.display_units}",
                   0, self.cursor)

    def _draw_work(self, renderer, f):
        app = self.app
        axes = app.axes
        draw_line(renderer, f,
                  f"WORK COORDINATE SYSTEM  ({app.unit_tag()})", 10, 70)
        hdr = "NO.   " + "".join(f"{ax:>10}   " for ax in axes)
        draw_line(renderer, f, hdr, 10, 118)
        self._scroll_to_cursor(9)
        active = app.stat.g5x_index
        for vis, s in enumerate(range(self.scroll,
                                      min(9, self.scroll + self.VISIBLE_ROWS))):
            y = self.TABLE_Y + vis * self.ROW_H
            draw_line(renderer, f,
                      WCS_NAMES[s] + ("*" if s + 1 == active else " "), 10, y)
            for a, ax in enumerate(axes):
                i = s * len(axes) + a
                fld = self.cursor.fields[i]
                fld.x, fld.y, fld.w, fld.h = (self.WCOL_X0 + a * self.WCOL_PITCH,
                                              y - 2, self.WCOL_W, 60)
                draw_field(renderer, f,
                           app.fmt_axis(ax, self.wcs_vals[s][ax]),
                           i, self.cursor)

    # ------------------------------------------------------------- probing
    def _pdefaults(self):
        """persist["probe"], MACHINE units (see PROBE_PARAMS). Keys no
        longer in PROBE_FIELDS (e.g. an old zero_height) are dropped."""
        d = self.app.persist.setdefault("probe", {})
        stale = [k for k in d if k not in PROBE_KIND]
        for k in stale:
            del d[k]
        for key, _lbl, dflt, _kind in PROBE_FIELDS:
            d.setdefault(key, dflt)
        if stale:
            self.app.save_persist()
        return d

    def _pset_disp(self, key):
        """Stored machine value -> display units (LEN and FEED scale by the
        same factor; NUM is unitless)."""
        v = self._pdefaults()[key]
        if PROBE_KIND[key] == "NUM":
            return v
        return self.app.machine_to_disp("Z", v)

    def _pset_text(self, key):
        v = self._pset_disp(key)
        if PROBE_KIND[key] == "NUM":
            return f"{v:g}"
        t = f"{v:.4f}".rstrip("0").rstrip(".")   # 0.0394 IN stays visible
        return "0" if t == "-0" else t

    def _pset_val(self, key, text):
        """Typed value is display units for LEN/FEED."""
        try:
            v = float(text)
        except ValueError:
            return
        if PROBE_KIND[key] != "NUM":
            v = self.app.disp_to_machine("Z", v)
        self._pdefaults()[key] = v
        self.app.save_persist()

    def _build_pset_cursor(self):
        self.cursor = FieldCursor(cols=1)
        for key, _lbl, _d, _kind in PROBE_FIELDS:
            self.cursor.add(0, 0, 300, 52,
                setter=lambda t, k=key: self._pset_val(k, t),
                getter=lambda k=key: self._pset_disp(k))

    def _build_probe_grid(self):
        self.cursor = FieldCursor(cols=3)
        for _ in range(9):
            self.cursor.add(0, 0, 80, 80)      # selection = cursor position
        page = PROBE_PAGES.get(self.chapter, {})
        # start on the centre Z where it exists, else the first live cell
        self.cursor.idx = 4 if (1, 1) in page else min(
            (r * 3 + c for (r, c) in page), default=0)

    def _grid_cell(self):
        page = PROBE_PAGES.get(self.chapter, {})
        return page.get((self.cursor.idx // 3, self.cursor.idx % 3))

    def _grid_macro(self):
        cell = self._grid_cell()
        return cell[0] if cell else None

    def on_touch(self, x, y):
        """Tap a probe grid cell to select it — same effect as the arrow keys,
        and never a path to motion: EXEC and its confirm still run the macro.
        Only the grid pages are touchable; the scrolling tables lay out rects
        for visible rows only, so their off-screen rects are stale."""
        if self.chapter not in PROBE_PAGES or not self.cursor:
            return False
        for i, f in enumerate(self.cursor.fields):
            if f.x <= x < f.x + f.w and f.y <= y < f.y + f.h:
                self.cursor.idx = i
                return True
        return False

    def _probe_ready(self):
        st = self.app.stat
        if st.estop or st.task_state != linuxcnc.STATE_ON:
            self.app.alarm("MACHINE NOT READY")
            return False
        if self._probe or st.interp_state != linuxcnc.INTERP_IDLE:
            self.app.alarm("PROGRAM RUNNING")
            return False
        # Args are converted to the current interp units, but the macros'
        # own constants and the [TOOLSETTER] ini values are not: refuse in
        # the foreign mode.
        if (st.program_units == 2) != (st.linear_units == 1.0):
            self.app.alarm("SWITCH TO MACHINE UNITS (G20/G21) FIRST")
            return False
        return True

    def _arm_probe(self, kind, code):
        """Fire the macro and start watching for it. Not armed on a mode
        failure, so no PROBING banner and no read-back."""
        try:
            self.app.mdi_async(code)
        except linuxcnc.error as e:
            self.app.alarm(str(e))
            return
        self._probe = {"kind": kind, "state": "ARMED",
                       "t0": time.monotonic()}

    def _wprobe_exec(self):
        name = self._grid_macro()
        if not name or not self._probe_ready():
            return
        d = self._pdefaults()
        keys = [k for k, _l, _x, _kind in PROBE_PARAMS]
        # #16 is wco_rotation to an edge-angle macro but cal_diameter to a cal
        # macro, so the tail is picked by family — never sent to both.
        if name in ANG_MACROS:
            keys += ["wco_rot"]
        elif name in CAL_MACROS:
            keys += ["cal_dia", "cal_x_width", "cal_y_width", "cal_axis"]
        # stored machine units -> interp units; :.4f never emits exponents
        args = " ".join(
            f"[{d[k]:g}]" if PROBE_KIND[k] == "NUM"
            else f"[{self.app.machine_to_interp(d[k]):.4f}]"
            for k in keys)
        self._probe_note = ""
        # CAL derives a stylus correction rather than setting an offset
        self._arm_probe("CAL" if self.chapter == "PCAL" else "WORK",
                        f"o<{name}> call {args}")

    def _tprobe_exec(self):
        if any(not self._tsetter.get(k) for k, _l in self.TSETTER_KEYS):
            self.app.alarm("TOOLSETTER NOT CONFIGURED - SEE INI [TOOLSETTER]")
            return
        if not self._probe_ready():
            return
        self._arm_probe("TOOL", f"o<{TOOL_SENSOR_MACRO}> call")

    def _draw_pset(self, renderer, f):
        draw_line(renderer, f,
                  f"PROBE SETTINGS  ({self.app.unit_tag()})", 10, 70)
        self._scroll_to_cursor(len(PROBE_FIELDS))
        for vis, i in enumerate(range(self.scroll,
                                      min(len(PROBE_FIELDS),
                                          self.scroll + self.VISIBLE_ROWS))):
            key, label, _x, _kind = PROBE_FIELDS[i]
            y = 150 + vis * 66
            draw_line(renderer, f, label, 10, y)
            fld = self.cursor.fields[i]
            fld.x, fld.y, fld.w, fld.h = 560, y - 2, 300, 56
            draw_field(renderer, f, self._pset_text(key), i, self.cursor)

    GRID_X, GRID_Y, CELL = 40, 180, 100

    def _draw_wprobe(self, renderer, f):
        page = PROBE_PAGES[self.chapter]
        title = dict(PROBE_PAGE_KEYS)[self.chapter]
        draw_line(renderer, f, f"WORK PROBE / {title}", 10, 70)
        for r in range(3):
            for c in range(3):
                i = r * 3 + c
                x = self.GRID_X + c * (self.CELL + 6)
                y = self.GRID_Y + r * (self.CELL + 6)
                fld = self.cursor.fields[i]
                fld.x, fld.y, fld.w, fld.h = x, y, self.CELL, self.CELL
                cell = page.get((r, c))
                if self.cursor.is_current(i):
                    SDL_SetRenderDrawColor(renderer, *CURSOR_C, 255)
                    SDL_RenderFillRect(renderer,
                                       SDL_Rect(x, y, self.CELL, self.CELL))
                SDL_SetRenderDrawColor(renderer,
                                       *(GRID_ON_C if cell else GRID_OFF_C), 255)
                SDL_RenderDrawRect(renderer, SDL_Rect(x, y, self.CELL, self.CELL))
                if cell:
                    draw_probe_icon(renderer, cell[1], r, c, x, y, self.CELL)
        # large diagram of the selected cell — same routine, bigger box
        cell = self._grid_cell()
        if cell:
            draw_probe_icon(renderer, cell[1], self.cursor.idx // 3,
                            self.cursor.idx % 3, 420, 170, 260)
        st = self.app.stat
        draw_line(renderer, f, cell[0] if cell else "(empty cell)",
                  730, 200, WHITE if cell else RED)
        draw_line(renderer, f, f"WRITES TO: {WCS[st.g5x_index]}", 730, 260)
        if self.chapter == "PCAL" and cell:
            # Each cal macro positions off one field and compares against
            # another, and they are not always the same one — so name both.
            labels = dict((k, l) for k, l, _x, _kind in PROBE_FIELDS)
            for n, key in enumerate(CAL_FIELDS.get(cell[0], ())):
                draw_line(renderer, f,
                          f"{labels[key]}: {self._pset_text(key)}"
                          f" {self.app.unit_tag()}",
                          730, 320 + n * 46, DIM)
        else:
            draw_line(renderer, f, "DIMS IN [P.SET]", 730, 320,
                      DIM)
        if self._probe_note:
            draw_line(renderer, f, self._probe_note, 730, 510, RED)
        # The routine's own placement instructions, read from its .ngc. Full
        # width below the grid — the macro authors already wrapped these.
        if cell:
            lines = macro_help(cell[0])
            for n, txt in enumerate(lines):
                draw_line(renderer, f, txt, 40, 560 + n * 44,
                          DIM if n == 0 else ACCENT)

    # [TOOLSETTER] keys tool_length.ngc needs, all machine units. Order is
    # the on-screen order (column-major, two columns).
    TSETTER_KEYS = (("X", "SETTER X"), ("Y", "SETTER Y"),
                    ("Z_SAFE", "Z SAFE"), ("Z_START", "Z START"),
                    ("MAXPROBE", "MAX PROBE"), ("SEARCH_VEL", "SEARCH FEED"),
                    ("PROBE_VEL", "PROBE FEED"), ("BACKOFF", "BACKOFF"),
                    ("Z_REF", "Z REF"))
    TSETTER_OPTIONAL = "PROBE_INPUT"     # setter contact; pre-trip check only

    def _read_toolsetter(self):
        """[TOOLSETTER] values the macro runs from, read once on entry so the
        draw loop does no file I/O. None for any key the INI lacks."""
        keys = [k for k, _l in self.TSETTER_KEYS] + [self.TSETTER_OPTIONAL]
        try:
            ini = linuxcnc.ini(self.app.stat.ini_filename)
            return {k: ini.find("TOOLSETTER", k) for k in keys}
        except Exception:
            return {k: None for k in keys}

    def _draw_tprobe(self, renderer, f):
        draw_line(renderer, f, "TOOL LENGTH PROBE", 10, 70)
        st = self.app.stat
        draw_line(renderer, f, f"TOOL IN SPINDLE: T{st.tool_in_spindle:02d}",
                  10, 150)
        vals = self._tsetter
        for n, (key, label) in enumerate(self.TSETTER_KEYS):
            v = vals.get(key)
            draw_line(renderer, f, f"{label}: {v if v else 'NOT SET'}",
                      10 if n < 5 else 700, 230 + (n % 5) * 46,
                      DIM if v else RED)
        pin = vals.get(self.TSETTER_OPTIONAL)
        draw_line(renderer, f,
                  f"TRIP INPUT: {pin}" if pin else "TRIP INPUT: NONE (OPTIONAL)",
                  700, 230 + 4 * 46, DIM)
        if st.tool_in_spindle <= 0:
            draw_line(renderer, f, "NO TOOL LOADED", 10, 520, RED)
        else:
            draw_line(renderer, f,
                      "Z.PRB MEASURES LOADED TOOL, WRITES LENGTH, APPLIES G43",
                      10, 520, ACCENT)

    PROBE_START_S = 2.0      # ARMED this long with the interp idle = no start

    def tick(self):
        """Every frame, on every screen (ScreenManager.draw), so a probe
        started here completes correctly even if the operator pages away.
        mdi_async returns before task has picked the call up, so idle right
        after EXEC means "not started yet", not "done": ARMED -> RUNNING on
        the first non-idle frame, RUNNING -> done on the next idle one."""
        p = self._probe
        if not p:
            return
        idle = self.app.stat.interp_state == linuxcnc.INTERP_IDLE
        if p["state"] == "ARMED":
            if not idle:
                p["state"] = "RUNNING"
            elif time.monotonic() - p["t0"] > self.PROBE_START_S:
                self._probe = None
                self.app.alarm("PROBE DID NOT START")
        elif idle:
            self._probe = None
            self._probe_done(p["kind"])

    def _poll_probe(self, renderer, f):
        """PROBING banner while a probe is armed or running."""
        if self._probe:
            draw_line(renderer, f, "PROBING...", 730, 130, RED)

    def _probe_done(self, kind):
        st = self.app.stat
        if kind == "WORK":
            # macros write with G10 L20 P0 = the active system; the var file
            # is not flushed yet, stat is
            st.poll()
            self._wcs_from_stat(st.g5x_index - 1)
        elif kind == "TOOL":
            # Contract with tool_length.ngc: it writes the MEASURED length
            # with G10 L1 (no wear) and G43s it. The .tbl comment, token
            # included, survives that rewrite, so wear is read back here
            # and re-added as geometry = measured; then G43 again so the
            # active length includes the wear too.
            self.app.command.load_tool_table()
            st.poll()
            self._read_table()
            tid = st.tool_in_spindle
            if tid > 0 and self._wear_of(tid)["Z"] != 0:
                measured = next((t.zoffset for t in st.tool_table
                                 if t.id == tid), None)
                if measured is not None:
                    self._set_tool_machine(tid, 0, measured, "GEOM")
                    try:
                        if not self.app.mdi("G43"):
                            self.app.alarm("G43 NOT ACKNOWLEDGED")
                    except linuxcnc.error as e:
                        self.app.alarm(str(e))
        elif kind == "CAL":
            # the macro computes the correction; entering it is manual
            # until the storage location is confirmed on the machine
            self._probe_note = "CAL DONE - ENTER RESULT AS CAL OFFSET"


# SystemScreen lives in systemscreen.py


class MessageScreen(Screen):
    def on_enter(self):
        self.set_root([K("CLEAR", self.app.alarms.reset, kind=ACT)])

    def draw(self, renderer, area):
        f = self.app.font
        y = 80
        al = self.app.alarms
        if al.active:
            draw_line(renderer, f, "ACTIVE", 10, y, RED); y += 60
            for e in list(al.active)[:5]:
                draw_line(renderer, f, f"{e['time']}  {e['text']}", 10, y, RED)
                y += 55
            y += 20
        draw_line(renderer, f, "HISTORY", 10, y); y += 60
        for e in list(reversed(al.history))[:10]:
            draw_line(renderer, f, f"{e['time']}  {e['text']}", 10, y)
            y += 55


class GraphicsScreen(Screen):
    PAN_STEP = 120                    # px per softkey press

    def on_enter(self):
        app = self.app
        if not hasattr(app, "backplot"):
            # sized to the CLIPPED content area (status bar, input line, and
            # pane all excluded) so edge indicators are never clipped
            app.backplot = Backplot2D(app.renderer, 1920 - PANE_W,
                                      SOFTKEY_Y - STATUS_H - BUFFER_H)
        bp = app.backplot
        if bp.needs_parse(app.stat):
            bp.load(app.stat)

        page1 = [
            K("XY",  lambda: bp.set_view("XY")),
            K("XZ",  lambda: bp.set_view("XZ")),
            K("YZ",  lambda: bp.set_view("YZ")),
            K("ISO", lambda: bp.set_view("ISO")),
            K("ZOOM-", lambda: bp.zoom(1.25)),
            K("ZOOM+", lambda: bp.zoom(0.75)),
            K("<-",  lambda: bp.pan(-self.PAN_STEP, 0)),
            K("->",  lambda: bp.pan(+self.PAN_STEP, 0)),
            K("UP",  lambda: bp.pan(0, -self.PAN_STEP)),
            K("DOWN", lambda: bp.pan(0, +self.PAN_STEP)),
        ]
        page2 = [
            K("FIT",    lambda: (bp.fit(), bp.rebake())),
            K("REDRAW", bp.reset_trace),
        ]
        self.set_root([page1, page2])

    def active_keys(self):
        bp = getattr(self.app, "backplot", None)
        return (bp.view,) if bp else ()

    def draw(self, renderer, area):
        app = self.app
        bp = app.backplot

        if bp.needs_parse(app.stat):          # file changed while on screen
            bp.load(app.stat)                 # background; returns at once
        bp.poll()                             # install a finished parse

        if bp.loading:
            n = bp.loading_count
            msg = f"PLOT LOADING  ({n:,} segs)" if n else "PLOT LOADING"
            plot_h = SOFTKEY_Y - STATUS_H - BUFFER_H
            draw_line(renderer, app.font, msg,
                      (1920 - PANE_W - text_width(app.font, msg)) // 2,
                      STATUS_H + plot_h // 2 - 20)
            return

        bp.update(app.stat)                   # advance gray-out
        bp.draw(0, STATUS_H, app.stat)        # plot + marker + UCS arrows

        # UCS letters (engine draws arrows; text needs the font)
        for name, (x, y) in bp.label_positions().items():
            r, g, b = AXIS_COLORS[name]
            draw_line(renderer, app.font, name, x + 6, y - 24, SDL_Color(r, g, b))

        # view name, top-right of the plot area; parse errors in red
        draw_line(renderer, app.font, bp.view,
                  1920 - PANE_W - 120, STATUS_H + 10)
        if bp.last_error:
            draw_line(renderer, app.font, bp.last_error, 10, STATUS_H + 10,
                      RED)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class ScreenManager:
    def __init__(self, app, screens, win_w, win_h):
        self.app = app
        self.screens = screens          # {index: Screen}
        self.W, self.H = win_w, win_h
        self.content = SDL_Rect(0, STATUS_H, win_w,
                                SOFTKEY_Y - STATUS_H - BUFFER_H)
        self.active = None
        self.active_idx = None
        self.help_on = False
        self.help_page = 0              # page of a multi-page help entry
        self.pressed_key = None         # softkey slot held by finger/mouse
        try:
            with open(os.path.join(os.path.dirname(__file__), "help.json")) as fp:
                self.help = json.load(fp)
        except (OSError, ValueError):
            self.help = {}

    def show(self, idx):
        if idx in self.screens:
            if self.active is not None and self.active is not self.screens[idx]:
                self.active.on_leave()
            self.active = self.screens[idx]
            self.active_idx = idx
            variant = PANE_SCREENS.get(idx)
            w = self.W - (PANE_W if variant else 0)
            self.content = SDL_Rect(0, STATUS_H, w,
                                    SOFTKEY_Y - STATUS_H - BUFFER_H)
            self.active.on_enter()

    def on_softkey(self, i):
        if self.help_on:
            self._help_key(i)
            return
        self.active.on_softkey(i)

    # ---- touch / pointer ----------------------------------------------------
    # Softkeys fire on RELEASE and only if the finger is still on the key it
    # went down on, so a mis-touch can be aborted by sliding off before lifting
    # — worth having when EXEC is one of the keys. Selection acts on press.

    def softkey_at(self, x, y):
        """Softkey slot 0-11 under a point, or None if the point is above the
        softkey band. Inverse of the layout drawn in draw()."""
        if y < SOFTKEY_Y:
            return None
        if x < KEY_X0:
            return 0                     # [<] stub
        return min(11, (x - KEY_X0) // KEY_W + 1)

    def on_press(self, x, y):
        if self.help_on:                 # [<]/[>] page, a tap elsewhere closes
            self._help_key(self.softkey_at(x, y))
            return
        self.pressed_key = self.softkey_at(x, y)
        if self.pressed_key is None and self.active:
            self.active.on_touch(x, y)

    def on_drag(self, x, y):
        # Slid off the key it went down on: drop the highlight and the arm.
        if self.pressed_key is not None and self.softkey_at(x, y) != self.pressed_key:
            self.pressed_key = None

    def on_release(self, x, y):
        i, self.pressed_key = self.pressed_key, None
        if i is not None and self.softkey_at(x, y) == i:
            self.on_softkey(i)

    def toggle_help(self):
        self.help_on = not self.help_on
        self.help_page = 0

    HELP_LINES = 16                     # body lines per help page

    def _help_pages(self, entry):
        """An entry's body as pages. `_body` is either a list of lines (cut
        into HELP_LINES pages here) or a list of pages, each a list of lines,
        for entries split by hand at sensible breaks."""
        body = entry.get("_body", [])
        if body and isinstance(body[0], list):
            pages = [p[:self.HELP_LINES] for p in body]
        else:
            pages = [body[k:k + self.HELP_LINES]
                     for k in range(0, len(body), self.HELP_LINES)]
        return pages or [[]]

    def _help_key(self, i):
        """While help is up: [>] next page, [<] previous page, any other
        softkey (or [>] on the last page) closes it."""
        n = len(self._help_pages(self._help_entry()))
        if i == 11 and self.help_page < n - 1:
            self.help_page += 1
        elif i == 0 and self.help_page > 0:
            self.help_page -= 1
        else:
            self.help_on = False

    def _help_entry(self):
        """Map the active screen (+ its chapter, if any) to a help entry."""
        name = type(self.active).__name__.replace("Screen", "").upper()
        chapter = getattr(self.active, "chapter", None)
        keys = []
        if chapter:
            # probe grid pages all share one entry
            if chapter in PROBE_PAGES:
                keys.append(f"{name}_PROBE")
            keys.append(f"{name}_{chapter}")
        keys.append(name)
        for k in keys:
            if k in self.help:
                return self.help[k]
        return self.help.get("_DEFAULT",
                              {"_title": "HELP", "_body": ["No help available."]})

    def on_key(self, sc):
        return self.active.on_key(sc)

    def on_edit_key(self, action):
        fn = getattr(self.active, "edit_key", None)
        if fn:
            fn(action)

    def draw(self, renderer):
        # background work for every screen (probe completion watchdog...)
        for s in self.screens.values():
            s.tick()
        # --- active screen, clipped between status bar and input line ---
        SDL_RenderSetClipRect(renderer, self.content)
        self.active.draw(renderer, self.content)
        SDL_RenderSetClipRect(renderer, None)

        items = self.active.softkey_items()

        # A key held by a finger lights the same as an active one, so the
        # label stays readable against the fill either way.
        def lit(i):
            return items[i][2] or i == self.pressed_key

        # --- active-key boxes (under the frame, so the dividers stay visible)
        SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
        kh = self.H - SOFTKEY_Y - 4
        for i in range(1, 11):
            if lit(i):
                SDL_RenderFillRect(renderer, SDL_Rect(
                    KEY_X0 + KEY_W * (i - 1) + 2, SOFTKEY_Y + 2, KEY_W - 4, kh))
        # The [<] / [>] stubs have no box of their own; light them on press
        # only, so a touch on them is acknowledged too.
        if self.pressed_key == 0:
            SDL_RenderFillRect(renderer, SDL_Rect(
                2, SOFTKEY_Y + 2, KEY_X0 - 4, kh))
        elif self.pressed_key == 11:
            x0 = KEY_X0 + KEY_W * 10
            SDL_RenderFillRect(renderer, SDL_Rect(
                x0 + 2, SOFTKEY_Y + 2, self.W - x0 - 4, kh))

        # --- softkey frame ---
        SDL_SetRenderDrawColor(renderer, *FRAME_C, 255)
        SDL_RenderDrawLine(renderer, 0, SOFTKEY_Y, self.W, SOFTKEY_Y)
        for i in range(12):
            SDL_RenderDrawLine(renderer, KEY_X0 + KEY_W * i, SOFTKEY_Y,
                               KEY_X0 + KEY_W * i, self.H)

        # --- labels: slot 0 in the left stub, 1-10 in the cells, 11 right stub
        f = self.app.font
        y = SOFTKEY_Y + 30

        def key_label(i, x):
            label, kind, _active = items[i]
            if label:
                draw_line(renderer, f, label, x, y,
                          BLACK if lit(i) else KEY_COLORS[kind])

        key_label(0, 12)
        for i in range(1, 11):
            key_label(i, KEY_X0 + KEY_W * (i - 1) + 12)
        key_label(11, self.W - 48)

        self._input_line(renderer)

        variant = PANE_SCREENS.get(self.active_idx)
        if variant:
            self.app.pane.draw(renderer, self.W - PANE_W, STATUS_H,
                               SOFTKEY_Y - STATUS_H, PANE_W, variant)

        if self.help_on:
            self._draw_help(renderer)

    def _draw_help(self, renderer):
        entry = self._help_entry()
        f = self.app.font
        # dim the screen, then a bordered panel
        SDL_SetRenderDrawBlendMode(renderer, SDL_BLENDMODE_BLEND)
        SDL_SetRenderDrawColor(renderer, *HELP_DIM_C)
        SDL_RenderFillRect(renderer, SDL_Rect(0, 0, self.W, self.H))
        px, py = 120, 90
        pw, ph = self.W - 240, self.H - 180
        SDL_SetRenderDrawColor(renderer, *BG_C, 255)
        SDL_RenderFillRect(renderer, SDL_Rect(px, py, pw, ph))
        SDL_SetRenderDrawColor(renderer, *HELP_BORDER_C, 255)
        SDL_RenderDrawRect(renderer, SDL_Rect(px, py, pw, ph))

        pages = self._help_pages(entry)
        self.help_page = min(self.help_page, len(pages) - 1)
        n = len(pages)
        title = "HELP - " + entry.get("_title", "")
        if n > 1:
            title += f"  ({self.help_page + 1}/{n})"
        draw_line(renderer, f, title, px + 30, py + 24, HELP_TITLE)
        y = py + 90
        for line in pages[self.help_page]:
            draw_line(renderer, f, line, px + 30, y)
            y += 44
        if n == 1:
            foot = "PRESS ANY SOFTKEY TO CLOSE"
        elif self.help_page < n - 1:
            foot = "[>] NEXT PAGE" + ("   [<] BACK" if self.help_page else "") \
                   + "   OTHER SOFTKEYS CLOSE"
        else:
            foot = "[<] BACK   ANY OTHER SOFTKEY CLOSES"
        draw_line(renderer, f, foot, px + 30, py + ph - 54, ACCENT)

    def _input_line(self, renderer):
        y = SOFTKEY_Y - BUFFER_H
        txt = self.app.input.text
        if (SDL_GetTicks() // 500) % 2:          # blinking entry cursor
            txt += "_"
        # a long raw line (SYSTEM editor) shows its tail, not its head
        while len(txt) > 1 and text_width(self.app.font, txt) > self.W - 20:
            txt = txt[1:]
        draw_line(renderer, self.app.font, txt, 10, y)