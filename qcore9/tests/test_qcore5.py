"""Tests for the qcore5 changes (see qcore5/CHANGES.md): the label-read gate (identity-backed, or an unambiguous and
corroborated cell read), the scale screen, the stacked-header prose stop, unit statements over two lines, duplicate
identity ids.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import json
import os

from .test_qcore import E, F, SUM, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401

COL = "Three Months Ended June 27, 2026"


def _stacked_under_prose(value_text="54,252"):
    p = [("The following table shows net sales by category for the three- and nine-month periods ended June 27,", 40, 50, "l"),
         ("2026 and June 28, 2025 (in millions):", 40, 62, "l"),
         ("Three Months Ended", 395, 80, "r"), ("Nine Months Ended", 535, 80, "r"),
         ("June 27,", 350, 92, "r"), ("June 28,", 420, 92, "r"), ("June 27,", 490, 92, "r"), ("June 28,", 560, 92, "r"),
         ("2026", 350, 104, "r"), ("2025", 420, 104, "r"), ("2026", 490, 104, "r"), ("2025", 560, 104, "r")]
    p += _row("Phones", [value_text, "44,582", "196,515", "160,561"], 120, xs=(350, 420, 490, 560))
    return make_bundle({"d1": [p]}, dict(r=F("Phones")), [T("r", "r", 54252e6, column=COL)], [])


def test_stacked_header_stops_at_prose_line():
    # qcore4 joined the prose line ('... three- and nine-month ...') into every column name and read the nine-month cell
    b = _stacked_under_prose()
    assert changed(qcore.run(b, [E("r", 54252e6, 1)])) == {}
    assert changed(qcore.run(b, [E("r", 54262e6, 1)])) == {"r": 54252e6}     # a digit misread of the one cell: fixed


def test_column_read_placed_by_both_header_readings_is_taken():
    # the extraction gives the 2026 cells the 2025 numbers on every row. The header sits directly above one number per
    # cell, so both header readings place the 2026 cell, and the extracted number is printed elsewhere on the row (the
    # extraction took the neighbouring column): the reads are taken. (In a stacked or multi-block table the two readings
    # disagree or the label repeats, and the cell goes to tier 2: test_stacked_header_stops_at_prose_line.)
    p = [("2026", 400, 80, "r"), ("2025", 480, 80, "r")]
    p += _row("Alpha", ["100", "900"], 100, xs=(400, 480)) + _row("Beta", ["200", "800"], 120, xs=(400, 480))
    fields = dict(a=F("Alpha"), b=F("Beta"))
    truth = [T("a26", "a", 100, column="2026"), T("b26", "b", 200, column="2026")]
    b = make_bundle({"d1": [p]}, fields, truth, [])
    res = qcore.run(b, [E("a26", 900), E("b26", 800)])
    assert changed(res) == {"a26": 100, "b26": 200}
    truth += [T("a25", "a", 900, column="2025"), T("b25", "b", 800, column="2025")]
    b = make_bundle({"d1": [p]}, fields, truth, [])
    res = qcore.run(b, [E("a26", 900), E("a25", 900), E("b26", 800), E("b25", 800)])
    assert changed(res) == {"a26": 100, "b26": 200}


def test_identity_backed_read_is_applied():
    # 999 is no misread of 1,200 and is printed nowhere: only the sum backs the page read
    rows = _row("Alpha", ["1,200"], 100) + _row("Beta", ["300"], 120) + _row("Total", ["1,500"], 140)
    b = make_bundle({"d1": [rows]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                    [T("a", "a", 1200), T("b", "b", 300), T("t", "t", 1500)], SUM)
    res = qcore.run(b, [E("a", 999), E("b", 300), E("t", 1500)])
    assert changed(res) == {"a": 1200}
    assert res["label_reads"][0]["why"] == "identity_ties"


def test_scale_jump_is_never_applied():
    rows = _row("Alpha", ["5,000"], 100) + _row("Beta", ["300"], 120) + _row("Gamma", ["2,750"], 140) + _row("Delta", ["410"], 160)
    fields = dict(a=F("Alpha"), b=F("Beta"), g=F("Gamma"), d=F("Delta"))
    b = make_bundle({"d1": [rows]}, fields, [T("a", "a", 5000), T("b", "b", 300), T("g", "g", 2750), T("d", "d", 410)], [])
    res = qcore.run(b, [E("a", 5000000), E("b", 300), E("g", 2750), E("d", 410)])
    assert changed(res) == {} and "a" in tier2_keys(res)


def test_unit_statement_over_two_lines():
    p = [("The following table summarizes the changes by component (in", 40, 60, "l"), ("millions):", 40, 72, "l"),
         ("2026", 400, 90, "r"), ("2025", 480, 90, "r")]
    p += _row("Alpha", ["1,234", "1,111"], 110, xs=(400, 480))
    b = make_bundle({"d1": [p]}, dict(a=F("Alpha")), [T("a26", "a", 1234e6, column="2026"), T("a25", "a", 1111e6, column="2025")], [])
    res = qcore.run(b, [E("a26", 1234e6), E("a25", 1111e6)])
    assert changed(res) == {} and not tier2_keys(res)        # read at the stated scale: confirmed, and printed (in millions)


def test_duplicate_identity_ids_load():
    rows = _row("Alpha", ["1,200"], 100) + _row("Beta", ["300"], 120) + _row("Total", ["1,500"], 140)
    ids = SUM + [dict(SUM[0])]
    b = make_bundle({"d1": [rows]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                    [T("a", "a", 1200), T("b", "b", 300), T("t", "t", 1500)], ids)
    bun = qcore.load_bundle(b)
    assert [i.id for i in bun.identities] == ["sum", "sum#2"]


def test_scale_drop_with_a_changed_digit_is_never_applied():
    # the page prints 329 (no unit statement), the extraction stores -829,000,000: a scale dropped together with a digit
    rows = _row("Alpha", ["(329)"], 100) + _row("Beta", ["300"], 120) + _row("Gamma", ["2,750"], 140)
    fields = dict(a=F("Alpha"), b=F("Beta"), g=F("Gamma"))
    b = make_bundle({"d1": [rows]}, fields, [T("a", "a", -329), T("b", "b", 300), T("g", "g", 2750)], [])
    res = qcore.run(b, [E("a", -829000000), E("b", 300), E("g", 2750)])
    assert changed(res) == {} and "a" in tier2_keys(res)


def test_strict_read_kept_when_extraction_is_printed_nowhere():
    # the page prints 1,200 + 300 against a total of 1,300 (the document disagrees with itself); the extraction's 1,000
    # tied the sum by accident and is printed nowhere. The one-glyph read of Alpha's unambiguous cell is kept, and the
    # sum, now failing, goes to tier 2
    rows = _row("Alpha", ["1,200"], 100) + _row("Beta", ["300"], 120) + _row("Total", ["1,300"], 140)
    b = make_bundle({"d1": [rows]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                    [T("a", "a", 1200), T("b", "b", 300), T("t", "t", 1300)], SUM)
    res = qcore.run(b, [E("a", 1000), E("b", 300), E("t", 1300)])
    assert changed(res) == {"a": 1200}
    assert "sum" in res["failing_after"] and {"a", "t"} & tier2_keys(res)
