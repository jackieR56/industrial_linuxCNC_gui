#!/usr/bin/env python3
# interface_test.py

import os
import re
import sys
import json
import time
import ctypes
import logging
import tempfile
import traceback
from logging.handlers import RotatingFileHandler
from sdl2 import *
from sdl2.sdlttf import *
from collections import deque
from datetime import datetime
from statuspane import StatusPane

import linuxcnc
import settings

# Import-time side effect, deliberately BEFORE anything can start a backplot
# parse worker: the gcode preview interpreter reads INI_FILE_NAME from the
# environment (the linuxcnc launcher sets it; a GUI started from its own
# terminal does not). Backplot2D's worker must not touch os.environ itself.
if settings.INI_PATH:
    os.environ.setdefault("INI_FILE_NAME", settings.INI_PATH)

import configfile
import units

from screens import (draw_line, text_width, InputBuffer,
                     PosScreen, ProgScreen, OffsetScreen,
                     MessageScreen, GraphicsScreen,
                     ScreenManager, WCS)
from systemscreen import SystemScreen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WIN_W, WIN_H = 1920, 1080
FONT_PATH = settings.get_path("GUI", "FONT_PATH", settings.DEFAULT_FONT)
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
# NOTE for the read-only-rootfs deployment: point this AND ERROR_LOG below
# at the writable data partition.
PERSIST = os.path.join(os.path.dirname(__file__), "machine_counters.json")

# tracebacks of exceptions caught in the main loop (rotating, 1 MB x 3)
ERROR_LOG = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                         "linuxcnc-gui-errors.log")
ERROR_STORM_S = 5.0     # identical exceptions within this window are counted
PERSIST_ALARM_S = 60.0  # at most one COUNTERS SAVE FAILED alarm per minute

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
        # set when an NML_ERROR arrives; App.poll consumes and clears it
        # (parts counter: an error during a cycle is an abort, not a part)
        self.error_seen = False

    def poll(self):
        while True:
            err = self._chan.poll()
            if not err:
                break
            kind, text = err
            if kind == linuxcnc.NML_ERROR:
                self.error_seen = True
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


class ErrorReporter:
    """Main-loop exception sink. This process is LinuxCNC's DISPLAY: if it
    dies the control shuts down, so a GUI bug must become an alarm line and
    a log entry, never an exit. Identical exceptions (same type and text)
    within ERROR_STORM_S are only counted; the count is reported once the
    window has passed, so a per-frame fault gives one alarm per 5 s, not
    60 per second. A different exception is reported immediately."""

    def __init__(self, app):
        self.app = app
        self.log = logging.getLogger("linuxcnc-gui")
        self.log.setLevel(logging.ERROR)
        self.log.propagate = False
        try:
            h = RotatingFileHandler(ERROR_LOG, maxBytes=1024 * 1024,
                                    backupCount=3)
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.log.addHandler(h)
        except OSError as e:
            print(f"GUI error log unavailable ({ERROR_LOG}): {e}",
                  file=sys.stderr)
        self._key = None            # (type, str) of the last reported one
        self._t = 0.0               # monotonic time of the last report
        self._suppressed = 0        # identical ones since then

    def report(self, e):
        """Call from an `except Exception as e:` block. Never raises."""
        try:
            now = time.monotonic()
            key = (type(e), str(e))
            if key == self._key and now - self._t < ERROR_STORM_S:
                self._suppressed += 1
                return
            self._flush()                       # previous storm's count
            self._key, self._t = key, now
            tb = "".join(traceback.format_exception(type(e), e,
                                                    e.__traceback__))
            print(tb, file=sys.stderr, end="")
            self.log.error("%s", tb.rstrip())
            self.app.alarm(f"GUI ERROR: {type(e).__name__}: {e}")
        except Exception:
            pass

    def tick(self):
        """Once per frame: report a pending count once the window is over."""
        try:
            if self._suppressed and time.monotonic() - self._t >= ERROR_STORM_S:
                self._flush()
        except Exception:
            pass

    def _flush(self):
        if not self._suppressed:
            return
        n, self._suppressed = self._suppressed, 0
        self._t = time.monotonic()
        name, msg = self._key[0].__name__, self._key[1]
        text = f"GUI ERROR (REPEATED {n}X): {name}: {msg}"
        print(text, file=sys.stderr)
        self.log.error("%s", text)
        self.app.alarm(text)


