import pytest

import units
from units import (MM_PER_INCH, disp_to_machine, machine_to_disp,
                   machine_to_interp, unit_factor, unit_tag)

MODES = ["MACHINE", "MM", "INCH", "PROGRAM"]


def expected(mode, machine_mm, program_mm):
    """(factor, tag) worked out independently of units.py."""
    if mode == "MACHINE":
        shown_mm = machine_mm
    elif mode == "MM":
        shown_mm = True
    elif mode == "INCH":
        shown_mm = False
    else:
        shown_mm = program_mm
    if shown_mm == machine_mm:
        f = 1.0
    else:
        f = 1 / MM_PER_INCH if machine_mm else MM_PER_INCH
    return f, "MM" if shown_mm else "INCH"


@pytest.mark.parametrize("program_mm", [False, True], ids=["G20", "G21"])
@pytest.mark.parametrize("machine_mm", [False, True], ids=["inch", "mm"])
@pytest.mark.parametrize("mode", MODES)
def test_factor_tag_round_trip(mode, machine_mm, program_mm):
    f, tag = expected(mode, machine_mm, program_mm)
    got = unit_factor(mode, machine_mm, program_mm)
    assert got == pytest.approx(f)
    assert unit_tag(mode, machine_mm, program_mm) == tag
    for v in (0.0, 1.0, -12.3456, 250.0):
        d = machine_to_disp(v, got, True)
        assert d == pytest.approx(v * f)
        assert disp_to_machine(d, got, True) == pytest.approx(v)
        # rotary axes pass straight through
        assert machine_to_disp(v, got, False) == v
        assert disp_to_machine(v, got, False) == v


def test_unknown_mode_is_machine():
    assert unit_factor("BOGUS", True, False) == 1.0
    assert unit_tag("BOGUS", False, True) == "INCH"


@pytest.mark.parametrize("machine_mm", [False, True])
def test_interp_identity_when_modes_agree(machine_mm):
    assert machine_to_interp(12.5, machine_mm, machine_mm) == 12.5


def test_interp_mm_machine_in_g20():
    assert machine_to_interp(25.0, True, False) == pytest.approx(25 / 25.4)


def test_interp_inch_machine_in_g21():
    assert machine_to_interp(1.0, False, True) == pytest.approx(25.4)


def test_linear_axes():
    assert units.LINEAR_AXES == "XYZUVW"
