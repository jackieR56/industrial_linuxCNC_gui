#!/usr/bin/env python3
# systemscreen.py — SYSTEM screen: config directory browser, plain-text and
#                   structured (fields) editors for the machine ini and hal
#                   files, live HAL helpers, backup/restore, control restart.
#
# Chapters:  DIR  USB  TEXT  FIELDS  INFO  PHYS  HAL
#   DIR/USB  browse the config directory / a USB drive, SELECT a file
#   TEXT     line/word editor over the selected file (ProgScreen EDIT clone)
#   FIELDS   ini:  one value field per KEY = value line
#            hal:  two cells per statement (net: signal|pins, setp: pin|value,
#                  loadrt: comp|args, addf: func|thread), APPLY / PINS live
#   INFO     paths, joints/kins, backup and launcher state, EXIT
#   PHYS     live IO grid of the field devices   } ioview.py, read-only,
#   HAL      live grid of the internal signals   } points from io_map.yaml
#
# Both editors work on one configfile.LineDoc, so edits made in FIELDS show
# in TEXT and vice versa, with a single dirty flag. The doc survives leaving
# the screen; a different file is only opened after a confirm when dirty.
#
# Typing here is raw (case kept): app.input.raw is set while TEXT/FIELDS is
# up and cleared by on_leave(). The main loop ignores the Shift/CapsLock page
# keys while raw is on, so an external keyboard can type capitals and '_'.

import os
import re
import shutil

import linuxcnc
from sdl2 import *

import configfile
import settings
from ioview import IoModel, IoView
from screens import (Screen, K, FieldCursor, draw_line, text_width,
                     WHITE, RED, BLACK, DIM, ACCENT, KEY_HILITE,
                     SOFTKEY_Y, BUFFER_H)

RESTART_FLAG_ENV = "GUI_RESTART_FLAG"     # exported by run_gui.sh


