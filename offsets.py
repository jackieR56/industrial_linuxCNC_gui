#!/usr/bin/env python3
# offsets.py — OFFSET/SETTING screen: tool table (geometry, wear via
#              tooltbl), work offsets, and the TPROBE / WPROBE / P.SET
#              probing pages with their probe-cycle state machine.

import json
import os
import time

import linuxcnc
from sdl2 import *

import tooltbl
from screens import (Screen, K, FieldCursor, draw_field, draw_line, ACT,
                     WHITE, RED, DIM, ACCENT,
                     WCS, WCS_NAMES, WCS_PARAM_BASE, AXIS_IDX)
from probe import (PROBE_PARAMS, PROBE_FIELDS, PROBE_KIND, PROBE_PAGES,
                   PROBE_PAGE_KEYS, CAL_MACROS, ANG_MACROS, CAL_FIELDS,
                   TOOL_SENSOR_MACRO, CURSOR_C, GRID_ON_C, GRID_OFF_C,
                   macro_help, draw_probe_icon)

# Tool wear now lives in tool.tbl (see tooltbl.py). The old side file is
# only looked for to import it once (OffsetScreen._migrate_legacy_wear).
LEGACY_WEAR_FILE = os.path.join(os.path.dirname(__file__), "tool_wear.json")


