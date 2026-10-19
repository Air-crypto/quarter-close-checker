# qcore9: tier 1 checking for numbers that obey linear identities

qcore9 (frozen: `frozen_manifest.json`; changes in `CHANGES.md`) is qcore8 (frozen, `../qcore8/`) with fixes for
qcore8's zero-config breaks and wider discovery. The earlier history is in `../qcore8/CHANGES.md` and before. Below,
"qcore" means this package (`import qcore9`; `python -m qcore9 ...`); the rest of this README is qcore8's (and earlier),
with the package renamed.
New in qcore9 (all zero-config only except the scaled page match):
- a sign is not proposed from a second print of the number when the extraction's citation shows the extracted sign;
- a row whose label spells the record's identity tokens ('ANA-128') is the label read, over rows that only carry the
  token among their numbers;
- a number printed at the page's scale matches a stored value only to its printed precision, never with a percent sign
  (all modes, hand mode included);
- a page read that rule (b) accepts keeps its page basis when a discovered identity also ties with it;
- discovery: sums whose failures are each one slip of one cell (a digit or a sign) are admitted, and sums of four to
  eight terms that foot exactly are checked to one and a half units (both notext only); section totals of total-like
  rows; within-statement ties of two total rows (notext) and ties off by one slip in other periods.

New in qcore8 (active only in zero-config / discovered-identity use; the default hand mode runs qcore7's = qcore6's
code path):
- discovery admits long sums that hold exactly in two or three places with large numbers however often they fail
  elsewhere (the failures are the extraction errors), pools transposed templates across documents by column name,
  finds roll-forwards over lagged columns, sparse two- and three-line sums, cross-document chains (a closing balance
  carried to the next statement) and period accumulation (year to date = last report's year to date + this period),
  and products with a rate factor;
- the rounding unit of a set of numbers is the finest unit at least two (or a tenth) of them show, not the most common
  one (round amounts no longer widen a sum's tolerance);
- a weak label's page read never changes a value on its own, even as a digit misread (tier 2 unless an identity ties).

New in qcore7 (kept; active only with `identities="auto"` / `"both"` or `schema="auto"`; the default hand mode runs qcore6's
code path):
- label cells (age bands such as "30-34", bare years, numbers inside a row's label text) are never read as values, and a
  final value equal to a number of its own row or column label goes to tier 2 (`value_is_label`);
- a change backed only by discovered identities needs independent backing (a hand identity, two independent discovered
  identities, or one confirmed on the page in two places); otherwise it goes to tier 2 (`auto_identity_unbacked`);
- discovery finds aggregate ties (a total equal to the sum of a field over the document's entities), treats field names
  such as `gross_amount` as total-like, drops tautologies, and emits the failing instances of regular templates as
  checks (flags), which gives notext mode on scanned forms and receipts useful flags;
- the auto schema votes the scale from the page's unit statement and accepts weak labels (which may only fix a digit
  misread, or change a value through an identity).

New in qcore6 (kept):
- **zero-config mode** (`identities="auto"`, `schema="auto"`): the identities are discovered from the documents
  themselves and the schema (labels, units) from the extraction's printed strings, each validated on the page
  ("Zero-config mode" below). `identities="both"` adds the discovered identities to a hand-written identities.json;
- **a re-read tier**: crops of the uncertain cells with one question each, for a cheap reader (a small vision model),
  whose answers come back as a third reader of those cells ("Re-read tier" below);
- a scanned PDF's text layer (an image covering the page under the text) is treated as an OCR: in text mode its strict
  cell reads count as corroborated only by their digits (a one/two-glyph misread), not by a sign, a blank fill, a
  whole-row read or "the extracted number is printed elsewhere / nowhere"; those need an identity;
- the result reports identity coverage (the share of numbers that some identity constrains).

qcore checks a machine extraction of numbers from PDFs. It works for any document set whose numbers obey
linear identities: subtotals, roll-forwards, cross-document ties and period accumulation. It uses no model and
is deterministic.

qcore changes a value only when the page (read from the PDF's text layer) and the identities agree, or when the
page's read of that exact cell is unambiguous and corroborated (qcore5's gate, kept in qcore6, below).

Everything else it routes to tier 2 with the evidence attached. It is the domain-neutral core of the
quarter-close checker (`v2/qclose`, frozen). Nothing in `qcore9/*.py` knows a domain. The LP-specific
translation lives in `qcore9/adapters/lp.py`, the CORD one in `qcore9/adapters/cord.py`.

```
import qcore9
result = qcore9.run(bundle_dir, extraction, page_text_mode="text",    # "notext": identities only;
                    identities="hand", schema="hand", reread=None)    # "ocr": needs ocr_dir=<dir of OCR json>
card   = qcore9.score(bundle_dir, extraction, result)                 # needs truth values

python -m qcore9 ocr   <page images> <ocr dir> --engine doctr|tesseract --bundle <bundle>
python -m qcore9 run   <bundle> <extraction.json> [--mode text|notext|ocr] [--ocr-dir <ocr dir>]
                       [--identities hand|auto|both] [--schema hand|auto] [--reread answers.jsonl] [--out result.json]
python -m qcore9 score <bundle> <extraction.json> [result.json] [same options as run] [--out score.json]
python -m qcore9 reread-requests <bundle> <extraction.json> --mode ocr|notext|text [--ocr-dir D] --images <dir>
                       --out <request dir> [--identities ...] [--schema ...] [--max-share 0.6]
python -m qcore9 reread-simulate <request dir> <bundle> [--digit 0.05] [--null 0.03] [--seed 0]   # tuning only (needs truth)
python -m pytest qcore9/tests -q
```

## Zero-config mode

`identities="auto"` needs no identities.json; `schema="auto"` needs no schema.json (the bundle then holds only the
documents, and truth.jsonl for scoring; the records come from truth.jsonl's keys and grid fields, as before). Both read
only the documents and the extraction under check, and both are validated on the page: an extraction alone never
creates an identity or a label that the page does not support.

**Identity discovery** (`auto.py`). The records of each document are laid out as a grid: a row per field (or per named
entity), a column per column/period. Candidate identities are then generated and kept only when they hold on the
document's own values:
- *row sums* (subtotals and totals, including roll-forwards: beginning + changes = ending): for each row, the spans of
  rows above it in reading order (2 to 25 rows; all sign patterns up to 4 terms, else all-plus or first-plus-rest-minus),
  hierarchically collapsed so a total over subtotals is found after the subtotals;
- *column sums* (the same over columns: segments that add to a consolidated column);
- *products* (quantity x unit price = amount over the rows of a table);
- *equal ties* (the same number printed in two places: net income on the income statement and the cash-flow
  statement; a total carried from a schedule): same value at least 100 units, same period, the same duration and years
  in the column headings, a shared label word or the same field.

A candidate is a *template* (the same rows in every column, or the same fields in every document); it is checked on
every instance. Each instance is read with the page read of each cell when there is one (P) else the extraction (E):
it holds, fails, or fails with every term confirmed on the page. A template is admitted when it holds on at least two
instances, at least one of them non-trivial, and fails on at most max(1, 10%) of them. When the target row's label is
total-like ("total", "net", "balance at end"), one holding instance is enough if it holds with every cell read on the
page (or nothing is page-read and nothing fails; or, with no page at all, it holds at least as often as it fails); a
total-like template that holds in at least five places and twice as often as it fails is also admitted. A template that
the page reads whole somewhere but never confirms there is refused.
Tolerances come from the printed rounding unit (half a unit per term). Terms that never exceed half a unit are
dropped (they would make any sum hold). Failing instances of an admitted template are emitted as identities only in
documents where the template also holds, or when it is exact everywhere else (so one inconsistent table does not
import a regularity it does not have).

**Auto schema.** Labels: the most common label in the extraction's `printed_text` (the text before the trailing
numbers) is accepted for a field when a page line with every word of that label prints the field's extracted number
on exactly one line, for at least half of the field's numbers; the page's full printed label is used. When no single
label covers a field (receipts print "TOTAL", "Total Due", "GRAND TOTAL"), the printed labels confirmed that way in at
least two documents, and not confirmed more often for another field, are kept. Fields whose rows are named entities
(items) get no label (the entity finds the row). Per document, a label confirmed in fewer than three cells is kept only
when it fits one line there, or when one fitting line prints the extracted number; otherwise that field is not read by
label in that document (`Field.no_label`). A field whose cells carry different labels in one document (beginning and
ending balances) gets no field label. Units: "currency" when the page prints the number at a scale of 1000 or with a
currency mark and states no unit. Sign: fixed ("pos"/"neg") when the page prints the other sign in two or more
documents while nine in ten extracted numbers share one sign (stored magnitudes). Number format: grouped when
dot-thousands strings are common. A set of scans gets the backed label read (a read needs an identity).

`result["coverage"]` reports `{numbers, in_identity, share}`; `result["identity_list"]` lists the identities used when
any were discovered (`source`: vsum, hsum, product, equal; `support`: hold / fail counts).

## Re-read tier

```
python -m qcore9 reread-requests <bundle> <extraction.json> --mode ocr --ocr-dir <ocr> --images <img dir> --out <req>
#   writes <req>/crops/rNNNNN.png, <req>/requests.jsonl, <req>/manifest.json
#   (page images at <img dir>/<doc>/page-<n>.png; the same frame as the OCR json, or the PDF page)
#   a reader answers each request: <req>/answers.jsonl lines {"req_id", "printed_text", "row_label", "column_label"}
python -m qcore9 run <bundle> <extraction.json> --mode ocr --ocr-dir <ocr> --reread <req>/answers.jsonl
```

**Which cells.** The cells of tier 2 items in priority order: the cells of failing identities, the single-cell items
(misreads, readers disagree, unverified reads), cells in identities that pass while the OCR disagrees on a cell (W3:
two errors that cancel or agree), blanks; at most `--max-share` (0.6) requests per number. Each crop is the column
header strip (the header lines that name the column, with their period and year lines; from an earlier page for a
continued table; else the nearest dated line above the row) stitched above the cell's row band, padded so no digit is
clipped, the cell outlined in red, deskewed and scaled to legible text, at most 340 x 1400 px.

**Placing the cell** (after real reads, round 1). The cell is placed by the run's own read of it, else the extraction's
cited box, and kept only if its row prints the record's label: at least 60% of the label's words (field label, or
per-document label, plus the entity's words; OCR spelling tolerated) on the row band, on either side of the cell (a log
prints the program right of its amount) or on a wrapped label line just above. Otherwise the row that prints the label
is looked up (on the cell's page, else the cited page, else the document; rows the OCR split into pieces are one row;
the best score, then the fewest extra words, must be unique) and the number on it is taken under the column: the
hinted cell's place, else the median place of the column's other cells read on the page, else the header cell naming
the column, else the column's year printed once on the nearest dated line above; a row with one number takes it.
A request that can't be placed is not sent (`manifest.json` `skipped_by`: unplaced_row_label_not_found,
unplaced_several_label_rows, unplaced_no_cell_on_label_row). Two records placed on one cell share one crop
(`also_keys`): the answer is a re-read of each, with the row label and heading checks run per record. Every request
has an outlined cell. In notext mode the cells are placed with the OCR (or the text layer when there is one) only to
make the crops.

**The question** asks for JSON: the printed string, the row label and the column heading the reader sees. A re-read
whose row label lacks a word of the record's label, or whose heading names another year than the record's column, is
no evidence (outcome `wrong_cell`): the crop sat on another cell.

**How answers are used** (the gate stays qcore5's). The re-read is a third reader of that one cell, but on a poor
scan it is not an independent one: the real reader (a small vision model, often the same kind of model as the
extractor) makes the same glyph confusions (6/5, 0/8, 2/0, 1/7) as the OCR and as the extraction. So:
- it changes a value only when it agrees with the page read of that exact cell (text layer or OCR), the page read is
  not a value extracted elsewhere in the document, and the qcore5 gate passes. For an OCR read, only rule (a) counts:
  an identity through the cell then ties (`reread_needs_identity` otherwise). The re-read never makes an OCR read
  "unambiguous and corroborated" on its own (on dev, real re-reads that agreed with an OCR misread changed 11 correct
  numbers before this rule);
- when it agrees with the extraction while the page read is another number altogether (not a glyph or two apart:
  `core._near`), the OCR's alternatives for that cell are not applied, and the cell is cleared from single-cell tier 2
  items (unless an identity through it still fails). When the page read is a glyph or two off the extraction, or
  there is no page read, an agreeing re-read changes nothing (dev SEC scans: 21 re-reads agreed with a wrong extracted
  number);
- when it disagrees with the final value, a `reread_disagrees` tier 2 item carries every read (model, page, re-read).

**Reader.** Chosen on dev before the freeze: one Opus reader with the gate above (0 broken on every dev set; fewer
silent errors than Haiku alone or both readers agreeing). See CHANGES.md "Freeze".
`result["rereads"]` logs each re-read's outcome (agrees, disagrees, null, wrong_cell, cleared).

`reread-simulate` writes answers from truth with read noise (5% one-digit errors, 3% null by default) for tuning; with
truth cell boxes it answers what is under the outline (another record's number when the crop sits on another cell).

## Bundle format

```
<bundle>/docs/<doc_id>.pdf    source PDFs with a text layer
<bundle>/schema.json          {"domain": str, "number_format": "us|dot_thousands",
                               "fields": {name: {"label": [str, ...], "units": str, "sign": "pos|neg|any", "scale": number}}}
<bundle>/truth.jsonl          one record per printed number: key, doc, field, column, period, entity, value, page, bbox
<bundle>/identities.json      [{"id", "terms": [[key, coef], ...], "const", "tol", "kind": "within_doc|cross_doc|cross_period"}]
<bundle>/meta.json            {"planted_document_issues": [{"keys": [...], "kind": str}], ...}
```

- An identity holds when `sum(coef * value[key]) + const` is within `tol` of 0.
- `run()` reads only the identity of each truth record: key, doc, field, column, period and entity. It never
  reads `value`, `page` or `bbox`. Those three are for `score()`.
- `bbox` is `[x0, top, x1, bottom]` in PDF points, with a top-left origin (pdfplumber).

**`number_format`** (optional, schema level): how the documents print numbers. `us` (the default) is
`1,234.56`. `dot_thousands` is `1.234.567,89` and `60.000` (sixty thousand): dots group thousands, a comma
marks decimals, and a dot group that is not three digits makes the token unreadable. It applies to page text
and to printed strings in the extraction.

The schema fields mean:
- **`label`**: printed row labels that identify the field.
  - Matching uses words longer than two letters. Tokens with a digit that are not amounts (`59263`, `2207` of
    `KIS-2207`, `2024`) are identity tokens: the line must print each of them (a label cut short with `...` needs
    only the ones before the cut), and the number spelling them is not read as a value. The same holds for a
    record's `entity`.
  - With several labels and an entity, only the labels that name the entity are read, when any do.
  - With an empty list, the row is found by the record's `entity` if it has one, otherwise by the field name.
- **`units`**:
  - A currency code (`USD`, `EUR`, ..., or `currency`) marks a money amount. For a money amount, percentages
    and multiples (`44%`, `1.35x`) are never read, and a page scale ("in thousands") applies.
  - A unit containing `rounded` or `approx` marks a figure printed rounded, such as prose like "$75.0M". Such
    a figure is never read off the page and never flagged as "not printed".
- **`scale`** (optional): stored values are printed in units of `scale` (e.g. 1000, stored as printed "in
  thousands"). A page whose scale statement equals it (or has none) then compares one to one.
- **`sign`**: `pos` or `neg` declares a stored sign convention that holds however the page prints the number.
  Paid-in capital, for example, is stored positive but printed "(2,850,000)". For these fields, the page's
  sign is ignored, and a flip back to the declared sign is accepted without page confirmation. `any` takes the
  page's sign.

The record fields mean:
- **`column`**: the column header text printed above the cell, or null for a one-number line.
- **`entity`**: a row-group heading the row sits under or names, such as a company in a schedule.

An **extraction** is a list of `{"key", "value", "page", "printed_text"}`. `value` may be null or a printed
string. A key that is missing counts as blank. qcore places each citation itself: it finds where the answered
number is printed on the answered page, and prefers the line containing `printed_text`. An optional `bbox`
overrides this placement.

`run()` returns:
- `fixes`: `[{key, old, new, reason, kind, found_by, checks, summary}]`. These are the tier 1 changes.
- `tier2`: `[{keys, reason, evidence}]`. `evidence` holds a summary, the failing identities and any suggested
  fix.
- `values`: the final values.
- `failing_before` and `failing_after`.

## OCR mode

`page_text_mode="ocr"` reads each page from OCR output instead of the PDF text layer. Make it first:

```
python -m qcore9 ocr <images> <ocr dir> --engine doctr --bundle <bundle>      # or --engine tesseract
```

`<images>/<doc>/page-N.png` are the page images; `<ocr dir>/<doc>.json` holds the words with their boxes (PDF points)
and confidences. Pages are deskewed first. `--bundle` gives the points-per-pixel scale from the PDF page width (else
`--dpi`).

Setup (CPU only):
- **doctr** (recommended on dev): `python3 -m pip install "onnxtr[cpu]"` (tested with OnnxTR 0.9.0, onnxruntime 1.29.0). The models are
  docTR's `db_resnet50` detector and `parseq` recogniser in ONNX form. OnnxTR downloads them on first use from the
  OnnxTR GitHub releases into `$ONNXTR_CACHE_DIR` (default `~/.cache/onnxtr`). Set `ONNXTR_CACHE_DIR` to a folder
  outside the shared project folder (the weights are ~190 MB).
- **tesseract**: the `tesseract` binary (5.x, e.g. `apt-get install tesseract-ocr`) and `python3 -m pip install
  pytesseract` (tested with 5.3.4 and 0.3.13). `ocr.py` sets `OMP_THREAD_LIMIT=1`: multi-threaded Tesseract is very slow on these images.

How the OCR text is used:
- **Cleanup.** Specks are dropped. Words are snapped to the schema's label vocabulary (edit distance), and header
  years and days are snapped to the document's. Number tokens get a missing parenthesis back, and `:`/`;`/`.` before
  three final digits read as a thousands comma. The page scale is voted per document from the cells where the OCR
  and the model agree (an OCR'd "In millions" line is often unreadable).
- **Two readers.** Where the OCR of a cell's own line agrees with the model, nothing changes. Where they disagree,
  the candidates are the OCR read, its look-alike readings (one glyph swapped: 3/8, 1/7, 0/6/8, 5/6, ...; letters
  read as digits) and digit-wise mixes of the two reads (`readers.py`). A candidate is accepted only when it is the
  unique best explanation under the identities: no identity that tied may fail, every changed cell lies on an
  identity that newly ties by a change larger than its tolerance, and no rival change set does as well. A rival set,
  or two placements the identities can't separate, goes to tier 2 (`readers_disagree`). A disagreement no identity can
  check goes to tier 2 (`ocr_disagrees`).
- **Not trusted as a text layer.** In `ocr` mode the label read never replaces a value on the OCR alone, a
  page-found swap is a review, "printed nowhere" skips values that tie or whose look-alike is printed, and OCR
  agreement does not clear a cell from a localized flag (the two readers often misread the same blurred glyph alike).

## Localized flags

Each failing identity's tier 2 item names in `keys` only its inputs that are not shown right: a cell is shown right
when its own line prints it (text mode), or when it is the one unconfirmed input of an identity that ties over
confirmed numbers. All the identity's inputs are kept in `evidence.all_keys`, and the minimum-size Fellegi-Holt
explanation is given as a hint in `evidence.most_likely`. `run()` reports `flags_before_localization` and
`flags_after_localization` (distinct keys named by tier 2).

## Algorithm

Write `x` for the extracted values. `Fail(x)` is the set of identities that fail, and `Tie(x)` is the set that
run and tie. An identity with a blank term is skipped, never failed, because a sum over part of its terms says
nothing.

### 1. Label read, with the guard

**Finding the field's own line.** For each record, qcore finds its own line on the cited page. That is the
line with the best label-word match, needing at least 75% of the words, with fewer extra words winning ties.
If the cited page has no such line, qcore searches the whole document.
- An entity's row must name the entity or sit directly under a line that does.
- If several lines tie, the table shape must leave exactly one:
  - for a record with a column: one number per header cell;
  - otherwise: a lone number.

**Choosing the column.** The column comes from the header line above the row. That is the nearest line, with
no 3-digit number, that has at least two text cells right of the row label, one of which uniquely matches the
record's `column` text. The column's position is learned from rows that print one number per header cell. On a
continued table, the header comes from the nearest earlier page that has one.

**When the read is empty.** The read is empty when:
- no number sits at the column's position;
- two numbers sit there;
- an entity row has no matching header.

It never falls back to a neighbouring column. The number must also sit under the matching header cell, and the
read is empty when that cell spans several numbers (a "Three Months Ended" cell over two years).

**Prose.** A lone dash followed by words is a bullet or a separator, not a zero. On a line of running prose
(a value number followed by two or more words), a record with no column reads only the number right after its
label phrase; the phrase must occur once, and a parenthesised number there is an aside, read positive. A record
with a column on such a line is not read.

**Scale.** A page statement such as "in thousands", "($000)" or "000s omitted" sets a scale `s` for money
fields. A printed `p` matches `v` when `|p*s - v| <= s/2`.
- On a scaled page, the read can confirm a value but never replace it. "in billions" is read (qcore5), and so is a
  unit statement broken over two lines ("... by component (in" / "millions):").
- A document that states no unit anywhere: the scale under which the extraction's money values are printed in the
  document most often (at least 3, twice the runner-up, not 1) is used only to find where a number is printed
  ("printed nowhere", corroboration), never to read or change a value (qcore5).
- A value equal to the printed figure goes to tier 2 as `scale`.

**Sign.** Parentheses, a leading minus or a trailing minus mean negative. A plain number means positive only
when the document marks negatives somewhere. Otherwise the sign is unknown.
- When the digits match and the page's sign differs, qcore proposes a sign fix.
- A negative value printed as a plain number in a document that never marks negatives goes to tier 2 as
  `sign_unclear`.

**Blanks.** A blank value is read from its own line like any other value.

**The label-read gate (qcore5).** A read that differs from the extracted value changes it only when one of these holds:
- (a) **identity-backed**: applying it makes an identity through the cell tie that did not tie before (by more than
  the identity's tolerance) and breaks nothing that tied; two reads that tie an identity only together are taken when
  they are the one such pair for it; and, after the reads below are applied, a group of withheld or uncorroborated
  reads on the inputs of an identity that still fails is taken when together they make it tie, some input of it keeps
  its value and nothing that ties breaks. A read that ties an identity which another read alone also ties is a rival:
  tier 2.
- (b) **unambiguous and corroborated**: the label best-matches exactly one line in the whole document (an entity's
  row must sit under the entity), the line is not prose (or the prose read is unique), and for a record with a column
  the header places the cell (both header readings, where both apply, agree); no other column of the row is placed on
  the same cell; the read is not the number extracted for another column of the row (unless the two cells are
  swapped); and the column is checked (another cell of the column agrees with the extraction, or the row's other
  columns all do, or both header readings place it). Then the read is corroborated by one of: a sign flip; a one-glyph
  misread of the extracted number; a blank; the extracted number printed elsewhere on the row, on the page or on the
  next page (the extraction took a neighbour); or the extracted number printed nowhere in the document (the extraction
  has no source on the page). A row of label-less entity fields read whole (count, unit price, amount) counts as (b).
- Reads of kind (b) are applied jointly across the documents, under the guard: while an identity that tied fails, the
  one other input whose page cell (or a number on its own line) makes it tie again completes the correction; otherwise
  the reads feeding the most such identities are withheld. A (b) read that is a sign flip, a one-glyph misread or a
  blank fill, of an extracted number printed nowhere in the document, is never withheld: the identity tied with that
  number by accident (or the documents disagree with each other) and goes to tier 2 failing.
- **Never a power of 1000.** A read that changes the value by a power of 1000 (exactly, or with a misread digit
  besides) is never applied; neither is a change of more than ~2.5 orders of magnitude in a document that states no
  unit while its numbers are stored at a voted scale. These go to tier 2 as `scale_jump`.
- Everything else is a tier 2 `label_unverified` item (the read, and why it was not taken, in the evidence); withheld
  reads are `label_withheld` suggestions. A page-found swap or a page value at the cited spot is tier 1 only when it
  makes a failing identity tie and breaks nothing.
- `run()` returns `label_reads`: every read with its decision (`tier1`, `tier2`, `withheld`, `scale_jump`) and why.
- Schema `"label_read": "backed"` keeps qcore4's rule (applied under the guard, kept only when an identity through it
  ties over confirmed inputs), plus the scale screen.

### 2. Repair search: swap, period swap, sign

For each failing identity and each of its inputs `i`, qcore tries three kinds of candidate:
- swap `i` with every other number from the same document;
- swap `i` with the same line (field, column, entity) in the adjacent period;
- negate `i`.

**Acceptance.** A candidate counts only if `Fail(x')` is a strict subset of `Fail(x)`: at least one failure
ties and nothing that tied breaks. This is evaluated exactly from the identities.

**Vetoes.** The page then filters the candidates.
- A swap is vetoed if a cited spot, or the field's own line, prints the current number and not the
  swapped-in one.
- A flip is vetoed if the own line shows the sign the value already has.

**Ranking.** Survivors are ranked by page confirmation, then by the number of failures they fix.
- Top candidates that share no record and no identity are all applied.
- Rivals become one tier 2 `review`.

Up to 40 rounds.

### 3. Misread repair

For a failing identity `k` and input `i` with coefficient `a`, the value that closes `k` is
`v = x_i - gap_k / a`. qcore keeps `v` only when all of these hold:
- every failing identity through `i` implies the same `v`;
- `v` is a digit-level misread of `x_i`, compared at `x_i`'s decimal precision: same sign, `|v| >= 100`, and
  either at most two digits differ or one digit was added or dropped;
- the strict-subset rule above holds.

Then the page decides:
- If the field's own cell prints `x_i`, nothing changes. The document is off, not the read.
- If `v` is printed on the field's own line, at a spot that no other record's citation box overlaps, the
  repair is tier 1.
- Otherwise it is a tier 2 suggestion, and the value is left as extracted for the later stages.

### 4. What is left, and routing

**Remaining failures.** Each remaining failing identity becomes one of:
- a wrong value: an input differs from its cited spot;
- a `source_error` document finding: every input matches the page;
- a `review`.

**Values no identity covers.** Two values that each print the other's number are a page-found swap.

**Blanks.** Blanks still empty go to tier 2 as `missing`. The item also lists every number whose identities
the blanks starve.

**Values printed nowhere.** An integer of at least 100 that is printed nowhere in its document is flagged.

**Routing.**
- A wrong value is tier 1 only when the page confirms it.
- A misassignment (swap, period swap or sign) is tier 1 unless the page contradicts it. A flip back to a
  declared sign is tier 1 even then.
- Everything else goes to tier 2.

Without page text (`notext`), only the declared-sign rule can reach tier 1.

## Scoring (`qcore.score`)

A tier 0 error is an extracted value or blank that differs from `truth.jsonl`. A blank where the truth is 0
does not count. For each error, the outcome is one of:
- **fixed**;
- **closer**: changed toward the truth;
- **wrong**: changed away from it;
- **tier2**: a tier 2 item names the key;
- **missed**.

**broke** counts tier 1 changes to values that were right.

Planted document issues are correct as printed, as in `synth_eval`. Tier 1 changing them counts as broke or
wrong. Each issue is also reported as one of:
- **routed**: a tier 2 item names one of its keys;
- **changed**;
- **silent**.

## Files

| File | Contents |
|---|---|
| `bundle.py` | Loading and validation; units helpers |
| `identities.py` | Evaluating identities (blanks skip) |
| `pdftext.py` | Page text: pdfplumber words with rotated watermark text dropped, grouped into lines (the same reading as v2's text tool); OCR json loading |
| `ocr.py` | Page images to OCR json (deskew; tesseract or docTR through OnnxTR) |
| `readers.py` | The two-reader reconciliation (look-alikes, digit mixes, unique best change set) |
| `localize.py` | Identity-localized flags |
| `page.py` | Cited spot, own-line read, header and column placement, scale, sign, number search |
| `search.py` | Swap / period swap / sign candidates under the strict-subset rule |
| `core.py` | The three stages, routing, `run()`; re-read answers (`load_rereads`, `reread_routing`) |
| `auto.py` | Zero-config: identity discovery (`discover`, qcore7: aggregate ties, regular templates, no tautologies; qcore8: relaxed admission, pooled and lagged templates, sparse sums, chains, accumulation, rate products, robust unit; qcore9: slip admission, tight tolerance, section-total sums, equal ties), auto schema (`auto_schema`, qcore7: page scale votes, weak labels) |
| `reread.py` | Re-read requests (cell selection, crops, questions) and the answer simulator |
| `score.py` | Outcome categories |
| `adapters/lp.py` | LP synthetic fund to bundle |
| `adapters/cord.py` | CORD receipts to bundle, with count x unit price = amount line identities |
| `adapters/lp_validate.py` | Comparison with frozen v2; the results are in `paper/qcore_validation.md` |
