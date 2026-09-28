#!/usr/bin/env python3
# iomap.py — model behind the SYSTEM PHYS / HAL IO viewer: the io_map.yaml
#            point list, discovery that writes it, and the live sampler.
#
#   PHYS  field devices (EtherCAT slaves, userspace drivers such as a Modbus
#         VFD or the sim vpanel, RT hardware drivers), one group per device
#   HAL   symbolic signals, grouped by hal file + section header, plus the
#         standard motion / iocontrol / joint pins
#
# io_map.yaml is generated from the running HAL (plus ethercat-conf.xml and
# the hal files) only when it does not exist; after that it is the user's
# file and is only read. Discovery is a list of plain functions, one per
# device family (PHYS_DISCOVERERS / HAL_DISCOVERERS) — add one for a new bus.
#
# Read-only: nothing here writes HAL.
#
# No SDL imports here — this module is unit-testable on its own.

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime

import configfile

try:
    import yaml
except ImportError:
    yaml = None


MAP_VERSION = 1

# ---------------------------------------------------------------------------
# Live HAL description (for discovery and the detail line)
# ---------------------------------------------------------------------------
HAL_TYPES = {1: "bit", 2: "float", 3: "s32", 4: "u32", 5: "port",
             6: "s64", 7: "u64"}
HAL_DIRS = {16: "IN", 32: "OUT", 48: "I/O"}
LINK_ARROWS = ("<==", "==>", "<=>")


def parse_value(text):
    """halcmd value text -> bool / int / float (None if unparsable)."""
    t = text.strip()
    if t == "TRUE":
        return True
    if t == "FALSE":
        return False
    try:
        return int(t, 0)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return None


class PinInfo:
    __slots__ = ("name", "owner", "type", "dir", "value", "signal")

    def __init__(self, name, owner, type_, dir_, value, signal=None):
        self.name, self.owner, self.type, self.dir = name, owner, type_, dir_
        self.value, self.signal = value, signal


class SigInfo:
    __slots__ = ("name", "type", "value", "driver", "readers")

    def __init__(self, name, type_, value, driver=None, readers=()):
        self.name, self.type, self.value = name, type_, value
        self.driver, self.readers = driver, list(readers)


class HalInfo:
    """Pins, signals and components of a running HAL, from halcmd -s."""

    def __init__(self, pins=None, sigs=None, comps=None):
        self.pins = pins or {}          # name -> PinInfo (HAL order)
        self.sigs = sigs or {}          # name -> SigInfo
        self.comps = comps or {}        # name -> "User" | "RT"

    def __bool__(self):
        return bool(self.pins)

    @classmethod
    def from_text(cls, pin_text="", sig_text="", comp_text=""):
        return cls(parse_show_pin(pin_text), parse_show_sig(sig_text),
                   parse_show_comp(comp_text))

    @classmethod
    def from_halcmd(cls, hal=None):
        hal = hal or configfile.Hal()
        if not hal.available():
            return cls()
        texts = []
        for what in ("pin", "sig", "comp"):
            rc, out, _e = hal.run("-s", "show", what)
            texts.append(out if rc == 0 else "")
        return cls.from_text(*texts)

    def with_prefix(self, prefix):
        return [p for n, p in self.pins.items() if n.startswith(prefix)]

    def owned_by(self, comp):
        return [p for p in self.pins.values() if p.owner == comp]


def parse_show_pin(text):
    """'owner type dir value name [arrow signal]' per row."""
    pins = {}
    for row in text.splitlines():
        p = row.split()
        if len(p) < 5:
            continue
        sig = p[6] if len(p) >= 7 and p[5] in LINK_ARROWS else None
        pins[p[4]] = PinInfo(p[4], p[0], p[1], p[2], parse_value(p[3]), sig)
    return pins


def parse_show_sig(text):
    """'type value name [==> reader | <== driver ...]' per row (script
    mode puts the links on the signal's own row)."""
    sigs = {}
    for row in text.splitlines():
        p = row.split()
        if len(p) < 3 or p[0] in LINK_ARROWS:
            continue
        driver, readers = None, []
        for arrow, name in zip(p[3::2], p[4::2]):
            if arrow == "<==":
                driver = name
            else:
                readers.append(name)
        sigs[p[2]] = SigInfo(p[2], p[0], parse_value(p[1]), driver, readers)
    return sigs


