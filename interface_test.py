#!/usr/bin/env python3
# interface_test.py

import os
import re
import sys
import json
import ctypes
from sdl2 import *
from sdl2.sdlttf import *
from collections import deque
from datetime import datetime
from statuspane import StatusPane

import linuxcnc
import settings

from screens import (render_text, blit_text, draw_line, text_width,
                     InputBuffer, FieldCursor,
                     PosScreen, ProgScreen, OffsetScreen,
                     MessageScreen, GraphicsScreen,
                     ScreenManager, WCS, AXIS_IDX, PANE_W)
from systemscreen import SystemScreen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WIN_W, WIN_H = 1920, 1080
FONT_PATH = settings.get_path("GUI", "FONT_PATH",
                              "/usr/share/fonts/truetype/dejavu/ISOCPEUR.TTF")
FONT_SIZE = settings.get_int("GUI", "FONT_SIZE", 48)
LARGE_FONT_SIZE = settings.get_int("GUI", "LARGE_FONT_SIZE", 180)

BACKGROUND = settings.color("BACKGROUND", (0, 0, 0))
EMG_ON     = settings.color("EMG_ON", (200, 0, 0))     # blink, bright phase
EMG_DIM    = settings.color("EMG_DIM", (80, 0, 0))     # blink, dim phase
ALARM      = settings.sdl_color("ALARM", (250, 0, 0))
# JOG_VEL = 30.0 / 60.0

MODES = ("None", "MANU", "AUTO", "MDI")

CAN_HOLD_MS = 1000     # hold CAN this long to wipe the whole input buffer

# persistent counters/settings (parts, run hours, display units).
# NOTE for the read-only-rootfs deployment: point this at the writable
# data partition.
PERSIST = os.path.join(os.path.dirname(__file__), "machine_counters.json")

# screen indices
POSITION = 0
PROG = 1
OFFSET = 2
SYSTEM = 3
MESSAGE = 4
GRAPHICS = 5

# page-select keys — remap freely
PAGE_KEYS = {
    SDL_SCANCODE_PAGEUP:    POSITION,
    SDL_SCANCODE_RSHIFT: PROG,
    SDL_SCANCODE_PAGEDOWN:    OFFSET,
    SDL_SCANCODE_LSHIFT:       SYSTEM,
    SDL_SCANCODE_CAPSLOCK:      MESSAGE,
    SDL_SCANCODE_TAB:    GRAPHICS,
}

# page keys that double as keyboard modifiers; ignored while the input
# buffer is in raw mode (SYSTEM editor pages)
RAW_MODIFIER_KEYS = {SDL_SCANCODE_LSHIFT, SDL_SCANCODE_RSHIFT,
                     SDL_SCANCODE_CAPSLOCK}

# softkeys: F1 = [<], F2..F11 = keys 1-10, F12 = [>]
SOFTKEYS = {
    SDL_SCANCODE_F1: 0,  SDL_SCANCODE_F2: 1,  SDL_SCANCODE_F3: 2,
    SDL_SCANCODE_F4: 3,  SDL_SCANCODE_F5: 4,  SDL_SCANCODE_F6: 5,
    SDL_SCANCODE_F7: 6,  SDL_SCANCODE_F8: 7,  SDL_SCANCODE_F9: 8,
    SDL_SCANCODE_F10: 9, SDL_SCANCODE_F11: 10, SDL_SCANCODE_F12: 11,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def program_number(path):
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r'\s*[Oo](\d+)', line)
                if m:
                    return int(m.group(1))
    except OSError:
        pass
    return None


