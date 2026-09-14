# arXiv Paper Manifest Generator

Scans a folder of **unzipped arXiv sources** and writes, for each paper, a copy of the
main `.tex` plus a Markdown **manifest** — title, authors, and every figure with its
caption and image file, checked against the files actually on disk.

Lets you see what a paper contains, and spot figures whose image file is missing,
without opening the PDF. Your paper folders are never modified; everything is written
to a separate `output/` folder.

Python 3.8+, standard library only — nothing to install.

## Use

Put the unzipped folders side by side and point the script at them:

```
papers/
├── arXiv-2203.13726v3/
├── arXiv-2210.13479v3/
└── arXiv-2305.19438v4/
```

```bash
python3 arxiv_manifest.py /path/to/papers
```

Added a new paper later? Re-run with `--skip-existing` and only that one is processed:

```bash
python3 arxiv_manifest.py /path/to/papers --skip-existing
```

| Flag | Effect |
|---|---|
| `--skip-existing` | Only process papers with no manifest yet |
| `--only FOLDER` | Process just one subfolder, by name |
| `--main FILE` | Force which `.tex` is the main one (with `--only`) — use if the script reports an ambiguous main |
| `--flatten` | Copy the main `.tex` with all `\input`/`\include` inlined into one file |
| `--output-dir DIR` | Write the output tree somewhere else |

## Result

```
2203.13726v3   ⚠ 1 flag   (40 figures, 0 missing files) -> 2203.13726v3/
2210.13479v3   OK         (22 figures, 0 missing files) -> 2210.13479v3/
2305.19438v4   OK         (13 figures, 0 missing files) -> 2305.19438v4/

3 papers written under /path/to/papers/output (original folders untouched).
```

```
papers/output/2210.13479v3/
├── 2210.13479v3.tex            ← copy of the main .tex, renamed
└── 2210.13479v3_manifest.md
```

Filenames carry the arXiv ID, so nothing collides when several papers are uploaded
together.

```markdown
## arXiv:2210.13479v3 — New Instantons for Matrix Models
**Authors:** Marcos Mariño, Ricardo Schiappa, Maximilian Schwick

**Figure environments:** 22   **Image files on disk:** 2

| Figure | Caption (short) | File | Status |
|---|---|---|---|
| 1  | Pictorial description of Borel resummation of a divergent series… | [inline: TikZ] | OK |
| 19 | Plot of the holomorphic effective potential (blue) for the cubic… | `CubicEffectivePotential.pdf` | OK |
| 21†| Integration contours for (anti) eigenvalue tunneling…             | [inline: TikZ] | OK |
```

Reading it:

- **Figure numbers** follow the order `\begin{figure}` appears in the source. Subfigures
  under one number appear as `3a`, `3b`.
- **File** is the filename exactly as the `.tex` writes it, never one guessed from disk.
- **`[inline: TikZ]`** (or `table` / `equation`) — the figure has no external image file.
- **`†`** — the figure comes after `\appendix`, so the PDF may number it `A.1`-style.
- **`⚠`** — needs your attention: a referenced image is missing, or a filename differs
  only in case or extension. The script reports these; it never renames anything and
  never guesses a match.
- A **Flags** section at the bottom lists anything else needing a human, and unreferenced
  image files left over on disk.

If two `.tex` files both look like plausible main files, the script refuses to guess and
writes `NEEDS MANUAL CHECK` listing the candidates — rerun with `--only` and `--main`.
