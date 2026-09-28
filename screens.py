#!/usr/bin/env python3
# screens.py — render helpers, Screen base with multi-level softkeys,
#              the POS / PROG / MESSAGE / GRAPHICS screens, and the
#              ScreenManager. OFFSET lives in offsets.py (probing data in
#              probe.py), SYSTEM in systemscreen.py.

import ctypes
import math
import re
from collections import deque
from sdl2 import *
from sdl2.sdlttf import *
from backplot import Backplot2D, AXIS_COLORS
import linuxcnc
import json
import os
import settings
import configfile
from filebrowser import FileBrowser
from lineeditor import LineEditor

WHITE = settings.sdl_color("TEXT", (255, 255, 255))
RED   = settings.sdl_color("ALARM", (250, 0, 0))
BLACK = settings.sdl_color("TEXT_INVERSE", (0, 0, 0))   # text on a lit fill
DIM    = settings.sdl_color("DIM", (150, 150, 150))
ACCENT = settings.sdl_color("ACCENT", (255, 150, 40))

BG_C       = settings.color("BACKGROUND", (0, 0, 0))
FRAME_C    = settings.color("FRAME", (0, 0, 255))        # softkey frame

STATUS_H  = 60          # content starts below this
SOFTKEY_Y = 960         # softkey frame line
KEY_X0, KEY_W = 60, 180 # [<] 0-60, ten keys, [>] 1860-1920
BUFFER_H = 54           # input line band above the softkey frame
PANE_W   = 460          # status pane width (statuspane imports this)

WCS_PARAM_BASE = 5221   # G54 X = 5221; each system +20; axes X..W
# 10-entry, 1-based by g5x_index (status bar):
WCS = ("None", "G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3")
# 9-entry, 0-based by system number (WORK table):
WCS_NAMES = ("G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3")

# LinuxCNC 9-tuple axis order — C is index 5 (B occupies 4 even if unused)
AXIS_IDX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}

PANE_SCREENS = {0: "LOADS", 1: "POSMODE", 2: "POSMODE", 5: "POSMODE"}
#               POS         PROG          OFFSET        GRAPHICS

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
# Shape primitives (the probe icons in probe.py draw with these too)
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
        self.editor = LineEditor(lambda: self.edit_lines, self._edit_set,
                                 self._edit_ins, self._edit_pop,
                                 normalize=space_words, collapse=True)
        # file browser state: DIR keeps its directory between visits, USB
        # starts again at the drive root
        if not hasattr(self, "files"):
            self.files = {
                "DIR": FileBrowser(lambda: self.app.prog_dir, self.FILE_ROWS),
                "USB": FileBrowser(self._usb_root, self.FILE_ROWS),
            }
        self.files["DIR"].path = self.files["DIR"].path or self.app.prog_dir
        self.files["USB"].path = None
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
            return self.editor.cur
        st = self.app.stat
        return (self.view_line if self.view_line is not None
                else max(0, (st.motion_line or st.current_line) - 1))

    def _search_goto(self, i):
        if self._in_editor():
            self.editor.goto(i)         # land on the first word of the hit
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
        self.editor.cur = 0
        self.editor.word = 0
        self.editor.scroll = 0
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

    # the working copy, as LineEditor sees it
    def _edit_set(self, i, text):
        self.edit_lines[i] = text

    def _edit_ins(self, i, text):
        self.edit_lines.insert(i, text)

    def _edit_pop(self, i):
        if self.edit_lines:
            self.edit_lines.pop(i)
        if not self.edit_lines:
            self.edit_lines = [""]

    def _edit_insert_word(self):
        """INSERT: buffer text becomes new word(s) AFTER the selected word
        (start of line if the line is empty)."""
        self.editor.insert_word(self.app.input.take())

    def _edit_alter(self):
        """ALTER: replace the selected word with the buffer text."""
        self.editor.alter(self.app.input.take())

    def _edit_delete_word(self):
        """DEL.WRD: remove the selected word."""
        self.editor.delete_word()

    def _edit_alter_line(self):
        """ALT.LIN: replace the whole cursored line with the buffer text."""
        self.editor.alter_line(self.app.input.take())

    def _edit_insert(self):
        """RETURN key: buffer text becomes a new LINE after the cursored one."""
        self.editor.insert_line(self.app.input.take())

    def _edit_delete_line(self):
        self.editor.delete_line()

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
    def _usb_root(self):
        for m in self.app.usb_mounts:
            if os.path.isdir(m):
                return m
        return None

    def _browser(self):
        return self.files[self.chapter]

    def _refresh_files(self):
        if not self._browser().refresh():
            self.note = "NO USB MOUNTED"
        self.set_root(self._root_row())

    def _cur_entry(self):
        return self._browser().cur_entry()

    def _file_select(self):
        e = self._cur_entry()
        if not e:
            return
        name, is_dir, path = e
        if is_dir:
            self._browser().path = path
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
        b = self._browser()
        for i, (name, is_dir, _p) in enumerate(b.entries):
            if not is_dir and os.path.splitext(name)[0].upper() in stems:
                b.cur = i
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
        base = self._browser().path
        if not base or not os.path.isdir(base):    # USB pulled / never there
            self.note = "NO USB MOUNTED" if self.chapter == "USB" else "DIRECTORY MISSING"
            return
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
            ed = self.editor
            if sc == SDL_SCANCODE_UP:
                ed.move(-1); return True
            if sc == SDL_SCANCODE_DOWN:
                ed.move(+1); return True
            if sc == SDL_SCANCODE_LEFT:
                ed.move_word(-1); return True
            if sc == SDL_SCANCODE_RIGHT:
                ed.move_word(+1); return True
            if sc == SDL_SCANCODE_PAGEUP:
                ed.page(-1, self.EDIT_ROWS); return True
            if sc == SDL_SCANCODE_PAGEDOWN:
                ed.page(+1, self.EDIT_ROWS); return True
            if sc == SDL_SCANCODE_RETURN:
                self._edit_insert(); return True
        elif ch in ("DIR", "USB"):
            if self._browser().on_key(sc):
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
        ed = self.editor
        # keep cursor visible
        ed.scroll_to_cursor(self.EDIT_ROWS)
        for row, idx in enumerate(range(ed.scroll,
                                        min(len(self.edit_lines),
                                            ed.scroll + self.EDIT_ROWS))):
            line = self.edit_lines[idx]
            prefix = f"{idx + 1:4d} "
            y = 130 + row * 55
            draw_line(renderer, f, prefix + line, 10, y)
            if idx != ed.cur:
                continue
            # highlight the selected WORD on the cursored line
            spans = [(m.start(), m.end())
                     for m in re.finditer(r'\S+', line)]
            if spans:
                ed.clamp_word()
                s, e = spans[ed.word]
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
        b = self._browser()
        draw_line(renderer, f, f"{self.chapter}  {b.path or '-'}", 10, 70)
        b.draw(renderer, f, 130, 60)


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
            from probe import PROBE_PAGES    # probe.py imports this module
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