def parse_show_comp(text):
    """'id type name [pid] state' per row."""
    comps = {}
    for row in text.splitlines():
        p = row.split()
        if len(p) >= 3 and p[1] in ("User", "RT"):
            comps[p[2]] = p[1]
    return comps


# ---------------------------------------------------------------------------
# Hal files: net comments, section headers, loadusr lines, lcec xml
# ---------------------------------------------------------------------------
# "# ======== MODE" / "# ---- hard panel DI" -> section header text
_HEADER = re.compile(r"^\s*#\s*[-=#*~]{4,}\s*([A-Za-z].*?)\s*[-=#*~]*\s*$")


def _comment(tail):
    t = tail.strip()
    return t.lstrip("#").strip() if t.startswith("#") else ""


class HalNet:
    __slots__ = ("signal", "pins", "file", "header", "comment")

    def __init__(self, signal, pins, file, header, comment):
        self.signal, self.pins, self.file = signal, pins, file
        self.header, self.comment = header, comment


class HalFiles:
    """Parsed hal files of the running config, in load order."""

    def __init__(self, paths):
        self.paths = []
        self.nets = []                  # HalNet, file order
        self.loadusr = []               # (comp name or None, argv tokens)
        for path in paths:
            try:
                doc = configfile.HalDoc(path)
            except OSError:
                continue
            self.paths.append(path)
            self._scan(doc, os.path.splitext(os.path.basename(path))[0])

    def _scan(self, doc, stem):
        header = ""
        for r in doc.recs:
            if r.kind == "comment":
                m = _HEADER.match(r.tail)
                if m:
                    header = m.group(1)
                continue
            if r.kind != "cmd":
                continue
            if r.cmd == "net" and len(r.toks) >= 2:
                self.nets.append(HalNet(r.toks[1], r.pin_names()[1:], stem,
                                        header, _comment(r.tail)))
            elif r.cmd == "loadusr":
                toks = r.toks[1:]
                name = None
                k = 0
                while k < len(toks) and toks[k].startswith("-"):
                    if toks[k] == "-Wn" and k + 1 < len(toks):
                        name = toks[k + 1]
                        k += 2
                    else:
                        k += 1
                self.loadusr.append((name, toks[k:]))

    def comment_for(self, name):
        """Comment of the net line that names this pin or signal."""
        for n in self.nets:
            if n.comment and (n.signal == name or name in n.pins):
                return n.comment
        return ""

    def net_for_pin(self, pin):
        for n in self.nets:
            if pin in n.pins:
                return n
        return None

    def lcec_xml(self):
        """Path argument of 'loadusr ... lcec_conf <xml>', if any."""
        for _name, argv in self.loadusr:
            for k, tok in enumerate(argv):
                if os.path.basename(tok) == "lcec_conf" and k + 1 < len(argv):
                    return argv[k + 1]
        return None


def hal_file_paths(config_dir, ini_doc=None):
    """HALFILEs of the ini (in order) + POSTGUI_HALFILE, then postgui.hal
    and ethercat.hal from the config dir if not already listed."""
    names = []
    if ini_doc is not None:
        for r in ini_doc.recs:
            if (r.kind == "key" and r.section == "HAL"
                    and r.key in ("HALFILE", "POSTGUI_HALFILE") and r.value):
                names.append(r.value.split()[0])
    names += ["postgui.hal", "ethercat.hal"]
    out = []
    for n in names:
        p = n if os.path.isabs(n) else os.path.join(config_dir, n)
        p = os.path.abspath(os.path.expanduser(p))
        if p not in out and p.endswith(".hal") and os.path.isfile(p):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# EtherCAT (linuxcnc-ethercat) xml
# ---------------------------------------------------------------------------
class XmlSlave:
    __slots__ = ("master", "idx", "type", "name", "vid", "pid", "pins")

    def __init__(self, master, idx, type_, name, vid="", pid="", pins=()):
        self.master, self.idx, self.type, self.name = master, idx, type_, name
        self.vid, self.pid, self.pins = vid, pid, list(pins)   # (halPin, dir)

    @property
    def prefix(self):
        return f"lcec.{self.master}.{self.name}"


