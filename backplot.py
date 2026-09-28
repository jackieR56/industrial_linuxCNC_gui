#!/usr/bin/env python3
# backplot.py — 2D toolpath backplot: gcode.parse -> segment list ->
#               baked SDL texture per view, incremental gray-out, live marker.
#
# All geometry is MACHINE units throughout (display-units setting does not
# apply here; the plot is unit-agnostic once scaled to fit).

import os
import math
import queue
import shutil
import tempfile
import threading
import types

import linuxcnc
import gcode

from sdl2 import *

import settings

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
TOOL_PALETTE = settings.get_palette(  # indexed by tool % len — feed moves
    "GUI_COLORS", "BP_TOOL_PALETTE", [
    (255, 255, 255), (255, 210,  60), ( 80, 220, 100), (100, 170, 255),
    (255, 120, 120), (220, 120, 255), ( 90, 230, 230), (255, 160,  40),
])
RAPID_COLOR   = settings.color("BP_RAPID", (70, 70, 130))     # traverses (dim blue)
GRAY_FEED     = settings.color("BP_GRAY_FEED", (85, 85, 85))  # executed feed
GRAY_RAPID    = settings.color("BP_GRAY_RAPID", (40, 40, 60)) # executed rapid
MARKER_COLOR  = settings.color("BP_MARKER", (255, 255, 0))
TOOL_VECTOR   = settings.color("BP_TOOL_VECTOR", (255, 160, 40))
BG_COLOR      = settings.color("BACKGROUND", (0, 0, 0))
AXIS_COLORS   = {"X": settings.color("BP_AXIS_X", (255, 60, 60)),
                 "Y": settings.color("BP_AXIS_Y", (60, 220, 60)),
                 "Z": settings.color("BP_AXIS_Z", (80, 120, 255))}

# ---------------------------------------------------------------------------
# Views: (name -> 3x2 projection). u,v are math coords, v-up; pixel flip
# happens in the transform. ISO is a fixed isometric — still just a matrix.
# ---------------------------------------------------------------------------
S3 = math.sqrt(3) / 2
VIEWS = {
    "XY":  ((1, 0, 0), (0, 1, 0)),
    "XZ":  ((1, 0, 0), (0, 0, 1)),
    "YZ":  ((0, 1, 0), (0, 0, 1)),
    "ISO": ((S3, -S3, 0), (0.5, 0.5, 1.0)),
}


