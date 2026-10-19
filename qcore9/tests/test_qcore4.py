"""Tests for the qcore4 changes (see ../../qcore4/CHANGES.md; kept in qcore5): OCR page text and the two-reader reconcile, identity-localized
flags, product identities and whole-row reads.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import json
import os

from .test_qcore import E, F, SUM, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from qcore9.identities import Checks
from qcore9.bundle import parse_identities
from qcore9 import localize, readers
from qcore9.page import ocr_number, _snap


def ocr_from_pdf(bundle, edits=None, conf=0.99):
    """An OCR folder made from the bundle's own PDF words, with some words replaced (`edits`: text -> OCR text)."""
    import pdfplumber
    out = os.path.join(bundle, "ocr")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(os.path.join(bundle, "docs")):
        pages = []
        with pdfplumber.open(os.path.join(bundle, "docs", f)) as pdf:
            for n, pg in enumerate(pdf.pages, 1):
                ws = [[(edits or {}).get(w["text"], w["text"]), w["x0"], w["top"], w["x1"], w["bottom"], conf]
                      for w in pg.extract_words(x_tolerance=1.5, y_tolerance=2)]
                pages.append(dict(page=n, width=pg.width, height=pg.height, skew=0.0, words=ws))
        json.dump(dict(engine="test", doc=f[:-4], pages=pages), open(os.path.join(out, f[:-4] + ".json"), "w"))
    return out


def _sum_bundle():
    rows = _row("Alpha", ["1,200"], 100) + _row("Beta", ["300"], 120) + _row("Total", ["1,500"], 140)
    return make_bundle({"d1": [rows]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                       [T("a", "a", 1200), T("b", "b", 300), T("t", "t", 1500)], SUM)


def test_ocr_mode_two_readers():
    b = _sum_bundle()
    ocr = ocr_from_pdf(b)
    # the model misread a; the OCR reads it right and the identity then ties: fixed
    res = qcore.run(b, [E("a", 1800), E("b", 300), E("t", 1500)], "ocr", ocr)
    assert changed(res) == {"a": 1200} and res["fixes"][0]["found_by"] == "two_readers"
    # the OCR misreads b; the model's numbers tie: nothing changes, nothing is flagged
    ocr2 = ocr_from_pdf(b, {"300": "800"})
    os.rename(ocr2, ocr2 + "_b")
    res = qcore.run(b, [E("a", 1200), E("b", 300), E("t", 1500)], "ocr", ocr2 + "_b")
    assert not res["fixes"] and not res["tier2"]


def test_ocr_mode_lookalike_and_agreement():
    b = _sum_bundle()
    # the OCR reads '1,800' for 1,200 (no) and the model 1,700: neither ties; the OCR look-alike 1,200 (8 -> 2 is not
    # a look-alike, so nothing is fixed) -> tier 2
    ocr = ocr_from_pdf(b, {"1,200": "1,800"})
    res = qcore.run(b, [E("a", 1700), E("b", 300), E("t", 1500)], "ocr", ocr)
    assert not res["fixes"] and "a" in tier2_keys(res)
    # OCR '1,500' read as '1,300'? both readers agree on a wrong a: never changed, routed
    ocr2 = ocr_from_pdf(b, {"1,200": "1,700"})
    os.rename(ocr2, ocr2 + "_c")
    res = qcore.run(b, [E("a", 1700), E("b", 300), E("t", 1500)], "ocr", ocr2 + "_c")
    assert not res["fixes"] and "a" in tier2_keys(res)


def test_ocr_mode_needs_dir():
    b = _sum_bundle()
    try:
        qcore.run(b, [E("a", 1200)], "ocr", None)
        assert False
    except ValueError:
        pass


def test_reconcile_rules():
    ids = parse_identities([dict(id="s", terms=[["a", 1], ["b", 1], ["t", -1]], tol=0.5)])
    ch = Checks(ids)
    # unique direct read
    acc, amb = readers.reconcile(ch, {"a": 1800, "b": 300, "t": 1500}, {"a": [1200]})
    assert acc == {"a": 1200} and not amb
    # a tie is never broken
    acc, amb = readers.reconcile(ch, {"a": 1200, "b": 300, "t": 1500}, {"b": [800]})
    assert not acc
    # two cells that could each close the gap: ambiguous
    acc, amb = readers.reconcile(ch, {"a": 1700, "b": 300, "t": 1500}, {"a": [1200], "b": [-200]})
    assert not acc and amb
    # a change within the identity's tolerance is not vouched for
    ids2 = parse_identities([dict(id="s", terms=[["a", 1], ["b", 1], ["t", -1]], tol=3)])
    acc, _ = readers.reconcile(Checks(ids2), {"a": 1204, "b": 300, "t": 1500}, {"a": [1202]})
    assert not acc


def test_lookalikes_and_merges():
    assert 1200 in readers.confusions(1700) and 8352 in readers.confusions(3352)
    assert readers.merges(26104, 25504) == {26504, 25104}
    assert readers.token_variants("72.634", lambda t: float(t.replace(",", ""))) >= {72634.0}
    assert ocr_number("22:855", "us") == "22,855" and ocr_number("13,858)", "us") == "(13,858)" and ocr_number("1.57", "us") == "1.57"
    assert _snap("Cther", {"other", "total"}, {}) == "Other" and _snap("Siock-tased", {"stock", "based"}, {}) == "Stock-based"


def test_product_identity():
    ids = parse_identities([dict(id="l", terms=[["p", 1]], products=[["c", "u", -1]], tol=0.5)], {"p", "c", "u"})
    ch = Checks(ids)
    assert ch.gap(0, {"p": 30000, "c": 2, "u": 15000}) == 0 and ch.failed(0, {"p": 30000, "c": 3, "u": 15000})
    assert ch.gap(0, {"p": 30000, "c": 2}) is None
    assert ch.coef("c", 0, {"u": 15000}) == -15000 and set(ch.inputs[0]) == {"p", "c", "u"}


def test_row_read_with_product_identity():
    rows = [("Coffee", 72, 100, "l"), ("2", 250, 100, "r"), ("15,000", 330, 100, "r"), ("30,000", 420, 100, "r")]
    fields = dict(cnt=F(None, "count"), unit=F(None, "IDR"), price=F(None, "IDR"))
    truth = [T("c", "cnt", 2, entity="Coffee #1"), T("u", "unit", 15000, entity="Coffee #1"), T("p", "price", 30000, entity="Coffee #1")]
    line = [dict(id="l", terms=[["p", 1]], products=[["c", "u", -1]], tol=0.5)]
    b = make_bundle({"d1": [rows]}, fields, truth, line)
    assert not qcore.run(b, [E("c", 2), E("u", 15000), E("p", 30000)])["fixes"]
    assert changed(qcore.run(b, [E("c", 2), E("u", 15000), E("p", 38000)])) == {"p": 30000}
    assert changed(qcore.run(b, [E("c", 3), E("u", 15000), E("p", 30000)])) == {"c": 2}


def test_localized_flags():
    ids = parse_identities([dict(id="s", terms=[["a", 1], ["b", 1], ["t", -1]], tol=0.5),
                            dict(id="r", terms=[["a", 1], ["x", -1]], tol=0.5)])
    ch = Checks(ids)
    vals = {"a": 1200, "b": 900, "t": 1500, "x": 1200}
    # x confirmed by the page and r ties: a is cleared, so s's candidates are b and t
    assert localize.candidates(ch, vals, {"x"}) == {"s": {"b", "t"}}
    # nothing confirmed (notext): nothing is cleared
    assert localize.candidates(ch, vals, set()) == {"s": {"a", "b", "t"}}