def parse_lcec_xml(path):
    """[XmlSlave] in bus order. Generic slaves carry their PDO halPins
    (dir 'I' = input from the field, 'O' = output to it)."""
    root = ET.parse(path).getroot()
    masters = [root] if root.tag == "master" else root.iter("master")
    out = []
    for m in masters:
        midx = m.get("idx", "0")
        for s in m.iter("slave"):
            idx = s.get("idx", "")
            sl = XmlSlave(midx, idx, s.get("type", ""), s.get("name") or idx,
                          s.get("vid", ""), s.get("pid", ""))
            for sm in s.iter("syncManager"):
                d = "O" if sm.get("dir", "").lower() == "out" else "I"
                for e in sm.iter():
                    if e.tag in ("pdoEntry", "complexEntry") and e.get("halPin"):
                        sl.pins.append((e.get("halPin"), d))
            out.append(sl)
    return out


# Beckhoff terminal descriptions for the sidebar / title. Anything not here
# shows its type string — extend freely.
DEVICE_INFO = {
    "EK1100": "EtherCAT coupler",
    "EK1110": "EtherCAT extension",
    "EK1122": "EtherCAT junction, 2 port",
    "EL1002": "2-ch digital input 24 V",
    "EL1004": "4-ch digital input 24 V",
    "EL1008": "8-ch digital input 24 V",
    "EL1018": "8-ch digital input 24 V, 10 us",
    "EL1088": "8-ch digital input 24 V, negative",
    "EL1809": "16-ch digital input 24 V",
    "EL1819": "16-ch digital input 24 V, 10 us",
    "EL1859": "8-ch digital in + 8-ch digital out 24 V",
    "EL2002": "2-ch digital output 24 V",
    "EL2004": "4-ch digital output 24 V",
    "EL2008": "8-ch digital output 24 V",
    "EL2022": "2-ch digital output 24 V 2 A",
    "EL2024": "4-ch digital output 24 V 2 A",
    "EL2088": "8-ch digital output 24 V, ground switching",
    "EL2622": "2-ch relay output",
    "EL2624": "4-ch relay output",
    "EL2809": "16-ch digital output 24 V",
    "EL2819": "16-ch digital output 24 V, diagnostics",
    "EL3001": "1-ch analog input +/-10 V",
    "EL3102": "2-ch analog input +/-10 V",
    "EL3162": "2-ch analog input 0-10 V",
    "EL3164": "4-ch analog input 0-10 V",
    "EL3204": "4-ch analog input PT100",
    "EL4002": "2-ch analog output 0-10 V",
    "EL4032": "2-ch analog output +/-10 V",
    "EL4104": "4-ch analog output 0-10 V",
    "EL5101": "incremental encoder interface",
    "EL5151": "incremental encoder interface",
    "EL6001": "serial interface RS232",
    "EL6021": "serial interface RS422/485",
    "EL6900": "TwinSAFE logic",
    "EL9505": "power supply 5 V",
    "EL9410": "E-bus power supply",
}

# Pin-suffix descriptions, first match wins. {0} = first regex group.
PIN_DESC = [
    (r"din-(\d+)-not", "digital input {0}, inverted"),
    (r"din-(\d+)", "digital input {0}"),
    (r"dout-(\d+)", "digital output {0}"),
    (r"ain-(\d+)-val", "analog input {0}, scaled"),
    (r"ain-(\d+)-raw", "analog input {0}, raw counts"),
    (r"ain-(\d+)-(error|overrange|underrange)", "analog input {0} {1}"),
    (r"aout-(\d+)-value", "analog output {0}"),
    (r"enc-count", "encoder counts"),
    (r"enc-pos", "encoder position, scaled"),
    (r"slave-online", "slave answers on the bus"),
    (r"slave-oper", "slave is operational"),
    (r"slave-state-(\w+)", "slave AL state {0}"),
    (r"link-up", "EtherCAT link up"),
    (r"all-op", "all slaves operational"),
    (r"slaves-responding", "slaves responding"),
]
_PIN_DESC = [(re.compile(r"(?:^|\.)" + p + r"$"), d) for p, d in PIN_DESC]


def pin_desc(tag):
    for rx, d in _PIN_DESC:
        m = rx.search(tag)
        if m:
            return d.format(*m.groups())
    return ""


_NUM = re.compile(r"(\d+)")


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in _NUM.split(s)]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class Context:
    """Everything the discoverers look at. `hal` may be empty (no HAL
    running) — discoverers then fall back to what the files say."""

    def __init__(self, config_dir, hal=None, files=None, ini_doc=None):
        self.config_dir = config_dir
        self.hal = hal or HalInfo()
        self.ini_doc = ini_doc
        self.files = files or HalFiles(hal_file_paths(config_dir, ini_doc))
        self.claimed = set()            # pins already placed in a PHYS device

    @classmethod
    def for_config(cls, ini_path, hal=None):
        config_dir = os.path.dirname(os.path.abspath(ini_path))
        try:
            ini_doc = configfile.IniDoc(ini_path)
        except OSError:
            ini_doc = None
        return cls(config_dir, hal, None, ini_doc)


