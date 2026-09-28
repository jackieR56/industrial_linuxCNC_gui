import os
import shutil

import pytest

import iomap
from iomap import (HalFiles, HalInfo, IoMap, Group, Point, Sampler,
                   parse_lcec_xml, parse_show_comp, parse_show_pin,
                   parse_show_sig, parse_value)

yaml = pytest.importorskip("yaml")

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fix(name):
    return os.path.join(FIX, name)


def text(name):
    with open(fix(name)) as f:
        return f.read()


# ---------------------------------------------------------------------------
# halcmd -s parsers
# ---------------------------------------------------------------------------
def test_parse_value():
    assert parse_value("TRUE") is True
    assert parse_value(" FALSE ") is False
    assert parse_value("0x0000002A") == 42
    assert parse_value("-3") == -3
    assert parse_value("1.5") == 1.5
    assert parse_value("junk") is None


def test_parse_show_pin():
    pins = parse_show_pin(text("show_pin.txt"))
    assert list(pins)[:2] == ["lcec.0.D1.din-0", "lcec.0.D1.din-1"]
    assert "row" not in pins and len(pins) == 7
    p = pins["lcec.0.D1.din-0"]
    assert (p.owner, p.type, p.dir, p.value, p.signal) == \
        ("lcec", "bit", "OUT", True, "cycle-start")
    assert pins["lcec.0.D1.din-1"].signal is None
    assert pins["lcec.0.G2.analog"].value == 1.5
    assert pins["lcec.0.G2.count"].value == 42
    assert pins["iocontrol.0.emc-enable-in"].signal == "estop-loop"


def test_parse_show_sig():
    sigs = parse_show_sig(text("show_sig.txt"))
    assert set(sigs) == {"estop-loop", "cycle-start", "max-vel"}
    s = sigs["cycle-start"]
    assert (s.type, s.value, s.driver) == ("bit", False, "lcec.0.D1.din-0")
    assert s.readers == ["halui.program.run", "foo.0.run"]
    m = sigs["max-vel"]
    assert (m.value, m.driver, m.readers) == (400, None, [])


def test_parse_show_comp():
    comps = parse_show_comp(text("show_comp.txt"))
    assert comps == {"halcmd12345": "User", "vpanel": "User",
                     "lcec": "RT", "trivkins": "RT"}


def test_halinfo_from_text():
    h = HalInfo.from_text(text("show_pin.txt"), text("show_sig.txt"),
                          text("show_comp.txt"))
    assert h
    assert not HalInfo()
    assert [p.name for p in h.with_prefix("iocontrol.")] == [
        "iocontrol.0.user-enable-out", "iocontrol.0.emc-enable-in"]
    assert len(h.owned_by("lcec")) == 4


# ---------------------------------------------------------------------------
# lcec xml, hal files
# ---------------------------------------------------------------------------
def test_parse_lcec_xml():
    slaves = parse_lcec_xml(fix("ethercat-conf.xml"))
    assert [(s.idx, s.type, s.name) for s in slaves] == [
        ("0", "EK1100", "D0"), ("1", "EL1008", "D1"), ("2", "generic", "G2")]
    g = slaves[2]
    assert g.prefix == "lcec.0.G2"
    assert (g.vid, g.pid) == ("0x00000002", "0x0c1e3052")
    assert g.pins == [("dout-0", "O"), ("din-0", "I"), ("word-in", "I")]
    assert slaves[0].pins == []


def test_parse_lcec_xml_single_master(tmp_path):
    p = tmp_path / "one.xml"
    p.write_text('<master idx="1"><slave idx="4" type="EL2008"/></master>')
    (s,) = parse_lcec_xml(str(p))
    assert (s.master, s.name, s.prefix) == ("1", "4", "lcec.1.4")


def test_hal_files():
    f = HalFiles([fix("io.hal"), fix("does-not-exist.hal")])
    assert f.paths == [fix("io.hal")]
    by_sig = {n.signal: n for n in f.nets}
    assert by_sig["estop-loop"].comment == "estop chain"
    assert by_sig["estop-loop"].header == "MODE"
    assert by_sig["cycle-start"].header == "hard panel DI"
    assert by_sig["cycle-start"].pins == ["lcec.0.D1.din-0",
                                          "halui.program.run"]
    assert f.comment_for("halui.program.run") == "green button"
    assert f.net_for_pin("iocontrol.0.emc-enable-in").signal == "estop-loop"
    assert f.loadusr == [("vpanel", ["vpanel.py", "--sim"]),
                         (None, ["lcec_conf", "ethercat-conf.xml"])]
    assert f.lcec_xml() == "ethercat-conf.xml"


