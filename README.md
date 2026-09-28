# Industrial LinuxCNC GUI

A industrial style operator interface for [LinuxCNC](https://linuxcnc.org/),
written in Python with [PySDL2](https://pypi.org/project/PySDL2/).

This repository is a mess and there are a few things I need to fix, if you see something weird it will probably be fixed soon

I was unsatisfied with the default interfaces of LinuxCNC, and wanted something more familiar to FANUC and Siemens controllers. I used Claude for most of this, I tried to keep the mess to a minimum but the screens.py file is kind of abysmal, and there are a few weird methods and nomenclature.

This project is intended for 3 to 5 axis mills, I may add lathes and mill turns later.

I have this GUI paired with a custom Keypad and panel. It will work with a normal keyboard but you will need to find new mappings for the page keys, as they are on [F3-F19]. Also overrides, cycle start, feed hold, jog buttons and mode dials will need physical IO card inputs, a [7I73 Pendant/control panel interface](https://store.mesanet.com/index.php?route=product/product&product_id=116) shoud be suitable, I use Beckhoff Ethercat IO modules.

This project is still a work in progress and may be unstable, I have yet to commission the actaul mill with this but have been messing with a cheap router and it works well so far.

---

## Requirements

- A working **LinuxCNC 2.9+** installation (provides the `linuxcnc`, `gcode`
  and `hal` Python modules — these are **not** on PyPI and come with LinuxCNC).
- Python 3.9+
- `PySDL2` and `pysdl2-dll`
- SDL2 with `SDL_ttf`
- A font file, set by `[GUI]FONT_PATH` in the machine ini
- `python3-yaml` (PyYAML) to read `io_map.yaml` for the PHYS / HAL IO viewer
  (without it the viewer still runs on a freshly discovered point list).
- For probing: the Probe Basic macros in your `SUBROUTINE_PATH` (see
  [`macros/`](macros/)).

Because it links against the LinuxCNC Python bindings (installed in the system
Python), this runs against the **system Python**, not an isolated virtualenv.

---

## Running

On the machine, LinuxCNC launches the GUI as its display program. In the ini:

```ini

[DISPLAY]
DISPLAY = /path/to/industrial_gui.py

[RS274NGC]
SUBROUTINE_PATH = /path/to/macros

[TOOLSETTER]
# Fixed tool setter for OFFSET -> PROBE -> Z.PRB (macros/tool_length.ngc).
# All values are MACHINE units, machine coordinates.
# setter centre
X = 400.0
Y = -50.0
# machine Z for travel to/from the setter
Z_SAFE = 0.0
# machine Z to rapid down to; must clear the setter with the longest tool
Z_START = -150.0
# search travel down from Z_START (positive); must reach the setter with
# the shortest tool
MAXPROBE = 200.0
# machine units per minute
SEARCH_VEL = 300.0
PROBE_VEL = 25.0
# retract before the slow probe
BACKOFF = 2.0
# machine Z at which a zero-length tool trips the setter (spindle gauge line
# on the trip point); calibrate with a tool of known length
Z_REF = -420.0
# optional: motion.digital-in number wired to the setter contact; the macro
# refuses to descend if it is already tripped
# PROBE_INPUT = 0
```

The numbers above are examples for a metric machine. Every `[TOOLSETTER]` key
except `PROBE_INPUT` is required; Z.PRB refuses to run (TOOLSETTER NOT
CONFIGURED) when one is missing, and the OFFSET -> PROBE page shows each value
or NOT SET. All values are machine units, and the macro (like the work probe
cycles) refuses to run unless the interpreter is in the machine's unit mode
(G21 on a metric machine, G20 on an inch one). The Probe Basic
`tool_sensor.ngc` / `touch_plate.ngc` (which read `[TOOLSENSOR]`) are not used
and live in `macros/unused/`.

---

## Configuration

Settings are read from the `[GUI]` and `[GUI_COLORS]` sections of the machine
ini (the one LinuxCNC passes with `-ini`). Every key is optional and falls back
to a built-in default. See [`gui_settings.sample.ini`](gui_settings.sample.ini)
for the full list, ready to paste.

- `[GUI]` — `FONT_PATH`, `FONT_SIZE`, `LARGE_FONT_SIZE`, `USB_MOUNTS`, `AXES`
  (`XYZAC` or `XYZ`), `ALWAYS_SHOW_POSITION`, `SHOW_MDI_HISTORY`, `IO_MAP`,
  `IO_FONT_SIZE`, `IO_SAMPLE_MS`
- `[GUI_COLORS]` — every screen color, as `R, G, B`
- `[DISPLAY]PROGRAM_PREFIX` — program directory (default `~/linuxcnc/nc_files`)
- `display_units` — persisted; set from the OFFSET → SETTING page

**Parts counter.** For an exact count, wire the HAL input
`status-pane.part-done` (in `postgui.hal`) to a signal pulsed by M30 or a user
M-code; each rising edge counts one part, and from the first pulse on the
GUI's own count is switched off. Without it the GUI counts AUTO cycles that
end without RESET or an error.

**Tool wear.** Wear is stored inside the tool table (`[EMCIO]TOOL_TABLE`) as a
`;W:Z<z> R<r>` prefix on each tool's comment, in machine units; the table's
Z/diameter is geometry + wear, and the OFFSET page shows geometry as table
value minus wear. When editing `tool.tbl` by hand, keep the token. An old
`tool_wear.json` is imported once and renamed to `tool_wear.json.migrated`.

---

## SYSTEM screen: ini / hal editor

The SYSTEM page maintains the machine configuration from the control:

- **DIR / USB** browse the configuration directory (the folder of the running
  ini) and the USB mounts. `SELECT` picks a file; `COPY` moves files between
  the two for backups.
- **TEXT** is a line/word editor like the program EDIT page.
- **FIELDS** is a structured view of the same file: one value field per
  `KEY = value` line of an ini; for a hal file, two cells per statement
  (`net` signal | pins, `setp` pin | value, `loadrt` comp | args, `addf`
  func | thread). Comments, blank lines and alignment are kept on save.
  `APPLY` sends a `setp` to the running HAL right away; `PINS` shows the live
  values of the pins on the selected line. Both need `halcmd` on the PATH.
- Every `SAVE` writes `<file>.bak` first; `RESTOR` copies it back.
- Editing is locked while a program runs.

Typing on TEXT/FIELDS keeps case and accepts every printable character, so use
an external keyboard there; Shift and Caps Lock stop acting as page keys while
those pages are open.

Ini and hal edits take effect after a restart of the control. Start LinuxCNC
through [`run_gui.sh`](run_gui.sh) and the `RESTRT` softkey saves, quits and
relaunches; started any other way, it reports `NO LAUNCHER` and you exit and
start again by hand.

### PHYS / HAL: live IO viewer

A read-only, PMC-style signal grid for commissioning and fault finding.

- **PHYS**: field devices, one entry in the left list per device: each
  EtherCAT slave (from `ethercat-conf.xml` / the `lcec_conf` line in the hal
  files, plus any live `lcec.*` slave), each userspace driver (`loadusr`
  components such as a Modbus VFD, or the sim `vpanel`), and RT hardware
  drivers (`hm2_*`, parport...). The device lamp is green when its status
  pins (slave online + operational) are all TRUE.
- **HAL**: the internal signals, grouped by hal file and section header
  comment, plus `LINUXCNC` (enables, tool change, spindle) and `JOINT n`
  (amp enable/fault, homing, limits, following error).

Each cell shows I/O (field side), a lamp or value, and the label; the lines
under the grid give the full name, type, direction, linked signal, rise/fall
counts, time since the last change and min/max. A cell that changed in the
last second is outlined. `(OPRT)`: `GRP`, `SRH`, `HOLD` (freeze the display,
counting continues), `LOG` (timestamped changes), `CLR.CNT`, and `REGEN` on
page 2.

The point list is `io_map.yaml` in the config dir (`[GUI]IO_MAP`). The
viewer writes it from the running HAL when it does not exist and never
overwrites it after that (except `REGEN`, which keeps a `.bak`). Edit it with
DIR -> TEXT: rename, reorder, delete, add `invert: true` for NC contacts, or
`bits: {0: RTSO, 3: FAULT}` to show the bits of a drive status word as lamps.
Changes load the next time PHYS / HAL is opened. Adding a new bus type is one
discoverer function in [`iomap.py`](iomap.py).

Values are read in-process from HAL about 20 times a second (`[GUI]IO_SAMPLE_MS`),
so a pulse shorter than that can be missed by the lamp. The counts and the log
still record it if a sample landed on it.

---

## Acknowledgements

- [LinuxCNC](https://linuxcnc.org/) — the motion control and interpreter this
  interface is built on.
- The probing subroutines in [`macros/`](macros/) are derived from the
  **Probe Basic / QtPyVCP** project and remain under their original license.
  See [`macros/README.md`](macros/README.md) for attribution and a note on
  local modifications.
