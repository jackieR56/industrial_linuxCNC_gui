#!/usr/bin/env python3
# probe.py — probing data and drawings shared by the OFFSET screen's probe
#            pages: the Probe Basic macro tables, the P.SET parameter list,
#            macro header help, and the scenario icons.

import os
import re

from sdl2 import *

import settings
from screens import _pc, draw_circle, draw_arrow

CURSOR_C   = settings.color("CURSOR_CELL", (60, 60, 60)) # probe grid cursor
GRID_ON_C  = settings.color("GRID_ON", (27, 95, 165))    # probe cell, populated
GRID_OFF_C = settings.color("GRID_OFF", (45, 45, 45))    # probe cell, empty

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


# ---------------------------------------------------------------------------
# Probe scenario icons — grid cells and the big diagram share one routine,
# so they can never disagree. green = feature faces, red = probe start,
# purple = probe motion.
# ---------------------------------------------------------------------------
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
