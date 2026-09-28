#!/usr/bin/env python3
# lineeditor.py — FANUC-style line/word editing shared by the PROG screen
#                 EDIT chapter (over a plain list of program lines) and the
#                 SYSTEM screen TEXT chapter (over a configfile.LineDoc).
#
# The cursor is a line (cur) and a word on it (word): a word is a run of
# non-blanks. Lines are reached only through the four callables, so the
# owner decides what an edit means (LineDoc marks itself dirty). Drawing
# stays with each screen; they lay the text out differently.

import re


class LineEditor:
    def __init__(self, get_lines, set_line, insert_line, delete_line,
                 normalize=None, collapse=False, take_space=False):
        self.get_lines = get_lines       # () -> list of lines (None = no doc)
        self.set_line = set_line         # (i, text)
        self.insert_line_at = insert_line  # (i, text)
        self.delete_line_at = delete_line  # (i); leaves at least [""]
        # Text munging, per owner:
        #   normalize   applied to typed text before it goes in (PROG:
        #               space_words, M3S1000 -> M3 S1000)
        #   collapse    after INSERT / DEL.WRD squeeze runs of blanks to
        #               one and strip the line (PROG)
        #   take_space  DEL.WRD also removes the following space, or the
        #               preceding one at end of line (SYSTEM, which keeps
        #               the file's own spacing otherwise)
        self.normalize = normalize
        self.collapse = collapse
        self.take_space = take_space
        self.cur = 0                     # line index
        self.word = 0                    # word index on that line
        self.scroll = 0                  # first line drawn

    def _norm(self, text):
        return self.normalize(text) if self.normalize else text

    def _squeeze(self, text):
        return re.sub(r'[ \t]+', ' ', text).strip() if self.collapse else text

    def words(self):
        """[(start, end)] spans of whitespace-separated words on the
        cursored line."""
        line = self.get_lines()[self.cur]
        return [(m.start(), m.end()) for m in re.finditer(r'\S+', line)]

    def clamp_word(self):
        n = len(self.words()) if self.get_lines() is not None else 0
        self.word = max(0, min(self.word, max(0, n - 1)))

    def insert_word(self, text):
        """INSERT: text becomes new word(s) AFTER the selected word (start
        of line if the line is empty)."""
        text = self._norm(text).strip()
        if not text:
            return
        line = self.get_lines()[self.cur]
        words = self.words()
        if not words:
            self.set_line(self.cur, text)
            self.word = 0
        else:
            _s, end = words[self.word]
            new = line[:end] + " " + text + line[end:]
            self.set_line(self.cur, self._squeeze(new))
            self.word += 1
        self.clamp_word()

    def alter(self, text):
        """ALTER: replace the selected word with the text."""
        text = self._norm(text).strip()
        words = self.words()
        if not text or not words:
            return
        s, e = words[self.word]
        line = self.get_lines()[self.cur]
        self.set_line(self.cur, line[:s] + text + line[e:])

    def delete_word(self):
        """DEL.WRD: remove the selected word."""
        words = self.words()
        if not words:
            return
        s, e = words[self.word]
        line = self.get_lines()[self.cur]
        if self.take_space:
            # take the following space with the word, or the preceding one at EOL
            if e < len(line) and line[e] == " ":
                e += 1
            elif s > 0 and line[s - 1] == " ":
                s -= 1
        self.set_line(self.cur, self._squeeze(line[:s] + line[e:]))
        self.clamp_word()

    def alter_line(self, text):
        """ALT.LIN: replace the whole cursored line with the text."""
        self.set_line(self.cur, self._norm(text))
        self.word = 0

    def insert_line(self, text):
        """RETURN key: text becomes a new LINE after the cursored one."""
        self.insert_line_at(self.cur + 1, self._norm(text))
        self.cur += 1
        self.word = 0

    def delete_line(self):
        self.delete_line_at(self.cur)
        self.cur = min(self.cur, len(self.get_lines()) - 1)
        self.clamp_word()

    # ---- cursor
    def move(self, dy):
        """Lines up (dy < 0) or down."""
        if dy < 0:
            self.cur = max(0, self.cur + dy)
        else:
            self.cur = min(len(self.get_lines()) - 1, self.cur + dy)
        self.clamp_word()

    def move_word(self, dx):
        """Words left (dx < 0; nothing to clamp but zero) or right."""
        if dx < 0:
            self.word = max(0, self.word + dx)
        else:
            self.word += dx
            self.clamp_word()

    def page(self, n, rows):
        """PAGE UP (n = -1) / PAGE DOWN (n = +1) by `rows` lines."""
        self.move(n * rows)

    def goto(self, i):
        """Search hit: land on the first word of line i."""
        self.cur = i
        self.word = 0
        self.clamp_word()

    def scroll_to_cursor(self, rows):
        """Keep the cursored line inside a window of `rows` lines."""
        if self.cur < self.scroll:
            self.scroll = self.cur
        elif self.cur >= self.scroll + rows:
            self.scroll = self.cur - rows + 1
