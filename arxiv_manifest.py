#!/usr/bin/env python3
"""
arXiv paper manifest generator.

Scans a directory of unzipped arXiv source folders (arXiv-2305.19438v4/, ...)
and, for each one, writes an upload-ready pair into a separate output tree:

    output/2305.19438v4/2305.19438v4.tex          (copy of the main .tex)
    output/2305.19438v4/2305.19438v4_manifest.md  (arXiv ID, title, authors,
                                                   ordered figure list, and a
                                                   cross-check of every
                                                   \includegraphics reference
                                                   against the files on disk)

The original paper folders are never written to, renamed in, or deleted from.

Principles:
  * never renames anything on disk
  * never fuzzy-matches a tex reference to a differently-named file
  * flags structural surprises instead of guessing

Usage:
    python3 arxiv_manifest.py [papers_dir] [--output-dir DIR] [--only FOLDER]
                              [--main FILE] [--flatten] [--skip-existing]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import unicodedata
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- constants --

IMAGE_EXTS = {".png", ".pdf", ".eps", ".ps", ".jpg", ".jpeg",
              ".svg", ".tif", ".tiff", ".gif"}
# order pdflatex/graphicx itself would try when the tex omits the extension
GRAPHICS_SEARCH_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg"]
CAPTION_MAX = 80
MAX_INPUT_DEPTH = 8
SIZE_TIEBREAK = 1.2      # largest must be >20% bigger than runner-up to win

MANIFEST_SUFFIX = "_manifest.md"
OUTPUT_DIRNAME = "output"

# commands whose whole argument should vanish when flattening to plain text
# \'e -> é etc: combining marks, applied then NFC-normalised
ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308",
           "~": "\u0303", "=": "\u0304", ".": "\u0307", "u": "\u0306",
           "v": "\u030C", "c": "\u0327", "H": "\u030B", "r": "\u030A",
           "k": "\u0328", "b": "\u0331", "d": "\u0323"}
SPECIAL_LETTERS = {"ss": "ß", "o": "ø", "O": "Ø", "aa": "å", "AA": "Å",
                   "l": "ł", "L": "Ł", "ae": "æ", "AE": "Æ", "oe": "œ",
                   "OE": "Œ", "i": "i", "j": "j"}

# `{\color{blue}blue}` and `\textcolor{blue}{blue}` must lose the colour name
# but keep the text: killing the command plus its FIRST argument does both.
KILL_WITH_ARG = ["color", "textcolor", "colorbox", "pagecolor", "rowcolor",
                 "cellcolor", "definecolor",
                 "thanks", "footnote", "footnotemark", "email", "affiliation",
                 "altaffiliation", "address", "inst", "institute", "orcidlink",
                 "orcid", "ead", "fnref", "corref", "authorrunning", "date",
                 "label", "vspace", "hspace"]

# ------------------------------------------------------------- tex plumbing --


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def match_brace(s: str, i: int) -> int:
    """`i` indexes an opening '{'. Return index of its matching '}', or -1."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def read_arg(s: str, pos: int, allow_optional: bool = True):
    """Read a braced argument starting at/after `pos`, skipping [optional] args.

    Returns (content, index_after_arg) or (None, pos) if there is no braced arg.
    """
    i, n = pos, len(s)
    while True:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if allow_optional and i < n and s[i] == "[":
            j = s.find("]", i)
            if j == -1:
                return None, pos
            i = j + 1
            continue
        break
    if i < n and s[i] == "{":
        j = match_brace(s, i)
        if j == -1:
            return None, pos
        return s[i + 1:j], j + 1
    return None, pos


def strip_comments(text: str) -> str:
    """Drop LaTeX % comments (but not \\%), keeping line structure intact."""
    out = []
    for line in text.splitlines(True):
        i, cut = 0, None
        while i < len(line):
            c = line[i]
            if c == "\\":
                i += 2
                continue
            if c == "%":
                cut = i
                break
            i += 1
        if cut is None:
            out.append(line)
        else:
            out.append(line[:cut] + ("\n" if line.endswith("\n") else ""))
    return "".join(out)


def _remove_cmd_with_arg(s: str, names) -> str:
    for name in names:
        pat = re.compile(r"\\" + name + r"\b\s*\*?")
        while True:
            m = pat.search(s)
            if not m:
                break
            content, end = read_arg(s, m.end())
            s = s[:m.start()] + (s[end:] if content is not None else s[m.end():])
    return s


