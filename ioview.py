#!/usr/bin/env python3
# ioview.py — SYSTEM PHYS / HAL chapters: a PMC-style signal grid.
#
#   IoModel  owns io_map.yaml (generate when missing, reload when edited),
#            the Sampler and a halcmd description of the live HAL for the
#            detail line. One instance, shared by both views.
#   IoView   one section ("phys" or "hal"): sidebar of devices/groups,
#            grid of cells, detail band for the cursored cell, change log.
#
# Read-only. The model lives in iomap.py (no SDL there).

import ctypes
import os
import time

from sdl2 import *
from sdl2.sdlttf import TTF_OpenFont, TTF_FontHeight

import configfile
import iomap
import settings
from screens import (draw_line, text_width, WHITE, RED, BLACK, DIM, ACCENT,
                     KEY_HILITE, SOFTKEY_Y, BUFFER_H)

IO_ON_C   = settings.color("IO_ON", settings.color("OK", (40, 200, 120)))
IO_OFF_C  = settings.color("IO_OFF", (45, 45, 45))
IO_EDGE_C = settings.color("IO_EDGE", settings.color("ACCENT", (255, 150, 40)))
ALARM_C   = settings.color("ALARM", (250, 0, 0))
DIM_C     = settings.color("DIM", (150, 150, 150))
INV_C     = settings.color("TEXT_INVERSE", (0, 0, 0))    # rect on a lit cursor

FONT_PATH = settings.get_path("GUI", "FONT_PATH",
                              "/usr/share/fonts/truetype/dejavu/ISOCPEUR.TTF")
IO_FONT_SIZE = settings.get_int("GUI", "IO_FONT_SIZE", 30)
IO_SAMPLE_MS = settings.get_int("GUI", "IO_SAMPLE_MS", 50)
IO_MAP = settings.get_str("GUI", "IO_MAP", "io_map.yaml")

HALCMD_PERIOD = 0.5                 # fallback reader: a subprocess per read