class SystemScreen(Screen):
    CHAPTERS = ("DIR", "USB", "TEXT", "FIELDS", "INFO", "PHYS", "HAL")

    FILE_ROWS = 12
    EDIT_ROWS = 11
    FIELD_ROWS = 10
    FIELD_ROWS_PINS = 5
    ROW_H = 55
    TITLE_Y = 66
    HDR_Y = 112                     # column headers (FIELDS)
    ROW_Y0 = 160                    # first table row (FIELDS)
    NOTE_Y = SOFTKEY_Y - BUFFER_H - 120        # 786, as ProgScreen
    STATUS_Y = NOTE_Y - 50                     # full cell text (FIELDS)
    PINS_Y = 445                    # live panel top when PINS is on
    PINS_ROWS = 4
    PINS_PERIOD_MS = 500

    # FIELDS column layout (x, w)
    INI_KEY_X, INI_KEY_W = 130, 550
    INI_VAL_X, INI_VAL_W = 700, 880
    HAL_CMD_X = 130
    HAL_C1_X, HAL_C1_W = 300, 560
    HAL_C2_X, HAL_C2_W = 880, 700
    TAIL_X, TAIL_W = 1600, 310

    # ------------------------------------------------------------- lifecycle
    def __init__(self, app):
        super().__init__(app)
        self.chapter = "DIR"
        self.config_dir = None
        self.sel_path = None
        self.doc = None
        self.ini_doc = None             # for [SEC]KEY resolution in hal docs
        self.hal = configfile.Hal()
        self.note = ""
        # browser
        self.dir_path = None
        self.usb_path = None
        self.entries = []
        self.file_cur = 0
        self.file_scroll = 0
        # text editor
        self.edit_cur = 0
        self.edit_word = 0
        self.edit_scroll = 0
        # fields
        self.scroll = 0
        self.field_lines = []           # per field: (line, lo, hi)
        self.fields_at = {}             # line -> [field index, ...]
        self._vis_fields = ()           # fields laid out this frame (touch)
        self._vis_rows = ()             # (y, index) rows drawn (touch)
        # live pins
        self.pins_on = False
        self.pins_cache = {}
        self.pins_next = 0
        self._char_w = None
        # IO viewer: one model (map + sampler), a view per section
        self.io_model = IoModel(app)
        self.io = {"PHYS": IoView(app, self.io_model, "phys"),
                   "HAL":  IoView(app, self.io_model, "hal")}

    def on_enter(self):
        ini = self.app.stat.ini_filename or settings.INI_PATH or ""
        ini = os.path.abspath(ini)
        self.config_dir = os.path.dirname(ini)
        if self.sel_path is None:
            self.sel_path = ini
        self.note = ""
        self._enter_chapter(self.chapter)

    def on_leave(self):
        self.app.input.raw = False
        self.app.input.clear()
        self.pins_on = False

    def active_keys(self):
        return (self.chapter,)

    # ------------------------------------------------------------- chapters
    def _root_row(self):
        row = [K(n, lambda n=n: self._enter_chapter(n)) for n in self.CHAPTERS]
        while len(row) < 8:
            row.append(K())
        row.append(K("EXIT", self._exit_menu))
        row.append(K("(OPRT)", self._oprt))
        return row

    def _enter_chapter(self, name):
        self.chapter = name
        self.note = ""
        self.cursor = None
        self.pins_on = False
        self.app.input.raw = name in ("TEXT", "FIELDS")
        if name in ("DIR", "USB"):
            self._refresh_files()
        elif name in ("TEXT", "FIELDS"):
            self._ensure_doc()
            if name == "FIELDS":
                self._build_fields()
        elif name in self.io:
            self.note = self.io_model.ensure()
        self.set_root(self._root_row())

    def _oprt(self):
        ch = self.chapter
        if ch == "DIR":
            self.push([
                K("SELECT", self._file_select),
                K("COPY",   lambda: self._confirm(self._copy_to_usb)),
                K("RESTOR", self._restore_cursored),
                K("REFRSH", self._refresh_files),
            ])
        elif ch == "USB":
            self.push([
                K("SELECT", self._file_select),
                K("COPY",   self._copy_from_usb_menu),
                K("REFRSH", self._refresh_files),
            ])
        elif ch == "TEXT":
            self.push([[
                K("INSERT",  self._edit_insert_word),
                K("ALTER",   self._edit_alter),
                K("DEL.WRD", self._edit_delete_word),
                K("ALT.LIN", self._edit_alter_line),
                K("DEL.LIN", self._edit_delete_line),
                K("SRH \\/", lambda: self._srh(+1)),
                K("SRH /\\", lambda: self._srh(-1)),
                K("SAVE",    self._save_menu),
                K("CANCEL",  self._cancel),
            ], self._page2()])
        elif ch == "FIELDS":
            page1 = [K("INPUT", self._field_input)]
            if self.doc is not None and self.doc.kind == "hal":
                page1 += [K("APPLY", self._apply_menu),
                          K("PINS",  self._toggle_pins)]
            else:
                page1 += [K("SEC \\/", lambda: self._sec(+1)),
                          K("SEC /\\", lambda: self._sec(-1))]
            page1 += [
                K("SRH \\/", lambda: self._srh(+1)),
                K("SRH /\\", lambda: self._srh(-1)),
                K("SAVE",    self._save_menu),
                K("CANCEL",  self._cancel),
            ]
            self.push([page1, self._page2()])
        elif ch == "INFO":
            self.push([K("RESTRT", self._restart_menu)])
        elif ch in self.io:
            v = self.io[ch]
            self.push([[
                K("GRP \\/",  lambda: v.next_group(+1)),
                K("GRP /\\",  lambda: v.next_group(-1)),
                K("SRH \\/",  lambda: self._srh(+1)),
                K("SRH /\\",  lambda: self._srh(-1)),
                K("HOLD",    self._io_hold),
                K("LOG",     lambda: setattr(v, "show_log", not v.show_log)),
                K("CLR.CNT", self.io_model.sampler.clear_counts),
            ], [
                K("REGEN",   self._io_regen_menu),
            ]])

    # ------------------------------------------------------------- IO viewer
    def _io_hold(self):
        s = self.io_model.sampler
        s.set_hold(not s.hold)
        self.note = "HOLD: DISPLAY FROZEN, COUNTERS AND LOG RUN" if s.hold else ""

    def _io_regen_menu(self):
        self.note = (f"REBUILD {os.path.basename(self.io_model.path or 'io_map.yaml')}"
                     " FROM LIVE HAL - OLD COPY KEPT AS .BAK")
        self._confirm(lambda: setattr(self, "note", self.io_model.regen()))

    def _page2(self):
        return [K("RESTOR", self._restore_selected),
                K("RESTRT", self._restart_menu)]

    def _confirm(self, exec_fn):
        self.push([K("CAN", self.pop),
                   K("EXEC", lambda: (exec_fn(), self.pop()))])

    def _exit_menu(self):
        if self.doc is not None and self.doc.dirty:
            self.note = "UNSAVED EDITS WILL BE LOST"
        self.push([K("CAN", self.pop),
                   K("EXEC", lambda: setattr(self.app, "quit", True))])

    # ------------------------------------------------------------- guards
    def _idle(self):
        if self.app.stat.interp_state != linuxcnc.INTERP_IDLE:
            self.note = "CANNOT EDIT WHILE RUNNING"
            return False
        return True

    def _editable(self):
        if not self._idle():
            return False
        if self.doc is None:
            self.note = "NO FILE SELECTED"
            return False
        return True

    # ------------------------------------------------------------- documents
    def _ensure_ini(self):
        ini = os.path.abspath(self.app.stat.ini_filename or settings.INI_PATH or "")
        if self.ini_doc is None or self.ini_doc.path != ini:
            try:
                self.ini_doc = configfile.IniDoc(ini)
            except OSError:
                self.ini_doc = None

    def _ensure_doc(self):
        if self.sel_path and (self.doc is None or self.doc.path != self.sel_path):
            self._load_doc(self.sel_path)

    def _load_doc(self, path):
        try:
            if path.lower().endswith(".hal"):
                self._ensure_ini()
            self.doc = configfile.load_doc(path, self.ini_doc)
        except OSError as e:
            self.doc = None
            self.note = f"CANNOT READ {os.path.basename(path)}: {e.strerror or e}"
            return False
        self.sel_path = path
        self.edit_cur = self.edit_word = self.edit_scroll = 0
        self.scroll = 0
        self.pins_on = False
        return True

    def _reload_doc(self):
        if self.doc is None:
            return
        idx = self.cursor.idx if self.cursor else 0
        cur = self.edit_cur
        if self._load_doc(self.doc.path):
            if self.chapter == "FIELDS":
                self._build_fields()
                if self.cursor:
                    self.cursor.idx = min(idx, len(self.cursor.fields) - 1)
            self.edit_cur = min(cur, len(self.doc.lines) - 1)

    def _save(self):
        if not self._editable():
            return False
        try:
            configfile.write_with_backup(self.doc.path, self.doc.text())
        except OSError as e:
            self.note = f"SAVE FAILED: {e.strerror or e}"
            return False
        self.doc.dirty = False
        if self.ini_doc is not None and self.ini_doc.path == self.doc.path:
            self.ini_doc = None          # re-read for [SEC]KEY resolution
        self.note = "SAVED (.BAK WRITTEN) - RESTART TO APPLY"
        return True

    def _save_menu(self):
        if self.doc is None:
            self.note = "NO FILE SELECTED"
            return
        if not self.doc.dirty:
            self.note = "NO CHANGES"
            return
        self.note = f"SAVE {self.doc.name()} - OLD COPY KEPT AS .BAK"
        self._confirm(self._save)

    def _cancel(self):
        if self.doc is None or not self.doc.dirty:
            self.note = "NO CHANGES"
            return
        self.note = f"DISCARD EDITS IN {self.doc.name()}"
        self._confirm(self._reload_doc)

    def _restore(self, path):
        try:
            configfile.restore_backup(path)
        except FileNotFoundError:
            self.note = "NO .BAK FILE"
            return
        except OSError as e:
            self.note = f"RESTORE FAILED: {e.strerror or e}"
            return
        if self.doc is not None and self.doc.path == path:
            self._reload_doc()
        self.note = f"RESTORED {os.path.basename(path)} FROM .BAK"

    def _restore_selected(self):
        if not self.sel_path:
            self.note = "NO FILE SELECTED"
            return
        self._restore_menu(self.sel_path)

    def _restore_cursored(self):
        e = self._cur_entry()
        if not e or e[1]:
            return
        self._restore_menu(e[2])

    def _restore_menu(self, path):
        if not self._idle():
            return
        if not os.path.exists(configfile.backup_path(path)):
            self.note = "NO .BAK FILE"
            return
        self.note = f"REPLACE {os.path.basename(path)} WITH ITS .BAK"
        self._confirm(lambda: self._restore(path))

    # ------------------------------------------------------------- restart
    def _restart_menu(self):
        if not self._idle():
            return
        if not os.environ.get(RESTART_FLAG_ENV):
            self.note = "NO LAUNCHER - START WITH run_gui.sh, OR EXIT AND START AGAIN"
            return
        self.note = "RESTART CONTROL - ALL MOTION STOPS"
        if self.doc is not None and self.doc.dirty:
            self.note += " - EDITS SAVED FIRST"
        self._confirm(self._restart)

    def _restart(self):
        if not self._idle():
            return
        if self.doc is not None and self.doc.dirty and not self._save():
            return
        flag = os.environ.get(RESTART_FLAG_ENV)
        try:
            with open(flag, "w") as f:
                f.write(self.app.stat.ini_filename or "")
        except OSError as e:
            self.note = f"RESTART FAILED: {e.strerror or e}"
            return
        self.app.quit = True

    # ------------------------------------------------------------- browser
    def _root(self):
        if self.chapter == "USB":
            for m in self.app.usb_mounts:
                if os.path.isdir(m):
                    return m
            return None
        return self.config_dir

    def _base(self):
        return self.usb_path if self.chapter == "USB" else self.dir_path

    def _set_base(self, base):
        if self.chapter == "USB":
            self.usb_path = base
        else:
            self.dir_path = base

    def _refresh_files(self):
        root = self._root()
        self.entries = []
        self.file_cur = 0
        self.file_scroll = 0
        if root is None:
            self.note = "NO USB MOUNTED"
            return
        base = self._base()
        if not base or not (base == root or base.startswith(root + os.sep)):
            base = root
        try:
            names = sorted(os.listdir(base), key=str.upper)
        except OSError:
            base = root
            try:
                names = sorted(os.listdir(base), key=str.upper)
            except OSError:
                names = []
        if base != root:
            self.entries.append(("..", True, os.path.dirname(base)))
        for n in names:
            if n.startswith(".") or n.endswith(".tmp"):
                continue
            p = os.path.join(base, n)
            self.entries.append((n, os.path.isdir(p), p))
        self.entries.sort(key=lambda e: (e[0] != "..", not e[1], e[0].upper()))
        self._set_base(base)
        for i, e in enumerate(self.entries):     # land on the selected file
            if e[2] == self.sel_path:
                self.file_cur = i

    def _cur_entry(self):
        return self.entries[self.file_cur] if self.entries else None

    def _file_select(self):
        e = self._cur_entry()
        if not e:
            return
        name, is_dir, path = e
        if is_dir:
            self._set_base(path)
            self._refresh_files()
            return
        if self.doc is not None and self.doc.dirty and path != self.doc.path:
            self.note = f"UNSAVED EDITS IN {self.doc.name()} - EXEC TO DISCARD"
            self._confirm(lambda: self._open_file(path))
            return
        self._open_file(path)

    def _open_file(self, path):
        if path == self.sel_path and self.doc is not None:
            self._enter_chapter("FIELDS" if self.doc.kind != "text" else "TEXT")
            return
        if self._load_doc(path):
            self._enter_chapter("FIELDS" if self.doc.kind != "text" else "TEXT")
            self.note = f"SELECTED {os.path.basename(path)}"

    def _first_usb(self):
        for m in self.app.usb_mounts:
            if os.path.isdir(m):
                return m
        return None

    def _copy_to_usb(self):
        e = self._cur_entry()
        if not e or e[1]:
            return
        usb = self._first_usb()
        if usb is None:
            self.note = "NO USB MOUNTED"
            return
        try:
            shutil.copy(e[2], os.path.join(usb, e[0]))
            self.note = f"COPIED {e[0]} -> USB"
        except OSError as err:
            self.note = f"COPY FAILED: {err.strerror or err}"

    def _copy_from_usb_menu(self):
        e = self._cur_entry()
        if not e or e[1]:
            return
        dst = os.path.join(self.config_dir, e[0])
        if os.path.exists(dst):
            self.note = f"COPY {e[0]} -> CONFIG DIR, OVERWRITES EXISTING"
        else:
            self.note = f"COPY {e[0]} -> CONFIG DIR"
        self._confirm(self._copy_from_usb)

    def _copy_from_usb(self):
        e = self._cur_entry()
        if not e or e[1]:
            return
        dst = os.path.join(self.config_dir, e[0])
        try:
            if os.path.exists(dst):
                shutil.copy2(dst, configfile.backup_path(dst))
            shutil.copy(e[2], dst)
            self.note = f"COPIED {e[0]} -> CONFIG DIR"
        except OSError as err:
            self.note = f"COPY FAILED: {err.strerror or err}"
            return
        if self.doc is not None and self.doc.path == dst:
            self._reload_doc()

    # ------------------------------------------------------------- text editor
    def _edit_words(self):
        line = self.doc.lines[self.edit_cur]
        return [(m.start(), m.end()) for m in re.finditer(r'\S+', line)]

    def _clamp_word(self):
        n = len(self._edit_words()) if self.doc else 0
        self.edit_word = max(0, min(self.edit_word, max(0, n - 1)))

    def _edit_insert_word(self):
        if not self._editable():
            return
        text = self.app.input.take().strip()
        if not text:
            return
        line = self.doc.lines[self.edit_cur]
        words = self._edit_words()
        if not words:
            self.doc.set_line(self.edit_cur, text)
            self.edit_word = 0
        else:
            _s, end = words[self.edit_word]
            self.doc.set_line(self.edit_cur, line[:end] + " " + text + line[end:])
            self.edit_word += 1
        self._clamp_word()

    def _edit_alter(self):
        if not self._editable():
            return
        text = self.app.input.take().strip()
        words = self._edit_words()
        if not text or not words:
            return
        s, e = words[self.edit_word]
        line = self.doc.lines[self.edit_cur]
        self.doc.set_line(self.edit_cur, line[:s] + text + line[e:])

    def _edit_delete_word(self):
        if not self._editable():
            return
        words = self._edit_words()
        if not words:
            return
        s, e = words[self.edit_word]
        line = self.doc.lines[self.edit_cur]
        # take the following space with the word, or the preceding one at EOL
        if e < len(line) and line[e] == " ":
            e += 1
        elif s > 0 and line[s - 1] == " ":
            s -= 1
        self.doc.set_line(self.edit_cur, line[:s] + line[e:])
        self._clamp_word()

    def _edit_alter_line(self):
        if not self._editable():
            return
        self.doc.set_line(self.edit_cur, self.app.input.take())
        self.edit_word = 0

    def _edit_insert_line(self):
        if not self._editable():
            return
        self.doc.insert_line(self.edit_cur + 1, self.app.input.take())
        self.edit_cur += 1
        self.edit_word = 0

    def _edit_delete_line(self):
        if not self._editable():
            return
        self.doc.delete_line(self.edit_cur)
        self.edit_cur = min(self.edit_cur, len(self.doc.lines) - 1)
        self._clamp_word()

    def edit_key(self, action):
        """Physical ALTER/INSERT/DELETE keys."""
        if self.doc is None:
            return
        if self.chapter == "TEXT":
            if action == "ALTER":
                self._edit_alter()
            elif action == "INSERT":
                self._edit_insert_word()
            elif action == "DELETE":
                self._edit_delete_word()
        elif self.chapter == "FIELDS" and self.cursor:
            if action == "ALTER":
                self._field_input()
            elif action == "DELETE":
                self._field_clear()

    # ------------------------------------------------------------- search
    def _cur_line(self):
        if self.chapter == "TEXT":
            return self.edit_cur
        if self.cursor and self.field_lines:
            return self.field_lines[self.cursor.idx][0]
        return 0

    def _goto_line(self, i):
        if self.chapter == "TEXT":
            self.edit_cur = i
            self.edit_word = 0
            self._clamp_word()
        elif self.cursor:
            for k, (line, _lo, _hi) in enumerate(self.field_lines):
                if line >= i:
                    self.cursor.idx = k
                    return
            self.cursor.idx = len(self.field_lines) - 1

    def _srh(self, direction):
        if self.chapter in self.io:
            pat = self.app.input.take().strip().lower()
            if pat and not self.io[self.chapter].search(pat, direction):
                self.note = f"{pat} NOT FOUND"
            return
        if self.doc is None:
            return
        pat = self.app.input.take().strip().lower()
        if not pat:
            return
        lines = self.doc.lines
        start = self._cur_line()
        rng = (range(start + 1, len(lines)) if direction > 0
               else range(start - 1, -1, -1))
        for i in rng:
            if pat in lines[i].lower():
                self._goto_line(i)
                return
        self.note = f"{pat} NOT FOUND"

    def _sec(self, direction):
        if self.doc is None or self.doc.kind != "ini":
            return
        cur = self._cur_line()
        secs = [i for i, _n in self.doc.sections()]
        nxt = ([i for i in secs if i > cur] if direction > 0
               else [i for i in reversed(secs) if i < cur - 1
                     and self._section_of(cur) != i])
        if nxt:
            self._goto_line(nxt[0] + 1)

    def _section_of(self, line):
        best = None
        for i, _n in self.doc.sections():
            if i <= line:
                best = i
        return best

    # ------------------------------------------------------------- fields
    def _build_fields(self):
        self.cursor = None
        self.field_lines = []
        self.fields_at = {}
        if self.doc is None or self.doc.kind == "text":
            return
        if self.doc.kind == "hal":
            self.cursor = FieldCursor(cols=2)
            for r in self.doc.recs:
                if r.kind != "cmd":
                    continue
                for c in self.doc.cells(r):
                    setter = None
                    if c.editable:
                        setter = (lambda t, a=(r.i, c.lo, c.hi):
                                  self._set_hal(a, t))
                    self._add_field(r.i, c.lo, c.hi, setter)
        else:
            self.cursor = FieldCursor(cols=1)
            for r in self.doc.recs:
                if r.kind == "key":
                    self._add_field(r.i, 0, 0,
                                    lambda t, i=r.i: self._set_ini(i, t))
        if not self.cursor.fields:
            self.cursor = None

    def _add_field(self, line, lo, hi, setter):
        k = self.cursor.add(0, 0, 0, 0, setter=setter)
        self.field_lines.append((line, lo, hi))
        self.fields_at.setdefault(line, []).append(k)

    def _rebuild_keep_cursor(self):
        idx = self.cursor.idx if self.cursor else 0
        self._build_fields()
        if self.cursor:
            self.cursor.idx = min(idx, len(self.cursor.fields) - 1)

    def _set_ini(self, i, text):
        if not self._editable():
            return
        self.doc.set_value(i, text)
        self._rebuild_keep_cursor()

    def _set_hal(self, a, text):
        if not self._editable():
            return
        i, lo, hi = a
        if not self.doc.replace(i, lo, hi, text):
            self.note = "NOTHING TO INSERT"
        self._rebuild_keep_cursor()

    def _field_input(self):
        if not self.cursor:
            return
        if not self._editable():
            return
        if not self.app.input.text.strip():
            self.note = "TYPE A VALUE FIRST"
            return
        f = self.cursor.current()
        if f.setter is None:
            self.note = "THIS CELL IS READ-ONLY - USE TEXT"
            return
        self.cursor.commit(self.app.input.take())

    def _field_clear(self):
        """DELETE key: empty an ini value."""
        if not self.cursor or not self._editable():
            return
        if self.doc.kind != "ini":
            self.note = "DELETE: INI VALUES ONLY - USE TEXT FOR HAL"
            return
        line = self.field_lines[self.cursor.idx][0]
        self.doc.set_value(line, "")
        self._rebuild_keep_cursor()

    def _cur_rec(self):
        if self.doc is None or not self.cursor or not self.field_lines:
            return None
        return self.doc.recs[self.field_lines[self.cursor.idx][0]]

    def _scroll_to_field(self, rows):
        if not self.cursor or not self.field_lines:
            return
        line = self.field_lines[self.cursor.idx][0]
        if line < self.scroll:
            self.scroll = line
        elif line >= self.scroll + rows:
            self.scroll = line - rows + 1
        n = len(self.doc.lines)
        self.scroll = max(0, min(self.scroll, max(0, n - rows)))

    # ------------------------------------------------------------- live HAL
    def _apply_menu(self):
        rec = self._cur_rec()
        if rec is None or rec.cmd not in ("setp", "sets"):
            self.note = "APPLY: SETP / SETS ROWS ONLY"
            return
        if len(rec.toks) < 3:
            self.note = "APPLY: NO VALUE ON THIS LINE"
            return
        if not self._idle():
            return
        if not self.hal.available():
            self.note = "HALCMD NOT FOUND"
            return
        val = self.doc.expand(rec.toks[2])
        self.note = f"halcmd {rec.cmd} {rec.toks[1]} {val}"
        self._confirm(lambda: self._apply(rec, val))

    def _apply(self, rec, val):
        fn = self.hal.setp if rec.cmd == "setp" else self.hal.sets
        ok, msg = fn(rec.toks[1], val)
        if ok:
            self.note = f"APPLIED {rec.toks[1]} = {val} (NOT SAVED)"
            self.pins_next = 0
        else:
            self.note = f"APPLY FAILED: {msg or 'halcmd error'}"

    def _toggle_pins(self):
        if not self.pins_on and not self.hal.available():
            self.note = "HALCMD NOT FOUND"
            return
        self.pins_on = not self.pins_on
        self.pins_next = 0

    # ------------------------------------------------------------- keys
    def on_key(self, sc):
        ch = self.chapter
        if ch in self.io:
            return self.io[ch].on_key(sc)
        if ch in ("DIR", "USB"):
            n = max(0, len(self.entries) - 1)
            if sc == SDL_SCANCODE_UP:
                self.file_cur = max(0, self.file_cur - 1); return True
            if sc == SDL_SCANCODE_DOWN:
                self.file_cur = min(n, self.file_cur + 1); return True
            if sc == SDL_SCANCODE_PAGEUP:
                self.file_cur = max(0, self.file_cur - self.FILE_ROWS); return True
            if sc == SDL_SCANCODE_PAGEDOWN:
                self.file_cur = min(n, self.file_cur + self.FILE_ROWS); return True
            if sc == SDL_SCANCODE_RETURN:
                self._file_select(); return True
        elif ch == "TEXT" and self.doc is not None:
            last = len(self.doc.lines) - 1
            if sc == SDL_SCANCODE_UP:
                self.edit_cur = max(0, self.edit_cur - 1)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_DOWN:
                self.edit_cur = min(last, self.edit_cur + 1)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_LEFT:
                self.edit_word = max(0, self.edit_word - 1); return True
            if sc == SDL_SCANCODE_RIGHT:
                self.edit_word += 1
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_PAGEUP:
                self.edit_cur = max(0, self.edit_cur - self.EDIT_ROWS)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_PAGEDOWN:
                self.edit_cur = min(last, self.edit_cur + self.EDIT_ROWS)
                self._clamp_word(); return True
            if sc == SDL_SCANCODE_RETURN:
                self._edit_insert_line(); return True
        elif ch == "FIELDS" and self.cursor:
            if sc == SDL_SCANCODE_RETURN:
                self._field_input(); return True
            return super().on_key(sc)
        return False

    def on_touch(self, x, y):
        """Tap a visible row (browser) or cell (fields) to move the cursor.
        Only rows laid out in the last frame are hit-tested, so stale rects
        of scrolled-away rows never match."""
        if self.chapter in self.io:
            return self.io[self.chapter].on_touch(x, y)
        if self.chapter in ("DIR", "USB"):
            for ry, idx in self._vis_rows:
                if ry - 2 <= y < ry + 56:
                    self.file_cur = idx
                    return True
        elif self.chapter == "FIELDS" and self.cursor:
            for k in self._vis_fields:
                f = self.cursor.fields[k]
                if f.x <= x < f.x + f.w and f.y <= y < f.y + f.h:
                    self.cursor.idx = k
                    return True
        return False

    # ------------------------------------------------------------- draw
    def draw(self, renderer, area):
        f = self.app.font
        ch = self.chapter
        self._vis_rows = ()
        self._vis_fields = ()
        if ch in ("DIR", "USB"):
            self._draw_files(renderer, f)
        elif ch == "TEXT":
            self._draw_text(renderer, f)
        elif ch == "FIELDS":
            self._draw_fields(renderer, f)
        elif ch in self.io:
            self.io[ch].draw(renderer, f)
        else:
            self._draw_info(renderer, f)
        if self.note:
            draw_line(renderer, f, self._fit(f, self.note, 1900), 10,
                      self.NOTE_Y, RED)

    def _fit(self, f, text, w):
        """Clip text to w pixels (from the right)."""
        if not text:
            return text
        if self._char_w is None:
            self._char_w = max(1, text_width(f, "0"))
        n = min(len(text), max(1, int(w / self._char_w) + 4))
        s = text[:n]
        while len(s) > 1 and text_width(f, s) > w:
            s = s[:-1]
        return s

    def _title(self, renderer, f, title):
        draw_line(renderer, f, title, 10, self.TITLE_Y)
        x = 10 + text_width(f, title) + 16
        if self.doc is not None and self.doc.dirty and self.chapter != "INFO":
            draw_line(renderer, f, "*", x, self.TITLE_Y, RED)
        if self.chapter in ("TEXT", "FIELDS"):
            w = "USE EXTERNAL KEYBOARD TO EDIT"
            draw_line(renderer, f, w, 1910 - text_width(f, w), self.TITLE_Y,
                      ACCENT)

    def _hilite_row(self, renderer, f, text, x, y, w, active, color=WHITE):
        if active:
            SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
            SDL_RenderFillRect(renderer, SDL_Rect(x - 4, y - 2, w, 56))
            draw_line(renderer, f, text, x, y, BLACK)
        else:
            draw_line(renderer, f, text, x, y, color)

    def _draw_files(self, renderer, f):
        base = self._base()
        self._title(renderer, f, f"{self.chapter}  {base or '-'}")
        if not self.entries:
            draw_line(renderer, f, "(empty)", 10, 130)
            return
        if self.file_cur < self.file_scroll:
            self.file_scroll = self.file_cur
        elif self.file_cur >= self.file_scroll + self.FILE_ROWS:
            self.file_scroll = self.file_cur - self.FILE_ROWS + 1
        vis = []
        for row, idx in enumerate(range(self.file_scroll,
                                        min(len(self.entries),
                                            self.file_scroll + self.FILE_ROWS))):
            name, is_dir, path = self.entries[idx]
            if is_dir:
                text = f"<DIR>  {name}"
            else:
                try:
                    size = os.path.getsize(path)
                    text = f"{size // 1024:5d}K  {name}" if size >= 1024 \
                           else f"{size:5d}B  {name}"
                except OSError:
                    text = f"    ?  {name}"
            sel = (path == self.sel_path)
            y = 130 + row * 60
            vis.append((y, idx))
            self._hilite_row(renderer, f, ("> " if sel else "  ") + text,
                             10, y, 1300, idx == self.file_cur,
                             ACCENT if sel else WHITE)
        self._vis_rows = tuple(vis)

    def _draw_text(self, renderer, f):
        self._title(renderer, f,
                    f"TEXT  {os.path.basename(self.sel_path) if self.sel_path else '-'}")
        if self.doc is None:
            draw_line(renderer, f, "NO FILE SELECTED - USE DIR", 10, 130)
            return
        lines = self.doc.lines
        if self.edit_cur < self.edit_scroll:
            self.edit_scroll = self.edit_cur
        elif self.edit_cur >= self.edit_scroll + self.EDIT_ROWS:
            self.edit_scroll = self.edit_cur - self.EDIT_ROWS + 1
        for row, idx in enumerate(range(self.edit_scroll,
                                        min(len(lines),
                                            self.edit_scroll + self.EDIT_ROWS))):
            line = lines[idx].rstrip("\r").expandtabs(4)
            prefix = f"{idx + 1:4d} "
            y = 130 + row * self.ROW_H
            draw_line(renderer, f, self._fit(f, prefix + line, 1900), 10, y)
            if idx != self.edit_cur:
                continue
            spans = [(m.start(), m.end()) for m in re.finditer(r'\S+', line)]
            if spans:
                self._clamp_word()
                s, e = spans[min(self.edit_word, len(spans) - 1)]
                x0 = 10 + text_width(f, prefix + line[:s])
                if x0 < 1900:
                    wpx = text_width(f, line[s:e])
                    SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
                    SDL_RenderFillRect(renderer,
                                       SDL_Rect(x0 - 3, y - 2, wpx + 6, 56))
                    draw_line(renderer, f, self._fit(f, line[s:e], 1900 - x0),
                              x0, y, BLACK)
            else:
                SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
                SDL_RenderFillRect(renderer,
                                   SDL_Rect(10 + text_width(f, prefix),
                                            y - 2, 26, 56))

    def _draw_cell(self, renderer, f, text, k, x, y, w, color=WHITE):
        fld = self.cursor.fields[k]
        fld.x, fld.y, fld.w, fld.h = x, y - 2, w, 56
        text = self._fit(f, text, w - 12)
        if self.cursor.is_current(k):
            SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
            SDL_RenderFillRect(renderer, SDL_Rect(fld.x, fld.y, fld.w, fld.h))
            draw_line(renderer, f, text, x + 6, y, BLACK)
        else:
            draw_line(renderer, f, text, x + 6, y, color)

    def _draw_fields(self, renderer, f):
        name = os.path.basename(self.sel_path) if self.sel_path else "-"
        self._title(renderer, f, f"FIELDS  {name}")
        if self.doc is None:
            draw_line(renderer, f, "NO FILE SELECTED - USE DIR", 10, 130)
            return
        if self.doc.kind == "text" or not self.cursor:
            draw_line(renderer, f, "NO STRUCTURED VIEW FOR THIS FILE - USE TEXT",
                      10, 130)
            return
        rows = self.FIELD_ROWS_PINS if self.pins_on else self.FIELD_ROWS
        self._scroll_to_field(rows)
        if self.doc.kind == "ini":
            self._draw_ini_rows(renderer, f, rows)
        else:
            self._draw_hal_rows(renderer, f, rows)
            if self.pins_on:
                self._draw_pins(renderer, f)
        self._draw_cell_status(renderer, f)

    def _draw_ini_rows(self, renderer, f, rows):
        draw_line(renderer, f, "LINE", 10, self.HDR_Y, DIM)
        draw_line(renderer, f, "KEY", self.INI_KEY_X, self.HDR_Y, DIM)
        draw_line(renderer, f, "VALUE", self.INI_VAL_X + 6, self.HDR_Y, DIM)
        vis = []
        recs = self.doc.recs
        for row, idx in enumerate(range(self.scroll,
                                        min(len(recs), self.scroll + rows))):
            r = recs[idx]
            y = self.ROW_Y0 + row * self.ROW_H
            draw_line(renderer, f, f"{idx + 1:4d}", 10, y, DIM)
            line = self.doc.lines[idx].rstrip("\r")
            if r.kind == "section":
                draw_line(renderer, f, f"[{r.section}]", self.INI_KEY_X, y, ACCENT)
            elif r.kind in ("comment", "other"):
                draw_line(renderer, f, self._fit(f, line.strip(), 1780),
                          self.INI_KEY_X, y, DIM)
            elif r.kind == "key":
                draw_line(renderer, f, self._fit(f, r.key, self.INI_KEY_W),
                          self.INI_KEY_X, y)
                k = self.fields_at[idx][0]
                self._draw_cell(renderer, f, r.value, k,
                                self.INI_VAL_X, y, self.INI_VAL_W)
                vis.append(k)
                if r.tail.strip():
                    draw_line(renderer, f, self._fit(f, r.tail.strip(), self.TAIL_W),
                              self.TAIL_X, y, DIM)
        self._vis_fields = tuple(vis)

    def _draw_hal_rows(self, renderer, f, rows):
        cur = self._cur_rec()
        labels = [c.label for c in self.doc.cells(cur)] if cur else ["", ""]
        draw_line(renderer, f, "LINE", 10, self.HDR_Y, DIM)
        draw_line(renderer, f, "CMD", self.HAL_CMD_X, self.HDR_Y, DIM)
        draw_line(renderer, f, labels[0], self.HAL_C1_X + 6, self.HDR_Y, DIM)
        draw_line(renderer, f, labels[1], self.HAL_C2_X + 6, self.HDR_Y, DIM)
        vis = []
        recs = self.doc.recs
        for row, idx in enumerate(range(self.scroll,
                                        min(len(recs), self.scroll + rows))):
            r = recs[idx]
            y = self.ROW_Y0 + row * self.ROW_H
            draw_line(renderer, f, f"{idx + 1:4d}", 10, y, DIM)
            line = self.doc.lines[idx].rstrip("\r")
            if r.kind == "blank":
                continue
            if r.kind == "comment":
                draw_line(renderer, f, self._fit(f, line.strip(), 1780),
                          self.HAL_CMD_X, y, DIM)
                continue
            if r.kind == "cont":
                draw_line(renderer, f, self._fit(f, "  ... " + line.strip(), 1780),
                          self.HAL_CMD_X, y, DIM)
                continue
            draw_line(renderer, f, self._fit(f, r.cmd, self.HAL_C1_X - self.HAL_CMD_X - 10),
                      self.HAL_CMD_X, y)
            ks = self.fields_at.get(idx, [])
            cells = self.doc.cells(r)
            for k, c, x, w in zip(ks, cells,
                                  (self.HAL_C1_X, self.HAL_C2_X),
                                  (self.HAL_C1_W, self.HAL_C2_W)):
                self._draw_cell(renderer, f, c.text, k, x, y, w,
                                WHITE if c.editable else DIM)
                vis.append(k)
            if r.tail.strip():
                draw_line(renderer, f, self._fit(f, r.tail.strip(), self.TAIL_W),
                          self.TAIL_X, y, DIM)
        self._vis_fields = tuple(vis)

    def _draw_cell_status(self, renderer, f):
        """Full text of the cursored cell, plus [SEC]KEY resolutions."""
        rec = self._cur_rec()
        if rec is None:
            return
        if self.doc.kind == "ini":
            text = f"[{rec.section}] {rec.key} = {rec.value}"
        else:
            _line, lo, hi = self.field_lines[self.cursor.idx]
            cells = self.doc.cells(rec)
            cell = next((c for c in cells if (c.lo, c.hi) == (lo, hi)), cells[0])
            text = f"{cell.label or rec.cmd.upper()}: {cell.text}"
            refs = []
            for tok in cell.text.split():
                v = self.doc.resolve(tok)
                if v is not None:
                    refs.append(f"{tok} = {v}")
            if refs:
                text += "   (" + "; ".join(refs) + ")"
            if not cell.editable:
                text += "   [READ-ONLY]"
        draw_line(renderer, f, self._fit(f, text, 1900), 10, self.STATUS_Y, ACCENT)

    def _draw_pins(self, renderer, f):
        now = SDL_GetTicks()
        if now >= self.pins_next:
            self.pins_cache = self.hal.snapshot()
            self.pins_next = now + self.PINS_PERIOD_MS
        y = self.PINS_Y
        SDL_SetRenderDrawColor(renderer, *DIM_RGB, 255)
        SDL_RenderDrawLine(renderer, 10, y, 1910, y)
        draw_line(renderer, f, "LIVE  (halcmd)", 10, y + 8, ACCENT)
        rec = self._cur_rec()
        names = rec.pin_names() if rec else []
        y += 60
        if not names:
            draw_line(renderer, f, "NO PINS ON THIS LINE", 10, y, DIM)
            return
        if not self.pins_cache:
            draw_line(renderer, f, "NO DATA - IS HAL RUNNING?", 10, y, DIM)
            return
        for name in names[:self.PINS_ROWS]:
            val = self.pins_cache.get(name, "--")
            draw_line(renderer, f, self._fit(f, name, 1100), 10, y)
            draw_line(renderer, f, self._fit(f, val, 700), 1150, y, ACCENT)
            y += 50
        if len(names) > self.PINS_ROWS:
            draw_line(renderer, f, f"(+{len(names) - self.PINS_ROWS} MORE)",
                      1150, y, DIM)

    def _draw_info(self, renderer, f):
        st = self.app.stat
        self._title(renderer, f, "SYSTEM")
        rows = [
            ("INI",     st.ini_filename or "-"),
            ("CONFIG",  self.config_dir or "-"),
            ("JOINTS",  f"{st.joints}      KINS  {st.kinematics_type}"),
            ("FILE",    (os.path.basename(self.sel_path) if self.sel_path else "-")
                        + ("   * UNSAVED EDITS" if self.doc is not None
                           and self.doc.dirty else "")),
        ]
        if self.sel_path:
            bak = configfile.backup_path(self.sel_path)
            rows.append(("BACKUP", os.path.basename(bak)
                         + ("" if os.path.exists(bak) else "   (NONE)")))
        flag = os.environ.get(RESTART_FLAG_ENV)
        rows.append(("RESTART", f"LAUNCHER OK ({flag})" if flag
                     else "NO LAUNCHER - START WITH run_gui.sh"))
        rows.append(("HALCMD", "OK" if self.hal.available() else "NOT FOUND"))
        io = self.io_model
        path = io.path or io.resolve_path(self.config_dir or ".")
        rows.append(("IOMAP", os.path.basename(path)
                     + ("" if os.path.exists(path) else "   (CREATED ON FIRST PHYS/HAL)")
                     + (f"   READ VIA {io.backend()}" if io.backend() != "-" else "")))
        y = 140
        for label, val in rows:
            draw_line(renderer, f, label, 10, y, DIM)
            draw_line(renderer, f, self._fit(f, val, 1660), 240, y)
            y += 60


DIM_RGB = settings.color("DIM", (150, 150, 150))
