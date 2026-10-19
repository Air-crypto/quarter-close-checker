"""Tests for the qcore2 changes: number formats, prose reads, identity tokens in labels, spanning headers.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import json
import os

import pytest

from .test_qcore import E, F, SUM, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from qcore9.pdftext import amount, parse_amount


def _fmt(root, fmt):
    p = os.path.join(root, "schema.json")
    s = json.load(open(p))
    s["number_format"] = fmt
    json.dump(s, open(p, "w"))
    return root


# ---------------------------------------------------------------- number formats

def test_parse_us_default_unchanged():
    assert parse_amount("1,234.56") == 1234.56
    assert parse_amount("(1,234)") == -1234
    assert parse_amount("60.000") == 60.0            # US: a decimal point
    assert amount("1,234-") == -1234


def test_parse_dot_thousands():
    f = "dot_thousands"
    assert parse_amount("60.000", f) == 60000
    assert parse_amount("1.234.567,89", f) == 1234567.89
    assert parse_amount("12,5", f) == 12.5
    assert parse_amount("(1.234)", f) == -1234
    assert parse_amount("-1.234,50", f) == -1234.5
    assert parse_amount("7,25%", f) == 7.25
    assert amount("1.234-", f) == -1234
    assert parse_amount("12.5", f) is None             # a dot group must have three digits
    assert parse_amount("1.2345", f) is None
    assert parse_amount("1,2,3", f) is None


def test_schema_number_format_validated():
    b = make_bundle({"d1": [_row("Alpha", ["1"], 100)]}, dict(a=F("Alpha")), [T("a", "a", 1)], [])
    _fmt(b, "roman")
    with pytest.raises(ValueError):
        qcore.load_bundle(b)
    _fmt(b, "dot_thousands")
    assert qcore.load_bundle(b).number_format == "dot_thousands"


def test_dot_thousands_bundle_reads_and_fixes():
    rows = _row("Alpha", ["60.000"], 100) + _row("Beta", ["1.234,50"], 120) + _row("Total", ["61.234,50"], 140)
    b = _fmt(make_bundle({"d1": [rows]}, dict(a=F("Alpha", "IDR"), b=F("Beta", "IDR"), t=F("Total", "IDR")),
                         [T("a", "a", 60000), T("b", "b", 1234.5), T("t", "t", 61234.5)], SUM), "dot_thousands")
    # a blank filled from its own line, and printed strings in the extraction parsed in the same format
    ext = [E("a", "60.000", 1, "Alpha"), E("b", None, 1), E("t", 61234.5, 1, "Total")]
    res = qcore.run(b, ext)
    assert changed(res) == {"b": 1234.5}
    card = qcore.score(b, ext, res)
    assert card["totals"]["fixed"] == 1 and card["totals"]["broke"] == 0
    # read as US, the same page would never give sixty thousand
    b_us = _fmt(b, "us")
    assert qcore.run(b_us, [E("a", None, 1), E("b", 1234.5, 1, "Beta"), E("t", 61234.5, 1, "Total")])["values"]["a"] != 60000


# ---------------------------------------------------------------- prose

PROSE = "- Central recorded the most admissions (28,488), and the group with the largest number was circulatory (36,909)."


def _prose_bundle():
    p = [("Main points", 72, 60, "l"), (PROSE, 60, 100, "l")]
    fields = dict(n=dict(label=["Central recorded the most admissions", "the largest number was circulatory"], units="count", sign="pos"))
    return make_bundle({"d1": [p]}, fields, [T("c", "n", 28488, entity="Central"), T("r", "n", 36909, entity="Circulatory")], [])


def test_prose_number_next_to_its_phrase_is_read_positive():
    b = _prose_bundle()
    ok = [E("c", 28488, 1, "(28,488)"), E("r", 36909, 1, "(36,909)")]
    res = qcore.run(b, ok)
    assert not res["fixes"]                                 # the bullet dash is not a zero, the parentheses are not a minus
    res = qcore.run(b, [E("c", 28448, 1, "(28,448)"), E("r", None, 1)])
    assert changed(res) == {"c": 28488, "r": 36909}


# ---------------------------------------------------------------- identity tokens

def test_entity_with_an_id_reads_its_own_row():
    hdr = [("Date", 72, 80, "l"), ("Reference Description", 140, 80, "l"), ("Charges", 330, 80, "r"), ("Credits", 410, 80, "r"),
           ("Balance", 500, 80, "r")]
    rows = []
    for top, (ref, desc, ch, cr, bal) in zip((100, 115, 130, 145), [("59263", "Invoice", "2,477.46", "", "27,417.01"),
                                                                     ("224231", "Payment - ACH", "", "24,939.55", "2,477.46"),
                                                                     ("59264", "Invoice", "337.98", "", "2,815.44"),
                                                                     ("59265", "Invoice", "1,797.82", "", "4,613.26")]):
        rows += [("02/08/2025", 72, top, "l"), (f"{ref} {desc}", 140, top, "l"), (bal, 500, top, "r")]
        rows += [(ch, 330, top, "r")] if ch else [(cr, 410, top, "r")]
    fields = dict(rb=dict(label=["Invoice", "Payment - ACH"], units="USD", sign="pos"))
    truth = [T("i1", "rb", 27417.01, entity="59263 Invoice", column="Balance"),
             T("p1", "rb", 2477.46, entity="224231 Payment - ACH", column="Balance"),
             T("i2", "rb", 2815.44, entity="59264 Invoice", column="Balance"),
             T("i3", "rb", 4613.26, entity="59265 Invoice", column="Balance")]
    b = make_bundle({"d1": [hdr + rows]}, fields, truth, [])
    good = [E(t["key"], t["value"], 1) for t in truth]
    assert not qcore.run(b, good)["fixes"]                   # every "Invoice" row no longer reads the same line
    bad = [dict(e) for e in good]
    bad[2]["value"] = 2851.44
    assert changed(qcore.run(b, bad)) == {"i2": 2815.44}


# ---------------------------------------------------------------- spanning header

def test_header_cell_spanning_two_numbers_refuses():
    # qcore5: without the year line the spanning cell can't say which of 528 / 494 it is (the qcore2 test had a year
    # line too; qcore5 reads that layout as a stacked header, see test_stacked_header_reads_year_column below)
    p = [("Three Months Ended June 30", 330, 80, "r"), ("Six Months Ended June 30", 500, 80, "r")]
    p += [("Revenue", 72, 110, "l")] + [(v, x, 110, "r") for v, x in zip(["528", "494", "1,109", "1,048"], (300, 360, 450, 510))]
    b = make_bundle({"d1": [p]}, dict(r=F("Revenue")), [T("r", "r", 528, column="Three Months Ended June 30, 2025")], [])
    res = qcore.run(b, [E("r", 582, 1)])
    assert not res["fixes"]


def test_stacked_header_reads_year_column():
    p = [("Three Months Ended June 30", 330, 80, "r"), ("Six Months Ended June 30", 500, 80, "r"),
         ("2025", 300, 92, "r"), ("2024", 360, 92, "r"), ("2025", 450, 92, "r"), ("2024", 510, 92, "r")]
    p += [("Revenue", 72, 110, "l")] + [(v, x, 110, "r") for v, x in zip(["528", "494", "1,109", "1,048"], (300, 360, 450, 510))]
    b = make_bundle({"d1": [p]}, dict(r=F("Revenue")), [T("r", "r", 528, column="Three Months Ended June 30, 2025")], [])
    res = qcore.run(b, [E("r", 582, 1)])
    assert changed(res) == {"r": 528}                         # 'Three Months Ended June 30' over '2025' names one column