class IoModel:
    def __init__(self, app):
        self.app = app
        self.map = iomap.IoMap()
        self.path = None
        self.mtime = None
        self.generated = False          # map in use came from discovery, not the file
        self.info = iomap.HalInfo()     # types / dirs / links for the detail line
        self.values = {}
        self.names = []                 # HAL names the sampler reads
        self.sampler = iomap.Sampler(self._read, IO_SAMPLE_MS / 1000.0)
        self._source = None

    # ---- live values ----
    def _pick_source(self):
        pane = getattr(self.app, "pane", None)
        if pane is not None and getattr(pane, "hal", None) is not None:
            try:
                src = iomap.hal_value_source(lambda: self.names)
                src()
                self.sampler.period = max(0.01, IO_SAMPLE_MS / 1000.0)
                return src
            except Exception:
                pass
        self.sampler.period = HALCMD_PERIOD
        return iomap.halcmd_source()

    def _read(self):
        if self._source is None:
            self._source = self._pick_source()
        vals = self._source()
        if vals is not None:
            self.values = vals
        return vals

    def tick(self):
        self.sampler.tick()

    def backend(self):
        if self._source is None:
            return "-"
        return "HAL" if self.sampler.period < HALCMD_PERIOD else "HALCMD"

    # ---- the map file ----
    def resolve_path(self, config_dir):
        p = os.path.expanduser(IO_MAP)
        return p if os.path.isabs(p) else os.path.join(config_dir, p)

    def _ini(self):
        return os.path.abspath(self.app.stat.ini_filename
                               or settings.INI_PATH or "")

    def _generate(self):
        self.info = iomap.HalInfo.from_halcmd()
        ctx = iomap.Context.for_config(self._ini(), self.info)
        return iomap.generate(ctx)

    def ensure(self):
        """Load or create the map. Returns a note for the operator ('' when
        nothing worth saying happened)."""
        self.path = self.resolve_path(os.path.dirname(self._ini()))
        note = ""
        if not os.path.exists(self.path):
            note = self._write_generated("GENERATED")
        else:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                mtime = None
            if mtime != self.mtime or self.generated:
                note = self._load(mtime)
        if not self.info:
            self.info = iomap.HalInfo.from_halcmd()
        return note

    def _write_generated(self, verb):
        gen = self._generate()
        name = os.path.basename(self.path)
        try:
            configfile.write_with_backup(self.path, iomap.map_to_yaml(gen))
            self.mtime = os.path.getmtime(self.path)
            written = True
        except OSError as e:
            written = False
            err = e.strerror or str(e)
        self._use(iomap.map_from_dict(gen), generated=True)
        nd, npd, ng, npg = self.map.counts()
        if not written:
            return f"CANNOT WRITE {name}: {err} - USING DISCOVERED LIST"
        if iomap.yaml is None:
            return f"{verb} {name} - PYYAML MISSING, EDITS WON'T LOAD"
        self.generated = False          # the file on disk is exactly this map
        return f"{verb} {name}: {nd} DEVICES / {npd} PINS, {ng} GROUPS / {npg} SIGNALS"

    def _load(self, mtime):
        name = os.path.basename(self.path)
        try:
            m = iomap.load_map(self.path)
        except RuntimeError:
            self._use(iomap.map_from_dict(self._generate()), generated=True)
            return "PYYAML MISSING - MAP EDITS IGNORED (apt install python3-yaml)"
        except ValueError as e:
            self.mtime = mtime          # don't re-parse every entry
            return f"{name} YAML ERROR: {e}"
        except OSError as e:
            return f"CANNOT READ {name}: {e.strerror or e}"
        self.mtime = mtime
        self._use(m, generated=False)
        if m.warnings:
            return f"{name}: {len(m.warnings)} BAD ENTRIES SKIPPED - {m.warnings[0]}"
        return ""

    def _use(self, m, generated):
        self.map = m
        self.generated = generated
        # everything the views read: points + device status pins
        names = [p.pin for p in m.points()]
        names += [s for sec in ("phys", "hal") for g in m.section(sec)
                  for s in g.status]
        self.names = list(dict.fromkeys(names))
        self.sampler.attach(m)

    def regen(self):
        return self._write_generated("REBUILT")

    # ---- lookups for the views ----
    def status(self, group):
        """True = all status pins TRUE, False = one is FALSE/missing,
        None = the group has none."""
        if not group.status:
            return None
        return all(bool(self.values.get(p)) for p in group.status)

    def describe(self, name):
        """'bit OUT ==> sig' / 'bit  driver lcec.0.D1.din-0' for a pin/sig."""
        pin = self.info.pins.get(name)
        if pin is not None:
            s = f"{pin.type} {pin.dir}"
            if pin.signal:
                arrow = {"OUT": "==>", "IN": "<=="}.get(pin.dir, "<=>")
                s += f"  {arrow} {pin.signal}"
            return s
        sig = self.info.sigs.get(name)
        if sig is not None:
            s = f"{sig.type} SIGNAL"
            if sig.driver:
                s += f"  <== {sig.driver}"
            if sig.readers:
                s += f"  ==> {sig.readers[0]}"
                if len(sig.readers) > 1:
                    s += f" +{len(sig.readers) - 1}"
            return s
        return "NOT IN HAL" if self.info else ""


def _ago(t):
    if t is None:
        return "-"
    d = time.monotonic() - t
    if d < 60:
        return f"{d:.1f}S"
    if d < 3600:
        return f"{int(d // 60)}M{int(d % 60):02d}S"
    return f"{int(d // 3600)}H{int(d % 3600 // 60):02d}M"


def _num(v):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "1" if v else "0"
    return f"{v:.4g}" if isinstance(v, float) else str(v)