def _phys_point(ctx, name, prefix, dir_=None):
    """Point dict for a device pin; dir from the device side (a HAL OUT pin
    of a driver is a physical input)."""
    info = ctx.hal.pins.get(name)
    tag = name[len(prefix) + 1:] if name.startswith(prefix + ".") else name
    if dir_ is None and info is not None:
        dir_ = {"OUT": "I", "IN": "O"}.get(info.dir, "IO")
    sig = info.signal if info is not None else None
    if sig is None:
        net = ctx.files.net_for_pin(name)
        sig = net.signal if net else None
    desc = ctx.files.comment_for(name) or (ctx.files.comment_for(sig) if sig else "")
    d = {"pin": name, "tag": tag}
    if dir_:
        d["dir"] = dir_
    d["label"] = sig or tag
    d["desc"] = desc or pin_desc(tag)
    ctx.claimed.add(name)
    return d


def _device_points(ctx, names, prefix, dirs=None):
    # device pins first, bus bookkeeping (slave-*) last
    names = sorted(names, key=lambda n: (".slave-" in n, natural_key(n)))
    return [_phys_point(ctx, n, prefix, (dirs or {}).get(n)) for n in names]


def discover_ethercat(ctx):
    xml = ctx.files.lcec_xml()
    if xml and not os.path.isabs(xml):
        xml = os.path.join(ctx.config_dir, xml)
    if not xml or not os.path.isfile(xml):
        xml = os.path.join(ctx.config_dir, "ethercat-conf.xml")
    slaves = []
    if os.path.isfile(xml):
        try:
            slaves = parse_lcec_xml(xml)
        except (OSError, ET.ParseError):
            slaves = []
    live = [p.name for p in ctx.hal.pins.values()
            if p.owner == "lcec" or p.name.startswith("lcec.")]
    if not slaves and not live:
        return []

    devices = []
    # master-level pins: lcec.<m>.<pin> (no slave segment)
    masters = sorted({s.master for s in slaves} |
                     {n.split(".")[1] for n in live if n.count(".") >= 2},
                     key=natural_key)
    for m in masters:
        pre = f"lcec.{m}"
        names = [n for n in live if n.startswith(pre + ".") and n.count(".") == 2]
        if not names:
            continue
        status = [n for n in (pre + ".link-up", pre + ".all-op") if n in names]
        devices.append({"device": f"{pre} MASTER", "type": "lcec",
                        "desc": "EtherCAT master", "source": pre,
                        "status": status,
                        "points": _device_points(ctx, names, pre)})

    seen = set()
    for s in slaves:
        pre = s.prefix
        seen.add(pre)
        names = [n for n in live if n.startswith(pre + ".")]
        dirs = {}
        if not names:                  # HAL not up: generic PDO pins from xml
            names = [f"{pre}.{p}" for p, _d in s.pins]
            dirs = {f"{pre}.{p}": d for p, d in s.pins}
        desc = DEVICE_INFO.get(s.type.upper(), "")
        if s.type.lower() == "generic":
            desc = f"generic slave vid {s.vid} pid {s.pid}".strip()
        if not names:
            desc = (desc + " - no pins in HAL").strip(" -")
        devices.append({"device": s.name, "type": s.type, "desc": desc,
                        "source": pre,
                        "status": [n for n in (pre + ".slave-online",
                                               pre + ".slave-oper")
                                   if n in names],
                        "points": _device_points(ctx, names, pre, dirs)})

    # live slaves the xml didn't name (or no xml at all)
    extra = sorted({".".join(n.split(".")[:3]) for n in live
                    if n.count(".") >= 3} - seen, key=natural_key)
    for pre in extra:
        names = [n for n in live if n.startswith(pre + ".")]
        devices.append({"device": pre.split(".", 2)[2], "type": "lcec",
                        "desc": "", "source": pre,
                        "status": [n for n in (pre + ".slave-online",
                                               pre + ".slave-oper")
                                   if n in names],
                        "points": _device_points(ctx, names, pre)})
    return devices


