# Industrial LinuxCNC GUI

A industrial style operator interface for [LinuxCNC](https://linuxcnc.org/),
written in Python with [PySDL2](https://pypi.org/project/PySDL2/).

I was unsatisfied with the default interfaces of LinuxCNC, and wanted something more familiar to FANUC and Siemens controllers. I used Claude for most of this, I tried to keep the mess to a minimum but the screens.py file is kind of abysmal, and there are a few weird

This projectis intended for 3 to 5 axis mills, I may add lathes and mill turns later if there is interest.

> **Status:** work in progress. Functional on simulation configs; hardware
> bring-up ongoing. Not yet validated on a live machine — treat accordingly.

---

## Requirements

- A working **LinuxCNC 2.9+** installation (provides the `linuxcnc`, `gcode`
  and `hal` Python modules — these are **not** on PyPI and come with LinuxCNC).
- Python 3.9+
- `PySDL2` and `pysdl2-dll`
- SDL2 with `SDL_ttf`
- A font file set in `interface_test.py`
- For probing: the Probe Basic macros in your `SUBROUTINE_PATH` (see
  [`macros/`](macros/)).

Because it links against the LinuxCNC Python bindings (installed in the system
Python), this runs against the **system Python**, not an isolated virtualenv.

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