RE_NEWCMD = re.compile(r"\\(?:new|renew|provide)command\s*\*?\s*\{?\s*\\([A-Za-z]+)\s*\}?")
RE_DEF = re.compile(r"\\def\s*\\([A-Za-z]+)\s*(?=\{)")


def collect_macros(source: str) -> dict:
    """Zero-argument \\newcommand / \\def shorthands (\\cN, \\N, \\tr, ...).

    Papers define these in the preamble, so a raw title reads `$\\N=2$`. Only
    argument-less, non-recursive, short definitions are collected; anything
    taking arguments is left alone rather than expanded wrongly.
    """
    macros = {}
    for m in RE_NEWCMD.finditer(source):
        name = m.group(1)
        tail = source[m.end():]
        i = 0
        while i < len(tail) and tail[i] in " \t\r\n":
            i += 1
        if i < len(tail) and tail[i] == "[":        # takes arguments - skip
            continue
        body, _ = read_arg(source, m.end(), allow_optional=False)
        if body and len(body) < 120 and ("\\" + name) not in body:
            macros.setdefault(name, body)
    for m in RE_DEF.finditer(source):
        name = m.group(1)
        body, _ = read_arg(source, m.end(), allow_optional=False)
        if body and len(body) < 120 and ("\\" + name) not in body:
            macros.setdefault(name, body)
    return macros


def expand_macros(text: str, macros: dict, passes: int = 3) -> str:
    if not macros or not text:
        return text
    for _ in range(passes):
        new = re.sub(r"\\([A-Za-z]+)",
                     lambda m: macros.get(m.group(1), m.group(0)), text)
        if new == text:
            break
        text = new
    return text


