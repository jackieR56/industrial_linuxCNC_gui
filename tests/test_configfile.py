import os
import shutil

import pytest

import configfile
from configfile import HalDoc, IniDoc, LineDoc, hal_tokenize

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
ALL = ["crlf.ini", "notrailing.ini", "machine.ini", "io.hal",
       "ethercat-conf.xml", "show_pin.txt"]


def fix(name):
    return os.path.join(FIX, name)


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def enc(doc):
    return doc.text().encode("utf-8", "surrogateescape")


@pytest.fixture
def copy(tmp_path):
    def _copy(name):
        dst = tmp_path / name
        shutil.copy(fix(name), dst)
        return str(dst)
    return _copy


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("cls", [LineDoc, IniDoc, HalDoc])
def test_round_trip_byte_exact(cls, name):
    assert enc(cls(fix(name))) == raw(fix(name))


def test_fixture_properties():
    assert b"\r\n" in raw(fix("crlf.ini"))
    assert not raw(fix("notrailing.ini")).endswith(b"\n")
    assert LineDoc(fix("notrailing.ini")).trailing_nl is False


def test_load_doc_dispatch():
    assert isinstance(configfile.load_doc(fix("machine.ini")), IniDoc)
    assert isinstance(configfile.load_doc(fix("io.hal")), HalDoc)
    assert configfile.load_doc(fix("show_pin.txt")).kind == "text"


def test_non_utf8_bytes_round_trip(tmp_path):
    p = tmp_path / "latin.ini"
    data = b"[A]\nNAME = caf\xe9\n"
    p.write_bytes(data)
    assert enc(IniDoc(str(p))) == data


# ---------------------------------------------------------------------------
# IniDoc
# ---------------------------------------------------------------------------
def test_ini_find():
    d = IniDoc(fix("machine.ini"))
    assert d.find("EMC", "MACHINE") == "test-mill"
    assert d.find("DISPLAY", "BP_TOOL_PALETTE") == "255,255,255; 255,210,60"
    assert d.find("DISPLAY", "EMPTY_KEY") == ""
    assert d.find("HAL", "HALFILE") == "core.hal"       # first of repeats
    assert d.find("TRAJ", "MAX_LINEAR_VELOCITY") == "400"
    assert d.find("EMC", "NOPE") is None
    halfiles = [r.value for r in d.recs
                if r.kind == "key" and r.section == "HAL" and r.key == "HALFILE"]
    assert halfiles == ["core.hal", "io.hal"]


def test_ini_crlf():
    d = IniDoc(fix("crlf.ini"))
    assert d.find("EMC", "MACHINE") == "crlf-mill"
    assert [s for _i, s in d.sections()] == ["EMC", "HAL"]
    i = next(r.i for r in d.recs if r.key == "MACHINE")
    assert d.set_value(i, "other")
    assert d.lines[i] == "MACHINE = other\r"
    assert enc(d) == raw(fix("crlf.ini")).replace(b"crlf-mill", b"other")


def test_ini_sections():
    d = IniDoc(fix("machine.ini"))
    assert d.sections() == [(2, "EMC"), (6, "DISPLAY"), (11, "HAL"),
                            (16, "TRAJ")]


def test_ini_record_kinds():
    d = IniDoc(fix("machine.ini"))
    assert d.recs[0].kind == "comment"
    assert d.recs[1].kind == "comment"
    assert d.recs[5].kind == "blank"


def _line(d, key):
    return next(r.i for r in d.recs if r.kind == "key" and r.key == key)


def test_ini_set_value_keeps_inline_comment():
    d = IniDoc(fix("machine.ini"))
    i = _line(d, "MACHINE")
    assert d.set_value(i, "  new-mill ")
    assert d.lines[i] == "MACHINE    = new-mill   # inline comment"
    assert d.find("EMC", "MACHINE") == "new-mill"
    assert d.dirty


def test_ini_set_value_on_empty_key():
    d = IniDoc(fix("machine.ini"))
    i = _line(d, "EMPTY_KEY")
    assert d.set_value(i, "x")
    assert d.lines[i] == "EMPTY_KEY = x"
    assert d.find("DISPLAY", "EMPTY_KEY") == "x"


def test_ini_clear_value():
    d = IniDoc(fix("machine.ini"))
    i = _line(d, "DEBUG")
    assert d.set_value(i, "")
    assert d.lines[i] == "DEBUG      = "
    assert d.find("EMC", "DEBUG") == ""
    assert d.set_value(i, "1")
    assert d.lines[i] == "DEBUG      = 1"


def test_ini_set_value_semicolon_value():
    d = IniDoc(fix("machine.ini"))
    i = _line(d, "BP_TOOL_PALETTE")
    assert d.set_value(i, "1,2,3; 4,5,6")
    assert d.find("DISPLAY", "BP_TOOL_PALETTE") == "1,2,3; 4,5,6"


def test_ini_set_value_non_key():
    d = IniDoc(fix("machine.ini"))
    assert d.set_value(2, "x") is False            # section line
    assert not d.dirty