class IoView:
    TITLE_Y = 66
    GRID_Y = 130
    SIDE_X, SIDE_W = 10, 390
    GRID_X, GRID_R = 420, 1910
    COLS = 3
    NOTE_Y = SOFTKEY_Y - BUFFER_H - 120        # SystemScreen.NOTE_Y
    LEGEND_Y = NOTE_Y + 62
    LOG_ROWS_MAX = 40

    _font = None
    _rh = None

    def __init__(self, app, model, section):
        self.app = app
        self.model = model
        self.section = section          # "phys" | "hal"
        self.gi = 0                     # group index
        self.idx = 0                    # point index in the group
        self.side_scroll = 0
        self.show_log = False
        self._cells = ()                # (rect, idx) laid out last frame
        self._side = ()                 # (rect, gi)
        self._cw = {}                   # font address -> width of "0"

    # ---- font / layout ----
    def font(self):
        if IoView._font is None:
            f = TTF_OpenFont(FONT_PATH.encode(), IO_FONT_SIZE)
            IoView._font = f or self.app.font
            IoView._rh = TTF_FontHeight(IoView._font) + 8
        return IoView._font

    def rows(self):
        self.font()
        detail_y = self.NOTE_Y - 2 * self._rh - 6
        return max(1, (detail_y - 10 - self.GRID_Y) // self._rh)

    def per_page(self):
        return self.rows() * self.COLS

    def groups(self):
        return self.model.map.section(self.section)

    def group(self):
        g = self.groups()
        if not g:
            return None
        self.gi = max(0, min(self.gi, len(g) - 1))
        return g[self.gi]

    def point(self):
        g = self.group()
        if g is None or not g.points:
            return None
        self.idx = max(0, min(self.idx, len(g.points) - 1))
        return g.points[self.idx]

    # ---- navigation ----
    def set_group(self, gi, last=False):
        g = self.groups()
        if not g:
            return
        self.gi = gi % len(g)
        self.idx = max(0, len(g[self.gi].points) - 1) if last else 0

    def next_group(self, d):
        self.set_group(self.gi + d)

    def move(self, d):
        """d points along the column-major grid; running off either end of
        the group lands in the neighbouring group."""
        g = self.group()
        if g is None:
            return
        n = len(g.points)
        i = self.idx + d
        if i >= n:
            if abs(d) == 1 or self.idx == n - 1:
                self.set_group(self.gi + 1)
            else:
                self.idx = n - 1
        elif i < 0:
            if abs(d) == 1 or self.idx == 0:
                self.set_group(self.gi - 1, last=True)
            else:
                self.idx = 0
        else:
            self.idx = i

    def search(self, pat, direction):
        """Next point whose label/pin/tag/desc contains pat, across groups."""
        groups = self.groups()
        flat = [(gi, i) for gi, g in enumerate(groups)
                for i in range(len(g.points))]
        if not flat:
            return False
        try:
            cur = flat.index((self.gi, self.idx))
        except ValueError:
            cur = 0
        n = len(flat)
        for k in range(1, n + 1):
            gi, i = flat[(cur + direction * k) % n]
            p = groups[gi].points[i]
            if any(pat in s.lower() for s in (p.label, p.pin, p.tag, p.desc)):
                self.gi, self.idx = gi, i
                return True
        return False

    def on_key(self, sc):
        if sc == SDL_SCANCODE_UP:
            self.move(-1); return True
        if sc == SDL_SCANCODE_DOWN:
            self.move(+1); return True
        if sc == SDL_SCANCODE_LEFT:
            self.move(-self.rows()); return True
        if sc == SDL_SCANCODE_RIGHT:
            self.move(+self.rows()); return True
        return False

    def on_touch(self, x, y):
        for (rx, ry, rw, rh), gi in self._side:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                self.set_group(gi)
                return True
        for (rx, ry, rw, rh), i in self._cells:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                self.idx = i
                return True
        return False

    # ---- drawing ----
    def _fit(self, f, text, w):
        if not text:
            return text
        key = ctypes.cast(f, ctypes.c_void_p).value
        if key not in self._cw:
            self._cw[key] = max(1, text_width(f, "0"))
        s = text[:max(1, int(w / self._cw[key]) + 4)]
        while len(s) > 1 and text_width(f, s) > w:
            s = s[:-1]
        return s

    def _text(self, r, f, text, x, y, color=WHITE, w=None):
        if w is not None:
            text = self._fit(f, text, w)
        if text:
            draw_line(r, f, text, x, y, color)

    @staticmethod
    def _rect(r, rgb, x, y, w, h, fill=True):
        SDL_SetRenderDrawColor(r, *rgb, 255)
        (SDL_RenderFillRect if fill else SDL_RenderDrawRect)(r, SDL_Rect(x, y, w, h))

    def draw(self, r, big):
        f = self.font()
        self.model.tick()
        g = self.group()
        self._title(r, big, g)
        self._draw_side(r, f)
        if g is None:
            self._text(r, big, "NO POINTS - (OPRT) > REGEN, OR EDIT "
                       + os.path.basename(self.model.path or "io_map.yaml"),
                       self.GRID_X, self.GRID_Y, DIM)
            self._cells = ()
            return
        if self.show_log:
            self._cells = ()
            self._draw_log(r, f)
        else:
            self._draw_grid(r, f, g)
            self._draw_detail(r, f)
        self._text(r, f, "I/O = FIELD SIDE   LAMP = ACTIVE   OUTLINE = CHANGED <1S"
                   "   ? = NOT IN HAL   R/F = RISE/FALL COUNT",
                   10, self.LEGEND_Y, DIM, 1900)

    def _title(self, r, big, g):
        name = "PHYS" if self.section == "phys" else "HAL"
        t = f"{name}  {g.name}" if g else name
        if g and g.type and g.type != g.name:
            t += f"  {g.type}"
        right = []
        if self.model.sampler.hold:
            right.append("HOLD")
        if not self.model.sampler.ok:
            right.append("NO HAL DATA")
        elif g and g.points and not self.show_log:
            pages = (len(g.points) - 1) // self.per_page() + 1
            right.append(f"PG {self.idx // self.per_page() + 1}/{pages}")
        if self.show_log:
            right.append("LOG")
        rt = "  ".join(right)
        rw = text_width(big, rt) if rt else 0
        self._text(r, big, t, 10, self.TITLE_Y, WHITE, 1890 - rw - 30)
        if rt:
            self._text(r, big, rt, 1910 - rw, self.TITLE_Y,
                       RED if "NO HAL DATA" in rt else ACCENT)

    def _draw_side(self, r, f):
        groups = self.groups()
        rh = self._rh
        # stop a row short of the note line; that row holds the "n/N" count
        n_rows = (self.NOTE_Y - 10 - self.GRID_Y) // rh - 1
        if self.gi < self.side_scroll:
            self.side_scroll = self.gi
        elif self.gi >= self.side_scroll + n_rows:
            self.side_scroll = self.gi - n_rows + 1
        vis = []
        lamp = rh - 20
        for row, gi in enumerate(range(self.side_scroll,
                                       min(len(groups), self.side_scroll + n_rows))):
            g = groups[gi]
            y = self.GRID_Y + row * rh
            cur = gi == self.gi
            if cur:
                self._rect(r, KEY_HILITE, self.SIDE_X, y - 2, self.SIDE_W, rh - 2)
            st = self.model.status(g)
            lx = self.SIDE_X + 6
            if st is None:
                self._rect(r, DIM_C, lx, y + 8, lamp, lamp, fill=False)
            else:
                self._rect(r, IO_ON_C if st else ALARM_C, lx, y + 8, lamp, lamp)
            cnt = str(len(g.points))
            cw = text_width(f, cnt)
            col = BLACK if cur else WHITE
            self._text(r, f, g.name, lx + lamp + 10, y, col,
                       self.SIDE_W - lamp - cw - 40)
            self._text(r, f, cnt, self.SIDE_X + self.SIDE_W - cw - 6, y,
                       BLACK if cur else DIM)
            vis.append(((self.SIDE_X, y - 2, self.SIDE_W, rh), gi))
        self._side = tuple(vis)
        if len(groups) > n_rows:
            y = self.GRID_Y + n_rows * rh
            self._text(r, f, f"{self.gi + 1}/{len(groups)}", self.SIDE_X, y, DIM)

    def _draw_grid(self, r, f, g):
        rows = self.rows()
        per = rows * self.COLS
        page = self.idx // per
        cw = (self.GRID_R - self.GRID_X) // self.COLS
        rh = self._rh
        lamp = rh - 20
        now = time.monotonic()
        vis = []
        for k, i in enumerate(range(page * per, min(len(g.points), (page + 1) * per))):
            p = g.points[i]
            col, row = divmod(k, rows)
            x = self.GRID_X + col * cw
            y = self.GRID_Y + row * rh
            w = cw - 8
            cur = i == self.idx
            if cur:
                self._rect(r, KEY_HILITE, x, y - 2, w, rh - 2)
            if self.model.sampler.recent(p, now):
                self._rect(r, IO_EDGE_C, x, y - 2, w, rh - 2, fill=False)
                self._rect(r, IO_EDGE_C, x + 1, y - 1, w - 2, rh - 4, fill=False)
            fg = BLACK if cur else WHITE
            tx = x + 6
            dir_w = text_width(f, "IO") + 8
            if p.dir:
                self._text(r, f, p.dir, tx, y, BLACK if cur else DIM)
            tx += dir_w
            lit = p.lit()
            if p.is_bit():
                if p.shown is None:
                    self._rect(r, ALARM_C, tx, y + 8, lamp, lamp, fill=False)
                else:
                    self._rect(r, INV_C if cur else DIM_C, tx - 1, y + 7,
                               lamp + 2, lamp + 2, fill=False)
                    self._rect(r, IO_ON_C if lit else IO_OFF_C, tx, y + 8,
                               lamp, lamp)
                tx += lamp + 10
                val = "?" if p.shown is None else ""
            else:
                val = p.text()
            vw = text_width(f, val) if val else 0
            label = p.label
            if self.section == "phys" and p.tag and p.tag != p.label:
                label = f"{p.tag}  {p.label}"
            self._text(r, f, label, tx, y,
                       (BLACK if cur else DIM) if p.missing else fg,
                       x + w - tx - vw - 16)
            if val:
                self._text(r, f, val, x + w - vw - 6, y,
                           RED if p.shown is None else (BLACK if cur else ACCENT))
            vis.append(((x, y - 2, w, rh), i))
        self._cells = tuple(vis)

    def _draw_detail(self, r, f):
        p = self.point()
        if p is None:
            return
        rh = self._rh
        y = self.NOTE_Y - 2 * rh - 6
        SDL_SetRenderDrawColor(r, *DIM_C, 255)
        SDL_RenderDrawLine(r, self.GRID_X, y - 6, self.GRID_R, y - 6)
        name = p.pin + (f" BIT {p.bit}" if p.bit is not None else "")
        line1 = f"{name}   {self.model.describe(p.pin)}"
        self._text(r, f, line1, self.GRID_X, y, ACCENT, self.GRID_R - self.GRID_X)
        stats = [f"R {p.rises}  F {p.falls}", f"LAST CHG {_ago(p.changed)}"]
        if p.shown is not None and not isinstance(p.shown, bool):
            stats.append(f"MIN {_num(p.vmin)}  MAX {_num(p.vmax)}")
        line2 = "   ".join(stats)
        if p.desc:
            line2 = p.desc + "   " + line2
        self._text(r, f, line2, self.GRID_X, y + rh, WHITE,
                   self.GRID_R - self.GRID_X)

    def _draw_log(self, r, f):
        log = self.model.sampler.log
        n = min(self.LOG_ROWS_MAX,
                (self.NOTE_Y - 10 - self.GRID_Y) // self._rh)
        if not log:
            self._text(r, f, "NO CHANGES SINCE THIS PAGE WAS OPENED"
                       " (OR CLR.CNT)", self.GRID_X, self.GRID_Y, DIM)
            return
        for row, (stamp, sec, label, old, new) in enumerate(reversed(log)):
            if row >= n:
                break
            y = self.GRID_Y + row * self._rh
            col = WHITE if sec == self.section else DIM
            self._text(r, f, f"{stamp}  {sec.upper():4s}  {label}",
                       self.GRID_X, y, col, 1100)
            self._text(r, f, f"{_num(old)} -> {_num(new)}",
                       self.GRID_X + 1150, y, ACCENT if new else col, 330)
