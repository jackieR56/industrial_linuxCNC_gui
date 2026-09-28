#!/usr/bin/env python3
# units.py - pure display/entry unit conversion (no sdl2 / linuxcnc).
#
# UNITS POLICY: all *stored* state is in MACHINE units; these helpers are the
# display/entry membrane. App (interface_test.py) wraps them with live stat:
#   machine_mm = (stat.linear_units == 1.0)
#   program_mm = (stat.program_units == 2)      # G21 active

MM_PER_INCH = 25.4
LINEAR_AXES = "XYZUVW"          # rotary axes (A/B/C) never unit-convert


def unit_factor(display_units, machine_mm, program_mm):
    """Multiply machine-unit linear values by this for display.
    display_units: "MACHINE" | "MM" | "INCH" | "PROGRAM" (anything else is
    treated as MACHINE)."""
    if display_units == "MM":
        want_mm = True
    elif display_units == "INCH":
        want_mm = False
    elif display_units == "PROGRAM":
        want_mm = program_mm
    else:                                        # MACHINE
        return 1.0
    if machine_mm == want_mm:
        return 1.0
    return 1 / MM_PER_INCH if machine_mm else MM_PER_INCH


def unit_tag(display_units, machine_mm, program_mm):
    """"MM" or "INCH": the units values are shown in."""
    f = unit_factor(display_units, machine_mm, program_mm)
    shown_mm = machine_mm if f == 1.0 else not machine_mm
    return "MM" if shown_mm else "INCH"


def machine_to_interp(v, machine_mm, program_mm):
    """Machine-unit linear value -> value for a G10/MDI word, which the
    interpreter reads in its *current* (G20/G21) units."""
    if machine_mm == program_mm:
        return v
    return v / MM_PER_INCH if machine_mm else v * MM_PER_INCH


def disp_to_machine(typed, factor, linear):
    """Typed display-unit value -> machine units (rotary passes through)."""
    return typed / factor if linear else typed


def machine_to_disp(v, factor, linear):
    """Machine-unit value -> display units (rotary passes through)."""
    return v * factor if linear else v