def test_ini_line_edits_reparse():
    d = IniDoc(fix("machine.ini"))
    d.insert_line(3, "NEWKEY = 7")
    assert d.find("EMC", "NEWKEY") == "7"
    d.delete_line(3)
    assert enc(d) == raw(fix("machine.ini"))


# ---------------------------------------------------------------------------
# hal_tokenize
# ---------------------------------------------------------------------------
def test_tokenize_comment_tail():
    ws, toks, tail, cont = hal_tokenize("setp a.b 1   # note")
    assert toks == ["setp", "a.b", "1"]
    assert ws == ["", " ", " "]
    assert tail == "   # note"
    assert not cont


def test_tokenize_continuation():
    ws, toks, tail, cont = hal_tokenize("loadrt foo \\")
    assert toks == ["loadrt", "foo"]
    assert cont and tail == ""


def test_tokenize_quoted():
    _ws, toks, _tail, _c = hal_tokenize('setp "a b.c" 2')
    assert toks == ["setp", '"a b.c"', "2"]


def test_tokenize_crlf_and_trailing_ws():
    ws, toks, tail, cont = hal_tokenize("net x a => b  \r")
    assert toks == ["net", "x", "a", "=>", "b"]
    assert tail == "  \r"
    assert "".join(w + t for w, t in zip(ws, toks)) + tail == "net x a => b  \r"


# ---------------------------------------------------------------------------
# HalDoc
# ---------------------------------------------------------------------------
def _rec(d, prefix):
    return next(r for r in d.recs if d.lines[r.i].startswith(prefix))


def test_hal_records():
    d = HalDoc(fix("io.hal"))
    assert d.recs[0].kind == "comment"
    assert d.recs[1].kind == "cmd" and d.recs[1].cont
    assert d.recs[2].kind == "cont" and d.recs[2].toks == ["bar=1"]
    assert d.recs[-1].kind == "comment"                # indented comment
    net = _rec(d, "net estop-loop")
    assert net.tail == "   # estop chain"
    assert net.pin_names() == ["estop-loop", "iocontrol.0.user-enable-out",
                               "iocontrol.0.emc-enable-in"]


def test_hal_cells():
    d = HalDoc(fix("io.hal"))
    net = _rec(d, "net estop-loop")
    c = d.cells(net)
    assert (c[0].label, c[0].text, c[0].editable) == ("SIGNAL", "estop-loop", True)
    assert c[1].text == "iocontrol.0.user-enable-out => iocontrol.0.emc-enable-in"
    c = d.cells(_rec(d, "setp foo.0.gain"))
    assert [(x.label, x.text) for x in c] == [("PIN", "foo.0.gain"),
                                              ("VALUE", "1.5")]
    c = d.cells(_rec(d, "loadusr -Wn"))
    assert [(x.label, x.text) for x in c] == [("COMP", "vpanel.py"),
                                              ("ARGS", "--sim")]
    c = d.cells(_rec(d, "loadrt foo"))                 # continued: read-only
    assert c[0].text == "foo" and not c[0].editable
    c = d.cells(_rec(d, "addf"))
    assert [(x.label, x.text) for x in c] == [("FUNC", "foo.0"),
                                              ("THREAD", "servo-thread")]
    c = d.cells(d.recs[0])
    assert not c[0].editable and not c[1].editable


def test_hal_fields():
    d = HalDoc(fix("io.hal"))
    f = d.fields()
    assert len(f) == 2 * sum(1 for r in d.recs if r.kind == "cmd")


def test_hal_replace_value_keeps_comment():
    d = HalDoc(fix("io.hal"))
    r = _rec(d, "net estop-loop")
    assert d.replace(r.i, 1, 2, "estop-new")
    assert d.lines[r.i] == ("net estop-new  iocontrol.0.user-enable-out => "
                            "iocontrol.0.emc-enable-in   # estop chain")
    r = _rec(d, "setp foo.0.gain")
    assert d.replace(r.i, 2, 3, "2.5")
    assert d.lines[r.i] == "setp foo.0.gain 2.5"
    r = _rec(d, "net cycle-start")
    assert d.replace(r.i, 2, None, "a.b  =>   c.d e.f")
    assert d.lines[r.i] == "net cycle-start a.b => c.d e.f  # green button"
    assert d.replace(r.i, 1, 2, "   ") is False
    assert d.replace(0, 1, 2, "x") is False


def test_hal_resolve_expand():
    ini = IniDoc(fix("machine.ini"))
    d = HalDoc(fix("io.hal"), ini)
    assert d.resolve("[TRAJ]MAX_LINEAR_VELOCITY") == "400"
    assert d.resolve("plain") is None
    assert d.expand("[TRAJ]MAX_LINEAR_VELOCITY*2") == "400*2"
    assert d.expand("[NO]KEY") == "[NO]KEY"
    assert HalDoc(fix("io.hal")).expand("[TRAJ]MAX_LINEAR_VELOCITY") == \
        "[TRAJ]MAX_LINEAR_VELOCITY"


# ---------------------------------------------------------------------------
# atomic_write / backups
# ---------------------------------------------------------------------------
def _leftovers(d):
    return [n for n in os.listdir(d) if n.endswith(".tmp")]


