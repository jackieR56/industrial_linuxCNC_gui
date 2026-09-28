# Probe and tool-setting macros

Put this directory on the interpreter's search path:

```ini
[RS274NGC]
SUBROUTINE_PATH = /path/to/industrial_linuxCNC_gui/macros
```

The GUI also reads the leading comment block of each `probe_*.ngc` file to
show the operator where to position the probe, so keep those headers.

## Attribution

All `probe_*.ngc` files, `reset_all_data.ngc`, `x_data_reset.ngc` and
`y_data_reset.ngc`, plus `unused/tool_sensor.ngc` and `unused/touch_plate.ngc`,
are derived from the **Probe Basic** project (part of QtPyVCP):

- Upstream: <https://github.com/kcjengr/probe_basic>
- License: GNU General Public License v3.0 (upstream `LICENSE.md`)

They remain under that license. This repository's own `LICENSE` covers the
rest of the project.

## Original files

- `tool_length.ngc`: tool length measurement on a fixed tool setter, reads
  its geometry from the `[TOOLSETTER]` ini section (see the top-level README).
  Written for this GUI, not derived from Probe Basic.

## Local modifications to the Probe Basic macros

- Positional argument order is unchanged (15 shared args, then the
  family-specific tail); the GUI's P.SET page maps onto that order.
- Header comments were left as upstream wrote them. The GUI strips the
  `author/version/date` lines and the "ensure all settings" trailer when it
  displays them.
- `tool_sensor.ngc` and `touch_plate.ngc` are not used by the GUI (they read
  the Probe Basic `[TOOLSENSOR]` section and write a work offset rather than a
  tool length) and live in `unused/` so they stay out of `SUBROUTINE_PATH`.
