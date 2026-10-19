"""Tests for the qcore3 changes (see ../qcore3/CHANGES.md), with the package renamed to qcore9.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import json
import os

import pytest

from .test_qcore import E, F, SUM, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from qcore9.pdftext import parse_amount
from qcore9.page import SCALE_RX, data_row


def _schema(root, **kw):
    p = os.path.join(root, "schema.json")
    s = json.load(open(p))
    s.update(kw)
    json.dump(s, open(p, "w"))
    return root


def test_parse_grouped():
    f = "grouped"
    assert parse_amount("60.000", f) == 60000 and parse_amount("60,000", f) == 60000
    assert parse_amount("1.234.000", f) == 1234000
    assert parse_amount("1.500,00", f) == 1500 and parse_amount("45,000.00", f) == 45000
    assert parse_amount("1.5", f) == 1.5 and parse_amount("(1.500)", f) == -1500
    for bad in ("57,0000", "1.234.56", "1.234,567", "12345.000"):
        assert parse_amount(bad, f) is None
    assert parse_amount("60.000") == 60.0              # US unchanged


def test_scale_statements_and_date_headers():
    assert SCALE_RX.search("(millions)") and SCALE_RX.search("(Millions of dollars)") and SCALE_RX.search("(In millions)")
    assert not data_row("June 27, 2026 September 27, 2025") and data_row("Total 1,234 1,100")


def test_grouped_bundle_reads_commas_and_dots():
    rows = _row("Alpha", ["60,000"], 100) + _row("Beta", ["1.500"], 120) + _row("Total", ["61.500"], 140)
    b = _schema(make_bundle({"d1": [rows]}, dict(a=F("Alpha", "IDR"), b=F("Beta", "IDR"), t=F("Total", "IDR")),
                            [T("a", "a", 60000), T("b", "b", 1500), T("t", "t", 61500)], SUM), number_format="grouped")
    good = [E("a", 60000), E("b", 1500), E("t", 61500)]
    assert not qcore.run(b, good)["fixes"]
    assert changed(qcore.run(b, [E("a", 66000), E("b", 1500), E("t", 61500)])) == {"a": 60000}


def test_scale_continues_from_earlier_page_and_page_replaces_whole_units():
    p1 = [("(Dollars in millions)", 72, 60, "l")]
    p2 = _row("Alpha", ["1,200"], 100) + _row("Beta", ["300"], 120) + _row("Total", ["1,500"], 140)
    b = make_bundle({"d1": [p1, p2]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                    [T("a", "a", 1.2e9, page=2), T("b", "b", 3e8, page=2), T("t", "t", 1.5e9, page=2)], SUM)
    good = [E("a", 1.2e9, 2), E("b", 3e8, 2), E("t", 1.5e9, 2)]
    res = qcore.run(b, good)
    assert not res["fixes"] and not res["tier2"]
    res = qcore.run(b, [E("a", 1.7e9, 2), E("b", 3e8, 2), E("t", 1.5e9, 2)])
    assert changed(res) == {"a": 1.2e9}


def test_label_read_backed_needs_identity():
    rows = _row("Alpha", ["100"], 100) + _row("Note", ["7"], 120)
    fields = dict(a=F("Alpha"), n=F("Note"))
    b = make_bundle({"d1": [rows]}, fields, [T("a", "a", 100), T("n", "n", 7)], [])
    res = qcore.run(b, [E("a", 100), E("n", 9)])                                 # replace (default)
    assert changed(res) == {"n": 7}       # qcore5: the cell is unambiguous and the extracted 9 is printed nowhere
    assert res["label_reads"][-1]["why"] == "extraction_printed_nowhere"
    assert changed(qcore.run(b, [E("a", 100), E("n", 7), ])) == {}
    _schema(b, label_read="backed")
    res = qcore.run(b, [E("a", 100), E("n", 9)])
    assert not res["fixes"] and "n" in tier2_keys(res)                           # no identity backs it: suggested only
    _schema(b, label_read="sometimes")
    with pytest.raises(ValueError):
        qcore.load_bundle(b)


def test_entity_row_fields_without_labels_are_not_read():
    # qcore5 (CHANGES item 9) reads such a row whole: the price the model has right places the other number as the
    # count. With no extracted value on the row agreeing (and no identity among its fields), the row is still not read.
    rows = [("Coffee", 72, 100, "l"), ("2", 300, 100, "r"), ("30,000", 400, 100, "r")]
    fields = dict(cnt=F(None, "count"), price=F(None, "IDR"))
    truth = [T("c", "cnt", 2, entity="Coffee #1"), T("p", "price", 30000, entity="Coffee #1")]
    b = make_bundle({"d1": [rows]}, fields, truth, [])
    assert changed(qcore.run(b, [E("c", 3), E("p", 30000)])) == {"c": 2}
    assert not qcore.run(b, [E("c", 3), E("p", 31000)])["fixes"]   # the row can't tell the count's number from the price's
