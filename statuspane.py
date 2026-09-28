#!/usr/bin/env python3
# statuspane.py — corner status pane per hand sketch.
#
# HAL component "status-pane" (optional, created when the hal module loads):
#   load-x/y/z/a/c/s   FLOAT IN  load bars (EtherCAT torque, % of rated)
#   home-x/y/z/a/c     BIT IN    POS-screen reference-position indicators
#   part-done          BIT IN    exact PARTS count: each rising edge counts
#                                one part; see PART_DONE_PIN below

import os
import re
import subprocess
import time
import linuxcnc
from sdl2 import *
import settings
from screens import draw_line, AXIS_IDX, PANE_W

try:
    import hal
    _HAL_OK = True
except ImportError:
    _HAL_OK = False

GREEN     = settings.sdl_color("OK", (40, 200, 120))
ORANGE    = settings.sdl_color("ACCENT", (255, 150, 40))
DIM       = settings.sdl_color("DIM", (150, 150, 150))
FRAME     = settings.color("FRAME", (0, 0, 255))
LOAD_RAIL = settings.color("LOAD_RAIL", (130, 60, 60))
LOAD_OK   = settings.color("LOAD_OK", (40, 200, 120))
LOAD_OVER = settings.color("LOAD_OVER", (220, 60, 60))

# HAL pins for load bars; wire these when the EtherCAT torque PDOs exist.
LOAD_PINS = {
    "X": "status-pane.load-x", "Y": "status-pane.load-y",
    "Z": "status-pane.load-z", "A": "status-pane.load-a",
    "C": "status-pane.load-c", "S": "status-pane.load-s",
}

# HAL pins for the POS-screen reference-position (home) indicators.
# Wire them in postgui.hal to the same signals that drive the pendant home
# lamps (pnl-home-lamp-*), so screen and panel can never disagree: lit only
# when the axis is homed AND parked within [PANEL]HOME_WINDOW of its
# [JOINT_N]HOME — Fanuc ZRN semantics. Jog away and the indicator clears.
HOME_PINS = {
    "X": "status-pane.home-x", "Y": "status-pane.home-y",
    "Z": "status-pane.home-z", "A": "status-pane.home-a",
    "C": "status-pane.home-c",
}

# Optional exact part count. Wire status-pane.part-done in postgui.hal to a
# signal pulsed by M30 or a user M-code (e.g. a motion.digital-out-NN set by
# M64/M65 in the program end). Each rising edge counts one part, and from
# the first edge on the interpreter-based count below is switched off.
# Without it the pane counts AUTO cycles that end without RESET or an
# NML error (App.aborted_since_start).
PART_DONE_PIN = "status-pane.part-done"

SAVE_PERIOD = 60.0             # s between counter saves while running


def _fmt_hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec // 60) % 60:02d}:{sec % 60:02d}"


