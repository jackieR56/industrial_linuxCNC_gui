# Industrial LinuxCNC GUI

A from-scratch, Fanuc 0i-style operator interface for [LinuxCNC](https://linuxcnc.org/),
written in Python with [PySDL2](https://pypi.org/project/PySDL2/). It runs
fullscreen as a kiosk-style control screen and is intended to be the **sole UI
on the LinuxCNC command channel** (see *Important constraints* below).

Built primarily for a 5-axis BT30 horizontal machining center (trunnion, axes
XYZAC), with a simpler 3-axis machine sharing the same codebase via
configuration flags.

> **Status:** work in progress. Functional on simulation configs; hardware
> bring-up ongoing. Not yet validated on a live machine — treat accordingly.

---

## Features

- **Fanuc-style page + softkey model** — POSITION, PROGRAM, OFFSET, SYSTEM,
  MESSAGE and GRAPHICS pages, each with a multi-level softkey menu and the
  familiar `[<]` / `[>]` navigation stubs.
- **Mode-dependent PROGRAM page** — chapters change with the mode dial
  (MANUAL: PRGRM/EDIT/DIR/USB · AUTO: PRGRM/CHECK/CURRNT/NEXT/RSTR ·
  MDI: PRGRM/MDI/CURRNT/NEXT).
- **Simple on-control editor** — word-level cursor with insert / alter /
  delete of words and lines, plus in-buffer N-search and text search.
- **Program directory + USB browser** — real directory listing with
  alphanumeric names; read/punch transfer to auto-mounted panel USB ports.
- **Tool + work offset tables** — tool geometry/wear (with editable comments
  read from `tool.tbl`), nine work coordinate systems, all with a
  units-conversion membrane (MACHINE / MM / INCH / PROGRAM, persisted).
- **2D backplot** — `gcode.parse`-driven toolpath preview with per-tool
  colors, live tool-tip marker, A/C orientation vector, incremental
  gray-out and multiple views.
- **Probing** — part- and tool-probing pages driving the Probe Basic macro
  set. Five work-probe families (outside, inside, edge-angle, boss/pocket,
  ridge/valley) plus stylus calibration, presented as a 3×3 direction grid
  with per-scenario diagrams; tool-length probing against a fixed sensor.
- **Status pane** — cycle/run timers, parts counter, live S/F and overrides,
  optional axis-load bars or position/modals.
- **Page help overlay** — per-page instructions from `help.json`.

---

## Requirements

- A working **LinuxCNC 2.9+** installation (provides the `linuxcnc`, `gcode`
  and `hal` Python modules — these are **not** on PyPI and come with LinuxCNC).
- Python 3.9+
- `PySDL2` and `pysdl2-dll`
- SDL2 with `SDL_ttf`
- A TrueType font (path set in `interface_test.py`)
- For probing: the Probe Basic macros in your `SUBROUTINE_PATH` (see
  [`macros/`](macros/)).

Because it links against the LinuxCNC Python bindings (installed in the system
Python), this runs against the **system Python**, not an isolated virtualenv.

---

## Layout

```
.
├── interface_test.py   # entry point: SDL main loop, App shared state,
│                        #   NDisplay, AlarmSystem, status bar, key routing
├── screens.py          # all screens, softkey framework, ScreenManager,
│                        #   InputBuffer / FieldCursor, render helpers
├── statuspane.py       # corner status pane
├── backplot.py         # 2D toolpath backplot engine
├── help.json           # per-page help text
└── macros/             # probing subroutines (see macros/README)
```

---

## Running

On the machine, LinuxCNC launches the GUI as its display program. In the ini:

```ini
[DISPLAY]
DISPLAY = /path/to/interface_test.py

[RS274NGC]
SUBROUTINE_PATH = /path/to/macros

[TOOLSENSOR]
# X, Y, HEIGHT, MAXPROBE, SEARCH_VEL, PROBE_VEL — required for tool probing
```

The GUI accepts the `-ini /path/to/machine.ini` argument LinuxCNC passes to
display programs. Make `interface_test.py` executable (`chmod +x`) or launch
it via a small wrapper script.

For development it can be run against a simulation config.

---

## Important constraints

- **One UI only.** The GUI is the sole client on the LinuxCNC command and
  error channels. Running Axis (or another GUI) alongside it causes
  command-channel and error-channel races — mode switches that don't take,
  programs that won't load, alarms landing in the wrong window. Close other
  GUIs before running this one.
- **Jog is HAL / physical-button only.** The GUI contains no jog code by
  design; jogging is wired in HAL to physical controls.
- **Units policy.** All stored state is kept in machine units; conversion
  happens only at the display/entry boundary. Rotary axes are never
  unit-converted.
- **Writable state files.** `machine_counters.json` and `tool_wear.json` hold
  per-machine runtime state and are written next to the code by default. On a
  read-only-rootfs deployment these must be relocated to a writable partition.
  They are intentionally excluded from version control.

---

## Configuration flags

Set in `App.__init__` (`interface_test.py`):

- `axes` — `"XYZAC"` or `"XYZ"`
- `always_show_position` — force the position/modals status pane variant
- `show_mdi_history` — keep executed MDI blocks listed (default off)
- `usb_mounts` — auto-mount paths for the panel USB ports
- `display_units` — persisted; also settable from the OFFSET → SETTING page

---

## Acknowledgements

- [LinuxCNC](https://linuxcnc.org/) — the motion control and interpreter this
  interface is built on.
- The probing subroutines in [`macros/`](macros/) are derived from the
  **Probe Basic / QtPyVCP** project and remain under their original license.
  See [`macros/README.md`](macros/README.md) for attribution and a note on
  local modifications.

---

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).

This project links against LinuxCNC (GPL-2.0) and bundles GPL-licensed probing
macros, and is licensed to match.
