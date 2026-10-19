#!/bin/bash
# Builds main.pdf (the paper) and supplement.pdf. They cross-reference each other, so each is compiled a few times.
set -e
cd "$(dirname "$0")"
pdflatex -interaction=nonstopmode main.tex >/dev/null
bibtex main >/dev/null
for i in 1 2; do
  pdflatex -interaction=nonstopmode supplement.tex >/dev/null
  pdflatex -interaction=nonstopmode main.tex >/dev/null
done
echo "built main.pdf and supplement.pdf"
