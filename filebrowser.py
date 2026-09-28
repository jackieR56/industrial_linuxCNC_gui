#!/usr/bin/env python3
# filebrowser.py — the DIR/USB directory lister shared by the PROG screen
#                  (program directory) and the SYSTEM screen (config
#                  directory). One instance per chapter; it remembers the
#                  directory it is in, never leaves its root, lists dirs
#                  first, and draws the cursored rows. The verbs (SELECT,
#                  NEW, COPY, ...) stay with each screen.

import os

from sdl2 import *


class FileBrowser:
    def __init__(self, roots_fn, rows, hide=lambda name: name.startswith(".")):
        self.roots_fn = roots_fn        # () -> root directory, None = absent
        self.rows = rows                # rows drawn per page (PAGE UP/DOWN)
        self.hide = hide                # name -> True to leave it unlisted
        self.path = None                # directory listed; kept between visits
        self.entries = []               # (name, is_dir, path)
        self.cur = 0
        self.scroll = 0

    def refresh(self, land_on=None):
        """Re-list self.path (falling back to the root when it is unset or
        outside it). The cursor goes to the top, or onto the entry whose path
        is `land_on`. False, with nothing listed, when there is no root."""
        root = self.roots_fn()
        self.entries = []
        self.cur = 0
        self.scroll = 0
        if root is None:
            return False
        base = self.path
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
            if self.hide(n):
                continue
            p = os.path.join(base, n)
            self.entries.append((n, os.path.isdir(p), p))
        # dirs first, both halves alphabetical
        self.entries.sort(key=lambda e: (e[0] != "..", not e[1], e[0].upper()))
        self.path = base
        for i, e in enumerate(self.entries):     # land on the selected file
            if e[2] == land_on:
                self.cur = i
        return True

    def cur_entry(self):
        return self.entries[self.cur] if self.entries else None

    def on_key(self, sc):
        """Cursor movement. RETURN is the screen's (its SELECT verb)."""
        n = max(0, len(self.entries) - 1)
        if sc == SDL_SCANCODE_UP:
            self.cur = max(0, self.cur - 1); return True
        if sc == SDL_SCANCODE_DOWN:
            self.cur = min(n, self.cur + 1); return True
        if sc == SDL_SCANCODE_PAGEUP:
            self.cur = max(0, self.cur - self.rows); return True
        if sc == SDL_SCANCODE_PAGEDOWN:
            self.cur = min(n, self.cur + self.rows); return True
        return False

    def hit(self, y, vis_rows):
        """Touch: put the cursor on the row drawn at y. `vis_rows` is what
        draw() returned for the last frame."""
        for ry, idx in vis_rows:
            if ry - 2 <= y < ry + 56:
                self.cur = idx
                return True
        return False

    def draw(self, renderer, f, y0=130, row_h=60, mark=False,
             selected_path=None):
        """The listing below the screen's own title line. With mark, every
        row gets a two-character gutter and `selected_path` is shown as
        '> name' in ACCENT. Returns ((y, index), ...) of the rows drawn."""
        # screens.py imports this module, so its helpers are fetched late
        from screens import draw_line, KEY_HILITE, BLACK, WHITE, ACCENT
        if not self.entries:
            draw_line(renderer, f, "(empty)", 10, y0)
            return ()
        if self.cur < self.scroll:
            self.scroll = self.cur
        elif self.cur >= self.scroll + self.rows:
            self.scroll = self.cur - self.rows + 1
        vis = []
        for row, idx in enumerate(range(self.scroll,
                                        min(len(self.entries),
                                            self.scroll + self.rows))):
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
            color = WHITE
            if mark:
                sel = (path == selected_path)
                text = ("> " if sel else "  ") + text
                color = ACCENT if sel else WHITE
            y = y0 + row * row_h
            vis.append((y, idx))
            x, w = 10, 1300
            if idx == self.cur:
                SDL_SetRenderDrawColor(renderer, *KEY_HILITE, 255)
                SDL_RenderFillRect(renderer, SDL_Rect(x - 4, y - 2, w, 56))
                draw_line(renderer, f, text, x, y, BLACK)
            else:
                draw_line(renderer, f, text, x, y, color)
        return tuple(vis)
