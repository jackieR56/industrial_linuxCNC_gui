import os
import shutil

import pytest

import tooltbl

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tool.tbl")


@pytest.fixture
def tbl(tmp_path):
    p = tmp_path / "tool.tbl"
    shutil.copy(FIXTURE, p)
    return str(p)


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ----------------------------------------------------------- split / join
@pytest.mark.parametrize("z, r, text", [
    (0.012, -0.003, "MAIN TOOL"),
    (-0.25, 0.0, "NEG"),
    (0.0, -1.5, ""),
    (1e-6, 2.0, "A B  C"),
    (0.0, 0.0, "PLAIN"),
    (0.0, 0.0, ""),
])
def test_round_trip(z, r, text):
    assert tooltbl.split_comment(tooltbl.join_comment(z, r, text)) == (z, r, text)


def test_no_token():
    assert tooltbl.split_comment("DRILL 6MM") == (0.0, 0.0, "DRILL 6MM")
    assert tooltbl.split_comment("") == (0.0, 0.0, "")
    assert tooltbl.split_comment(None) == (0.0, 0.0, "")


def test_token_format():
    assert (tooltbl.join_comment(0.012, -0.003, "MAIN TOOL")
            == "W:Z0.012000 R-0.003000 MAIN TOOL")
    assert tooltbl.join_comment(0.5, 0.0, "") == "W:Z0.500000 R0.000000"


def test_token_empty_user_text():
    assert tooltbl.split_comment("W:Z0.100000 R0.000000") == (0.1, 0.0, "")
    assert tooltbl.split_comment(" W:Z0.100000 R0.000000 ") == (0.1, 0.0, "")


def test_zero_wear_omits_token():
    assert tooltbl.join_comment(0.0, 0.0, "MAIN TOOL") == "MAIN TOOL"


def test_user_text_starting_with_w():
    for text in ("WIDE FACEMILL", "W:SPECIAL", "WZ1 R2", "W:Z R1"):
        assert tooltbl.split_comment(text) == (0.0, 0.0, text)
        assert tooltbl.split_comment(
            tooltbl.join_comment(0.0, 0.0, text)) == (0.0, 0.0, text)
        assert tooltbl.split_comment(
            tooltbl.join_comment(0.1, 0.2, text)) == (0.1, 0.2, text)


def test_user_text_that_looks_like_a_token_is_protected():
    text = "W:Z1 R2 LOOKS LIKE WEAR"
    joined = tooltbl.join_comment(0.0, 0.0, text)
    assert joined.startswith("W:Z0.000000 R0.000000 ")
    assert tooltbl.split_comment(joined) == (0.0, 0.0, text)


# ----------------------------------------------------------- read_table
def test_read_table_fixture():
    t = tooltbl.read_table(FIXTURE)
    assert sorted(t) == [1, 2, 3, 4]
    assert t[1]["wear"] == {"Z": 0.012, "R": -0.003}
    assert t[1]["comment"] == "MAIN TOOL"
    assert t[1]["tagged"] is True
    assert t[2] == {"wear": {"Z": 0.0, "R": 0.0}, "comment": "DRILL 6MM",
                    "line": 1, "tagged": False}
    assert t[3]["comment"] == "" and t[3]["wear"] == {"Z": 0.0, "R": 0.0}
    assert t[4]["comment"] == ""


def test_geometry_rewrite_keeps_wear(tbl):
    # simulate LinuxCNC rewriting the geometry part after a G10 L1
    with open(tbl, newline="") as f:
        text = f.read()
    with open(tbl, "w", newline="") as f:
        f.write(text.replace("Z+100.000000", "Z+12.345000"))
    before = tooltbl.read_table(FIXTURE)
    after = tooltbl.read_table(tbl)
    assert after[1]["wear"] == before[1]["wear"]
    assert after[1]["comment"] == before[1]["comment"]


# ----------------------------------------------------------- write_comment
def test_write_comment_only_touches_that_line(tbl):
    orig = _bytes(tbl).split(b"\n")
    tooltbl.write_comment(tbl, 2, -0.05, 0.001, "DRILL 6MM")
    new = _bytes(tbl).split(b"\n")
    assert len(new) == len(orig)
    for i, (a, b) in enumerate(zip(orig, new)):
        if i != 1:
            assert a == b
    assert new[1] == b"T2 P2 D+6.000000 Z+85.250000 ;W:Z-0.050000 R0.001000 DRILL 6MM"
    assert tooltbl.read_table(tbl)[2]["wear"] == {"Z": -0.05, "R": 0.001}


def test_write_comment_no_semicolon_line(tbl):
    tooltbl.write_comment(tbl, 3, 0.2, 0.0, "NEW")
    line = _bytes(tbl).split(b"\n")[2]
    assert line == b"T3 P3 D+0.000000 Z+0.000000 ;W:Z0.200000 R0.000000 NEW"
    e = tooltbl.read_table(tbl)[3]
    assert e["wear"] == {"Z": 0.2, "R": 0.0} and e["comment"] == "NEW"


def test_write_comment_zero_wear_drops_token(tbl):
    tooltbl.write_comment(tbl, 1, 0.0, 0.0, "MAIN TOOL")
    assert _bytes(tbl).split(b"\n")[0] == b"T1 P1 D+10.000000 Z+100.000000 ;MAIN TOOL"
    tooltbl.write_comment(tbl, 1, 0.0, 0.0, "")
    assert _bytes(tbl).split(b"\n")[0] == b"T1 P1 D+10.000000 Z+100.000000"


def test_write_comment_keeps_crlf(tmp_path):
    p = tmp_path / "crlf.tbl"
    p.write_bytes(b"T1 P1 Z+1.000000 ;A\r\nT2 P2 Z+2.000000\r\n")
    tooltbl.write_comment(str(p), 2, 0.1, 0.0, "B")
    assert p.read_bytes() == (b"T1 P1 Z+1.000000 ;A\r\n"
                              b"T2 P2 Z+2.000000 ;W:Z0.100000 R0.000000 B\r\n")


def test_write_comment_missing_tool(tbl):
    before = _bytes(tbl)
    with pytest.raises(KeyError):
        tooltbl.write_comment(tbl, 99, 0.1, 0.1, "X")
    assert _bytes(tbl) == before


def test_write_comment_missing_file(tmp_path):
    with pytest.raises(OSError):
        tooltbl.write_comment(str(tmp_path / "nope.tbl"), 1, 0.0, 0.0, "")