def draw_status_box(renderer, font, x, y, w, h, text, active):
    if active:
        blink = (SDL_GetTicks() // 500) % 2          # flips every 500 ms
        SDL_SetRenderDrawColor(renderer, *(EMG_ON if blink else EMG_DIM), 255)
        SDL_RenderFillRect(renderer, SDL_Rect(x, y, w, h))
        draw_line(renderer, font, text, x + 8, y + 6)
    else:
        SDL_SetRenderDrawColor(renderer, *BACKGROUND, 255)
        SDL_RenderFillRect(renderer, SDL_Rect(x, y, w, h))


class NDisplay:
    _N_RE = re.compile(r'[Nn](\d+)')

    def __init__(self):
        self._path = None
        self._lines = []
        self._last_n = None

    def lines(self):
        return self._lines

    def invalidate(self):
        """Force re-read on next update (e.g. after on-control edit/save)."""
        self._path = None

    def reset(self):
        """RESET key: clear the sticky N (display shows N---- until next run)."""
        self._last_n = None

    def update(self, stat):
        if stat.file != self._path:
            self._path = stat.file
            self._last_n = None
            try:
                with open(stat.file) as f:
                    self._lines = f.readlines()
            except OSError:
                self._lines = []

        # Only track the executing block while a program is actually running.
        # When idle/aborted, motion_line/current_line can still report the
        # block where execution stopped (looks like a "random" N on RESET) —
        # so clear rather than cling to it.
        if stat.interp_state == linuxcnc.INTERP_IDLE:
            self._last_n = None
            return None

        line = stat.motion_line or stat.current_line
        if 0 < line <= len(self._lines):
            m = self._N_RE.search(self._lines[line - 1])
            if m:
                self._last_n = int(m.group(1))
        return self._last_n

    def text(self, stat):
        n = self.update(stat)
        return f"N{n:05d}" if n is not None else "N----"


class AlarmSystem:
    def __init__(self, active_max=20, history_max=200):
        self.active = deque(maxlen=active_max)
        self.history = deque(maxlen=history_max)
        self._chan = linuxcnc.error_channel()

    def poll(self):
        while True:
            err = self._chan.poll()
            if not err:
                break
            kind, text = err
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "text": (text or "").strip() or "(no message)",
                "alarm": kind in (linuxcnc.NML_ERROR, linuxcnc.OPERATOR_ERROR),
            }
            self.active.appendleft(entry)

    def reset(self):
        for entry in reversed(self.active):
            entry = dict(entry)
            entry["cleared"] = datetime.now().strftime("%H:%M:%S")
            self.history.append(entry)
        self.active.clear()

    def latest(self):
        return self.active[0] if self.active else None


