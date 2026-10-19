# Quarter-close checker paper: LaTeX source

- `main.tex` is the paper (two columns, 25 pages). `supplement.tex` is the 36-page supplement with the long per-run tables.
- Build both with `bash build.sh` (pdflatex + bibtex; TeX Live 2022 or newer, with pgfplots and xr-hyper).
- On Overleaf, upload the whole folder and compile `main.tex`. The included `supplement.aux` lets the paper's references to supplement sections resolve. If you change the supplement, compile `supplement.tex` too.
- `figures/` has the TikZ and pgfplots figures and the PDF charts. `parts/` and `tab_*.tex` are generated result tables. Edit the numbers at their source, not by hand.
- `refs.bib` and `qclosenat.bst` are the bibliography and its style. `main.bbl` is included so the paper compiles without bibtex.
