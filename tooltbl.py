"""Tool wear stored inside LinuxCNC's tool table (tool.tbl).

LinuxCNC's table holds one Z/diameter per tool: geometry + wear combined.
The GUI's wear share lives in the same file, as a tagged prefix on the
tool's comment, so the table is the single source of truth:

    T1 P1 D+0.000000 Z+0.000000 ;W:Z0.012000 R-0.003000 MAIN TOOL

z and r are MACHINE units (%.6f). LinuxCNC keeps the comment text verbatim
when it rewrites the line (G10 L1, load_tool_table), so the token survives.

Token rule (join_comment): written whenever either wear value is non-zero;
omitted when both are 0.0 -- unless the user text itself looks like a
token, in which case a zero token is written so the text cannot be
mistaken for wear on the next read.

No sdl2/linuxcnc imports: unit-testable.
"""

import re

import configfile

WEAR_RE = re.compile(
    r'^\s*W:Z(?P<z>[-+0-9.eE]+)\s+R(?P<r>[-+0-9.eE]+)\s*(?P<rest>.*)$')
_T_RE = re.compile(r'^\s*[Tt](\d+)\b')


def _parse_token(comment):
    """(z, r, rest) or None when comment carries no valid token."""
    m = WEAR_RE.match(comment or "")
    if not m:
        return None
    try:
        return float(m.group("z")), float(m.group("r")), m.group("rest")
    except ValueError:
        return None


def split_comment(comment):
    """Comment text (after ';') -> (wear_z, wear_r, user_text).
    (0.0, 0.0, comment) when there is no token."""
    comment = (comment or "").strip()
    tok = _parse_token(comment)
    if tok is None:
        return 0.0, 0.0, comment
    z, r, rest = tok
    return z, r, rest.strip()


def join_comment(wear_z, wear_r, user_text):
    """Inverse of split_comment. See the module doc for the token rule."""
    text = (user_text or "").strip()
    if wear_z == 0.0 and wear_r == 0.0 and _parse_token(text) is None:
        return text
    tok = f"W:Z{float(wear_z):.6f} R{float(wear_r):.6f}"
    return f"{tok} {text}" if text else tok


def _read_lines(path):
    with open(path, newline="", encoding="utf-8",
              errors="surrogateescape") as f:
        return f.read().splitlines(keepends=True)


def read_table(path):
    """{tid: {"wear": {"Z": z, "R": r}, "comment": user_text,
    "line": index, "tagged": bool}} from the T lines of `path`.
    First line wins for a duplicated tool. Raises OSError."""
    out = {}
    for i, line in enumerate(_read_lines(path)):
        m = _T_RE.match(line)
        if not m:
            continue
        tid = int(m.group(1))
        if tid in out:
            continue
        raw = line.split(";", 1)[1] if ";" in line else ""
        z, r, text = split_comment(raw)
        out[tid] = {"wear": {"Z": z, "R": r}, "comment": text, "line": i,
                    "tagged": _parse_token(raw.strip()) is not None}
    return out


def write_comment(path, tid, wear_z, wear_r, user_text):
    """Rewrite only the ';...' part of tool tid's line (every other byte
    kept, including line endings). KeyError if no T line for tid.
    Raises OSError."""
    lines = _read_lines(path)
    for i, line in enumerate(lines):
        m = _T_RE.match(line)
        if not (m and int(m.group(1)) == tid):
            continue
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        base = body.split(";", 1)[0].rstrip()
        c = join_comment(wear_z, wear_r, user_text)
        lines[i] = (f"{base} ;{c}" if c else base) + eol
        configfile.atomic_write(path, "".join(lines), backup=False)
        return
    raise KeyError(tid)