# Userspace components that are part of LinuxCNC or this GUI, not devices.
CORE_USER_COMPS = ("halui", "iocontrol", "status-pane", "lcec_conf",
                   "inihal", "axisui", "gscreen", "gmoccapy", "qtvcp")


def _is_core_user(name):
    return (name in CORE_USER_COMPS or name.startswith("halcmd")
            or name.startswith("__"))


def _split_device(ctx, comp, names, desc):
    """One device per component; split by the second name segment when the
    component clearly has channels (vpanel.hp.* / vpanel.op.*)."""
    subs = {}
    for n in names:
        parts = n.split(".")
        key = parts[1] if len(parts) >= 3 else None
        subs.setdefault(key, []).append(n)
    if None not in subs and 2 <= len(subs) <= 12:
        return [{"device": f"{comp}.{k}", "type": comp, "desc": desc,
                 "source": f"{comp}.{k}", "status": [],
                 "points": _device_points(ctx, v, f"{comp}.{k}")}
                for k, v in subs.items()]
    return [{"device": comp, "type": comp, "desc": desc, "source": comp,
             "status": [], "points": _device_points(ctx, names, comp)}]


def discover_userspace(ctx):
    """loadusr drivers: Modbus VFDs (vfdmod, hy_vfd, mb2hal...), the sim
    vpanel, hal_input... One device per component."""
    comps = [c for c, t in ctx.hal.comps.items() if t == "User"]
    if not comps:                           # no live HAL: go by the files
        comps = [n for n, _argv in ctx.files.loadusr if n]
    out = []
    for comp in comps:
        if _is_core_user(comp):
            continue
        names = [p.name for p in ctx.hal.owned_by(comp)
                 if p.name not in ctx.claimed]
        if not names:
            continue
        out += _split_device(ctx, comp, names, "userspace component")
    return out


# RT hardware drivers (component name prefixes).
RT_HW_PREFIXES = ("hm2_", "hostmot2", "hal_parport", "parport", "hal_gpio",
                  "hal_pi_gpio", "hal_bb_gpio", "hal_ppmc", "opto_ac5",
                  "pluto_", "mesa_", "serport")


def discover_rt_hw(ctx):
    out = []
    for comp, t in ctx.hal.comps.items():
        if t != "RT" or not comp.startswith(RT_HW_PREFIXES):
            continue
        names = [p.name for p in ctx.hal.owned_by(comp)
                 if p.name not in ctx.claimed]
        if names:
            out += _split_device(ctx, comp, names, "hardware driver")
    return out


def discover_hal_files(ctx):
    """Every net'd signal, grouped '<file> / <section header>'."""
    groups, order, seen = {}, [], set()
    for net in ctx.files.nets:
        if net.signal in seen:
            continue
        if ctx.hal and net.signal not in ctx.hal.sigs:
            continue
        seen.add(net.signal)
        g = f"{net.file} / {net.header}" if net.header else net.file
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append({"pin": net.signal, "label": net.signal,
                          "desc": net.comment})
    out = [{"group": g, "points": groups[g]} for g in order]
    other = [s for s in ctx.hal.sigs if s not in seen]
    if other:
        out.append({"group": "OTHER SIGNALS",
                    "points": [{"pin": s, "label": s, "desc": ""}
                               for s in sorted(other, key=natural_key)]})
    return out


CORE_PINS = (
    "iocontrol.0.emc-enable-in", "iocontrol.0.user-enable-out",
    "iocontrol.0.user-request-enable", "halui.estop.is-activated",
    "halui.machine.is-on", "motion.motion-enabled", "motion.in-position",
    "motion.coord-mode", "motion.teleop-mode", "motion.probe-input",
    "iocontrol.0.tool-prepare", "iocontrol.0.tool-prepared",
    "iocontrol.0.tool-change", "iocontrol.0.tool-changed",
    "iocontrol.0.tool-prep-number", "iocontrol.0.tool-number",
    "spindle.0.on", "spindle.0.forward", "spindle.0.reverse",
    "spindle.0.brake", "spindle.0.at-speed", "spindle.0.inhibit",
    "spindle.0.speed-out", "spindle.0.speed-in",
    "iocontrol.0.coolant-flood", "iocontrol.0.coolant-mist",
    "iocontrol.0.lube",
)
JOINT_PINS = ("amp-enable-out", "amp-fault-in", "homed", "homing",
              "home-sw-in", "pos-lim-sw-in", "neg-lim-sw-in", "f-errored",
              "motor-pos-cmd", "motor-pos-fb", "f-error")


