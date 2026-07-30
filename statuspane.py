#!/usr/bin/env python3
# statuspane.py — corner status pane per hand sketch.

import time
import linuxcnc
from sdl2 import *
from screens import draw_line, AXIS_IDX, PANE_W

try:
    import hal
    _HAL_OK = True
except ImportError:
    _HAL_OK = False

GREEN  = SDL_Color(40, 200, 120)
ORANGE = SDL_Color(255, 150, 40)
DIM    = SDL_Color(150, 150, 150)

# HAL pins for load bars; wire these when the EtherCAT torque PDOs exist.
LOAD_PINS = {
    "X": "status-pane.load-x", "Y": "status-pane.load-y",
    "Z": "status-pane.load-z", "A": "status-pane.load-a",
    "C": "status-pane.load-c", "S": "status-pane.load-s",
}


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
        self.run_mark = time.monotonic()
        self._was_running = False

        # optional HAL load component
        self.hal = None
        if _HAL_OK:
            try:
                self.hal = hal.component("status-pane")
                for name in LOAD_PINS.values():
                    self.hal.newpin(name.split(".", 1)[1],
                                    hal.HAL_FLOAT, hal.HAL_IN)
                self.hal.ready()
            except Exception as e:
                print("status-pane HAL init failed:", e)
                self.hal = None

    # ------------------------------------------------------------- persist
    def _save(self):
        self.app.persist["parts"] = self.parts
        self.app.persist["run_seconds"] = self.run_total
        self.app.save_persist()

    def reset_parts(self):
        self.parts = 0
        self._save()

    # ------------------------------------------------------------- update
    def update(self):
        """Call once per frame from the main loop (before draw)."""
        st = self.app.stat
        now = time.monotonic()

        # accumulate power-on/run meter (persist every ~60s)
        self.run_total += now - self.run_mark
        self.run_mark = now
        if int(self.run_total) % 60 == 0:
            self._save()

        running = (st.task_mode == linuxcnc.MODE_AUTO
                   and st.interp_state != linuxcnc.INTERP_IDLE)
        paused = bool(st.task_paused)

        if running and not self._was_running:          # cycle start
            self.cycle_start = now
            self.cycle_accum = 0.0
        if running and paused and self.cycle_start is not None:
            self.cycle_accum += now - self.cycle_start  # bank time, stop clock
            self.cycle_start = None
        if running and not paused and self.cycle_start is None:
            self.cycle_start = now                      # resume
        if not running and self._was_running:           # cycle end
            if self.cycle_start is not None:
                self.cycle_accum += now - self.cycle_start
                self.cycle_start = None
            self.parts += 1                             # M2/M30 completion
            self._save()
        self._was_running = running

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

    # ------------------------------------------------------------- draw
    def draw(self, renderer, x0, y0, h, w, variant):
        """variant: 'LOADS' or 'POSMODE'."""
        app, st, f = self.app, self.app.stat, self.app.font
        # frame
        SDL_SetRenderDrawColor(renderer, 0, 0, 255, 255)
        SDL_RenderDrawLine(renderer, x0, y0, x0, y0 + h)
        SDL_RenderDrawLine(renderer, x0, y0, x0 + w, y0)

        x = x0 + 14
        y = y0 + 8

        draw_line(renderer, f, f"CYCLE {_fmt_hms(self.cycle_seconds())}", x, y)
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
            SDL_SetRenderDrawColor(renderer, 130, 60, 60, 255)
            SDL_RenderDrawLine(renderer, bar_x, y + self.BAR_H // 2,
                               bar_x + bar_w, y + self.BAR_H // 2)
            for tx in (bar_x + bar_w // 2, bar_x + bar_w):
                SDL_RenderDrawLine(renderer, tx, y, tx, y + self.BAR_H)
            val = self._load(axis)
            if val is not None and val > 0:
                fill = int(min(val, 200.0) / 200.0 * bar_w)
                over = val > 100.0
                SDL_SetRenderDrawColor(renderer,
                                       *(220, 60, 60) if over else (40, 200, 120),
                                       255)
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