def test_atomic_write_new_file(tmp_path):
    p = str(tmp_path / "a.ini")
    configfile.atomic_write(p, "x = 1\r\n")
    assert raw(p) == b"x = 1\r\n"                      # newline="" keeps CRLF
    assert _leftovers(tmp_path) == []
    assert configfile.list_backups(p) == []


def test_atomic_write_surrogateescape(tmp_path):
    src = tmp_path / "s.ini"
    src.write_bytes(b"N = caf\xe9\n")
    d = IniDoc(str(src))
    configfile.atomic_write(str(src), d.text())
    assert raw(str(src)) == b"N = caf\xe9\n"


def test_atomic_write_preserves_mode(tmp_path):
    p = tmp_path / "m.ini"
    p.write_text("a")
    os.chmod(p, 0o640)
    configfile.atomic_write(str(p), "b")
    assert os.stat(p).st_mode & 0o777 == 0o640


def test_backup_rotation_after_seven_writes(tmp_path):
    p = str(tmp_path / "r.ini")
    for n in range(7):
        configfile.atomic_write(p, f"v{n}\n")
    assert raw(p) == b"v6\n"
    names = sorted(os.listdir(tmp_path))
    assert names == ["r.ini", "r.ini.bak", "r.ini.bak.1", "r.ini.bak.2",
                     "r.ini.bak.3", "r.ini.bak.4"]
    assert raw(p + ".bak") == b"v5\n"
    for k, v in zip(range(1, 5), (4, 3, 2, 1)):
        assert raw(f"{p}.bak.{k}") == f"v{v}\n".encode()
    assert configfile.backup_path(p) == p + ".bak"
    assert configfile.list_backups(p) == [p + ".bak"] + [
        f"{p}.bak.{k}" for k in range(1, 5)]


def test_rotation_skips_missing_intermediates(tmp_path):
    p = str(tmp_path / "g.ini")
    for name, text in (("g.ini", "cur"), ("g.ini.bak", "b0"),
                       ("g.ini.bak.2", "b2")):
        (tmp_path / name).write_text(text)
    configfile.rotate_backups(p, keep=5)
    assert raw(p + ".bak") == b"cur"
    assert raw(p + ".bak.1") == b"b0"
    assert not os.path.exists(p + ".bak.2")
    assert raw(p + ".bak.3") == b"b2"
    assert configfile.list_backups(p) == [p + ".bak", p + ".bak.1",
                                          p + ".bak.3"]


def test_write_with_backup_wrapper(tmp_path):
    p = str(tmp_path / "w.ini")
    configfile.write_with_backup(p, "one")
    configfile.write_with_backup(p, "two")
    assert raw(p) == b"two" and raw(p + ".bak") == b"one"


def test_atomic_write_failure_leaves_original(tmp_path, monkeypatch):
    p = str(tmp_path / "f.ini")
    with open(p, "wb") as f:
        f.write(b"original\n")

    def boom(*_a, **_k):
        raise OSError("disk on fire")
    monkeypatch.setattr(configfile.os, "replace", boom)
    with pytest.raises(OSError):
        configfile.atomic_write(p, "new\n", backup=False)
    assert raw(p) == b"original\n"
    assert _leftovers(tmp_path) == []


def test_atomic_write_failure_in_write(tmp_path):
    p = str(tmp_path / "e.ini")
    with open(p, "wb") as f:
        f.write(b"original\n")
    with pytest.raises(UnicodeEncodeError):
        configfile.atomic_write(p, "€", backup=False, encoding="ascii",
                                errors="strict")
    assert raw(p) == b"original\n"
    assert _leftovers(tmp_path) == []


def test_copy_and_sync(tmp_path):
    src = fix("crlf.ini")
    dst = str(tmp_path / "usb" / "crlf.ini")
    os.mkdir(tmp_path / "usb")
    configfile.copy_and_sync(src, dst)
    assert raw(dst) == raw(src)
    configfile.copy_and_sync(fix("notrailing.ini"), dst)   # overwrite
    assert raw(dst) == raw(fix("notrailing.ini"))
    assert os.listdir(tmp_path / "usb") == ["crlf.ini"]     # no .bak, no tmp


def test_copy_and_sync_missing_src(tmp_path):
    with pytest.raises(FileNotFoundError):
        configfile.copy_and_sync(str(tmp_path / "nope"), str(tmp_path / "d"))
    assert os.listdir(tmp_path) == []


def test_restore_backup(tmp_path):
    p = str(tmp_path / "x.hal")
    configfile.atomic_write(p, "old\n")
    configfile.atomic_write(p, "new\n")
    configfile.restore_backup(p)
    assert raw(p) == b"old\n"
    assert configfile.list_backups(p) == [p + ".bak"]      # no rotation
    assert _leftovers(tmp_path) == []


def test_restore_backup_missing(tmp_path):
    p = str(tmp_path / "y.hal")
    with open(p, "w") as f:
        f.write("keep")
    with pytest.raises(FileNotFoundError):
        configfile.restore_backup(p)
    assert raw(p) == b"keep"