class OffsetScreen(Screen):
    """OFFSET/SETTING chapter: [OFFSET] [SETING] [WORK] (+PROBE pages)."""

    VISIBLE_ROWS = 9
    ROW_H = 74
    TABLE_Y = 200
    WCOL_X0, WCOL_W, WCOL_PITCH = 170, 240, 256
    UNIT_CYCLE = ("MACHINE", "MM", "INCH", "PROGRAM")

    # ------------------------------------------------------------- lifecycle
    def __init__(self, app):
        super().__init__(app)
        # running probe cycle: None or
        # {"kind": "WORK"|"TOOL"|"CAL", "state": "ARMED"|"RUNNING", "t0": s}
        self._probe = None
        self._probe_note = ""
        self._tsetter = {}
        self.wcs_vals = None             # machine units, seeded on entry

    def on_enter(self):
        self.chapter = getattr(self, "chapter", "OFFSET")
        self.sub = getattr(self, "sub", "GEOM")          # WEAR | GEOM
        self.scroll = 0
        self._read_table()
        self._refresh_wcs()
        self._enter_chapter(self.chapter)

    def active_keys(self):
        ch = self.chapter
        if ch == "OFFSET":                       # WEAR/GEOM share the row
            return (ch, self.sub)
        if ch in PROBE_PAGES:                    # chapter POUT -> label OUT
            return tuple(lbl for pg, lbl in PROBE_PAGE_KEYS if pg == ch)
        return (ch,)

    # ------------------------------------------------------------- root menu
    def _root_row(self, extra=None):
        row = [
            K("OFFSET", lambda: self._enter_chapter("OFFSET")),
            K("SETING", lambda: self._enter_chapter("SETING")),
            K("WORK",   lambda: self._enter_chapter("WORK")),
        ]
        row += extra or []
        while len(row) < 9:
            row.append(K())
        row.append(K("(OPRT)", self._oprt))
        return row

    def _enter_chapter(self, name):
        self.chapter = name
        if name == "OFFSET":
            self.set_root(self._root_row([
                K("WEAR", lambda: self._set_sub("WEAR")),
                K("GEOM", lambda: self._set_sub("GEOM")),
                K("PROBE", lambda: self._enter_chapter("TPROBE")),
            ]))
            self._build_tool_cursor()
        elif name == "SETING":
            self.set_root(self._root_row())
            self._build_setting_cursor()
        elif name == "WORK":
            self._refresh_wcs()
            self.set_root(self._root_row([
                K("PROBE", lambda: self._enter_chapter("WPROBE")),
            ]))
            self._build_work_cursor()
        elif name == "TPROBE":
            self.cursor = FieldCursor(cols=1)      # nothing to edit here
            self._tsetter = self._read_toolsetter()
            self.set_root([
                K("Z.PRB", lambda: self._confirm(self._tprobe_exec)),
                K(), K(), K(), K(), K(), K(), K(),
                K("RETURN", lambda: self._enter_chapter("OFFSET")),
                K(),
            ])
        elif name == "WPROBE":
            self._enter_chapter("POUT")
        elif name in PROBE_PAGES:
            self._build_probe_grid()
            row = [K(lbl, lambda n=pg: self._enter_chapter(n))
                   for pg, lbl in PROBE_PAGE_KEYS]      # six page keys
            row += [K("P.SET", lambda: self._enter_chapter("PSET")),
                    K("EXEC", lambda: self._confirm(self._wprobe_exec)),
                    K(),
                    K("RETURN", lambda: self._enter_chapter("WORK"))]
            self.set_root(row)
        elif name == "PSET":
            self._build_pset_cursor()
            self.set_root([
                K("INPUT", lambda: self.cursor.commit(self.app.input.take())),
                K(), K(), K(), K(), K(), K(), K(),
                K("RETURN", lambda: self._enter_chapter("POUT")),
                K(),
            ])

    def _set_sub(self, sub):
        self.sub = sub
        self._build_tool_cursor()

    # ------------------------------------------------------------- OPRT menus
    def _oprt(self):
        if self.chapter == "OFFSET":
            page1 = [
                K("NO.SRH", self._no_srh),
                K("MEASUR", self._tool_measure),
                K("INP.C.", self._input_counter),
                K("+INPUT", lambda: self.cursor.commit_add(self.app.input.take())),
                K("INPUT",  lambda: self.cursor.commit(self.app.input.take())),
            ]
            page2 = [
                K("CLEAR", self._clear_menu),
                K("READ",  lambda: self._confirm(self._read_exec)),
                K("PUNCH", lambda: self._confirm(self._punch_exec)),
            ]
            self.push([page1, page2])
        elif self.chapter == "SETING":
            self.push([
                K("INPUT", lambda: self.cursor.commit(self.app.input.take() or " ")),
            ])
        elif self.chapter == "WORK":
            self.push([
                K("NO.SRH", self._no_srh),
                K("MEASUR", self._work_measure),
                K("+INPUT", lambda: self.cursor.commit_add(self.app.input.take())),
                K("INPUT",  lambda: self.cursor.commit(self.app.input.take())),
            ])

    def _clear_menu(self):
        self.push([
            K("ALL",  lambda: self._clear("ALL"),  kind=ACT),
            K("WEAR", lambda: self._clear("WEAR"), kind=ACT),
            K("GEOM", lambda: self._clear("GEOM"), kind=ACT),
        ])

    def _confirm(self, exec_fn):
        self.push([K("CAN", self.pop), K("EXEC", lambda: (exec_fn(), self.pop()))])

    # ------------------------------------------------------------- tool data
    # Internal wear/geom values are MACHINE units; conversion only at the
    # MDI strings (interp units) and the display/entry membrane.

    def _tools(self):
        # real entries only: skip index 0 spindle slot, id -1 empties
        return [t for t in self.app.stat.tool_table[1:] if t.id > 0]

    def _tool_tbl_path(self):
        try:
            ini = linuxcnc.ini(self.app.stat.ini_filename)
            tbl = ini.find("EMCIO", "TOOL_TABLE") or "tool.tbl"
        except linuxcnc.error:
            tbl = "tool.tbl"
        return os.path.join(os.path.dirname(self.app.stat.ini_filename), tbl)

    # tool.tbl is the single source of truth: LinuxCNC's Z/diameter is
    # geometry + wear combined, and the wear share is a ";W:Z.. R.." token
    # at the start of the tool's comment (machine units, tooltbl.py). A G10
    # L1 from anywhere keeps the comment, so the split cannot drift apart.
    def _read_table(self):
        """Refresh self.wear / self.comments (user text only, token
        stripped) from the .tbl. Runs the one-time JSON import first."""
        path = self._tool_tbl_path()
        try:
            tbl = tooltbl.read_table(path)
        except OSError:
            tbl = {}
        # an unreadable/empty table is not "no tokens": leave the JSON alone
        if (tbl and os.path.exists(LEGACY_WEAR_FILE)
                and self._migrate_legacy_wear(path, tbl)):
            try:
                tbl = tooltbl.read_table(path)
            except OSError:
                tbl = {}
        self.wear = {tid: dict(e["wear"]) for tid, e in tbl.items()}
        self.comments = {tid: e["comment"] for tid, e in tbl.items()}

    def _migrate_legacy_wear(self, path, tbl):
        """Old tool_wear.json -> tokens, once. Imported only when no tool
        in the table carries a token yet; either way the JSON is renamed
        to .migrated so this never runs twice. True if the .tbl changed."""
        changed = False
        try:
            if any(e["tagged"] for e in tbl.values()):
                os.replace(LEGACY_WEAR_FILE, LEGACY_WEAR_FILE + ".migrated")
                self.app.alarm("OLD TOOL_WEAR.JSON IGNORED (RENAMED)")
                return False
            with open(LEGACY_WEAR_FILE) as f:
                old = {int(k): v for k, v in json.load(f).items()}
            for tid, w in old.items():
                z, r = float(w.get("Z", 0.0)), float(w.get("R", 0.0))
                if tid in tbl and (z or r):
                    tooltbl.write_comment(path, tid, z, r, tbl[tid]["comment"])
                    changed = True
            os.replace(LEGACY_WEAR_FILE, LEGACY_WEAR_FILE + ".migrated")
            if changed:
                self.app.command.load_tool_table()
            self.app.alarm("TOOL WEAR MOVED INTO TOOL.TBL")
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.app.alarm(f"TOOL WEAR MIGRATION FAILED: {e}")
        return changed

    def _write_wear_token(self, tid, z, r, text=None):
        """File half of every wear/comment change. False (alarm raised) on
        failure. Callers then load_tool_table so LinuxCNC's in-memory
        comment -- which its own G10 rewrites put back -- carries it."""
        if text is None:
            text = self.comments.get(tid, "")
        try:
            tooltbl.write_comment(self._tool_tbl_path(), tid, z, r, text)
        except OSError as e:
            self.app.alarm(f"TOOL.TBL WRITE FAILED: {e}")
            return False
        except KeyError:
            self.app.alarm(f"T{tid} NOT FOUND IN TOOL.TBL")
            return False
        return True

    def _reload_table(self):
        """load_tool_table and wait for task to take it, so a following
        G10 L1 rewrites the file from the fresh copy (comment included)."""
        self.app.command.load_tool_table()
        self.app.command.wait_complete()

    def _set_tool_comment(self, tid, text):
        """Rewrite the user text of tool tid's comment, keeping its wear
        token, then reload so LinuxCNC's in-memory copy carries it."""
        w = self._wear_of(tid)
        if not self._write_wear_token(tid, w["Z"], w["R"], text):
            return
        self.app.command.load_tool_table()
        self._read_table()

    def _wear_of(self, tid):
        """In-memory copy of the .tbl token; not persisted from here."""
        return self.wear.setdefault(tid, {"Z": 0.0, "R": 0.0})

    def _geom_of(self, tid):
        # LinuxCNC stores geom+wear combined (we wrote it), so geom = stored - wear
        for t in self.app.stat.tool_table:
            if t.id == tid:
                w = self._wear_of(tid)
                return {"Z": t.zoffset - w["Z"], "R": t.diameter / 2 - w["R"]}
        return {"Z": 0.0, "R": 0.0}

    def _g10(self, code):
        """Blocking offset write. False (alarm raised) when the mode switch
        fails or task has not acknowledged the block after 5 s; callers
        must not update their local copy then."""
        try:
            ok = self.app.mdi(code)
        except linuxcnc.error as e:
            self.app.alarm(str(e))
            return False
        if not ok:
            self.app.alarm("G10 NOT ACKNOWLEDGED")
        return ok

    def _write_tool(self, tid, geom=None):
        """G10 L1 with geom + wear. Pass geom when wear has just changed:
        _geom_of derives it from the stored value minus the CURRENT wear,
        so it must be taken before the wear is edited."""
        g, w = geom or self._geom_of(tid), self._wear_of(tid)
        z = self.app.machine_to_interp(g['Z'] + w['Z'])
        r = self.app.machine_to_interp(g['R'] + w['R'])
        if not self._g10(f"G10 L1 P{tid} Z{z:.4f} R{r:.4f}"):
            return False
        self.app.command.load_tool_table()
        return True

    def _set_tool_val(self, tid, col, text, table):
        """Cursor/INPUT path: text is display units."""
        try:
            typed = float(text)
        except ValueError:
            return
        self._set_tool_machine(tid, col,
                               self.app.disp_to_machine("Z", typed), table)

    def _set_tool_machine(self, tid, col, val, table):
        """Machine-units entry point (MEASUR and probe cycles)."""
        axis = "Z" if col == 0 else "R"
        if table == "WEAR":
            # order: token into the file, reload (LinuxCNC's in-memory
            # comment now has it), then the G10 -- whose table rewrite
            # keeps that comment.
            geom = self._geom_of(tid)            # before the wear changes
            w = self._wear_of(tid)
            old = dict(w)
            new = dict(w, **{axis: val})
            if not self._write_wear_token(tid, new["Z"], new["R"]):
                return
            self._reload_table()
            w.update(new)
            if not self._write_tool(tid, geom):
                # G10 refused: put the old token back so stored - wear
                # still gives the old geometry
                w.update(old)
                if self._write_wear_token(tid, old["Z"], old["R"]):
                    self.app.command.load_tool_table()
            self._read_table()
        else:                                   # GEOM: adjust so geom == val
            w = self._wear_of(tid)[axis]
            mdi_val = self.app.machine_to_interp(val + w)
            if not self._g10(f"G10 L1 P{tid} {axis}{mdi_val:.4f}"):
                return
            self.app.command.load_tool_table()

    def _get_tool_val(self, tid, col, table):
        """Machine units."""
        axis = "Z" if col == 0 else "R"
        return (self._wear_of(tid) if table == "WEAR" else self._geom_of(tid))[axis]

    def _build_tool_cursor(self):
        self.cursor = FieldCursor(cols=3)
        for t in self._tools():
            for col in (0, 1):
                self.cursor.add(0, 0, 300, 64,     # rects positioned at draw time
                    setter=lambda s, tid=t.id, c=col: self._set_tool_val(
                        tid, c, s, self.sub),
                    getter=lambda tid=t.id, c=col: self.app.machine_to_disp(
                        "Z", self._get_tool_val(tid, c, self.sub)))
            # comment column: setter only (no getter -> +INPUT safely no-ops)
            self.cursor.add(0, 0, 420, 64,
                setter=lambda s, tid=t.id: self._set_tool_comment(tid, s))

    # ------------------------------------------------------------- settings
    def _build_setting_cursor(self):
        self.cursor = FieldCursor(cols=1)
        self.cursor.add(0, 0, 900, 60, setter=self._set_units)

    def _set_units(self, text):
        t = text.strip().upper()
        if t in self.UNIT_CYCLE:
            self.app.display_units = t
        else:                                    # any other commit = cycle
            i = self.UNIT_CYCLE.index(self.app.display_units)
            self.app.display_units = self.UNIT_CYCLE[(i + 1) % 4]
        self.app.save_persist()

    # ------------------------------------------------------------- work data
    def _refresh_wcs(self):
        """Page-entry seed. While a program/MDI runs the var file and the
        offsets are in motion, so the local copy is kept (read once if
        there is none yet)."""
        if (self.wcs_vals is None
                or self.app.stat.interp_state == linuxcnc.INTERP_IDLE):
            self.wcs_vals = self._read_var_wcs()

    def _wcs_from_stat(self, sys_idx):
        """Copy the ACTIVE system's offsets from stat (poll first). The var
        file lags a G10 until task's next command, stat does not."""
        st = self.app.stat
        if sys_idx + 1 != st.g5x_index:
            return False
        for ax in "XYZABC":
            self.wcs_vals[sys_idx][ax] = st.g5x_offset[AXIS_IDX[ax]]
        return True

    def _read_var_wcs(self):
        """All nine systems from the var file: {sys_index: {axis: val}}.
        Machine units. Read on page entry only; edits update locally.
        The var file is flushed only on task's NEXT command, so a G10 just
        made (e.g. by a probe) is not in it yet: the active system is taken
        from stat.g5x_offset, which is current."""
        out = {i: {ax: 0.0 for ax in "XYZABC"} for i in range(9)}
        try:
            ini = linuxcnc.ini(self.app.stat.ini_filename)
            var = ini.find("RS274NGC", "PARAMETER_FILE") or "linuxcnc.var"
            var = os.path.join(os.path.dirname(self.app.stat.ini_filename), var)
            params = {}
            with open(var) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        params[int(parts[0])] = float(parts[1])
            for s in range(9):
                base = WCS_PARAM_BASE + 20 * s
                for ax, off in (("X", 0), ("Y", 1), ("Z", 2),
                                ("A", 3), ("B", 4), ("C", 5)):
                    out[s][ax] = params.get(base + off, 0.0)
        except (OSError, ValueError):
            pass
        st = self.app.stat
        a = st.g5x_index - 1
        if 0 <= a < 9:
            for ax in "XYZABC":
                out[a][ax] = st.g5x_offset[AXIS_IDX[ax]]
        return out

    def _set_wcs(self, sys_idx, ax, text):
        try:
            typed = float(text)
        except ValueError:
            return
        machine_val = self.app.disp_to_machine(ax, typed)
        mdi_val = (self.app.machine_to_interp(machine_val)
                   if ax in self.app.LINEAR_AXES else machine_val)
        if not self._g10(f"G10 L2 P{sys_idx + 1} {ax}{mdi_val:.4f}"):
            return
        self.wcs_vals[sys_idx][ax] = machine_val       # store machine units
        self.app.stat.poll()                           # fresh after wait_complete
        self._wcs_from_stat(sys_idx)

    def _build_work_cursor(self):
        axes = self.app.axes
        self.cursor = FieldCursor(cols=len(axes))
        for s in range(9):
            for ax in axes:
                self.cursor.add(0, 0, self.WCOL_W, 56,
                    setter=lambda t, si=s, a=ax: self._set_wcs(si, a, t),
                    getter=lambda si=s, a=ax: self.app.machine_to_disp(
                        a, self.wcs_vals[si][a]))

    # ------------------------------------------------------------- OPRT leaves
    def _no_srh(self):
        txt = self.app.input.take().strip()
        try:
            n = int(float(txt))
        except ValueError:
            return
        if self.chapter == "OFFSET":
            for row, t in enumerate(self._tools()):
                if t.id == n:
                    self.cursor.goto_row(row)
                    return
        elif self.chapter == "WORK":
            if 1 <= n <= 9:
                self.cursor.goto_row(n - 1)

    def _parse_axis_val(self, txt):
        txt = txt.strip()
        if txt and txt[0] in self.app.axes:
            try:
                return txt[0], float(txt[1:])
            except ValueError:
                pass
        return None, None

    def _tool_measure(self):
        """MEASUR on tool page: Z<ref> -> length = machine Z - ref.
        Typed ref is display units. Probe cycles call
        _set_tool_machine directly."""
        ax, val = self._parse_axis_val(self.app.input.take())
        if ax != "Z":
            return
        row = self.cursor.row()
        tools = self._tools()
        if row < len(tools):
            tid = tools[row].id
            ref_m = self.app.disp_to_machine("Z", val)
            length = self.app.stat.actual_position[2] - ref_m
            self._set_tool_machine(tid, 0, length, "GEOM")

    def _work_measure(self):
        """MEASUR on work page: G10 L20 — current position becomes <val>.
        Typed value is display units."""
        ax, val = self._parse_axis_val(self.app.input.take())
        if ax is None:
            return
        sys_idx = self.cursor.row()
        val_m = self.app.disp_to_machine(ax, val)
        mdi_val = (self.app.machine_to_interp(val_m)
                   if ax in self.app.LINEAR_AXES else val_m)
        if not self._g10(f"G10 L20 P{sys_idx + 1} {ax}{mdi_val:.4f}"):
            return

        idx = AXIS_IDX[ax]
        st = self.app.stat
        if sys_idx + 1 == st.g5x_index:
            st.poll()                                    # fresh after wait_complete
            self.wcs_vals[sys_idx][ax] = st.g5x_offset[idx]
        else:
            # L20 semantics: offset = machine pos - g92 - desired reading
            self.wcs_vals[sys_idx][ax] = (st.actual_position[idx]
                                          - st.g92_offset[idx] - val_m)

    def _input_counter(self):
        """INP.C.: axis letter -> write the RELATIVE counter value into the
        cursored field. Setters expect display units, so convert."""
        txt = self.app.input.take().strip()
        if txt not in tuple(self.app.axes):
            return
        idx = AXIS_IDX[txt]
        rel = self.app.stat.actual_position[idx] - self.app.rel_origin[idx]
        self.cursor.commit(f"{self.app.machine_to_disp(txt, rel):.4f}")

    def _clear(self, what):
        tools = self._tools()
        geoms = {t.id: self._geom_of(t.id) for t in tools}   # before any change
        if what in ("ALL", "WEAR"):
            for t in tools:                      # one pass of zero tokens
                w = self._wear_of(t.id)
                if w["Z"] or w["R"]:
                    if not self._write_wear_token(t.id, 0.0, 0.0):
                        self.app.command.load_tool_table()
                        self._read_table()
                        return
                self.wear[t.id] = {"Z": 0.0, "R": 0.0}
            self._reload_table()
        if what == "ALL":
            for t in tools:
                if not self._g10(f"G10 L1 P{t.id} Z0 R0"):
                    break
        elif what == "GEOM":                     # geometry 0: stored = wear
            for t in tools:
                w = self._wear_of(t.id)
                z = self.app.machine_to_interp(w["Z"])
                r = self.app.machine_to_interp(w["R"])
                if not self._g10(f"G10 L1 P{t.id} Z{z:.4f} R{r:.4f}"):
                    break
        elif what == "WEAR":                     # re-apply geom, wear now 0
            for t in tools:
                if not self._write_tool(t.id, geoms[t.id]):
                    break
        self.app.command.load_tool_table()
        self._read_table()
        self.pop()

    def _read_exec(self):
        self.app.command.load_tool_table()
        self._read_table()

    def _punch_exec(self):
        # Nothing to save: every edit already landed in tool.tbl (G10 L1
        # and the wear token). Kept as a reload, same as READ.
        self._read_exec()

    # ------------------------------------------------------------- draw
    def draw(self, renderer, area):
        f = self.app.font
        self._poll_probe(renderer, f)
        if self.chapter == "OFFSET":
            self._draw_tools(renderer, f)
        elif self.chapter == "SETING":
            self._draw_settings(renderer, f)
        elif self.chapter == "WORK":
            self._draw_work(renderer, f)
        elif self.chapter == "TPROBE":
            self._draw_tprobe(renderer, f)
        elif self.chapter in PROBE_PAGES:
            self._draw_wprobe(renderer, f)
        elif self.chapter == "PSET":
            self._draw_pset(renderer, f)

    def _scroll_to_cursor(self, nrows):
        row = self.cursor.row()
        if row < self.scroll:
            self.scroll = row
        elif row >= self.scroll + self.VISIBLE_ROWS:
            self.scroll = row - self.VISIBLE_ROWS + 1
        self.scroll = max(0, min(self.scroll, max(0, nrows - self.VISIBLE_ROWS)))

    def _draw_tools(self, renderer, f):
        app = self.app
        title = ("TOOL OFFSET / "
                 + ("WEAR" if self.sub == "WEAR" else "GEOMETRY")
                 + f"  ({app.unit_tag()})")
        draw_line(renderer, f, title, 10, 70)
        draw_line(renderer, f,
                  "NO.       LENGTH(Z)          RADIUS        COMMENT", 10, 118)
        tools = self._tools()
        self._scroll_to_cursor(len(tools))
        for vis, row in enumerate(range(self.scroll,
                                        min(len(tools),
                                            self.scroll + self.VISIBLE_ROWS))):
            t = tools[row]
            y = self.TABLE_Y + vis * self.ROW_H
            draw_line(renderer, f, f"{t.id:03d}", 10, y)
            for col, x in ((0, 160), (1, 560)):
                i = row * 3 + col
                fld = self.cursor.fields[i]
                fld.x, fld.y, fld.w, fld.h = x, y - 2, 340, 60
                val = self._get_tool_val(t.id, col, self.sub)
                draw_field(renderer, f, app.fmt_axis("Z", val), i, self.cursor)
            i = row * 3 + 2
            fld = self.cursor.fields[i]
            fld.x, fld.y, fld.w, fld.h = 960, y - 2, 470, 60
            draw_field(renderer, f, self.comments.get(t.id, ""), i, self.cursor)

    def _draw_settings(self, renderer, f):
        draw_line(renderer, f, "SETTING", 10, 70)
        fld = self.cursor.fields[0]
        fld.x, fld.y, fld.w, fld.h = 10, self.TABLE_Y - 2, 900, 60
        draw_field(renderer, f,
                   f"DISPLAY UNITS   = {self.app.display_units}",
                   0, self.cursor)

    def _draw_work(self, renderer, f):
        app = self.app
        axes = app.axes
        draw_line(renderer, f,
                  f"WORK COORDINATE SYSTEM  ({app.unit_tag()})", 10, 70)
        hdr = "NO.   " + "".join(f"{ax:>10}   " for ax in axes)
        draw_line(renderer, f, hdr, 10, 118)
        self._scroll_to_cursor(9)
        active = app.stat.g5x_index
        for vis, s in enumerate(range(self.scroll,
                                      min(9, self.scroll + self.VISIBLE_ROWS))):
            y = self.TABLE_Y + vis * self.ROW_H
            draw_line(renderer, f,
                      WCS_NAMES[s] + ("*" if s + 1 == active else " "), 10, y)
            for a, ax in enumerate(axes):
                i = s * len(axes) + a
                fld = self.cursor.fields[i]
                fld.x, fld.y, fld.w, fld.h = (self.WCOL_X0 + a * self.WCOL_PITCH,
                                              y - 2, self.WCOL_W, 60)
                draw_field(renderer, f,
                           app.fmt_axis(ax, self.wcs_vals[s][ax]),
                           i, self.cursor)

    # ------------------------------------------------------------- probing
    def _pdefaults(self):
        """persist["probe"], MACHINE units (see PROBE_PARAMS). Keys no
        longer in PROBE_FIELDS (e.g. an old zero_height) are dropped."""
        d = self.app.persist.setdefault("probe", {})
        stale = [k for k in d if k not in PROBE_KIND]
        for k in stale:
            del d[k]
        for key, _lbl, dflt, _kind in PROBE_FIELDS:
            d.setdefault(key, dflt)
        if stale:
            self.app.save_persist()
        return d

    def _pset_disp(self, key):
        """Stored machine value -> display units (LEN and FEED scale by the
        same factor; NUM is unitless)."""
        v = self._pdefaults()[key]
        if PROBE_KIND[key] == "NUM":
            return v
        return self.app.machine_to_disp("Z", v)

    def _pset_text(self, key):
        v = self._pset_disp(key)
        if PROBE_KIND[key] == "NUM":
            return f"{v:g}"
        t = f"{v:.4f}".rstrip("0").rstrip(".")   # 0.0394 IN stays visible
        return "0" if t == "-0" else t

    def _pset_val(self, key, text):
        """Typed value is display units for LEN/FEED."""
        try:
            v = float(text)
        except ValueError:
            return
        if PROBE_KIND[key] != "NUM":
            v = self.app.disp_to_machine("Z", v)
        self._pdefaults()[key] = v
        self.app.save_persist()

    def _build_pset_cursor(self):
        self.cursor = FieldCursor(cols=1)
        for key, _lbl, _d, _kind in PROBE_FIELDS:
            self.cursor.add(0, 0, 300, 52,
                setter=lambda t, k=key: self._pset_val(k, t),
                getter=lambda k=key: self._pset_disp(k))

    def _build_probe_grid(self):
        self.cursor = FieldCursor(cols=3)
        for _ in range(9):
            self.cursor.add(0, 0, 80, 80)      # selection = cursor position
        page = PROBE_PAGES.get(self.chapter, {})
        # start on the centre Z where it exists, else the first live cell
        self.cursor.idx = 4 if (1, 1) in page else min(
            (r * 3 + c for (r, c) in page), default=0)

    def _grid_cell(self):
        page = PROBE_PAGES.get(self.chapter, {})
        return page.get((self.cursor.idx // 3, self.cursor.idx % 3))

    def _grid_macro(self):
        cell = self._grid_cell()
        return cell[0] if cell else None

    def on_touch(self, x, y):
        """Tap a probe grid cell to select it — same effect as the arrow keys,
        and never a path to motion: EXEC and its confirm still run the macro.
        Only the grid pages are touchable; the scrolling tables lay out rects
        for visible rows only, so their off-screen rects are stale."""
        if self.chapter not in PROBE_PAGES or not self.cursor:
            return False
        for i, f in enumerate(self.cursor.fields):
            if f.x <= x < f.x + f.w and f.y <= y < f.y + f.h:
                self.cursor.idx = i
                return True
        return False

    def _probe_ready(self):
        st = self.app.stat
        if st.estop or st.task_state != linuxcnc.STATE_ON:
            self.app.alarm("MACHINE NOT READY")
            return False
        if self._probe or st.interp_state != linuxcnc.INTERP_IDLE:
            self.app.alarm("PROGRAM RUNNING")
            return False
        # Args are converted to the current interp units, but the macros'
        # own constants and the [TOOLSETTER] ini values are not: refuse in
        # the foreign mode.
        if (st.program_units == 2) != (st.linear_units == 1.0):
            self.app.alarm("SWITCH TO MACHINE UNITS (G20/G21) FIRST")
            return False
        return True

    def _arm_probe(self, kind, code):
        """Fire the macro and start watching for it. Not armed on a mode
        failure, so no PROBING banner and no read-back."""
        try:
            self.app.mdi_async(code)
        except linuxcnc.error as e:
            self.app.alarm(str(e))
            return
        self._probe = {"kind": kind, "state": "ARMED",
                       "t0": time.monotonic()}

    def _wprobe_exec(self):
        name = self._grid_macro()
        if not name or not self._probe_ready():
            return
        d = self._pdefaults()
        keys = [k for k, _l, _x, _kind in PROBE_PARAMS]
        # #16 is wco_rotation to an edge-angle macro but cal_diameter to a cal
        # macro, so the tail is picked by family — never sent to both.
        if name in ANG_MACROS:
            keys += ["wco_rot"]
        elif name in CAL_MACROS:
            keys += ["cal_dia", "cal_x_width", "cal_y_width", "cal_axis"]
        # stored machine units -> interp units; :.4f never emits exponents
        args = " ".join(
            f"[{d[k]:g}]" if PROBE_KIND[k] == "NUM"
            else f"[{self.app.machine_to_interp(d[k]):.4f}]"
            for k in keys)
        self._probe_note = ""
        # CAL derives a stylus correction rather than setting an offset
        self._arm_probe("CAL" if self.chapter == "PCAL" else "WORK",
                        f"o<{name}> call {args}")

    def _tprobe_exec(self):
        if any(not self._tsetter.get(k) for k, _l in self.TSETTER_KEYS):
            self.app.alarm("TOOLSETTER NOT CONFIGURED - SEE INI [TOOLSETTER]")
            return
        if not self._probe_ready():
            return
        self._arm_probe("TOOL", f"o<{TOOL_SENSOR_MACRO}> call")

    def _draw_pset(self, renderer, f):
        draw_line(renderer, f,
                  f"PROBE SETTINGS  ({self.app.unit_tag()})", 10, 70)
        self._scroll_to_cursor(len(PROBE_FIELDS))
        for vis, i in enumerate(range(self.scroll,
                                      min(len(PROBE_FIELDS),
                                          self.scroll + self.VISIBLE_ROWS))):
            key, label, _x, _kind = PROBE_FIELDS[i]
            y = 150 + vis * 66
            draw_line(renderer, f, label, 10, y)
            fld = self.cursor.fields[i]
            fld.x, fld.y, fld.w, fld.h = 560, y - 2, 300, 56
            draw_field(renderer, f, self._pset_text(key), i, self.cursor)

    GRID_X, GRID_Y, CELL = 40, 180, 100

    def _draw_wprobe(self, renderer, f):
        page = PROBE_PAGES[self.chapter]
        title = dict(PROBE_PAGE_KEYS)[self.chapter]
        draw_line(renderer, f, f"WORK PROBE / {title}", 10, 70)
        for r in range(3):
            for c in range(3):
                i = r * 3 + c
                x = self.GRID_X + c * (self.CELL + 6)
                y = self.GRID_Y + r * (self.CELL + 6)
                fld = self.cursor.fields[i]
                fld.x, fld.y, fld.w, fld.h = x, y, self.CELL, self.CELL
                cell = page.get((r, c))
                if self.cursor.is_current(i):
                    SDL_SetRenderDrawColor(renderer, *CURSOR_C, 255)
                    SDL_RenderFillRect(renderer,
                                       SDL_Rect(x, y, self.CELL, self.CELL))
                SDL_SetRenderDrawColor(renderer,
                                       *(GRID_ON_C if cell else GRID_OFF_C), 255)
                SDL_RenderDrawRect(renderer, SDL_Rect(x, y, self.CELL, self.CELL))
                if cell:
                    draw_probe_icon(renderer, cell[1], r, c, x, y, self.CELL)
        # large diagram of the selected cell — same routine, bigger box
        cell = self._grid_cell()
        if cell:
            draw_probe_icon(renderer, cell[1], self.cursor.idx // 3,
                            self.cursor.idx % 3, 420, 170, 260)
        st = self.app.stat
        draw_line(renderer, f, cell[0] if cell else "(empty cell)",
                  730, 200, WHITE if cell else RED)
        draw_line(renderer, f, f"WRITES TO: {WCS[st.g5x_index]}", 730, 260)
        if self.chapter == "PCAL" and cell:
            # Each cal macro positions off one field and compares against
            # another, and they are not always the same one — so name both.
            labels = dict((k, l) for k, l, _x, _kind in PROBE_FIELDS)
            for n, key in enumerate(CAL_FIELDS.get(cell[0], ())):
                draw_line(renderer, f,
                          f"{labels[key]}: {self._pset_text(key)}"
                          f" {self.app.unit_tag()}",
                          730, 320 + n * 46, DIM)
        else:
            draw_line(renderer, f, "DIMS IN [P.SET]", 730, 320,
                      DIM)
        if self._probe_note:
            draw_line(renderer, f, self._probe_note, 730, 510, RED)
        # The routine's own placement instructions, read from its .ngc. Full
        # width below the grid — the macro authors already wrapped these.
        if cell:
            lines = macro_help(cell[0])
            for n, txt in enumerate(lines):
                draw_line(renderer, f, txt, 40, 560 + n * 44,
                          DIM if n == 0 else ACCENT)

    # [TOOLSETTER] keys tool_length.ngc needs, all machine units. Order is
    # the on-screen order (column-major, two columns).
    TSETTER_KEYS = (("X", "SETTER X"), ("Y", "SETTER Y"),
                    ("Z_SAFE", "Z SAFE"), ("Z_START", "Z START"),
                    ("MAXPROBE", "MAX PROBE"), ("SEARCH_VEL", "SEARCH FEED"),
                    ("PROBE_VEL", "PROBE FEED"), ("BACKOFF", "BACKOFF"),
                    ("Z_REF", "Z REF"))
    TSETTER_OPTIONAL = "PROBE_INPUT"     # setter contact; pre-trip check only

    def _read_toolsetter(self):
        """[TOOLSETTER] values the macro runs from, read once on entry so the
        draw loop does no file I/O. None for any key the INI lacks."""
        keys = [k for k, _l in self.TSETTER_KEYS] + [self.TSETTER_OPTIONAL]
        try:
            ini = linuxcnc.ini(self.app.stat.ini_filename)
            return {k: ini.find("TOOLSETTER", k) for k in keys}
        except Exception:
            return {k: None for k in keys}

    def _draw_tprobe(self, renderer, f):
        draw_line(renderer, f, "TOOL LENGTH PROBE", 10, 70)
        st = self.app.stat
        draw_line(renderer, f, f"TOOL IN SPINDLE: T{st.tool_in_spindle:02d}",
                  10, 150)
        vals = self._tsetter
        for n, (key, label) in enumerate(self.TSETTER_KEYS):
            v = vals.get(key)
            draw_line(renderer, f, f"{label}: {v if v else 'NOT SET'}",
                      10 if n < 5 else 700, 230 + (n % 5) * 46,
                      DIM if v else RED)
        pin = vals.get(self.TSETTER_OPTIONAL)
        draw_line(renderer, f,
                  f"TRIP INPUT: {pin}" if pin else "TRIP INPUT: NONE (OPTIONAL)",
                  700, 230 + 4 * 46, DIM)
        if st.tool_in_spindle <= 0:
            draw_line(renderer, f, "NO TOOL LOADED", 10, 520, RED)
        else:
            draw_line(renderer, f,
                      "Z.PRB MEASURES LOADED TOOL, WRITES LENGTH, APPLIES G43",
                      10, 520, ACCENT)

    PROBE_START_S = 2.0      # ARMED this long with the interp idle = no start

    def tick(self):
        """Every frame, on every screen (ScreenManager.draw), so a probe
        started here completes correctly even if the operator pages away.
        mdi_async returns before task has picked the call up, so idle right
        after EXEC means "not started yet", not "done": ARMED -> RUNNING on
        the first non-idle frame, RUNNING -> done on the next idle one."""
        p = self._probe
        if not p:
            return
        idle = self.app.stat.interp_state == linuxcnc.INTERP_IDLE
        if p["state"] == "ARMED":
            if not idle:
                p["state"] = "RUNNING"
            elif time.monotonic() - p["t0"] > self.PROBE_START_S:
                self._probe = None
                self.app.alarm("PROBE DID NOT START")
        elif idle:
            self._probe = None
            self._probe_done(p["kind"])

    def _poll_probe(self, renderer, f):
        """PROBING banner while a probe is armed or running."""
        if self._probe:
            draw_line(renderer, f, "PROBING...", 730, 130, RED)

    def _probe_done(self, kind):
        st = self.app.stat
        if kind == "WORK":
            # macros write with G10 L20 P0 = the active system; the var file
            # is not flushed yet, stat is
            st.poll()
            self._wcs_from_stat(st.g5x_index - 1)
        elif kind == "TOOL":
            # Contract with tool_length.ngc: it writes the MEASURED length
            # with G10 L1 (no wear) and G43s it. The .tbl comment, token
            # included, survives that rewrite, so wear is read back here
            # and re-added as geometry = measured; then G43 again so the
            # active length includes the wear too.
            self.app.command.load_tool_table()
            st.poll()
            self._read_table()
            tid = st.tool_in_spindle
            if tid > 0 and self._wear_of(tid)["Z"] != 0:
                measured = next((t.zoffset for t in st.tool_table
                                 if t.id == tid), None)
                if measured is not None:
                    self._set_tool_machine(tid, 0, measured, "GEOM")
                    try:
                        if not self.app.mdi("G43"):
                            self.app.alarm("G43 NOT ACKNOWLEDGED")
                    except linuxcnc.error as e:
                        self.app.alarm(str(e))
        elif kind == "CAL":
            # the macro computes the correction; entering it is manual
            # until the storage location is confirmed on the machine
            self._probe_note = "CAL DONE - ENTER RESULT AS CAL OFFSET"