# ---------------------------------------------------------------------------
# Shared state handed to every screen
#
# UNITS POLICY: all *stored* state (rel_origin, wcs_vals, wear, tool geom,
# backplot segments) is ALWAYS in machine units. Conversion happens only at
# the display/entry membrane via the helpers below.
# ---------------------------------------------------------------------------
class App:
    LINEAR_AXES = units.LINEAR_AXES  # rotary axes (A/B/C) never unit-convert

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
        self.quit = False                   # set by SYSTEM -> EXIT -> EXEC
        self.axes = settings.get_str("GUI", "AXES", "XYZAC")   # "XYZ" for 3-axis
        self.rel_origin = [0.0] * 9         # machine units, 9-tuple indexed
        # parts counter: True once RESET or an NML error hit the running
        # cycle; StatusPane clears it at cycle start, checks it at cycle end
        self.aborted_since_start = False
        self._persist_alarm_t = None        # last COUNTERS SAVE FAILED alarm

        # persistent settings/counters (self.alarms exists by now)
        self.persist = self._load_persist()
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

    # ------------------------------------------------------------- persist
    @staticmethod
    def _read_json(path):
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: not a JSON object")
        return data

    def _load_persist(self):
        """PERSIST, else its .bak (with an alarm), else {} (with an alarm).
        A missing PERSIST is a first start: {} without an alarm."""
        try:
            return self._read_json(PERSIST)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            pass
        # move the bad file aside so the next save's .bak rotation cannot
        # overwrite the good backup with it
        try:
            os.replace(PERSIST, PERSIST + ".corrupt")
        except OSError:
            pass
        try:
            data = self._read_json(PERSIST + ".bak")
            self.alarm("COUNTERS FILE CORRUPT - USING BACKUP")
            return data
        except (OSError, ValueError):
            self.alarm("COUNTERS FILE CORRUPT - COUNTERS RESET")
            return {}

    def save_persist(self):
        """Atomic write via configfile.atomic_write (temp file, fsync,
        rename) with one backup, PERSIST.bak, so a power cut leaves the old
        or the new file, never a torn one. Runs every 60 s, so a failure
        alarms at most once per PERSIST_ALARM_S."""
        self.persist["display_units"] = self.display_units
        try:
            configfile.atomic_write(PERSIST, json.dumps(self.persist),
                                    backup=True, keep=1)
        except OSError as e:
            now = time.monotonic()
            if (self._persist_alarm_t is None
                    or now - self._persist_alarm_t >= PERSIST_ALARM_S):
                self._persist_alarm_t = now
                self.alarm(f"COUNTERS SAVE FAILED: {e}")

    # ------------------------------------------------------------- alarms
    def alarm(self, text):
        """Push a GUI-side alarm onto the alarm line / MESSAGE list, same
        entry shape as AlarmSystem.poll(). Cleared by RESET like the rest."""
        self.alarms.active.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": text.upper()[:160],
            "alarm": True,
        })

    # ------------------------------------------------------------- state
    def machine_off(self):
        """True in ESTOP, ESTOP RESET or OFF: no drive is enabled, so it is
        safe to end the DISPLAY process (which shuts LinuxCNC down). Gates
        SYSTEM EXIT/RESTART and closing the window. Reads the last poll."""
        return self.stat.task_state in (linuxcnc.STATE_ESTOP,
                                        linuxcnc.STATE_ESTOP_RESET,
                                        linuxcnc.STATE_OFF)

    def motion_quiet(self):
        """True when nothing is moving or about to: machine off, or machine
        on with the interpreter idle, all axes in position, zero commanded
        velocity and no joint homing. For actions that must not disturb
        motion but do not need the machine off. Reads the last poll."""
        st = self.stat
        return self.machine_off() or (
            st.interp_state == linuxcnc.INTERP_IDLE
            and st.inpos
            and st.current_vel == 0
            and not any(j["homing"] for j in st.joint[:st.joints]))

    def poll(self):
        self.stat.poll()
        self.alarms.poll()
        # parts counter: an NML error is an abort. Flagged even when the
        # interp is already idle (the error can abort the program before
        # this poll sees it running); StatusPane clears the flag at cycle
        # start, so an error between cycles cannot cost the next part.
        if self.alarms.error_seen:
            self.aborted_since_start = True
            self.alarms.error_seen = False
        # file-change -> reparse O-number
        if self.stat.file != self._last_file:
            self._last_file = self.stat.file
            self.prog_num = program_number(self.stat.file)

    def _switch_mode(self, mode):
        """Switch task mode and confirm it. Raises linuxcnc.error on a
        timeout or when task refused (e.g. MDI while a program runs).
        mode() returns at once when already in `mode`; the poll + compare
        is the real check, not wait_complete()."""
        self.command.mode(mode)
        if self.command.wait_complete() == -1:
            raise linuxcnc.error("MODE SWITCH TIMEOUT")
        self.stat.poll()
        if self.stat.task_mode != mode:
            raise linuxcnc.error(
                f"MODE SWITCH REJECTED ({MODES[self.stat.task_mode]})")

    def mdi(self, code):
        """Blocking MDI for short commands (G10 etc.). Raises linuxcnc.error
        if the mode switch fails. Returns False if the command had not
        completed after wait_complete's 5 s (it keeps running); callers
        that need the result (offset read-back) must treat that as
        failure. Operator MDI blocks go through mdi_async instead."""
        self._switch_mode(linuxcnc.MODE_MDI)
        self.command.mdi(code)
        return self.command.wait_complete() != -1

    def mdi_async(self, code):
        """Fire an MDI command and return immediately — does NOT wait for the
        command to finish executing. The mode switch is fast so we wait for
        that, but the command itself (e.g. a probe macro with motion) runs
        across many frames; callers watch stat.interp_state for completion so
        the UI loop keeps running. Do not send another command until the
        interp returns to INTERP_IDLE."""
        self._switch_mode(linuxcnc.MODE_MDI)   # mode switch only — fast
        self.command.mdi(code)            # no wait_complete: let it run

    def run_from_line(self, line):
        """Cycle start from program line `line` (restart). Raises
        linuxcnc.error if AUTO mode cannot be entered."""
        self._switch_mode(linuxcnc.MODE_AUTO)
        self.command.auto(linuxcnc.AUTO_RUN, line)

    def reset(self):
        """RESET key: abort program/MDI execution (interp rewinds to
        program top), spindle off, coolant off, rewind the N display, and
        move active alarms to history. Explicit spindle/coolant-off after
        abort is belt-and-suspenders: task abort stops them for a running
        program, but this also covers a spindle started manually via MDI."""
        c = self.command
        c.abort()
        # never raise from the RESET key path
        if c.wait_complete() == -1:
            self.alarm("ABORT TIMEOUT")
        self.aborted_since_start = True        # parts counter: not a part
        if not self.stat.estop and self.stat.task_state == linuxcnc.STATE_ON:
            c.spindle(linuxcnc.SPINDLE_OFF)
            c.mist(linuxcnc.MIST_OFF)
            c.flood(linuxcnc.FLOOD_OFF)
        self.ndisp.reset()
        self.alarms.reset()

    def reload_program(self, path):
        """Open `path` in AUTO. Raises linuxcnc.error on failure (callers
        catch it); last_program is only remembered on success."""
        self._switch_mode(linuxcnc.MODE_AUTO)
        self.command.program_open(path)
        if self.command.wait_complete() == -1:
            raise linuxcnc.error("PROGRAM OPEN TIMEOUT")
        self.ndisp.invalidate()
        if hasattr(self, "backplot"):
            self.backplot.invalidate()
        self.persist["last_program"] = path      # <-- remember it
        self.save_persist()

    # ------------------------------------------------------------- units
    # thin wrappers over units.py (pure, unit-tested) fed from live stat
    def _machine_mm(self):
        return self.stat.linear_units == 1.0

    def _program_mm(self):
        return self.stat.program_units == 2

    def unit_factor(self):
        """Multiply machine-unit linear values by this for display."""
        return units.unit_factor(self.display_units, self._machine_mm(),
                                 self._program_mm())

    def unit_tag(self):
        return units.unit_tag(self.display_units, self._machine_mm(),
                              self._program_mm())

    def machine_to_interp(self, v):
        """Machine-unit linear value -> value for a G10/MDI word, which the
        interpreter reads in its *current* (G20/G21) units."""
        return units.machine_to_interp(v, self._machine_mm(),
                                       self._program_mm())

    def disp_to_machine(self, ax, typed):
        """Typed display-unit value -> machine units (rotary passes through)."""
        return units.disp_to_machine(typed, self.unit_factor(),
                                     ax in self.LINEAR_AXES)

    def machine_to_disp(self, ax, v):
        return units.machine_to_disp(v, self.unit_factor(),
                                     ax in self.LINEAR_AXES)

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
    large_font = TTF_OpenFont(FONT_PATH.encode(), LARGE_FONT_SIZE) if font else None
    if not font or not large_font:
        print(f"GUI: could not open font {FONT_PATH} "
              "([GUI]FONT_PATH in the ini) - exiting", file=sys.stderr)
        sys.exit(1)

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

    errors = ErrorReporter(app)

    # Every stage of the frame is guarded: this process is the DISPLAY, and
    # an escaped exception would shut the control down. Stages are guarded
    # separately (and each event on its own) so a fault in one - say a draw
    # bug - does not also starve the event loop and lock out the RESET key.
    # except Exception deliberately lets KeyboardInterrupt/SystemExit out.
    while running:
        try:
            app.poll()
            app.pane.update()
        except Exception as e:
            errors.report(e)

        # --- events -------------------------------------------------------
        while SDL_PollEvent(ctypes.byref(event)):
            try:
                if event.type == SDL_QUIT:
                    # closing the window ends LinuxCNC: machine off only
                    if app.machine_off():
                        running = False
                    else:
                        app.alarm("TURN MACHINE OFF BEFORE CLOSING")

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
            except Exception as e:
                errors.report(e)

        try:
            # CAN held past the threshold: wipe the buffer, once per press
            if can_down is not None and SDL_GetTicks() - can_down >= CAN_HOLD_MS:
                app.input.clear()
                can_down = None

            # --- draw -------------------------------------------------------
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
        except Exception as e:
            errors.report(e)

        # SYSTEM EXIT/RESTART set app.quit; honour it only machine-off.
        # Own guard, so a draw that fails every frame cannot block EXIT.
        try:
            if app.quit:
                app.quit = False
                if app.machine_off():
                    running = False
                else:
                    # a RESTRT that lost the race leaves its flag file:
                    # remove it or the next EXIT would relaunch the control
                    flag = os.environ.get("GUI_RESTART_FLAG")
                    if flag:
                        try:
                            os.remove(flag)
                        except OSError:
                            pass
                    app.alarm("TURN MACHINE OFF BEFORE CLOSING")
        except Exception as e:
            errors.report(e)
        errors.tick()

        # always end the frame, even after an exception mid-draw, so the
        # operator never looks at a frozen screen
        SDL_RenderPresent(renderer)
        SDL_Delay(16)   # ~60 Hz

    try:
        app.pane.flush()        # keep the last minute of RUN time / parts
    except Exception as e:
        errors.report(e)

    TTF_CloseFont(font)
    TTF_CloseFont(large_font)
    SDL_DestroyRenderer(renderer)
    SDL_DestroyWindow(window)
    TTF_Quit()
    SDL_Quit()

if __name__ == "__main__":
    main()