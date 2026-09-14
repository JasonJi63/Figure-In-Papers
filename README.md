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
├── arXiv-1111.11111v1/
├── arXiv-2222.22222v3/
└── arXiv-3333.33333v2/
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
1111.11111v1   OK         (12 figures, 0 missing files) -> 1111.11111v1/
2222.22222v3   ⚠ 1 flag   (31 figures, 1 missing file)  -> 2222.22222v3/
3333.33333v2   OK         (8 figures, 0 missing files)  -> 3333.33333v2/

3 papers written under /path/to/papers/output (original folders untouched).
```

```
papers/output/1111.11111v1/
├── 1111.11111v1.tex            ← copy of the main .tex, renamed
└── 1111.11111v1_manifest.md
```

Filenames carry the arXiv ID, so nothing collides when several papers are uploaded
together.

```markdown
## arXiv:1111.11111v1 — Title Of The Paper As Written In \title{}
**Authors:** A. Author, B. Coauthor, C. Thirdauthor

**Figure environments:** 12   **Image files on disk:** 7

| Figure | Caption (short) | File | Status |
|---|---|---|---|
| 1  | Caption of the first figure, truncated for the table…      | `plot1.pdf`   | OK |
| 2a | Caption of a two-panel figure — (a) left panel…            | `panelA.png`  | OK |
| 2b | right panel                                                | `panelB.png`  | OK |
| 3  | A figure drawn in the source rather than included…         | [inline: TikZ] | OK |
| 4  | A figure whose image file is not where the tex says…       | `Fig4.eps`    | ⚠ not found on disk — folder has `fig4.png` (case/extension differ, not auto-matched) |
| 9† | A figure appearing after \appendix…                        | `plot9.pdf`   | OK |
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