def _core_point(ctx, name):
    info = ctx.hal.pins[name]
    d = {"pin": name, "label": name}
    if info.signal:
        d["desc"] = f"net {info.signal}"
    return d


def discover_core(ctx):
    pins = ctx.hal.pins
    out = []
    pts = [_core_point(ctx, n) for n in CORE_PINS if n in pins]
    if pts:
        out.append({"group": "LINUXCNC", "points": pts})
    j = 0
    while f"joint.{j}.homed" in pins:
        pts = [_core_point(ctx, f"joint.{j}.{s}") for s in JOINT_PINS
               if f"joint.{j}.{s}" in pins]
        out.append({"group": f"JOINT {j}", "points": pts})
        j += 1
    return out


PHYS_DISCOVERERS = [discover_ethercat, discover_userspace, discover_rt_hw]
HAL_DISCOVERERS = [discover_core, discover_hal_files]


def generate(ctx):
    """-> map dict {version, phys: [...], hal: [...]}"""
    phys, hal = [], []
    for fn in PHYS_DISCOVERERS:
        phys += fn(ctx)
    for fn in HAL_DISCOVERERS:
        hal += fn(ctx)
    return {"version": MAP_VERSION, "phys": phys, "hal": hal}


# ---------------------------------------------------------------------------
# YAML out (no dependency) / in (PyYAML)
# ---------------------------------------------------------------------------
YAML_HEADER = """\
# io_map.yaml - point list for the SYSTEM PHYS / HAL IO viewer.
#
# Generated by the GUI from the running HAL, ethercat-conf.xml and the hal
# files, only when this file does not exist. It is yours now: reorder,
# rename, delete. SYSTEM -> PHYS -> (OPRT) -> REGEN rebuilds it (old copy
# kept as io_map.yaml.bak).
#
# phys: list of devices           hal: list of groups
#   device: sidebar name            group: sidebar name
#   type / desc / source: info      points: [...]
#   status: [pins]  all TRUE = green lamp in the sidebar
#   points: [...]
#
# point keys (only pin is required):
#   pin:    HAL pin or signal name
#   tag:    short address shown in the cell (e.g. din-3)
#   dir:    I / O / IO, from the field side
#   label:  cell text               desc: detail line text
#   invert: true  -> lamp lit when the bit is FALSE (NC contact)
#   bits:   {0: RTSO, 3: FAULT}  -> one lamp per named bit of an int word
"""


def _q(s):
    return json.dumps(str(s), ensure_ascii=False)


def _flow(d):
    parts = []
    for k, v in d.items():
        if isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, dict):
            parts.append(f"{k}: {{" + ", ".join(f"{int(b)}: {_q(n)}"
                                                for b, n in v.items()) + "}")
        else:
            parts.append(f"{k}: {_q(v)}")
    return "{" + ", ".join(parts) + "}"


def map_to_yaml(m, stamp=None):
    stamp = stamp or datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [YAML_HEADER, f"# generated {stamp}\n", f"version: {MAP_VERSION}\n"]
    for section, key in (("phys", "device"), ("hal", "group")):
        items = m.get(section) or []
        out.append(f"\n{section}:" + ("\n" if items else " []\n"))
        for g in items:
            out.append(f"  - {key}: {_q(g.get(key, ''))}\n")
            for k in ("type", "desc", "source"):
                if g.get(k):
                    out.append(f"    {k}: {_q(g[k])}\n")
            if section == "phys":
                out.append("    status: [" + ", ".join(_q(s) for s in g.get("status", []))
                           + "]\n")
            pts = g.get("points") or []
            if not pts:
                out.append("    points: []\n")
                continue
            out.append("    points:\n")
            for p in pts:
                out.append(f"      - {_flow(p)}\n")
    return "".join(out)