def test_hal_file_paths(tmp_path):
    for n in ("machine.ini", "io.hal"):
        shutil.copy(fix(n), tmp_path / n)
    (tmp_path / "postgui.hal").write_text("# empty\n")
    ini = iomap.configfile.IniDoc(str(tmp_path / "machine.ini"))
    paths = iomap.hal_file_paths(str(tmp_path), ini)
    assert paths == [str(tmp_path / "io.hal"), str(tmp_path / "postgui.hal")]


def test_pin_desc_and_natural_key():
    assert iomap.pin_desc("din-3-not") == "digital input 3, inverted"
    assert iomap.pin_desc("lcec.0.D1.ain-2-overrange") == \
        "analog input 2 overrange"
    assert iomap.pin_desc("nothing") == ""
    assert sorted(["d10", "d2", "d1"], key=iomap.natural_key) == \
        ["d1", "d2", "d10"]


def test_generate_from_fixtures(tmp_path):
    for n in ("io.hal", "ethercat-conf.xml"):
        shutil.copy(fix(n), tmp_path / n)
    hal = HalInfo.from_text(text("show_pin.txt"), text("show_sig.txt"),
                            text("show_comp.txt"))
    files = HalFiles([str(tmp_path / "io.hal")])
    ctx = iomap.Context(str(tmp_path), hal, files)
    m = iomap.generate(ctx)
    names = [d["device"] for d in m["phys"]]
    assert names == ["D0", "D1", "G2"]
    d1 = m["phys"][1]
    assert d1["desc"] == "8-ch digital input 24 V"
    pt = d1["points"][0]
    assert (pt["pin"], pt["tag"], pt["dir"], pt["label"], pt["desc"]) == (
        "lcec.0.D1.din-0", "din-0", "I", "cycle-start", "green button")
    groups = [g["group"] for g in m["hal"]]
    assert "LINUXCNC" in groups and "io / MODE" in groups
    # round-trip the generated map through yaml
    back = yaml.safe_load(iomap.map_to_yaml(m, stamp="x"))
    assert back["phys"][1]["points"][0] == pt


# ---------------------------------------------------------------------------
# yaml
# ---------------------------------------------------------------------------
MAP = {
    "version": 1,
    "phys": [{"device": 'VFD "main"', "type": "vfdmod", "desc": "spindle: 1",
              "source": "vfd", "status": ["vfd.ok"],
              "points": [{"pin": "vfd.status", "tag": "status", "dir": "I",
                          "label": "STAT", "desc": "# not a comment",
                          "bits": {0: "RTSO", 3: "FAULT"}},
                         {"pin": "vfd.run", "invert": True}]},
             {"device": "EMPTY", "status": [], "points": []}],
    "hal": [{"group": "MODE", "points": [{"pin": "estop-loop",
                                          "label": "estop-loop",
                                          "desc": "été"}]}],
}


def test_map_to_yaml_round_trip():
    y = iomap.map_to_yaml(MAP, stamp="2026-01-01 00:00")
    assert "# generated 2026-01-01 00:00" in y
    assert yaml.safe_load(y) == MAP


def test_map_to_yaml_empty():
    back = yaml.safe_load(iomap.map_to_yaml({}, stamp="x"))
    assert back == {"version": 1, "phys": [], "hal": []}


def test_map_from_dict_and_load_map(tmp_path):
    p = tmp_path / "io_map.yaml"
    p.write_text(iomap.map_to_yaml(MAP, stamp="x"), encoding="utf-8")
    m = iomap.load_map(str(p))
    assert m.warnings == []
    assert m.counts() == (2, 4, 1, 1)
    status, b0, b3, run = m.phys[0].points
    assert status.hex and status.bit is None
    assert (b0.bit, b0.label, b0.tag) == (0, "RTSO", "status.0")
    assert b3.bit == 3 and run.invert and run.label == "vfd.run"
    assert m.hal[0].points[0].section == "hal"