# ---------------------------------------------------------------------------
# Canon: receives interpreter output, collects flat segment list.
# Segment = (x0,y0,z0, x1,y1,z1, tool, is_rapid, seq)
# ---------------------------------------------------------------------------
class PlotCanon:
    ARC_CHORD_DEG = 4.0                # tessellation step; ~90 segs per circle

    def __init__(self, stat, parameter_file, cancelled=lambda: False):
        self.stat = stat                        # a snapshot, never live stat
        self.parameter_file = parameter_file    # read by the gcode module
        self.cancelled = cancelled              # -> True aborts the parse
        # gcode.parse emits positions in INCHES ("internal units") regardless
        # of machine units — verified empirically (identical inch-sized
        # extents on mm and inch configs; no units callback consulted).
        # k converts internal inches -> machine units at emit time:
        self.k = (stat.linear_units or 1.0) * 25.4   # mm machine: 25.4, inch: 1.0
        self.to = (0.0, 0.0, 0.0)       # active tool offset, INCH frame
        self.segments = []
        self.cur_tool = 0
        self.cur_seq = 0
        self.plane = 1                           # 1=XY 2=YZ 3=XZ
        self.lo = (0.0, 0.0, 0.0)                # last position, INCH frame
        # offsets in the INCH frame (interp callbacks deliver inches; the
        # live seed in load() is divided by k before landing here)
        self.g5x = (0.0, 0.0, 0.0)
        self.g92 = (0.0, 0.0, 0.0)
        self.rot_xy = 0.0

    # ---- anything we don't care about becomes a no-op ----
    def __getattr__(self, name):
        return lambda *a, **k: None

    # ---- callbacks that must return real values ----
    def check_abort(self):                return self.cancelled()
    def get_block_delete(self):           return False
    def get_axis_mask(self):              return self.stat.axis_mask
    def get_external_angular_units(self): return self.stat.angular_units or 1.0
    def get_external_length_units(self):  return self.stat.linear_units or 1.0
    def get_tool(self, pocket):
        try:
            t = self.stat.tool_table[pocket]
            return (t.id, t.xoffset, t.yoffset, t.zoffset, t.aoffset,
                    t.boffset, t.coffset, t.uoffset, t.voffset, t.woffset,
                    t.diameter, t.frontangle, t.backangle, t.orientation)
        except Exception:
            return (-1,) + (0.0,) * 12 + (0,)

    # ---- state callbacks ----
    def next_line(self, st):
        # sequence_number is 0-based; motion_line is 1-based. If the gray
        # boundary consistently leads/lags one block, drop the +1.
        self.cur_seq = getattr(st, "sequence_number", 0) + 1

    def set_plane(self, plane):
        self.plane = plane

    def set_g5x_offset(self, index, x, y, z, *abc_uvw):
        self.g5x = (x, y, z)

    def set_g92_offset(self, x, y, z, *abc_uvw):
        self.g92 = (x, y, z)

    def set_xy_rotation(self, rot):
        self.rot_xy = math.radians(rot)

    def change_tool(self, pocket):
        info = self.get_tool(pocket)
        self.cur_tool = info[0] if info[0] > 0 else pocket

    def tool_offset(self, xo, yo, zo, *rest):
        """Canon callback fired on G43/G49: the interp's coordinate
        convention shifts by the offset delta, so adjust lo to match
        (glcanon's formula) — otherwise the first move after G43 draws a
        phantom tool-length-sized segment. Values arrive in the inch frame."""
        x, y, z = self.lo
        ox, oy, oz = self.to
        self.lo = (x - xo + ox, y - yo + oy, z - zo + oz)
        self.to = (xo, yo, zo)

    # ---- coordinate translation: program -> machine ----
    def _xform(self, x, y, z):
        if self.rot_xy:
            c, s = math.cos(self.rot_xy), math.sin(self.rot_xy)
            x, y = x * c - y * s, x * s + y * c
        return (x + self.g5x[0] + self.g92[0],
                y + self.g5x[1] + self.g92[1],
                z + self.g5x[2] + self.g92[2])

    # ---- motion callbacks ----
    def _emit(self, pt, rapid):
        """lo/pt are in the offset-applied INCH frame; segments are stored
        in MACHINE units (scaled by k) so the live marker matches."""
        k = self.k
        self.segments.append((self.lo[0] * k, self.lo[1] * k, self.lo[2] * k,
                              pt[0] * k, pt[1] * k, pt[2] * k,
                              self.cur_tool, rapid, self.cur_seq))
        self.lo = pt

    def straight_traverse(self, x, y, z, a, b, c, u, v, w):
        self._emit(self._xform(x, y, z), True)

    def straight_feed(self, x, y, z, a, b, c, u, v, w):
        self._emit(self._xform(x, y, z), False)

    straight_probe = straight_feed

    def rigid_tap(self, x, y, z, *rest):
        # down-stroke only; a tapped hole plots fine as a single plunge line
        self._emit(self._xform(x, y, z), False)

    def arc_feed(self, e1, e2, cc1, cc2, rot, e3, a, b, c, u, v, w):
        # plane mapping — the canonical ARC_FEED axis order is CYCLIC:
        # G17 XY -> (X, Y, helix Z); G18 ZX -> (Z, X, helix Y);
        # G19 YZ -> (Y, Z, helix X).  Note G18 sends Z FIRST.
        if self.plane == 1:   ax1, ax2, ax3 = 0, 1, 2   # G17
        elif self.plane == 3: ax1, ax2, ax3 = 2, 0, 1   # G18 (Z, X)
        else:                 ax1, ax2, ax3 = 1, 2, 0   # G19

        # self.lo is machine coords; arc params are program coords.
        # Work in program coords for the arc, transform each emitted point.
        inv = self._inverse(self.lo)
        p1, p2 = inv[ax1], inv[ax2]

        theta0 = math.atan2(p2 - cc2, p1 - cc1)
        theta1 = math.atan2(e2 - cc2, e1 - cc1)
        r = math.hypot(p1 - cc1, p2 - cc2)

        if rot > 0:                              # counterclockwise
            if theta1 <= theta0: theta1 += 2 * math.pi
            theta1 += (rot - 1) * 2 * math.pi
        else:                                    # clockwise
            if theta1 >= theta0: theta1 -= 2 * math.pi
            theta1 -= (abs(rot) - 1) * 2 * math.pi

        span = theta1 - theta0
        n = max(2, int(abs(span) / math.radians(self.ARC_CHORD_DEG)))
        h0 = inv[ax3]

        for i in range(1, n + 1):
            t = i / n
            th = theta0 + span * t
            pt = [0.0, 0.0, 0.0]
            pt[ax1] = cc1 + r * math.cos(th)
            pt[ax2] = cc2 + r * math.sin(th)
            pt[ax3] = h0 + (e3 - h0) * t
            self._emit(self._xform(*pt), False)

    def _inverse(self, m):
        x = m[0] - self.g5x[0] - self.g92[0]
        y = m[1] - self.g5x[1] - self.g92[1]
        z = m[2] - self.g5x[2] - self.g92[2]
        if self.rot_xy:
            c, s = math.cos(-self.rot_xy), math.sin(-self.rot_xy)
            x, y = x * c - y * s, x * s + y * c
        return (x, y, z)