# ---------------------------------------------------------------------------
# Shared state handed to every screen
#
# UNITS POLICY: all *stored* state (rel_origin, wcs_vals, wear, tool geom,
# backplot segments) is ALWAYS in machine units. Conversion happens only at
# the display/entry membrane via the helpers below.
# ---------------------------------------------------------------------------
class App:
    LINEAR_AXES = "XYZUVW"          # rotary axes (A/B/C) never unit-convert

    def __init__(self):
        self.stat = linuxcnc.stat()
        self.ndisp = NDisplay()
        self.prog_num = None
        self._last_file = None
        self.font = None
        self.large_font = None
        self.command = linuxcnc.command()
        self.input = InputBuffer()
        self.alarms = AlarmSystem()
        # True for the stepper machine
        self.always_show_position = settings.get_bool(
            "GUI", "ALWAYS_SHOW_POSITION", True)
        self.pane = None
        self.quit = False                   # set by SYSTEM -> EXIT -> EXEC                    # set after renderer exists
        self.axes = settings.get_str("GUI", "AXES", "XYZAC")   # "XYZ" for 3-axis
        self.rel_origin = [0.0] * 9         # machine units, 9-tuple indexed

        # persistent settings/counters
        try:
            with open(PERSIST) as f:
                self.persist = json.load(f)
        except (OSError, ValueError):
            self.persist = {}
        self.display_units = self.persist.get("display_units", "MACHINE")
        self._pending_program = self.persist.get("last_program")

        # --- PROG page flags ---
        # True: keep executed MDI blocks listed
        self.show_mdi_history = settings.get_bool("GUI", "SHOW_MDI_HISTORY", True)
        # auto-mounted panel USB ports (udev rule -> fixed paths):
        self.usb_mounts = settings.get_list("GUI", "USB_MOUNTS",
                                            ["/media/usb0", "/media/usb1"])
        # machine program directory from the ini
        self.stat.poll()
        try:
            ini = linuxcnc.ini(self.stat.ini_filename)
            self.prog_dir = os.path.expanduser(
                ini.find("DISPLAY", "PROGRAM_PREFIX") or "~/linuxcnc/nc_files")
        except linuxcnc.error:
            self.prog_dir = os.path.expanduser("~/linuxcnc/nc_files")

    def save_persist(self):
        self.persist["display_units"] = self.display_units
        try:
            with open(PERSIST, "w") as f:
                json.dump(self.persist, f)
        except OSError:
            pass

    def poll(self):
        self.stat.poll()
        self.alarms.poll()
        # file-change -> reparse O-number
        if self.stat.file != self._last_file:
            self._last_file = self.stat.file
            self.prog_num = program_number(self.stat.file)

    def mdi(self, code):
        self.command.mode(linuxcnc.MODE_MDI)
        self.command.wait_complete()
        self.command.mdi(code)
        self.command.wait_complete()

    def mdi_async(self, code):
        """Fire an MDI command and return immediately — does NOT wait for the
        command to finish executing. The mode switch is fast so we wait for
        that, but the command itself (e.g. a probe macro with motion) runs
        across many frames; callers watch stat.interp_state for completion so
        the UI loop keeps running. Do not send another command until the
        interp returns to INTERP_IDLE."""
        self.command.mode(linuxcnc.MODE_MDI)
        self.command.wait_complete()      # mode switch only — fast
        self.command.mdi(code)            # no wait_complete: let it run

    def reset(self):
        """RESET key: abort program/MDI execution (interp rewinds to
        program top), spindle off, coolant off, rewind the N display, and
        move active alarms to history. Explicit spindle/coolant-off after
        abort is belt-and-suspenders: task abort stops them for a running
        program, but this also covers a spindle started manually via MDI."""
        c = self.command
        c.abort()
        c.wait_complete()
        if not self.stat.estop and self.stat.task_state == linuxcnc.STATE_ON:
            c.spindle(linuxcnc.SPINDLE_OFF)
            c.mist(linuxcnc.MIST_OFF)
            c.flood(linuxcnc.FLOOD_OFF)
        self.ndisp.reset()
        self.alarms.reset()

    def reload_program(self, path):
        self.command.mode(linuxcnc.MODE_AUTO)
        self.command.wait_complete()
        self.command.program_open(path)
        self.command.wait_complete()
        self.ndisp.invalidate()
        if hasattr(self, "backplot"):
            self.backplot.invalidate()
        self.persist["last_program"] = path      # <-- remember it
        self.save_persist()

    # ------------------------------------------------------------- units
    def unit_factor(self):
        """Multiply machine-unit linear values by this for display."""
        du = self.display_units
        if du == "MM":      want_mm = True
        elif du == "INCH":  want_mm = False
        elif du == "PROGRAM":
            want_mm = (self.stat.program_units == 2)
        else:                                        # MACHINE
            return 1.0
        machine_mm = (self.stat.linear_units == 1.0)
        if machine_mm == want_mm:
            return 1.0
        return 1 / 25.4 if machine_mm else 25.4

    def unit_tag(self):
        f = self.unit_factor()
        machine_mm = (self.stat.linear_units == 1.0)
        shown_mm = machine_mm if f == 1.0 else not machine_mm
        return "MM" if shown_mm else "INCH"

    def machine_to_interp(self, v):
        """Machine-unit linear value -> value for a G10/MDI word, which the
        interpreter reads in its *current* (G20/G21) units."""
        machine_mm = self.stat.linear_units == 1.0
        interp_mm = self.stat.program_units == 2
        if machine_mm == interp_mm:
            return v
        return v / 25.4 if machine_mm else v * 25.4

    def disp_to_machine(self, ax, typed):
        """Typed display-unit value -> machine units (rotary passes through)."""
        return typed / self.unit_factor() if ax in self.LINEAR_AXES else typed

    def machine_to_disp(self, ax, v):
        return v * self.unit_factor() if ax in self.LINEAR_AXES else v

    def fmt_axis(self, ax, machine_val, width=9, prec=4):
        """Machine-unit value -> display string in current display units."""
        return "{: {w}.{p}f}".format(self.machine_to_disp(ax, machine_val),
                                     w=width, p=prec)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    SDL_Init(SDL_INIT_VIDEO)
    TTF_Init()

    window = SDL_CreateWindow(
        b"CNC Controller",
        SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
        WIN_W, WIN_H,
        SDL_WINDOW_SHOWN | SDL_WINDOW_FULLSCREEN_DESKTOP,
    )

    SDL_StartTextInput()

    SDL_ShowCursor(0)

    renderer = SDL_CreateRenderer(
        window, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_TARGETTEXTURE)
    font = TTF_OpenFont(FONT_PATH.encode(), FONT_SIZE)
    if not font:
        print("Could not open font:", FONT_PATH)
        return
    large_font = TTF_OpenFont(FONT_PATH.encode(), LARGE_FONT_SIZE)
    if not large_font:
        print("Could not open font:", FONT_PATH)
        return

    app = App()
    app.font = font
    app.large_font = large_font
    app.renderer = renderer        # GraphicsScreen needs it
    app.pane = StatusPane(app)

    mgr = ScreenManager(app, {
        POSITION: PosScreen(app),
        PROG:     ProgScreen(app),
        OFFSET:   OffsetScreen(app),
        SYSTEM:   SystemScreen(app),
        MESSAGE:  MessageScreen(app),
        GRAPHICS: GraphicsScreen(app),
    }, WIN_W, WIN_H)
    mgr.show(POSITION)

    event = SDL_Event()
    running = True
    finger = None                   # id of the one finger we track at a time
    can_down = None                 # SDL_GetTicks() when CAN went down, else None

    if app._pending_program and os.path.exists(app._pending_program):
        try:
            app.reload_program(app._pending_program)
        except linuxcnc.error:
            pass          # stale path / machine not ready — start blank
    app._pending_program = None

    while running:
        app.poll()
        app.pane.update()

        # --- events -------------------------------------------------------
        while SDL_PollEvent(ctypes.byref(event)):
            if event.type == SDL_QUIT:
                running = False

            elif event.type == SDL_TEXTINPUT:
                app.input.feed(event.text.text.decode("utf-8"))

            elif event.type == SDL_KEYDOWN and event.key.repeat == 0:
                sc = event.key.keysym.scancode
                if sc == SDL_SCANCODE_ESCAPE:        # RESET key
                    app.reset()
                    mgr.screens[PROG].view_line = 0  # rewind PRGRM display
                elif sc == SDL_SCANCODE_BACKSPACE:   # CAN
                    can_down = SDL_GetTicks()
                    if app.input.text:
                        app.input.backspace()
                elif sc == SDL_SCANCODE_LALT:
                    mgr.on_edit_key("ALTER")
                elif sc == SDL_SCANCODE_INSERT:
                    mgr.on_edit_key("INSERT")
                elif sc == SDL_SCANCODE_DELETE:
                    mgr.on_edit_key("DELETE")
                elif sc == SDL_SCANCODE_LCTRL:
                    mgr.toggle_help()
                elif sc in PAGE_KEYS:
                    # The SYSTEM editor pages take raw text from an external
                    # keyboard, where Shift and Caps Lock are modifiers, not
                    # page keys.
                    if not (app.input.raw and sc in RAW_MODIFIER_KEYS):
                        mgr.show(PAGE_KEYS[sc])
                elif sc in SOFTKEYS:
                    mgr.on_softkey(SOFTKEYS[sc])
                else:
                    mgr.on_key(sc)

            elif event.type == SDL_KEYUP:
                if event.key.keysym.scancode == SDL_SCANCODE_BACKSPACE:
                    can_down = None

            # a lost focus can swallow the KEYUP — disarm the hold ourselves
            elif event.type == SDL_WINDOWEVENT:
                if event.window.event == SDL_WINDOWEVENT_FOCUS_LOST:
                    can_down = None

            # --- touch / pointer ------------------------------------------
            # Both families are handled: SDL synthesises mouse events from
            # touch by default, but that is a hint a kiosk config can turn
            # off. The SDL_TOUCH_MOUSEID guard is what stops one tap firing
            # twice while synthesis is on. Finger coords are normalised.
            elif event.type == SDL_MOUSEBUTTONDOWN:
                if (event.button.button == SDL_BUTTON_LEFT
                        and event.button.which != SDL_TOUCH_MOUSEID):
                    mgr.on_press(event.button.x, event.button.y)

            elif event.type == SDL_MOUSEBUTTONUP:
                if (event.button.button == SDL_BUTTON_LEFT
                        and event.button.which != SDL_TOUCH_MOUSEID):
                    mgr.on_release(event.button.x, event.button.y)

            elif event.type == SDL_MOUSEMOTION:
                if event.motion.state and event.motion.which != SDL_TOUCH_MOUSEID:
                    mgr.on_drag(event.motion.x, event.motion.y)

            elif event.type == SDL_FINGERDOWN and finger is None:
                finger = event.tfinger.fingerId      # ignore a resting palm
                mgr.on_press(int(event.tfinger.x * mgr.W),
                             int(event.tfinger.y * mgr.H))

            elif event.type == SDL_FINGERMOTION and event.tfinger.fingerId == finger:
                mgr.on_drag(int(event.tfinger.x * mgr.W),
                            int(event.tfinger.y * mgr.H))

            elif event.type == SDL_FINGERUP and event.tfinger.fingerId == finger:
                finger = None
                mgr.on_release(int(event.tfinger.x * mgr.W),
                               int(event.tfinger.y * mgr.H))

        # CAN held past the threshold: wipe the buffer, once per press
        if can_down is not None and SDL_GetTicks() - can_down >= CAN_HOLD_MS:
            app.input.clear()
            can_down = None

        # --- draw -----------------------------------------------------------
        SDL_SetRenderDrawColor(renderer, *BACKGROUND, 255)
        SDL_RenderClear(renderer)

        # active screen content + softkey frame/labels + pane
        mgr.draw(renderer)

        # status bar (drawn over the top band)
        label_text = f"O{app.prog_num:04d}" if app.prog_num is not None else "O----"
        draw_line(renderer, font, label_text, 10, 10)
        draw_line(renderer, font, app.ndisp.text(app.stat), 220, 10)

        t = app.stat.tool_in_spindle
        draw_line(renderer, font, f"T{t:02d}" if t is not None else "T-", 500, 10)
        draw_line(renderer, font, WCS[app.stat.g5x_index], 640, 10)
        draw_line(renderer, font, MODES[app.stat.task_mode], 760, 10)
        draw_line(renderer, font, app.unit_tag(), 890, 10)

        draw_status_box(renderer, font, 990, 6, 120, 50, "EMG", app.stat.estop)

        # machine clock, right-aligned in the top band
        clock = datetime.now().strftime("%H:%M:%S")
        clock_x = WIN_W - 10 - text_width(font, clock)
        draw_line(renderer, font, clock, clock_x, 10)

        # alarm text is unbounded, so trim it to stop short of the clock
        latest = app.alarms.latest()
        if latest is not None:
            text = latest["text"]
            avail = clock_x - 20 - 1110
            while text and text_width(font, text) > avail:
                text = text[:-1]
            draw_line(renderer, font, text, 1110, 10, ALARM)

        SDL_RenderPresent(renderer)
        SDL_Delay(16)   # ~60 Hz
        if app.quit:
            running = False

    TTF_CloseFont(font)
    TTF_CloseFont(large_font)
    SDL_DestroyRenderer(renderer)
    SDL_DestroyWindow(window)
    TTF_Quit()
    SDL_Quit()

if settings.INI_PATH:
    os.environ.setdefault("INI_FILE_NAME", settings.INI_PATH)

if __name__ == "__main__":
    main()