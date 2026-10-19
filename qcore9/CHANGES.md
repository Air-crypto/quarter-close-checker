# qcore9: changes from qcore8 (frozen; see frozen_manifest.json)

qcore9 is a copy of `qcore8/` (package `qcore9`); qcore8 stays frozen. Earlier history: `../qcore8/CHANGES.md` and
before. The changes are generic: no dataset, document or field names appear in `qcore9/*.py`.

**Scope.** All but one change are in zero-config code paths (`pages.q7`, true whenever identities or the schema are
discovered, or discovery in `auto.py`). One change (`page.find_value`, change 3) applies in every mode, hand mode
included; its effect on hand mode is measured run by run under "Hand mode". The gate is unchanged: a value changes only
when (a) a failing identity then ties and nothing that tied breaks (discovered identities need independent backing), or
(b) the read of that exact cell is unambiguous and corroborated; tier 2 otherwise. qcore9 adds no new way to change a
value.

## Changes

### 1. Two prints with opposite signs (`core.diagnose`, zero-config only)
qcore8 broke 9 numbers on qcore8's fresh set (now dev, set I: `utilbank_10`, zero-config): a bank statement prints
each check twice, '(542.03)' in the transaction list and '542.03' in the checks-paid list. The zero-config label read
found the checks-paid line (its label spells the check number) and proposed the page's sign, flipping correct amounts.
qcore9 (`cited_sign_shown`, only with `pages.q7`): when the extraction's own citation (page and box) is a different
print of the same number at the record's scale, and that print shows the extracted sign, the label read is no evidence
of a sign error and no sign proposal is made. An identity can still flip the sign (repair search). With the bundle's
identities and schema (q7 off) qcore8's rule stands.

### 2. A row whose label spells the identity tokens (`page._label_lines`, `page._ids_in_label`, zero-config only)
qcore8 broke 7 numbers on set I `inventory_09` (zero-config): the record 'ANA-128 ...' was read from the line of a
neighbouring item whose numbers include 128 (a count), because `ids_ok` accepts an identity token anywhere on the line.
qcore9 (`_label_lines`, only with `pages.q7` and identity tokens): if any candidate line spells every identity token in
its label part (`_ids_in_label`: inside a word with letters or marks, 'ANA-128' or '#1275', or as a bare number with no
bare number before it on the line, '1275 08/22 542.03'), the other candidates are dropped before scoring. Flags on `inventory_09` clean zero-config text
fell from 19.2% to 0.4% (try runs, `/home/claude/q9/try/`).

### 3. Scaled page matches (`page.find_value`, all modes)
`find_value` lists every place a document prints a number, and qcore8 matched a printed number at the page's scale
('in thousands') within half a page unit whatever its printed precision: '0.4' on a page in thousands matched any
stored value from -100 to 900. On set I `budget_11` (a report that stores values at the page's own unit: 326.0 means
326.0 thousand) this let '0.4' corroborate a wrong candidate, and qcore8 broke one number in hand mode (remaining budget
326 -> 331) and one in auto mode (YTD actual 368.8 -> 378.8). qcore9 (`_print_unit`, `_at_scale_p`):
- a token with a percent sign is never a scaled amount;
- a scaled match is held to half the printed number's last digit (times the scale), not half a page unit;
- a stored value below one page unit must be the printed number times the scale exactly ('(0.40)' in millions is
  400,000; a stored 368.8 is not '0.4' in thousands).
This is the one change that applies in hand mode; it is generic (it only removes page matches that the printed digits
contradict). A first version that required every scaled match to be at least one page unit stopped confirming SEC sub-unit lines
('Other expense, net (0.40)' in millions) and added 54 hand-mode flags on sets C and notes; it was replaced by the
precision rule before the freeze.