# ---------------------------------------------------------------------------
# The plot engine
# ---------------------------------------------------------------------------
class Backplot2D:
    def __init__(self, renderer, w, h):
        self.renderer = renderer
        self.W, self.H = w, h
        self.tex = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_RGBA8888,
                                     SDL_TEXTUREACCESS_TARGET, w, h)
        self.segments = []
        self.view = "XY"
        self.scale = 1.0
        self.pan_x = 0            # pixel pan offsets
        self.pan_y = 0
        self._center = (0.0, 0.0) # projected u,v center of extents
        self._progress = 0        # count of segments already grayed
        self._last_line = 0
        self.last_error = None
        self._parsed_file = None
        # background parse state. SDL work (fit/rebake) stays on the main
        # thread; the worker only builds the segment list.
        self.loading = False
        self._loading_file = None
        self._gen = 0             # bumped per load/invalidate; stale -> abort
        self._thread = None
        self._results = queue.Queue()
        self._canon = None        # in-flight canon, for loading_count
        # (ini_filename, live_var_path, startup_code), read on the main
        # thread in load(); the worker never opens the ini itself
        self._ini_cache = None

    # ------------------------------------------------------------------ parse
    SNAP_FIELDS = ("file", "ini_filename", "linear_units", "angular_units",
                   "axis_mask", "tool_table", "g5x_offset", "g92_offset",
                   "rotation_xy", "program_units")

    def load(self, stat):
        """Start parsing stat.file in the background. Call on file change;
        poll() picks up the result."""
        path = stat.file
        if not path:
            return
        # the main loop keeps polling live stat, so the worker gets a copy
        snap = types.SimpleNamespace(
            **{f: getattr(stat, f) for f in self.SNAP_FIELDS})
        _, snap.parameter_file, snap.startup_code = self._ini_values(
            snap.ini_filename)
        self._gen += 1
        if self._thread is not None:
            # gcode.parse is not reentrant; the bumped gen makes the old
            # parse abort at its next check_abort, so this is short
            self._thread.join()
        self.loading = True
        self._loading_file = path
        self._thread = threading.Thread(target=self._parse_worker,
                                        args=(snap, self._gen), daemon=True)
        self._thread.start()

    def _ini_values(self, ini_filename):
        """Main thread: PARAMETER_FILE (absolute) and RS274NGC_STARTUP_CODE,
        cached per ini_filename."""
        c = self._ini_cache
        if c is None or c[0] != ini_filename:
            ini = linuxcnc.ini(ini_filename)
            var = ini.find("RS274NGC", "PARAMETER_FILE") or "linuxcnc.var"
            var = os.path.join(os.path.dirname(ini_filename), var)
            startup = ini.find("RS274NGC", "RS274NGC_STARTUP_CODE") or ""
            c = self._ini_cache = (ini_filename, var, startup)
        return c

    def invalidate(self):
        """Force a re-parse (program reloaded); cancels any in-flight parse."""
        self._gen += 1
        self._parsed_file = None
        self._loading_file = None
        self.loading = False

    @property
    def loading_count(self):
        """Segments collected so far by the in-flight parse."""
        c = self._canon
        return len(c.segments) if c is not None else 0

    def poll(self):
        """Main thread, once per frame: install a finished parse."""
        while True:
            try:
                gen, path, segments, error = self._results.get_nowait()
            except queue.Empty:
                return
            if gen != self._gen:
                continue                       # superseded — drop it
            self.segments = segments
            self.last_error = error
            self._parsed_file = path
            self._loading_file = None
            self.loading = False
            self._progress = 0
            self._last_line = 0
            self.fit()
            self.rebake()

    def _parse_worker(self, stat, gen):
        """Worker thread: gcode.parse into a segment list. No SDL here.
        Reads only the `stat` snapshot built by load() — no live stat, no
        ini, no os.environ writes. INI_FILE_NAME (read by the preview
        interpreter) is set at import time in interface_test.py, right
        after `import settings`, before any worker can start."""
        path = stat.file
        error = None

        # scratch copy of the parameter file so parse can't clobber live params
        live_var = stat.parameter_file
        tmp_var = tempfile.NamedTemporaryFile(suffix=".var", delete=False)
        tmp_var.close()
        try:
            shutil.copy(live_var, tmp_var.name)
        except OSError:
            pass

        canon = PlotCanon(stat, tmp_var.name,
                          cancelled=lambda: gen != self._gen)
        self._canon = canon
        # Seed the starting coordinate frame from LIVE stat, not the disk var:
        # the var file is flushed lazily, so offsets/rotation set via MDI can
        # be stale on disk while active on the machine. Stat offsets are
        # MACHINE units; the canon's internal frame is inches -> divide by k.
        canon.g5x = tuple(v / canon.k for v in stat.g5x_offset[:3])
        canon.g92 = tuple(v / canon.k for v in stat.g92_offset[:3])
        canon.rot_xy = math.radians(stat.rotation_xy)
        # print(f"backplot parse frame: g5x={canon.g5x} g92={canon.g92} "
        #       f"rot_xy={stat.rotation_xy} lin_units={stat.linear_units} "
        #       f"prog_units={stat.program_units} k={canon.k:.4f}")

        unitcode = "G%d" % (20 + (stat.linear_units == 1))     # mm machine -> G21
        initcode = stat.startup_code

        try:
            result, seq = gcode.parse(path, canon, unitcode, initcode)
            if result > gcode.MIN_ERROR:
                error = gcode.strerror(result)
        except Exception as e:
            error = str(e)
        finally:
            os.unlink(tmp_var.name)
            if self._canon is canon:
                self._canon = None

        self._results.put((gen, path, canon.segments, error))

    def needs_parse(self, stat):
        return (stat.file and stat.file != self._parsed_file
                and stat.file != self._loading_file)

    # ------------------------------------------------------------------ views
    def _project(self, x, y, z):
        (ux, uy, uz), (vx, vy, vz) = VIEWS[self.view]
        return (x * ux + y * uy + z * uz, x * vx + y * vy + z * vz)

    def _to_px(self, u, v):
        cx, cy = self._center
        px = self.W / 2 + (u - cx) * self.scale + self.pan_x
        py = self.H / 2 - (v - cy) * self.scale + self.pan_y   # v-up -> y-down
        return int(px), int(py)

    def fit(self):
        """Scale/center so the whole path fills the view with margin."""
        self.pan_x = self.pan_y = 0
        if not self.segments:
            self.scale = 1.0
            self._center = (0.0, 0.0)
            return
        us, vs = [], []
        for s in self.segments:
            for (x, y, z) in ((s[0], s[1], s[2]), (s[3], s[4], s[5])):
                u, v = self._project(x, y, z)
                us.append(u); vs.append(v)
        du = max(us) - min(us) or 1e-9
        dv = max(vs) - min(vs) or 1e-9
        self.scale = 0.9 * min(self.W / du, self.H / dv)
        self._center = ((max(us) + min(us)) / 2, (max(vs) + min(vs)) / 2)

    def set_view(self, name):
        self.view = name
        self.fit()
        self.rebake()

    def zoom(self, factor):
        self.scale *= factor
        self.rebake()

    def pan(self, dx, dy):
        self.pan_x += dx
        self.pan_y += dy
        self.rebake()

    # ------------------------------------------------------------------ bake
    def _seg_color(self, seg, executed):
        rapid = seg[7]
        if executed:
            return GRAY_RAPID if rapid else GRAY_FEED
        if rapid:
            return RAPID_COLOR
        return TOOL_PALETTE[seg[6] % len(TOOL_PALETTE)]

    def _draw_seg(self, seg, executed):
        r, g, b = self._seg_color(seg, executed)
        SDL_SetRenderDrawColor(self.renderer, r, g, b, 255)
        x0, y0 = self._to_px(*self._project(seg[0], seg[1], seg[2]))
        x1, y1 = self._to_px(*self._project(seg[3], seg[4], seg[5]))
        SDL_RenderDrawLine(self.renderer, x0, y0, x1, y1)

    def rebake(self):
        """Full redraw of the static plot into the texture.
        Segments up to _progress stay gray (survives view/zoom changes)."""
        SDL_SetRenderTarget(self.renderer, self.tex)
        SDL_SetRenderDrawColor(self.renderer, *BG_COLOR, 255)
        SDL_RenderClear(self.renderer)
        for i, seg in enumerate(self.segments):
            self._draw_seg(seg, executed=(i < self._progress))
        SDL_SetRenderTarget(self.renderer, None)

    def reset_trace(self):
        """REDRAW softkey: clear the gray-out."""
        self._progress = 0
        self._last_line = 0
        self.rebake()

    # ------------------------------------------------------------------ frame
    def update(self, stat):
        """Advance the gray-out incrementally. Cheap: 0-5 lines/frame typical."""
        cur = stat.motion_line or stat.current_line
        if cur < self._last_line:          # program restarted / rewound
            self.reset_trace()
        self._last_line = cur
        if cur <= 0 or self._progress >= len(self.segments):
            return

        first = self._progress
        while (self._progress < len(self.segments)
               and self.segments[self._progress][8] < cur):
            self._progress += 1
        # NOTE: seq is non-monotonic through O-word loops; the pointer stalls
        # there and loops gray out on their final pass. Acceptable.

        if self._progress > first:
            SDL_SetRenderTarget(self.renderer, self.tex)
            for i in range(first, self._progress):
                self._draw_seg(self.segments[i], executed=True)
            SDL_SetRenderTarget(self.renderer, None)

    def draw(self, dst_x, dst_y, stat):
        """Blit plot + live marker + tool-axis vector + UCS icon."""
        SDL_RenderCopy(self.renderer, self.tex, None,
                       SDL_Rect(dst_x, dst_y, self.W, self.H))

        # --- live marker: crosshair at the TOOL TIP.
        # actual_position includes the tool offset; the plotted path does not
        # (it's program coords + g5x + g92), so subtract tool_offset to land
        # the marker on the path.
        p = stat.actual_position
        t = stat.tool_offset
        tip = (p[0] - t[0], p[1] - t[1], p[2] - t[2])
        mx, my = self._to_px(*self._project(*tip))
        mx += dst_x; my += dst_y
        r, g, b = MARKER_COLOR
        SDL_SetRenderDrawColor(self.renderer, r, g, b, 255)

        inside = (dst_x + 14 <= mx <= dst_x + self.W - 14
                  and dst_y + 14 <= my <= dst_y + self.H - 14)
        if not inside:
            # tool is outside the fitted view: clamp to the border and draw
            # an arrow pointing toward the actual position
            cx = min(max(mx, dst_x + 24), dst_x + self.W - 24)
            cy = min(max(my, dst_y + 24), dst_y + self.H - 24)
            dx, dy = mx - cx, my - cy
            d = math.hypot(dx, dy) or 1.0
            ux, uy = dx / d, dy / d
            ex, ey = cx + int(ux * 18), cy + int(uy * 18)
            SDL_RenderDrawLine(self.renderer, cx, cy, ex, ey)
            ang = math.atan2(ey - cy, ex - cx)
            for da in (2.6, -2.6):
                hx = ex + int(10 * math.cos(ang + da))
                hy = ey + int(10 * math.sin(ang + da))
                SDL_RenderDrawLine(self.renderer, ex, ey, hx, hy)
        else:
            SDL_RenderDrawLine(self.renderer, mx - 12, my, mx + 12, my)
            SDL_RenderDrawLine(self.renderer, mx, my - 12, mx, my + 12)

            # --- tool orientation vector: the spindle axis relative to the
            # part, back-rotated through A (tilt about X) and C (about Z).
            # v = (sinC*sinA, cosC*sinA, cosA); A=C=0 -> straight +Z.
            # If your trunnion reads mirrored, flip the sign of `a` or `c`.
            a = math.radians(p[3])
            c = math.radians(p[5])
            v = (math.sin(c) * math.sin(a),
                 math.cos(c) * math.sin(a),
                 math.cos(a))
            u, vv = self._project(*v)      # direction only — no pan/scale
            L = 70                         # px at full length
            ex, ey = mx + int(u * L), my - int(vv * L)
            SDL_SetRenderDrawColor(self.renderer, *TOOL_VECTOR, 255)
            SDL_RenderDrawLine(self.renderer, mx, my, ex, ey)
            if u * u + vv * vv < 0.01:
                # axis points at the viewer: small square instead of a dot
                SDL_RenderDrawRect(self.renderer,
                                   SDL_Rect(mx - 5, my - 5, 10, 10))

        # --- UCS icon, bottom-left corner ---
        self._draw_ucs(dst_x + 70, dst_y + self.H - 70)

    def _draw_ucs(self, ox, oy):
        L = 50
        self._ucs_labels = {}            # rebuilt fresh: no stale letters from
                                         # axes skipped in the current view
        for name, vec in (("X", (1, 0, 0)), ("Y", (0, 1, 0)), ("Z", (0, 0, 1))):
            u, v = self._project(*vec)
            if abs(u) < 1e-6 and abs(v) < 1e-6:
                continue                        # axis points into the screen
            ex, ey = ox + int(u * L), oy - int(v * L)
            r, g, b = AXIS_COLORS[name]
            SDL_SetRenderDrawColor(self.renderer, r, g, b, 255)
            SDL_RenderDrawLine(self.renderer, ox, oy, ex, ey)
            # arrowhead: two short back-strokes
            ang = math.atan2(oy - ey, ex - ox)
            for da in (2.6, -2.6):
                hx = ex + int(9 * math.cos(ang + da))
                hy = ey - int(9 * math.sin(ang + da))
                SDL_RenderDrawLine(self.renderer, ex, ey, hx, hy)
            self._ucs_labels[name] = (ex, ey)   # screen reads these to label

    def label_positions(self):
        """{axis: (x, y)} of arrow tips, for the screen to draw letters at."""
        return getattr(self, "_ucs_labels", {})