def clean_latex(s: str, macros: dict = None) -> str:
    """Best-effort LaTeX -> plain text, preserving inline math verbatim."""
    if not s:
        return ""
    s = expand_macros(s, macros)
    math = []

    def stash(m):
        math.append(m.group(0))
        return "\x00%d\x00" % (len(math) - 1)

    s = re.sub(r"\$[^$]*\$", stash, s)
    s = _remove_cmd_with_arg(s, KILL_WITH_ARG)
    s = _remove_cmd_with_arg(s, ["cite", "citep", "citet", "citealp", "nocite"])
    s = re.sub(r"\\(?:eq|auto|c|C|name|page)?ref\s*\*?\s*\{([^}]*)\}", r"[\1]", s)
    s = s.replace("\\\\", ", ")
    s = re.sub(r"\\and\b", ", ", s)

    def _accent(m):
        mark = ACCENTS.get(m.group(1))
        return unicodedata.normalize("NFC", m.group(2) + mark) if mark else m.group(2)

    s = re.sub(r"\\([`'^\"~=.])\s*\{?(\w)\}?", _accent, s)      # \'{e} -> é
    s = re.sub(r"\\([uvcHrkbd])\s*\{(\w)\}", _accent, s)        # \c{c}  -> ç
    for name, ch in sorted(SPECIAL_LETTERS.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(r"\\" + name + r"(?![a-zA-Z])\s*(\{\})?", ch, s)
    s = re.sub(r"\\[,;:!]|\\ ", " ", s)                         # \, \; thin spaces
    s = re.sub(r"\\[a-zA-Z]+\s*\*?\s*\{", "{", s)           # \textbf{X} -> {X}
    s = re.sub(r"\\[a-zA-Z]+\s*", " ", s)                   # bare macros
    s = s.replace("{", "").replace("}", "").replace("~", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*,\s*(,\s*)+", ", ", s).strip(" ,")
    s = re.sub(r"\x00(\d+)\x00", lambda m: math[int(m.group(1))], s)
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------ step 1: arXiv ID ----

RE_ID_NEW = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")
RE_ID_OLD_SLASH = re.compile(r"([a-z-]{2,}(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)")
RE_ID_OLD_FLAT = re.compile(r"([a-z-]{2,})[-_]?(\d{7})(v\d+)?")


def parse_arxiv_id(folder_name: str) -> Optional[str]:
    m = RE_ID_NEW.search(folder_name)
    if m:
        return m.group(1)
    m = RE_ID_OLD_SLASH.search(folder_name)
    if m:
        return m.group(1)
    stripped = re.sub(r"(?i)arxiv", "", folder_name)
    m = RE_ID_OLD_FLAT.search(stripped)
    if m:
        arch = m.group(1).strip("-_")
        if arch:
            return "%s/%s%s" % (arch, m.group(2), m.group(3) or "")
    return None


# -------------------------------------------------- step 2: main .tex file --

@dataclass
class MainTexResult:
    path: Optional[Path] = None
    note: str = ""
    ambiguous: bool = False
    candidates: list = field(default_factory=list)
    total_tex: int = 0
    extra_flags: list = field(default_factory=list)


def _visible(p: Path, root: Path) -> bool:
    parts = p.relative_to(root).parts
    return not any(part.startswith(".") or part == "__MACOSX" for part in parts)


def find_main_tex(folder: Path) -> MainTexResult:
    tex_files = sorted(p for p in folder.rglob("*.tex")
                       if p.is_file() and _visible(p, folder))
    res = MainTexResult(total_tex=len(tex_files))
    if not tex_files:
        res.ambiguous = True
        res.note = "no .tex file found in this folder"
        return res
    if len(tex_files) == 1:
        res.path = tex_files[0]
        res.note = "only .tex file in the folder"
        return res

    candidates = []
    for p in tex_files:
        body = strip_comments(read_text(p))
        if "\\documentclass" in body and "\\begin{document}" in body:
            candidates.append(p)
    res.candidates = candidates

    if not candidates:
        res.ambiguous = True
        res.note = ("none of the %d .tex files contains both \\documentclass and "
                    "\\begin{document}" % len(tex_files))
        return res
    if len(candidates) == 1:
        res.path = candidates[0]
        res.note = ("only 1 of %d .tex files has \\documentclass + \\begin{document}"
                    % len(tex_files))
        return res

    # A duplicated source tree (e.g. an "ARXIV V3/" subfolder holding a second
    # copy of everything) is not a real ambiguity: byte-identical files produce
    # byte-identical manifests. Take the shallowest copy and say so.
    digests = {}
    for p in candidates:
        digests.setdefault(hashlib.md5(p.read_bytes()).hexdigest(), []).append(p)
    if len(digests) == 1:
        shallow = sorted(candidates,
                         key=lambda p: (len(p.relative_to(folder).parts),
                                        p.as_posix()))[0]
        res.path = shallow
        res.note = ("%d byte-identical copies; used the shallowest"
                    % len(candidates))
        res.extra_flags.append(
            "%d byte-identical copies of the main tex exist (%s) — this folder "
            "holds a duplicated source tree. The manifest describes `%s`; "
            "consider deleting the duplicate before uploading."
            % (len(candidates),
               ", ".join("`%s`" % c.relative_to(folder).as_posix()
                         for c in candidates),
               shallow.relative_to(folder).as_posix()))
        return res
    if len(digests) < len(candidates):
        candidates = [ps[0] for ps in digests.values()]
        res.candidates = candidates
        if len(candidates) == 1:
            res.path = candidates[0]
            res.note = "1 distinct main file after ignoring byte-identical copies"
            return res

    by_size = sorted(candidates, key=lambda p: p.stat().st_size, reverse=True)
    big, second = by_size[0].stat().st_size, by_size[1].stat().st_size
    if second == 0 or big > SIZE_TIEBREAK * second:
        res.path = by_size[0]
        res.note = ("largest of %d plausible mains (%d B vs %d B)"
                    % (len(candidates), big, second))
        return res

    res.ambiguous = True
    res.note = ("%d files look like plausible mains and are within %d%% in size"
                % (len(candidates), int((SIZE_TIEBREAK - 1) * 100)))
    return res


# ---------------------------------------------- step 3: load & flatten tex --

RE_INPUT = re.compile(r"\\(input|include)\b")
RE_GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{")


def load_source(main_tex: Path, folder: Path):
    """Return (flattened_source, graphics_paths, flags)."""
    flags = []
    seen = set()

    def resolve(name: str, base: Path) -> Optional[Path]:
        name = name.strip().strip('"')
        cands = []
        for stem in (name, name + ".tex"):
            cands.append((base / stem))
            cands.append((folder / stem))
            cands.append((main_tex.parent / stem))
        for c in cands:
            try:
                if c.is_file() and c.suffix == ".tex":
                    return c.resolve()
            except OSError:
                continue
        return None

    def expand(path: Path, depth: int) -> str:
        text = strip_comments(read_text(path))
        if depth >= MAX_INPUT_DEPTH:
            flags.append("\\input nesting deeper than %d levels in `%s` — not expanded"
                         % (MAX_INPUT_DEPTH, path.name))
            return text
        out, pos = [], 0
        for m in RE_INPUT.finditer(text):
            if m.start() < pos:
                continue
            arg, end = read_arg(text, m.end(), allow_optional=False)
            if arg is None:
                continue
            out.append(text[pos:m.start()])
            pos = end
            target = resolve(arg, path.parent)
            if target is None:
                flags.append("`\\%s{%s}` in `%s` could not be resolved to a file "
                             "— its figures are NOT in this manifest"
                             % (m.group(1), arg, path.name))
            elif target in seen:
                flags.append("`\\%s{%s}` already included earlier (cycle) — skipped"
                             % (m.group(1), arg))
            else:
                seen.add(target)
                out.append(expand(target, depth + 1))
        out.append(text[pos:])
        return "".join(out)

    seen.add(main_tex.resolve())
    source = expand(main_tex, 0)

    graphics_paths = []
    m = RE_GRAPHICSPATH.search(source)
    if m:
        inner, _ = read_arg(source, m.end() - 1, allow_optional=False)
        if inner:
            graphics_paths = [g.strip() for g in re.findall(r"\{([^}]*)\}", inner)]
    return source, graphics_paths, flags


# ------------------------------------------------- step 4: title & authors --

def extract_metadata(source: str, macros: dict = None):
    flags = []

    title = None
    for m in re.finditer(r"\\(?:title|TITLE)\b\s*\*?", source):
        arg, _ = read_arg(source, m.end())
        if arg:
            title = clean_latex(arg, macros)
            break
    if not title:
        flags.append("no `\\title{...}` found — title left blank, not guessed")

    blocks = []
    for m in re.finditer(r"\\author\b\s*\*?", source):
        arg, _ = read_arg(source, m.end())
        if arg:
            cleaned = clean_latex(arg, macros)
            # affiliation markers: "Name${}^{1,2}$" / "Name$^{a}$" -> "Name"
            cleaned = re.sub(r"\$\s*\{?\}?\s*\^\s*\{?[^${}]*\}?\s*\$", "", cleaned)
            cleaned = re.sub(r"\s+([,;])", r"\1", cleaned).strip(" ,;")
            if cleaned and cleaned not in blocks:
                blocks.append(cleaned)
    authors = ", ".join(blocks)
    authors = re.sub(r"\s*,\s*(,\s*)+", ", ", authors).strip(" ,")
    if not authors:
        flags.append("no `\\author{...}` found — authors left blank, not guessed")
    return title or "", authors, flags


# ----------------------------------------------------- step 5: the figures --

RE_FIG_BEGIN = re.compile(r"\\begin\{(figure\*?)\}")
RE_FIG_BOUND = re.compile(r"\\(begin|end)\{figure\*?\}")
RE_INCLUDEGRAPHICS = re.compile(r"\\includegraphics\b\s*\*?")
RE_SUBFIG_ENV = re.compile(r"\\begin\{(subfigure|subfloat)\*?\}")
RE_INLINE_GFX = re.compile(
    r"\\begin\{(tikzpicture|pgfpicture|pspicture|axis)\}|\\tikz\b|\\includestandalone\b")
# a \begin{figure} can legitimately wrap a table or a display equation: those
# still consume a figure number in the PDF, so they are content, not a problem
RE_INLINE_TABLE = re.compile(r"\\begin\{(tabularx?\*?|tabu|array|longtable)\}")
RE_INLINE_EQ = re.compile(
    r"\\begin\{(align|equation|gather|eqnarray|multline|aligned|displaymath)\*?\}"
    r"|\\\\\[")

RESET_PATTERNS = [
    (re.compile(r"\\appendix\b"), r"\appendix"),
    (re.compile(r"\\setcounter\s*\{\s*figure\s*\}"), r"\setcounter{figure}"),
    (re.compile(r"\\renewcommand\s*\{?\s*\\thefigure"), r"\renewcommand{\thefigure}"),
    (re.compile(r"\\(?:counterwithin|numberwithin)\s*\*?\s*\{\s*figure\s*\}"),
     r"\counterwithin{figure}"),
]


@dataclass
class Ref:
    raw: str                      # exactly as written in the tex
    resolved: Optional[str] = None
    status: str = "OK"
    sub_caption: str = ""


@dataclass
class Figure:
    number: int
    env: str
    caption: str = ""
    refs: list = field(default_factory=list)
    inline: Optional[str] = None
    after_reset: bool = False
    flags: list = field(default_factory=list)


def _find_env_end(s: str, start: int) -> int:
    depth = 1
    for m in RE_FIG_BOUND.finditer(s, start):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.start()
    return -1


def _spans_of_subfigures(body: str, macros: dict = None):
    """Return [(start, end, caption)] for subfigure/subfloat environments."""
    spans = []
    for m in RE_SUBFIG_ENV.finditer(body):
        name = m.group(1)
        end_m = re.compile(r"\\end\{" + name + r"\*?\}").search(body, m.end())
        end = end_m.start() if end_m else len(body)
        cap = ""
        cm = re.search(r"\\(?:sub)?caption\b\s*\*?", body[m.end():end])
        if cm:
            arg, _ = read_arg(body, m.end() + cm.end())
            cap = clean_latex(arg or "", macros)
        spans.append((m.start(), end, cap))
    return spans


def detect_counter_resets(source: str):
    """Return (earliest_offset_or_None, [(label, line)])."""
    hits = []
    for pat, label in RESET_PATTERNS:
        for m in pat.finditer(source):
            hits.append((label, m.start(), source.count("\n", 0, m.start()) + 1))
    if not hits:
        return None, []
    hits.sort(key=lambda h: h[1])
    return hits[0][1], [(h[0], h[2]) for h in hits]


def extract_figures(source: str, reset_offset: Optional[int],
                    macros: dict = None):
    figs = []
    doc = source.find("\\begin{document}")
    scan_from = doc if doc != -1 else 0
    pos, number = scan_from, 0

    for m in RE_FIG_BEGIN.finditer(source, scan_from):
        if m.start() < pos:
            continue
        env = m.group(1)
        body_start = m.end()
        end = _find_env_end(source, body_start)
        if end == -1:
            body = source[body_start:]
            pos = len(source)
            unterminated = True
        else:
            body = source[body_start:end]
            pos = end
            unterminated = False

        number += 1
        fig = Figure(number=number, env=env)
        if unterminated:
            fig.flags.append("`\\begin{%s}` has no matching `\\end` — "
                             "parsed to end of file" % env)
        if reset_offset is not None and m.start() > reset_offset:
            fig.after_reset = True

        sub_spans = _spans_of_subfigures(body, macros)

        # main caption = first \caption that is not inside a subfigure block
        for cm in re.finditer(r"\\caption\b\s*\*?", body):
            if any(s <= cm.start() < e for s, e, _ in sub_spans):
                continue
            arg, _ = read_arg(body, cm.end())
            if arg is not None:
                fig.caption = clean_latex(arg, macros)
                break

        for gm in RE_INCLUDEGRAPHICS.finditer(body):
            arg, _ = read_arg(body, gm.end())
            if arg is None:
                fig.flags.append("an `\\includegraphics` has no braced filename")
                continue
            name = arg.strip().strip('"')
            if name.startswith("{") and name.endswith("}"):
                name = name[1:-1]
            sub_cap = ""
            for s, e, cap in sub_spans:
                if s <= gm.start() < e:
                    sub_cap = cap
                    break
            fig.refs.append(Ref(raw=name, sub_caption=sub_cap))

        if not fig.refs:
            if RE_INLINE_GFX.search(body):
                fig.inline = "TikZ"
            elif RE_INLINE_TABLE.search(body):
                fig.inline = "table"
            elif RE_INLINE_EQ.search(body):
                fig.inline = "equation"
            else:
                fig.flags.append("no `\\includegraphics`, TikZ, table or equation "
                                 "content found in this figure environment")
        if not fig.caption:
            fig.flags.append("no `\\caption{...}` found")
        figs.append(fig)
    return figs


# ---------------------------------------------- step 6: cross-check on disk --

def build_disk_index(folder: Path):
    """Exact-case relative paths on disk + lowercase-stem hint map."""
    exact, by_stem, images = set(), {}, []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or not _visible(p, folder):
            continue
        rel = p.relative_to(folder).as_posix()
        exact.add(rel)
        if p.suffix.lower() in IMAGE_EXTS:
            images.append(rel)
            by_stem.setdefault(p.stem.lower(), []).append(rel)
    return exact, by_stem, images


def _search_dirs(folder: Path, main_tex: Path, graphics_paths):
    dirs = [""]
    sub = main_tex.parent.relative_to(folder).as_posix()
    if sub not in (".", ""):
        dirs.append(sub + "/")
    for g in graphics_paths:
        g = g.strip()
        if not g:
            continue
        g = g[2:] if g.startswith("./") else g
        g = g if g.endswith("/") else g + "/"
        if g not in dirs:
            dirs.append(g)
        if sub not in (".", "") and (sub + "/" + g) not in dirs:
            dirs.append(sub + "/" + g)
    return dirs


def crosscheck(figs, folder: Path, main_tex: Path, graphics_paths):
    exact, by_stem, images = build_disk_index(folder)
    dirs = _search_dirs(folder, main_tex, graphics_paths)
    used = set()

    for fig in figs:
        for ref in fig.refs:
            raw = ref.raw[2:] if ref.raw.startswith("./") else ref.raw
            has_ext = Path(raw).suffix.lower() in IMAGE_EXTS
            found = []
            for d in dirs:
                cand = d + raw
                if cand in exact and cand not in found:
                    found.append(cand)
                if not has_ext:
                    for ext in GRAPHICS_SEARCH_EXTS:
                        c2 = cand + ext
                        if c2 in exact and c2 not in found:
                            found.append(c2)
            if len(found) == 1:
                ref.resolved = found[0]
                ref.status = ("OK" if has_ext
                              else "OK — extension omitted in tex, resolved to `%s`"
                              % found[0])
                used.add(found[0])
            elif len(found) > 1:
                ref.resolved = found[0]
                ref.status = ("⚠ ambiguous — %s all exist; LaTeX would pick one, "
                              "check which you want"
                              % ", ".join("`%s`" % f for f in found))
                used.update(found)
            else:
                hints = by_stem.get(Path(raw).stem.lower(), [])
                if hints:
                    ref.status = ("⚠ not found on disk — folder has %s "
                                  "(case/extension differ, not auto-matched)"
                                  % ", ".join("`%s`" % h for h in hints))
                else:
                    ref.status = "⚠ not found on disk"
    unreferenced = [i for i in images if i not in used]
    return unreferenced, images


# --------------------------------------------------- step 7: render/write ---

def _cell(text: str, limit: int = CAPTION_MAX) -> str:
    text = (text or "").replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or "—"


def _sub_label(n: int, i: int, total: int) -> str:
    return str(n) if total <= 1 else "%d%s" % (n, chr(ord("a") + i))


def render_manifest(arxiv_id, folder_name, title, authors, main_tex_rel, tex_note,
                    total_tex, figs, flags, reset_hits, unreferenced, images,
                    source_folder="", copy_note="") -> str:
    head = "arXiv:%s" % arxiv_id if arxiv_id else "%s (arXiv ID not recognised)" % folder_name
    L = []
    L.append("<!-- generated by arxiv_manifest.py — regenerate any time; "
             "manual edits will be overwritten -->")
    L.append("## %s — %s" % (head, title or "*title not found*"))
    L.append("**Authors:** %s" % (authors or "*not found*"))
    L.append("")
    L.append("**Source folder:** `%s`" % source_folder)
    L.append("**Main tex:** `%s` (%d .tex file%s in folder; %s)"
             % (main_tex_rel, total_tex, "" if total_tex == 1 else "s", tex_note))
    if copy_note:
        L.append("**Copied alongside this manifest:** %s" % copy_note)
    L.append("**Figure environments:** %d   **Image files on disk:** %d"
             % (len(figs), len(images)))
    L.append("")

    if reset_hits:
        L.append("> ⚠ **Figure numbering may not match the compiled PDF.** Found %s. "
                 "Numbers below are positional (order of `\\begin{figure}` in the "
                 "source); figures after the first such marker are tagged `†`."
                 % ", ".join("`%s` (flattened source line %d)" % (lbl, ln)
                             for lbl, ln in reset_hits))
        L.append("")

    L.append("| Figure | Caption (short) | File | Status |")
    L.append("|---|---|---|---|")
    if not figs:
        L.append("| — | *no figure environments found* | — | — |")
    for fig in figs:
        dagger = "†" if fig.after_reset else ""
        if fig.inline:
            L.append("| %d%s | %s | [inline: %s] | OK |"
                     % (fig.number, dagger, _cell(fig.caption), fig.inline))
        elif not fig.refs:
            L.append("| %d%s | %s | — | ⚠ no image source found |"
                     % (fig.number, dagger, _cell(fig.caption)))
        else:
            total = len(fig.refs)
            for i, ref in enumerate(fig.refs):
                label = _sub_label(fig.number, i, total) + dagger
                if total == 1:
                    cap = _cell(fig.caption)
                elif i == 0:
                    cap = _cell(fig.caption)
                    if ref.sub_caption:
                        cap = _cell(fig.caption + " — (a) " + ref.sub_caption)
                else:
                    cap = _cell(ref.sub_caption) if ref.sub_caption else "↳"
                L.append("| %s | %s | `%s` | %s |"
                         % (label, cap, ref.raw, ref.status))
    L.append("")

    fig_flags = []
    for fig in figs:
        for f in fig.flags:
            fig_flags.append("Figure %d: %s" % (fig.number, f))
    all_flags = list(flags) + fig_flags
    if all_flags:
        L.append("### Flags")
        for f in all_flags:
            L.append("- ⚠ %s" % f)
        L.append("")

    if unreferenced:
        L.append("### Image files on disk not referenced by any figure")
        groups = {}
        for u in unreferenced:
            groups.setdefault(u.rsplit("/", 1)[0] if "/" in u else "", []).append(u)
        referenced_names = {Path(r.resolved).name for f in figs for r in f.refs
                            if r.resolved}
        for d in sorted(groups):
            items = groups[d]
            if d and len(items) > 3:
                dup = sum(1 for i in items if Path(i).name in referenced_names)
                note = (" — filenames match files already used at the top level, "
                        "so this looks like a duplicate copy"
                        if dup > len(items) / 2 else "")
                L.append("- `%s/` — %d image files%s: %s, …"
                         % (d, len(items), note,
                            ", ".join("`%s`" % Path(i).name for i in items[:3])))
            else:
                for i in items:
                    L.append("- `%s`" % i)
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def render_stub(arxiv_id, folder_name, reason, candidates, folder: Path) -> str:
    head = "arXiv:%s" % arxiv_id if arxiv_id else folder_name
    L = ["<!-- generated by arxiv_manifest.py -->",
         "## %s — ⚠ NEEDS MANUAL CHECK" % head,
         "",
         "This folder was **not** parsed: %s." % reason,
         ""]
    if candidates:
        L.append("Candidate main files (pick one, or tell the script which):")
        for c in candidates:
            L.append("- `%s` (%d bytes)"
                     % (c.relative_to(folder).as_posix(), c.stat().st_size))
        L.append("")
    L.append("Re-run the script after resolving this.")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ driver --

def safe_id(arxiv_id: Optional[str], folder_name: str) -> str:
    """Filename-safe stem: legacy IDs carry a '/' that cannot go in a path."""
    base = (arxiv_id or folder_name).replace("/", "")
    base = re.sub(r"[^A-Za-z0-9._+-]", "_", base).strip("._")
    return base or "paper"


def process_folder(folder: Path, out_root: Path, main_override: Optional[str] = None,
                   flatten: bool = False):
    """Write output/<id>/<id>.tex + <id>_manifest.md. Never writes into `folder`.

    Returns (status_line, wrote_anything).
    """
    name = folder.name
    arxiv_id = parse_arxiv_id(name)
    sid = safe_id(arxiv_id, name)
    label = arxiv_id or name
    flags = []
    if not arxiv_id:
        flags.append("arXiv ID not recognised from folder name `%s` — output named "
                     "after the folder instead" % name)

    out_dir = out_root / sid
    manifest_path = out_dir / (sid + MANIFEST_SUFFIX)

    if main_override:
        forced = folder / main_override
        if not forced.is_file():
            return ("%-28s ⚠ --main %s not found in this folder"
                    % (label, main_override), False)
        main = MainTexResult(path=forced, note="forced via --main",
                             total_tex=len(list(folder.rglob("*.tex"))))
    else:
        main = find_main_tex(folder)

    if main.ambiguous or main.path is None:
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            render_stub(arxiv_id, name, main.note, main.candidates, folder),
            encoding="utf-8")
        return ("%-28s ⚠ SKIPPED — %s" % (label, main.note), True)

    flags.extend(main.extra_flags)
    source, graphics_paths, load_flags = load_source(main.path, folder)
    flags.extend(load_flags)

    macros = collect_macros(source)
    title, authors, meta_flags = extract_metadata(source, macros)
    flags.extend(meta_flags)

    reset_offset, reset_hits = detect_counter_resets(source)
    figs = extract_figures(source, reset_offset, macros)
    unreferenced, images = crosscheck(figs, folder, main.path, graphics_paths)

    # --- the copied .tex (copy, never move; original left untouched) ---
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_copy = out_dir / (sid + ".tex")
    main_rel = main.path.relative_to(folder).as_posix()
    has_inputs = RE_INPUT.search(strip_comments(read_text(main.path))) is not None
    if flatten:
        tex_copy.write_text(source, encoding="utf-8")
        copy_note = ("`%s` — `%s` with every `\\input`/`\\include` inlined and "
                     "comments stripped (`--flatten`)" % (tex_copy.name, main_rel))
    else:
        shutil.copy2(main.path, tex_copy)
        copy_note = "`%s` — verbatim copy of `%s`" % (tex_copy.name, main_rel)
        if has_inputs:
            flags.append("`%s` pulls in other files via `\\input`/`\\include`, so "
                         "the copied `%s` is NOT self-contained — re-run with "
                         "`--flatten` if you want a single complete file"
                         % (main_rel, tex_copy.name))

    md = render_manifest(arxiv_id, name, title, authors, main_rel, main.note,
                         main.total_tex, figs, flags, reset_hits,
                         unreferenced, images,
                         source_folder=name, copy_note=copy_note)
    manifest_path.write_text(md, encoding="utf-8")

    bad = sum(1 for f in figs for r in f.refs if r.status.startswith("⚠"))
    n_flags = len(flags) + sum(len(f.flags) for f in figs) + bad
    status = "OK" if n_flags == 0 else "⚠ %d flag%s" % (n_flags, "" if n_flags == 1 else "s")
    return ("%-28s %-12s (%d figure%s, %d missing file%s) -> %s/"
            % (label, status, len(figs), "" if len(figs) == 1 else "s",
               bad, "" if bad == 1 else "s", sid), True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("papers_dir", nargs="?", default=".",
                    help="directory containing the unzipped paper folders")
    ap.add_argument("--output-dir", metavar="DIR",
                    help="where the per-paper output folders go "
                         "(default: <papers_dir>/%s)" % OUTPUT_DIRNAME)
    ap.add_argument("--only", metavar="FOLDER",
                    help="process just this one subfolder (by name)")
    ap.add_argument("--main", metavar="FILE",
                    help="force this .tex (path relative to the folder) as the "
                         "main file; requires --only")
    ap.add_argument("--flatten", action="store_true",
                    help="copy the main tex with all \\input/\\include inlined "
                         "instead of verbatim")
    ap.add_argument("--skip-existing", action="store_true",
                    help="leave papers that already have an output manifest alone")
    args = ap.parse_args(argv)

    if args.main and not args.only:
        print("error: --main requires --only", file=sys.stderr)
        return 2

    root = Path(args.papers_dir).expanduser().resolve()
    if not root.is_dir():
        print("error: %s is not a directory" % root, file=sys.stderr)
        return 2
    out_root = (Path(args.output_dir).expanduser().resolve()
                if args.output_dir else root / OUTPUT_DIRNAME)

    folders = sorted(p for p in root.iterdir()
                     if p.is_dir() and not p.name.startswith(".")
                     and p.name != "__MACOSX"
                     and p.resolve() != out_root)          # never scan our own output
    if args.only:
        folders = [p for p in folders if p.name == args.only]
        if not folders:
            print("error: no subfolder named %r in %s" % (args.only, root),
                  file=sys.stderr)
            return 2
    if not folders:
        print("no paper folders found in %s" % root)
        return 0

    print("scanning %s (%d folder%s)" % (root, len(folders),
                                         "" if len(folders) == 1 else "s"))
    print("output   %s\n" % out_root)

    written, claimed = 0, {}
    for folder in folders:
        if not any(folder.rglob("*.tex")):
            print("%-28s -- not a paper folder (no .tex) — untouched" % folder.name)
            continue
        sid = safe_id(parse_arxiv_id(folder.name), folder.name)
        if sid in claimed:
            print("%-28s ⚠ SKIPPED — would write to output/%s/, already claimed by "
                  "`%s`" % (folder.name, sid, claimed[sid]))
            continue
        claimed[sid] = folder.name
        if args.skip_existing and (out_root / sid / (sid + MANIFEST_SUFFIX)).exists():
            print("%-28s -- skipped (output/%s/ already has a manifest)"
                  % (folder.name, sid))
            continue
        try:
            line, wrote = process_folder(folder, out_root, args.main, args.flatten)
            written += int(wrote)
        except Exception as exc:                                   # noqa: BLE001
            line = "%-28s ⚠ ERROR — %s: %s" % (folder.name, type(exc).__name__, exc)
        print(line)

    if written:
        print("\n%d paper%s written under %s "
              "(original folders untouched)."
              % (written, "" if written == 1 else "s", out_root))
    else:
        print("\nnothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