class StatusPane:
    ROW = 44                       # compact line pitch; pane font is app.font
    BAR_H = 18

    def __init__(self, app):
        self.app = app
        # persistent counters live in app.persist (shared file, one owner)
        self.parts = app.persist.get("parts", 0)
        self.run_total = app.persist.get("run_seconds", 0.0)

        # cycle timing state
        self.cycle_start = None
        self.cycle_accum = 0.0     # survives pauses
        self.last_cycle = app.persist.get("last_cycle_seconds", 0.0)
        self.run_mark = time.monotonic()
        self._last_save = self.run_mark
        self._was_running = False
        # part-done HAL input: edge detect; once seen, HAL owns the count
        self._part_done_prev = False
        self.hal_counts = False

        # optional HAL component: load bars + home indicators
        self.hal = None
        if _HAL_OK:
            try:
                self.hal = hal.component("status-pane")
                for name in LOAD_PINS.values():
                    self.hal.newpin(name.split(".", 1)[1],
                                    hal.HAL_FLOAT, hal.HAL_IN)
                for name in HOME_PINS.values():
                    self.hal.newpin(name.split(".", 1)[1],
                                    hal.HAL_BIT, hal.HAL_IN)
                self.hal.newpin(PART_DONE_PIN.split(".", 1)[1],
                                hal.HAL_BIT, hal.HAL_IN)
                self.hal.ready()
                self._run_postgui()
            except Exception as e:
                print("status-pane HAL init failed:", e)
                self.hal = None

    def _run_postgui(self):
        """LinuxCNC only auto-runs POSTGUI_HALFILE for the stock GUIs, so a
        custom DISPLAY has to do it once its pins exist. linuxcnc chdirs into
        the config dir, so a bare filename resolves. Missing file = nothing to
        wire, which is not an error."""
        path = os.path.join(os.getcwd(), "postgui.hal")
        if not os.path.exists(path):
            return
        try:
            subprocess.run(["halcmd", "-f", path], check=True)
        except Exception as e:
            print("postgui.hal failed:", e)

    # ------------------------------------------------------------- persist
    def _save(self):
        self.app.persist["parts"] = self.parts
        self.app.persist["run_seconds"] = self.run_total
        self.app.persist["last_cycle_seconds"] = self.last_cycle
        self.app.save_persist()
        self._last_save = time.monotonic()

    def flush(self):
        """Final save on a normal quit so the last minute of RUN time (and
        the last cycle) is not lost. Called once by main() after the loop."""
        now = time.monotonic()
        self.run_total += now - self.run_mark
        self.run_mark = now
        self._save()

    def _part_done(self):
        """Rising edge on status-pane.part-done (False without HAL)."""
        if self.hal is None:
            return False
        try:
            val = bool(self.hal[PART_DONE_PIN.split(".", 1)[1]])
        except Exception:
            return False
        edge = val and not self._part_done_prev
        self._part_done_prev = val
        return edge

    def reset_parts(self):
        self.parts = 0
        self._save()

    # ------------------------------------------------------------- update
    def update(self):
        """Call once per frame from the main loop (before draw)."""
        st = self.app.stat
        now = time.monotonic()

        # accumulate power-on/run meter (persist every SAVE_PERIOD)
        self.run_total += now - self.run_mark
        self.run_mark = now
        if now - self._last_save >= SAVE_PERIOD:
            self._save()

        # exact count from HAL; the first pulse hands counting to HAL
        if self._part_done():
            self.hal_counts = True
            self.parts += 1
            self._save()

        running = (st.task_mode == linuxcnc.MODE_AUTO
                   and st.interp_state != linuxcnc.INTERP_IDLE)
        paused = bool(st.task_paused)

        if running and not self._was_running:          # cycle start
            self.cycle_start = now
            self.cycle_accum = 0.0
            self.app.aborted_since_start = False
        if running and paused and self.cycle_start is not None:
            self.cycle_accum += now - self.cycle_start  # bank time, stop clock
            self.cycle_start = None
        if running and not paused and self.cycle_start is None:
            self.cycle_start = now                      # resume
        if not running and self._was_running:           # cycle end
            if self.cycle_start is not None:
                self.cycle_accum += now - self.cycle_start
                self.cycle_start = None
            self.last_cycle = self.cycle_accum          # hold for the next run
            # A part is an M2/M30 completion. Measured on a sim: after M30
            # stat.command still holds the last block ("M30"); after an
            # abort or E-stop from ANY source (GUI RESET, halui.abort, a
            # panel E-stop) task clears it to "". aborted_since_start
            # (GUI RESET / NML error) is kept as a second guard. HAL
            # part-done overrides this count.
            ended_ok = bool(self._END_RE.search(st.command or ""))
            if (not self.hal_counts and ended_ok
                    and not self.app.aborted_since_start):
                self.parts += 1
            self._save()
        self._was_running = running

    _END_RE = re.compile(r"(?<![A-Za-z0-9.])[Mm]0*(2|30)(?![0-9.])")

    def cycle_seconds(self):
        t = self.cycle_accum
        if self.cycle_start is not None:
            t += time.monotonic() - self.cycle_start
        return t

    def _load(self, axis):
        if self.hal is None:
            return None
        try:
            return self.hal[LOAD_PINS[axis].split(".", 1)[1]]
        except Exception:
            return None

    def at_home(self, axis):
        """Reference-position state for the POS-screen indicators.
        True/False from the HAL pin; None when the axis has no pin.
        With HAL present the pin is the only truth — an unwired pin reads
        False, which shows 'not referenced' rather than a comforting lie.
        The stat.homed fallback is for dev runs outside LinuxCNC and means
        'homed', not 'parked at home'; it assumes trivkins (joint order ==
        app.axes order)."""
        if self.hal is not None:
            if axis not in HOME_PINS:
                return None
            try:
                return bool(self.hal[HOME_PINS[axis].split(".", 1)[1]])
            except Exception:
                return None
        try:
            joint = self.app.axes.index(axis)
            return bool(self.app.stat.homed[joint])
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------- draw
    def draw(self, renderer, x0, y0, h, w, variant):
        """variant: 'LOADS' or 'POSMODE'."""
        app, st, f = self.app, self.app.stat, self.app.font
        # frame
        SDL_SetRenderDrawColor(renderer, *FRAME, 255)
        SDL_RenderDrawLine(renderer, x0, y0, x0, y0 + h)
        SDL_RenderDrawLine(renderer, x0, y0, x0 + w, y0)

        x = x0 + 14
        y = y0 + 8

        draw_line(renderer, f, f"CYCLE {_fmt_hms(self.cycle_seconds())}", x, y)
        y += self.ROW
        draw_line(renderer, f, f"LAST  {_fmt_hms(self.last_cycle)}", x, y, DIM)
        y += self.ROW
        draw_line(renderer, f, f"RUN   {_fmt_hms(self.run_total)}", x, y)
        y += self.ROW
        draw_line(renderer, f, f"PARTS {self.parts:04d}", x, y)
        y += self.ROW + 10

        if variant == "LOADS" and not app.always_show_position:
            y = self._draw_loads(renderer, x, y)
        else:
            y = self._draw_posmode(renderer, x, y)

        # S/F commanded (orange, like the sketch) + actual.
        # Feeds are linear -> unit-convert for display. S is RPM, untouched.
        uf = app.unit_factor()
        machine_mm = st.linear_units == 1.0
        interp_mm = st.program_units == 2
        # commanded F is in interp units: -> machine -> display
        f_cmd_m = st.settings[1] * (1.0 if interp_mm == machine_mm
                                    else (25.4 if machine_mm else 1 / 25.4))
        f_cmd = f_cmd_m * uf
        s_cmd = st.settings[2]
        s_act = st.spindle[0]["speed"]
        f_act = st.current_vel * 60.0 * uf       # machine units/s -> disp/min
        draw_line(renderer, f, f"S{s_cmd:5.0f}  F{f_cmd:6.1f}", x, y, ORANGE)
        y += self.ROW
        draw_line(renderer, f, f"S{s_act:5.0f}  F{f_act:6.1f}  ACT", x, y, DIM)
        y += self.ROW + 6

        # overrides
        so = st.spindle[0]["override"] * 100
        fo = st.feedrate * 100
        ro = st.rapidrate * 100
        draw_line(renderer, f, f"S{so:3.0f}% F{fo:3.0f}% R{ro:3.0f}%", x, y)

    def _draw_loads(self, renderer, x, y):
        f = self.app.font
        bar_x = x + 56
        bar_w = PANE_W - 140
        # scale header: 0 / 100 / 200
        draw_line(renderer, f, "0", bar_x - 8, y)
        draw_line(renderer, f, "100", bar_x + bar_w // 2 - 20, y)
        draw_line(renderer, f, "200", bar_x + bar_w - 28, y)
        y += self.ROW

        for axis in tuple(self.app.axes) + ("S",):
            draw_line(renderer, f, axis, x, y - 6)
            # rail + 100% tick + end tick
            SDL_SetRenderDrawColor(renderer, *LOAD_RAIL, 255)
            SDL_RenderDrawLine(renderer, bar_x, y + self.BAR_H // 2,
                               bar_x + bar_w, y + self.BAR_H // 2)
            for tx in (bar_x + bar_w // 2, bar_x + bar_w):
                SDL_RenderDrawLine(renderer, tx, y, tx, y + self.BAR_H)
            val = self._load(axis)
            if val is not None and val > 0:
                fill = int(min(val, 200.0) / 200.0 * bar_w)
                over = val > 100.0
                SDL_SetRenderDrawColor(renderer,
                                       *(LOAD_OVER if over else LOAD_OK), 255)
                SDL_RenderFillRect(renderer,
                                   SDL_Rect(bar_x, y, fill, self.BAR_H))
            y += self.ROW
        return y + 8

    def _draw_posmode(self, renderer, x, y):
        st, f = self.app.stat, self.app.font
        for ax in self.app.axes:
            idx = AXIS_IDX[ax]
            work = (st.actual_position[idx] - st.g5x_offset[idx]
                    - st.g92_offset[idx] - st.tool_offset[idx])
            draw_line(renderer, f,
                      "{} {}".format(ax, self.app.fmt_axis(ax, work)), x, y)
            y += self.ROW
        y += 8

        # modal G-codes (x10-encoded) + M-codes (raw), one combined list
        codes = []
        for v in st.gcodes[1:]:
            if v == -1:
                continue
            codes.append(f"G{v // 10}" + (f".{v % 10}" if v % 10 else ""))
        for v in st.mcodes[1:]:
            if v == -1:
                continue
            codes.append(f"M{v}")

        for j in range(0, len(codes), 5):
            draw_line(renderer, f, " ".join(codes[j:j + 5]), x, y, DIM)
            y += self.ROW - 6
        return y + 8