### 4. A field's own line names the record's entity (`core.owns`, zero-config only)
qcore8 broke one number on set I `census_10` (low extractions, text, `identities="auto"`): the bundle's schema gives the
admissions field one label per day ('Mon 07 Sep' ... 'Sun 01 Nov', days printed as rows), and the identity completion
(`completion`, "a number printed on its own line") counted Thursday's line as Sunday's own line, so a week-total sum that
fails on the page itself (a planted document error on Thursday) was "completed" by changing Sunday's correct 13 to
Thursday's 11. qcore9: with `pages.q7`, a record with an entity owns only the lines of the labels that name the entity,
when any do (as the label read and rule (b) already did). Used by `completion` and the page-new/page-old flags.

### 5. Discovery, and the page basis of a read (`auto.py`, `core.diagnose`; zero-config only)
The zero-config gap to hand identities on the SEC dev sets is mostly in notext mode (no page): discovery found sums
in clean statements but dropped them where the extraction has errors, because the failures are the errors. Measured
one at a time on SEC dev A/B/C/sec, zero-config, all models (`/home/claude/q9/abl.sh`; silent / false flags, sum over
12 runs per mode; qcore8 3,075 / 1,393 notext, 216 / 740 text):
- **Slip admission** (`Discovery.slipped`, `_one_slip`, notext only; plain vertical sums of three or more terms): a sum
  that holds with a large number at least once and whose every failure is one slip of one cell (one digit replaced at
  the same length with at least three digits, or one sign flipped), as often as it holds at most, is admitted. A
  financial statement prints two or three columns; one extraction error in one of them left qcore8 one hold against
  one failure, too few for a total without a 'total' label ('Cash flows from investing activities'). A coincidental
  sum fails by an arbitrary amount, not by one digit of one cell. notext: -13 silent, +11 false flags; text: -7, +109,
  so it is off with page text.
- **Tight tolerance** (`Discovery.stats`, `TIGHT_MAX`, notext only): a sum of 4 to 8 terms of large numbers (every
  exactly holding instance `big`) that foots exactly (within half a unit) somewhere and is never off by one unit is checked to 1.5 units everywhere instead of half a unit
  per term (an instance off by 2-7 units, a last-digit misread in a long column, is then a failing check). Instances the
  page confirms cell by cell are never failed by it. notext: -28 silent, +116 false flags; text -3, +56, so it is off
  with page text. Real statements do round off by 2 units at times (PG cash flow 2025 investing: -2,777 + 64 - 11 - 33 =
  -2,757 against -2,755 printed), which is where most of its false flags come from; a 2.5-unit version gave only -3
  silent (+21 flags) and was not kept.
- **Section-total sums** (`Discovery._cue_sums`, all zero-config): when no sum is found for a total-like row (vertical
  sums), the row is tried as the sum of the last two to five section totals above it (rows already found as sums, or
  labels starting with total / net / subtotal / grand) plus up to two trailing lines: total liabilities and equity =
  total liabilities + total equity + noncontrolling interests. notext -7 silent, +10 false flags; text 0 / 0.
- **Equal ties** (`Discovery.equal`, all zero-config): two rows of one statement with no page (notext) may tie when both
  labels are total-like, they are not adjacent, and they hold in two or more columns with large and different numbers
  (total assets = total liabilities and equity; qcore8 skipped every within-document tie in notext as a possible copy);
  and a tie that holds in one period and is off by one slip of one cell in the others is admitted (net income on the
  income statement and the cash-flow statement). notext -17 silent, +15 false flags; text -1, +15.
- **Page basis kept** (`core.diagnose`, all zero-config): a page read accepted because an identity ties is also checked
  against rule (b); if rule (b) takes it on its own, it keeps its page basis, so `auto_unbacked` no longer withholds it
  when the only identity through it is discovered. Before this, the new identities turned 18 of qcore8's correct page
  fixes on SEC text into `auto_identity_unbacked` flags (e.g. PLD G&A 108,871,000 -> 106,871,000). Text: fixes
  1,161 -> 1,276.
Changes 3-5 together, SEC dev zero-config: notext silent 3,075 -> 3,011 (false flags 1,393 -> 1,541); text silent 216 ->
166, fixes 1,176 -> 1,279, false flags 740 -> 761 (try runs t8 and ab2_nt_none; text includes change 3).