class Point:
    """One cell. `bit` set = a virtual lamp for one bit of the word `pin`."""
    __slots__ = ("pin", "tag", "label", "desc", "dir", "invert", "bit",
                 "hex", "section", "group",
                 "value", "shown", "missing", "rises", "falls", "changed",
                 "vmin", "vmax")

    def __init__(self, pin, tag="", label="", desc="", dir_="", invert=False,
                 bit=None, hex_=False):
        self.pin, self.tag, self.label, self.desc = pin, tag, label or pin, desc
        self.dir, self.invert, self.bit, self.hex = dir_, invert, bit, hex_
        self.section = self.group = ""
        self.reset()

    def reset(self):
        self.value = self.shown = None
        self.missing = True
        self.rises = self.falls = 0
        self.changed = None             # monotonic time of the last change
        self.vmin = self.vmax = None

    def read(self, values):
        """Raw HAL value -> this point's value (None = not in HAL)."""
        v = values.get(self.pin)
        if v is None or self.bit is None:
            return v
        try:
            return bool((int(v) >> self.bit) & 1)
        except (TypeError, ValueError):
            return None

    def is_bit(self):
        return self.bit is not None or isinstance(self.shown, bool)

    def lit(self):
        """Lamp state for a bit point (after invert), else None."""
        if not isinstance(self.shown, bool):
            return None
        return self.shown != self.invert

    def text(self):
        v = self.shown
        if v is None:
            return "?"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.4g}"
        if self.hex:
            return f"0x{int(v) & 0xFFFFFFFF:04X}"
        return str(v)


class Group:
    __slots__ = ("name", "type", "desc", "source", "status", "points")

    def __init__(self, name, type_="", desc="", source="", status=(),
                 points=()):
        self.name, self.type, self.desc, self.source = name, type_, desc, source
        self.status, self.points = list(status), list(points)


class IoMap:
    def __init__(self, phys=(), hal=(), warnings=()):
        self.phys, self.hal = list(phys), list(hal)
        self.warnings = list(warnings)

    def section(self, name):
        return self.phys if name == "phys" else self.hal

    def points(self):
        for sec in ("phys", "hal"):
            for g in self.section(sec):
                yield from g.points

    def counts(self):
        return (len(self.phys), sum(len(g.points) for g in self.phys),
                len(self.hal), sum(len(g.points) for g in self.hal))


def _points_from(d, where, warn):
    raw = d.get("pin")
    if not isinstance(raw, str) or not raw.strip():
        warn.append(f"{where}: point without pin")
        return []
    pin = raw.strip()
    s = lambda k: str(d.get(k) or "").strip()
    bits = d.get("bits")
    p = Point(pin, s("tag"), s("label"), s("desc"), s("dir").upper(),
              bool(d.get("invert", False)), hex_=bool(bits))
    out = [p]
    if bits is None:
        return out
    if not isinstance(bits, dict):
        warn.append(f"{where}: {pin}: bits must be a mapping")
        return out
    for b, name in bits.items():
        try:
            b = int(b)
        except (TypeError, ValueError):
            warn.append(f"{where}: {pin}: bad bit {b!r}")
            continue
        if not 0 <= b < 64:
            warn.append(f"{where}: {pin}: bit {b} out of range")
            continue
        out.append(Point(pin, f"{p.tag or pin.rsplit('.', 1)[-1]}.{b}",
                         str(name), f"bit {b} of {pin}", p.dir,
                         p.invert, bit=b))
    return out


def map_from_dict(m):
    """Validated IoMap from a loaded yaml document. Bad entries are skipped
    and reported in .warnings; never raises on content."""
    warn = []
    if not isinstance(m, dict):
        return IoMap(warnings=["io_map: top level is not a mapping"])
    out = IoMap()
    for sec, key in (("phys", "device"), ("hal", "group")):
        items = m.get(sec) or []
        if not isinstance(items, list):
            warn.append(f"{sec}: not a list")
            continue
        for gi, g in enumerate(items):
            if not isinstance(g, dict):
                warn.append(f"{sec}[{gi}]: not a mapping")
                continue
            name = str(g.get(key) or g.get("group") or g.get("device")
                       or f"{sec.upper()} {gi + 1}")
            status = g.get("status") or []
            if not isinstance(status, list):
                status = [status]
            grp = Group(name, str(g.get("type") or ""),
                        str(g.get("desc") or ""), str(g.get("source") or ""),
                        [str(x) for x in status if x])
            pts = g.get("points") or []
            if not isinstance(pts, list):
                warn.append(f"{name}: points is not a list")
                pts = []
            for pi, p in enumerate(pts):
                if not isinstance(p, dict):
                    warn.append(f"{name}[{pi}]: point is not a mapping")
                    continue
                for pt in _points_from(p, name, warn):
                    pt.section, pt.group = sec, name
                    grp.points.append(pt)
            out.section(sec).append(grp)
    out.warnings = warn
    return out


