#!/usr/bin/env python3
# configfile.py — line-preserving INI and HAL documents for the SYSTEM
#                 screen editor, plus backup/restore and a halcmd wrapper.
#
# Every document keeps the file as a list of physical lines and re-parses
# records from those lines after each edit, so structured edits and plain
# text edits act on the same data and the file round-trips byte for byte
# when untouched (comments, blank lines, alignment, CRLF, missing trailing
# newline). configparser is deliberately not used: LinuxCNC inis repeat
# keys ([HAL]HALFILE) and configparser would drop comments.
#
# No SDL imports here — this module is unit-testable on its own.

import os
import re
import shutil
import subprocess


# ---------------------------------------------------------------------------
# Base document
# ---------------------------------------------------------------------------
class LineDoc:
    kind = "text"

    def __init__(self, path):
        self.path = path
        self.lines = []
        self.trailing_nl = True
        self.dirty = False
        self.load()

    def load(self):
        with open(self.path, encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            text = f.read()
        self.lines = text.split("\n")
        # "a\nb\n".split -> ["a", "b", ""]: the final "" means a trailing newline
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
            self.trailing_nl = True
        else:
            self.trailing_nl = False
        if not self.lines:
            self.lines = [""]
            self.trailing_nl = True
        self.dirty = False
        self.parse()

    def text(self):
        return "\n".join(self.lines) + ("\n" if self.trailing_nl else "")

    def name(self):
        return os.path.basename(self.path)

    # ---- line edits (TEXT chapter) ----
    def set_line(self, i, s):
        self.lines[i] = s
        self._changed()

    def insert_line(self, i, s):
        self.lines.insert(i, s)
        self._changed()

    def delete_line(self, i):
        self.lines.pop(i)
        if not self.lines:
            self.lines = [""]
        self._changed()

    def _changed(self):
        self.dirty = True
        self.parse()

    # ---- structure (subclasses) ----
    def parse(self):
        self.recs = []

    def fields(self):
        """[(line_index, lo, hi)] editable cells, in display order. lo/hi
        are subclass-specific (HAL token slices; unused for INI)."""
        return []


class TextDoc(LineDoc):
    kind = "text"


# ---------------------------------------------------------------------------
# INI
# ---------------------------------------------------------------------------
class IniRec:
    __slots__ = ("i", "kind", "section", "key", "pre", "value", "tail")

    def __init__(self, i, kind, section=None, key=None, pre="", value="",
                 tail=""):
        self.i, self.kind, self.section = i, kind, section
        self.key, self.pre, self.value, self.tail = key, pre, value, tail


_INI_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*\r?$")
# `pre` swallows the key, the '=', and the spacing after it. Only whitespace
# followed by '#' counts as an inline comment: ';' can be inside a value
# (BP_TOOL_PALETTE = 255,255,255; 255,210,60; ...).
_INI_KEY = re.compile(
    r"^(?P<pre>\s*[^=#;\s][^=]*?\s*=[ \t]*)"
    r"(?P<value>.*?)"
    r"(?P<tail>(?:[ \t]+#.*)?\r?)$")


class IniDoc(LineDoc):
    kind = "ini"

    def parse(self):
        self.recs = []
        section = None
        for i, line in enumerate(self.lines):
            s = line.strip()
            if not s:
                self.recs.append(IniRec(i, "blank", section))
                continue
            if s[0] in "#;":
                self.recs.append(IniRec(i, "comment", section))
                continue
            m = _INI_SECTION.match(line)
            if m:
                section = m.group("name").strip()
                self.recs.append(IniRec(i, "section", section))
                continue
            m = _INI_KEY.match(line)
            if m:
                pre = m.group("pre")
                key = pre.split("=", 1)[0].strip()
                self.recs.append(IniRec(i, "key", section, key, pre,
                                        m.group("value").strip(),
                                        m.group("tail")))
                continue
            self.recs.append(IniRec(i, "other", section))

    def fields(self):
        return [(r.i, 0, 0) for r in self.recs if r.kind == "key"]

    def set_value(self, i, new):
        r = self.recs[i]
        if r.kind != "key":
            return False
        new = new.strip()
        pre = r.pre
        if new and pre.endswith("="):        # "KEY =" -> "KEY = value"
            pre += " "
        self.lines[i] = pre + new + r.tail
        self._changed()
        return True

    def find(self, section, key):
        for r in self.recs:
            if r.kind == "key" and r.section == section and r.key == key:
                return r.value
        return None

    def sections(self):
        return [(r.i, r.section) for r in self.recs if r.kind == "section"]


# ---------------------------------------------------------------------------
# HAL
# ---------------------------------------------------------------------------
class HalRec:
    __slots__ = ("i", "kind", "cmd", "toks", "ws", "tail", "cont")

    def __init__(self, i, kind, cmd=None, toks=(), ws=(), tail="",
                 cont=False):
        self.i, self.kind, self.cmd = i, kind, cmd
        self.toks, self.ws, self.tail, self.cont = list(toks), list(ws), tail, cont

    def render(self):
        out = "".join(w + t for w, t in zip(self.ws, self.toks))
        if self.cont:
            out += " \\"
        return out + self.tail

    def pin_names(self):
        """HAL pin/signal names this statement touches, for PINS/APPLY."""
        if self.cmd == "net":
            return [t for t in self.toks[1:] if t not in HAL_ARROWS]
        if self.cmd in ("setp", "sets", "unlinkp", "getp", "gets"):
            return self.toks[1:2]
        return []


class Cell:
    __slots__ = ("label", "text", "editable", "lo", "hi")

    def __init__(self, label, text, editable, lo, hi):
        self.label, self.text, self.editable = label, text, editable
        self.lo, self.hi = lo, hi


HAL_ARROWS = ("=>", "<=", "<=>")
_HAL_TOK = re.compile(r'([ \t]*)("[^"]*"|\S+)')
_INI_REF = re.compile(r"\[([^\]]+)\]([A-Za-z0-9_.-]+)")


def hal_tokenize(line):
    """-> (ws, toks, tail, cont). ws[k] is the whitespace before toks[k];
    tail is from the first '#' token (with its leading whitespace) to EOL,
    plus a stray '\\r'. A trailing '\\' token sets cont."""
    body = line.rstrip("\r")
    cr = "\r" if line.endswith("\r") else ""
    ws, toks, tail = [], [], ""
    for m in _HAL_TOK.finditer(body):
        if m.group(2).startswith("#"):
            tail = body[m.start():]
            break
        ws.append(m.group(1))
        toks.append(m.group(2))
    else:
        # no comment: keep any trailing whitespace verbatim
        end = sum(len(w) + len(t) for w, t in zip(ws, toks))
        tail = body[end:]
    cont = False
    if toks and toks[-1] == "\\":
        toks.pop()
        ws.pop()
        cont = True
        if not tail.startswith(" "):
            tail = tail.lstrip(" ")
    return ws, toks, tail + cr, cont


class HalDoc(LineDoc):
    kind = "hal"

    def __init__(self, path, ini=None):
        self.ini = ini              # IniDoc for [SEC]KEY resolution (optional)
        super().__init__(path)

    def parse(self):
        self.recs = []
        prev_cont = False
        for i, line in enumerate(self.lines):
            s = line.strip()
            if prev_cont:
                ws, toks, tail, cont = hal_tokenize(line)
                self.recs.append(HalRec(i, "cont", None, toks, ws, tail, cont))
                prev_cont = cont
                continue
            if not s:
                self.recs.append(HalRec(i, "blank", tail=line))
                continue
            if s.startswith("#"):
                self.recs.append(HalRec(i, "comment", tail=line))
                continue
            ws, toks, tail, cont = hal_tokenize(line)
            if not toks:                       # whitespace + comment only
                self.recs.append(HalRec(i, "comment", tail=line))
                continue
            self.recs.append(HalRec(i, "cmd", toks[0], toks, ws, tail, cont))
            prev_cont = cont

    def cells(self, rec):
        """Two Cell objects for a cmd record (a read-only pair otherwise)."""
        t = rec.toks
        n = len(t)
        ro = rec.cont or rec.kind != "cmd"

        def cell(label, lo, hi, editable=True):
            hi_ = n if hi is None else min(hi, n)
            return Cell(label, " ".join(t[lo:hi_]), editable and not ro,
                        lo, hi)

        c = rec.cmd
        if rec.kind != "cmd":
            return [Cell("", "", False, 0, 0), Cell("", "", False, 0, 0)]
        if c == "net":
            return [cell("SIGNAL", 1, 2), cell("PINS", 2, None)]
        if c in ("setp", "sets"):
            return [cell("PIN" if c == "setp" else "SIGNAL", 1, 2),
                    cell("VALUE", 2, 3)]
        if c in ("unlinkp", "getp", "gets", "delsig"):
            return [cell("PIN" if c != "delsig" else "SIGNAL", 1, 2),
                    Cell("", "", False, 2, 2)]
        if c == "loadrt":
            return [cell("COMP", 1, 2), cell("ARGS", 2, None)]
        if c == "loadusr":
            k = 1
            while k < n and t[k].startswith("-"):
                # -W, -Wn NAME, -w, -i: only -Wn takes an argument
                k += 2 if t[k] == "-Wn" else 1
            return [cell("COMP", k, k + 1), cell("ARGS", k + 1, None)]
        if c == "addf":
            return [cell("FUNC", 1, 2), cell("THREAD", 2, None)]
        # start / source / alias / anything else: whole line, read-only
        return [Cell(c.upper(), " ".join(t[1:]), False, 1, None),
                Cell("", "", False, n, n)]

    def fields(self):
        out = []
        for r in self.recs:
            if r.kind != "cmd":
                continue
            for c in self.cells(r):
                out.append((r.i, c.lo, c.hi))
        return out

    def replace(self, i, lo, hi, text):
        """Replace toks[lo:hi] of line i with the tokens of `text`."""
        rec = self.recs[i]
        if rec.kind != "cmd":
            return False
        new_ws, new_toks, _tail, _cont = hal_tokenize(text)
        if not new_toks:
            return False
        n = len(rec.toks)
        hi_ = n if hi is None else min(hi, n)
        lo = min(lo, n)
        first_ws = rec.ws[lo] if lo < n else " "
        ws = [first_ws] + [" "] * (len(new_toks) - 1)
        rec.toks[lo:hi_] = new_toks
        rec.ws[lo:hi_] = ws
        self.lines[i] = rec.render()
        self._changed()
        return True

    def resolve(self, tok):
        """'[TRAJ]MAX_LINEAR_VELOCITY' -> '400' via the ini, else None."""
        if self.ini is None:
            return None
        m = _INI_REF.fullmatch(tok)
        if not m:
            return None
        return self.ini.find(m.group(1), m.group(2))

    def expand(self, tok):
        """Value with [SEC]KEY references substituted (for halcmd setp)."""
        if self.ini is None:
            return tok
        return _INI_REF.sub(
            lambda m: self.ini.find(m.group(1), m.group(2)) or m.group(0), tok)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def load_doc(path, ini=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ini":
        return IniDoc(path)
    if ext == ".hal":
        return HalDoc(path, ini)
    return TextDoc(path)


def backup_path(path):
    return path + ".bak"


def write_with_backup(path, text):
    """<path>.bak = previous content, then atomic replace. Raises OSError."""
    if os.path.exists(path):
        shutil.copy2(path, backup_path(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape",
              newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def restore_backup(path):
    """Copy <path>.bak over <path>. Raises FileNotFoundError / OSError."""
    shutil.copy2(backup_path(path), path)


# ---------------------------------------------------------------------------
# halcmd
# ---------------------------------------------------------------------------
class Hal:
    """Thin halcmd wrapper. Every call is a short subprocess; callers keep
    them off the per-frame path (PINS refreshes on a timer)."""

    def available(self):
        return shutil.which("halcmd") is not None

    def run(self, *args, timeout=1.0):
        try:
            p = subprocess.run(["halcmd", *args], capture_output=True,
                               text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, "", str(e)
        return p.returncode, p.stdout, p.stderr

    def setp(self, pin, value):
        rc, _out, err = self.run("setp", pin, value)
        return rc == 0, (err.strip().splitlines() or [""])[-1]

    def sets(self, sig, value):
        rc, _out, err = self.run("sets", sig, value)
        return rc == 0, (err.strip().splitlines() or [""])[-1]

    def snapshot(self):
        """{pin_or_signal_name: value_text} from one show pin + one show sig.
        -s (script) rows: pins  'owner type dir value name [<= sig]'
                          sigs  'type value name'"""
        vals = {}
        rc, out, _e = self.run("-s", "show", "pin")
        if rc == 0:
            for row in out.splitlines():
                p = row.split()
                if len(p) >= 5:
                    vals[p[4]] = p[3]
        rc, out, _e = self.run("-s", "show", "sig")
        if rc == 0:
            for row in out.splitlines():
                p = row.split()
                if len(p) >= 3:
                    vals[p[2]] = p[1]
        return vals