def test_map_from_dict_warnings():
    m = iomap.map_from_dict({"phys": [{"device": "D", "points": [
        {"tag": "x"}, "str", {"pin": "a", "bits": {"z": "n", 99: "m"}}]}],
        "hal": "nope"})
    assert len(m.warnings) == 5
    assert m.counts() == (1, 1, 0, 0)
    assert iomap.map_from_dict([]).warnings


def test_load_map_bad_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("phys: [\n")
    with pytest.raises(ValueError):
        iomap.load_map(str(p))


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _sampler(points, values, clock):
    s = Sampler(lambda: values.get("v"), period=0.05, clock=clock)
    s.attach(IoMap(hal=[Group("G", points=points)]))
    return s


def test_sampler_edges_and_period():
    clock = Clock()
    bit, num, word = Point("b"), Point("n"), Point("w")
    word_bit = Point("w", bit=2)
    for p in (bit, num, word, word_bit):
        p.section = "hal"
    vals = {"v": {"b": False, "n": 5, "w": 0}}
    s = _sampler([bit, num, word, word_bit], vals, clock)

    assert s.tick()                               # primes, no edges
    assert bit.rises == bit.falls == 0 and s.ok
    assert bit.lit() is False and word_bit.shown is False

    vals["v"] = {"b": True, "n": 7, "w": 4}
    clock.t += 0.01
    assert not s.tick()                           # inside the period
    assert bit.rises == 0

    clock.t += 0.05
    assert s.tick()
    assert bit.rises == 1 and bit.changed == clock.t
    assert num.rises == 1 and (num.vmin, num.vmax) == (5, 7)
    assert word_bit.rises == 1 and word_bit.lit() is True
    assert s.recent(bit)
    assert not s.recent(bit, now=clock.t + Sampler.EDGE_S)

    vals["v"] = {"b": False, "n": 3, "w": 4}
    clock.t += 0.1
    s.tick()
    assert (bit.rises, bit.falls) == (1, 1)
    assert (num.rises, num.falls) == (1, 1) and num.vmin == 3
    assert word_bit.rises == 1 and word_bit.falls == 0

    # log: bool / int changes only, in order
    assert [(e[2], e[3], e[4]) for e in s.log] == [
        ("b", False, True), ("n", 5, 7), ("w", 0, 4), ("w", False, True),
        ("b", True, False), ("n", 7, 3)]

    s.clear_counts()
    assert bit.rises == bit.falls == 0 and not s.log
    assert (num.vmin, num.vmax) == (3, 3)


def test_sampler_hold_and_missing():
    clock = Clock()
    p, gone = Point("b"), Point("gone")
    vals = {"v": {"b": False}}
    s = _sampler([p, gone], vals, clock)
    s.sample()
    assert gone.missing and gone.text() == "?"
    s.set_hold(True)
    vals["v"] = {"b": True}
    s.sample()
    assert p.value is True and p.shown is False and p.rises == 1
    s.set_hold(False)
    assert p.shown is True and p.text() == "1"


def test_sampler_source_failure():
    clock = Clock()

    def bad():
        raise RuntimeError("hal gone")
    s = Sampler(bad, clock=clock)
    s.attach(IoMap())
    assert s.tick() is False and s.ok is False
    s2 = Sampler(lambda: None, clock=clock)
    assert s2.sample() is False and not s2.ok


def test_hal_value_source_injected():
    clock = Clock()
    calls = []

    def get(n):
        calls.append(n)
        if n == "missing":
            raise RuntimeError(n)
        return 1
    read = iomap.hal_value_source(lambda: ["a", "missing"], get, clock)
    assert read() == {"a": 1}
    read()
    assert calls.count("missing") == 1            # skipped until recheck
    clock.t += iomap.MISSING_RECHECK_S
    read()
    assert calls.count("missing") == 2
    assert iomap.hal_value_source(lambda: ["missing"], get, clock)() is None


def test_halcmd_source_fake():
    class FakeHal:
        def snapshot(self):
            return {"a": "TRUE", "b": "2.5"}
    assert iomap.halcmd_source(FakeHal())() == {"a": True, "b": 2.5}


def test_point_text():
    p = Point("x", hex_=True)
    p.shown = 0x2A
    assert p.text() == "0x002A"
    p = Point("y")
    p.shown = 1.23456
    assert p.text() == "1.235"