## Tried on dev and not kept
- Tight tolerance and slip admission with page text (see change 5: text -3 and -7 silent for +56 and +109 false flags).
- Tight tolerance at 2.5 units (notext -3 silent, +21 false flags): little gain.
- A scaled page match only for stored values of at least one page unit (first version of change 3): SEC statements print
  sub-unit amounts ('(0.40)' in millions); hand-mode flags rose by 54 on sets C and notes.
- Not tried for lack of time: checking field names against the printed labels of a row (set E's one scrambled MSFT
  intangibles table, 24 numbers, accounts for the haiku notes gap).

## Tests
`tests/test_qcore9.py` adds 8 tests: a statement printing each check twice with opposite signs (qcore8 zero-config flipped
both checks; qcore9 changes nothing in any mode), the page sign fix still applied
without a second print, `_ids_in_label`, scaled page matches to the printed precision and never with a percent sign,
`Discovery.slipped`, `Discovery._one_slip`, the section-total cue, and a small balance sheet (clean stays clean; a
misread moves only to the truth). The 71 earlier tests are kept (renamed to qcore9). `python -m pytest qcore9/tests -q`:
see frozen_manifest.json for the count at freeze. Not unit-tested: change 4 and the page basis (they need a full
bundle; covered by the dev grid, census_10 and the SEC text runs).

## Dev
Every change was made and tuned on dev data only, all of it spent by earlier studies: sets A-H, SEC set 1 dev, notes,
CORD train / validation, realscan dev, synthetic 00-01 (`dom`), and set I = qcore8's fresh set (`out/qcore8_heldout/data`:
budget, census, inventory, utilbank 09-11 from `gen_domains8`, corp_fin / invoices / public_stats 09-10; clean / low /
high extractions), which became dev when qcore8 was run on it. Scripts: `out/qcore9_work/dev9.py` (runs, scoring;
sets H and I strict, half a printed unit), `out/qcore9_work/dev_report9.py` (the frozen grid report). The frozen grid,
qcore9 against qcore8 on every dev set and identity setting, with every broken cell explained:
`out/qcore9_work/dev_summary9.md`.

## Hand mode
Changes 1, 2, 4 and 5 run only with `pages.q7` (discovered identities or schema) or in `auto.py`, so with the bundle's
own identities and schema they are not reached. Change 3 (`find_value`) applies in hand mode with page text (notext
never reads the page). Hand mode, text, every dev set, run by run against qcore8 before the freeze
(`/home/claude/q9/try/hand_tx2`): silent wrong 283 -> 234 (set C: 85 -> 39, from loose scaled matches that had
corroborated wrong values), broken 6 -> 5 (set I `budget_11` remaining budget 326 -> 331 no longer happens; the other 5
are qcore8's own breaks on sets C and E, unchanged), fixes unchanged (3,786), flags on correct numbers 8,150 -> 8,157 (+7,
set I high). The frozen grid's run-by-run hand check is in `out/qcore9_work/dev_summary9.md`. Not done: a generic rule for
the SNA swap and the CRM principal on sets C / E (hand-mode breaks qcore8 already had).

## Not opened
- `data_ext/sec4/` and `sec_eval4/` (the next test's new SEC filings, held out) were never opened, listed or read, nor was any
  file under them.
- Everything that was opened is dev for this round: sets A-H (earlier rounds' held-out sets, SEC A/B/C among them), the
  earlier dev sets, and set I (`out/qcore8_heldout/data`, with qcore8's runs on it).
- Page images of dev sets were not looked at; every diagnosis used page text, extractions, truth and run records.

## Known limits (dev)
- notext zero-config still leaves about one and a half times as many SEC numbers silent as hand identities (dev A/B/C,
  luna above all): most of the lost errors sit in statements with so many extraction errors that no template holds
  anywhere; discovery has nothing to admit.
- Flags rise a little in zero-config notext (SEC dev: 5.04% -> about 5.5% of numbers), from the slip, tight and equal
  checks; their failing instances flag every input that no single fix explains.
- The tight tolerance assumes a statement foots within a unit; real statements are sometimes off by 2 (PG), which shows
  as a flag.
