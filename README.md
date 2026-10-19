# Cheap model + checker: catching silent errors in extracted financial numbers

Code and paper for **"Cheap model + checker"** by Aarush Yadav (UCLA) and Arihan Yadav (UW–Madison).

Investors, limited partners and data providers receive a steady stream of financial statements: private-fund quarterly packets, public 10-Qs and 10-Ks. Vision-language models are increasingly used to extract the numbers, and their worst errors are *silent*: a plausible number that is wrong and that nobody flags. These documents are internally redundant (subtotals, roll-forwards, the balance-sheet equation), so many errors can be found, and some fixed, without a second model.

This repository has:

- **`qcore9/`**: the checker. It is a deterministic tier between a cheap extraction model and a stronger model or a person. It checks the extraction against linear identities `Ax = b`. It repairs a value only when an identity or the page backs the change, and it only ever writes a number the page prints or that was already extracted. Everything else goes to review as a small packet. This is the exact frozen code (freeze 1) used for the paper's final held-out test.
- **`paper/`**: the paper (`qclose_paper.pdf`, 25 pages), the supplement (`supplement.pdf`, 36 pages) and their LaTeX source.

## Headline result

Across four held-out sets of degraded SEC 10-Q/10-K filings, Claude Haiku plus the checker left **28 silent errors**, against **174** for Claude Opus alone (paired 95% CI of the difference −231 to −72). This needs a born-digital text layer behind the page image; with OCR only, the cheap model plus the checker does not beat the strong model. See the paper for zero-config mode, OCR mode, other document types and limitations.

## Running the checker

Python 3.10+ with `pip install pdfplumber numpy`. OCR mode also needs `onnxtr[cpu]` (docTR models) or Tesseract.

```
python -m qcore9 run   <bundle> <extraction.json> [--mode text|notext|ocr] [--ocr-dir <dir>]
python -m qcore9 score <bundle> <extraction.json> [result.json] --out score.json
python -m pytest qcore9/tests -q        # 79 tests
```

`qcore9/README.md` documents the bundle format, zero-config mode (`--identities auto --schema auto`), OCR mode, the re-read tier and the full algorithm. The private-equity adapter (`qcore9/adapters/lp.py`) needs the earlier PE checker, which is not included here.

## Building the paper

```
cd paper && bash build.sh
```

The evaluation data (SEC filings, model extractions, synthetic documents) is not included because of its size.

## License

MIT, see `LICENSE`.