def load_map(path):
    """IoMap from a yaml file. Raises OSError, RuntimeError (no PyYAML) or
    ValueError (yaml syntax)."""
    if yaml is None:
        raise RuntimeError("PyYAML missing (apt install python3-yaml)")
    with open(path, encoding="utf-8") as f:
        try:
            doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(str(e).splitlines()[0] if str(e) else "yaml error")
    return map_from_dict(doc)


# ---------------------------------------------------------------------------
# Live values
#
# NEVER poll hal.get_info_pins() / get_info_signals(): in LinuxCNC
# 2.9.0~pre1 they leak every dict they return (~0.5 KB per pin per call).
# At 20 Hz on a real machine that OOM-killed the GUI in minutes.
# hal.get_value() is leak-free (measured) and ~4 us per name.
# ---------------------------------------------------------------------------
MISSING_RECHECK_S = 2.0


def hal_value_source(names_fn, get=None, clock=time.monotonic):
    """In-process reader: hal.get_value() for each name in names_fn().
    Names not in HAL are skipped and retried every MISSING_RECHECK_S, so a
    device that comes up later appears. Returns None if nothing could be
    read (HAL gone). Only valid once this process owns a ready HAL
    component."""
    if get is None:
        import hal
        get = hal.get_value
    missing = set()
    state = {"next": 0.0}

    def read():
        now = clock()
        if now >= state["next"]:
            missing.clear()
            state["next"] = now + MISSING_RECHECK_S
        names = names_fn()
        vals = {}
        for n in names:
            if n in missing:
                continue
            try:
                vals[n] = get(n)
            except RuntimeError:
                missing.add(n)
        if names and not vals:
            return None
        return vals
    return read


def halcmd_source(hal=None):
    hal = hal or configfile.Hal()

    def read():
        snap = hal.snapshot()
        return {k: parse_value(v) for k, v in snap.items()} if snap else None
    return read


class Sampler:
    """Reads every mapped point at most once per `period` seconds, counts
    edges, tracks min/max and keeps a change log. hold freezes what the
    screen shows; counting and the log keep running."""

    EDGE_S = 1.0                        # "recently changed" outline time

    def __init__(self, source, period=0.05, clock=time.monotonic,
                 log_len=200):
        self.source = source
        self.period = period
        self.clock = clock
        self.points = []
        self.log = deque(maxlen=log_len)  # (hh:mm:ss.mmm, section, label, old, new)
        self.hold = False
        self.ok = False                 # last read succeeded
        self._next = 0.0
        self._primed = False

    def attach(self, iomap):
        self.points = list(iomap.points())
        self._primed = False
        self._next = 0.0

    def clear_counts(self):
        for p in self.points:
            p.rises = p.falls = 0
            p.changed = None
            numeric = p.value is not None and not isinstance(p.value, bool)
            p.vmin = p.vmax = p.value if numeric else None
        self.log.clear()

    def recent(self, p, now=None):
        now = self.clock() if now is None else now
        return p.changed is not None and now - p.changed < self.EDGE_S

    def tick(self):
        now = self.clock()
        if now < self._next:
            return False
        self._next = now + self.period
        return self.sample(now)

    def sample(self, now=None):
        now = self.clock() if now is None else now
        try:
            values = self.source()
        except Exception:
            values = None
        self.ok = values is not None
        if values is None:
            return False
        stamp = None
        for p in self.points:
            v = p.read(values)
            p.missing = v is None
            if v is None:
                if not self.hold:
                    p.shown = None
                continue
            old = p.value
            p.value = v
            if not self.hold:
                p.shown = v
            if not isinstance(v, bool):
                p.vmin = v if p.vmin is None else min(p.vmin, v)
                p.vmax = v if p.vmax is None else max(p.vmax, v)
            if not self._primed or old is None or old == v:
                continue
            p.changed = now
            if isinstance(v, bool):
                if v:
                    p.rises += 1
                else:
                    p.falls += 1
            elif v > old:
                p.rises += 1
            else:
                p.falls += 1
            if isinstance(v, float) or (p.hex and p.bit is None):
                continue                # analog noise / words: lamps log bits
            if stamp is None:
                t = datetime.now()
                stamp = t.strftime("%H:%M:%S.") + f"{t.microsecond // 1000:03d}"
            self.log.append((stamp, p.section, p.label, old, v))
        self._primed = True
        return True

    def set_hold(self, on):
        self.hold = on
        if not on:
            for p in self.points:
                p.shown = p.value
