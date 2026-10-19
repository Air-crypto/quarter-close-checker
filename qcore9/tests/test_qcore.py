"""qcore unit tests on tiny bundles generated here (reportlab PDFs with a text layer).

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import qcore9 as qcore  # noqa: E402
from qcore9.bundle import parse_identities  # noqa: E402

H = 792.0


def _pdf(path, pages):
    """pages: [[(text, x, y_top, align)]] with y measured from the top of the page."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=(612, H))
    for items in pages:
        c.setFont("Helvetica", 10)
        for text, x, top, align in items:
            y = H - top - 8
            (c.drawRightString if align == "r" else c.drawString)(x, y, text)
        c.showPage()
    c.save()


def _row(label, nums, top, xs=(400,)):
    out = [(label, 72, top, "l")]
    for n, x in zip(nums, xs):
        out.append((n, x, top, "r"))
    return out


def make_bundle(docs, fields, truth, identities, meta=None):
    """docs: {doc_id: [page items]}; truth rows without page/bbox are fine (scoring needs only values)."""
    root = tempfile.mkdtemp(prefix="qcore_test_")
    os.makedirs(os.path.join(root, "docs"))
    for d, pages in docs.items():
        _pdf(os.path.join(root, "docs", f"{d}.pdf"), pages)
    json.dump(dict(domain="test", fields=fields), open(os.path.join(root, "schema.json"), "w"))
    with open(os.path.join(root, "truth.jsonl"), "w") as fh:
        for t in truth:
            fh.write(json.dumps(dict(dict(column=None, period=None, entity=None, page=1, bbox=None), **t)) + "\n")
    json.dump(identities, open(os.path.join(root, "identities.json"), "w"))
    json.dump(meta or dict(planted_document_issues=[]), open(os.path.join(root, "meta.json"), "w"))
    return root


def F(label, units="USD", sign="any"):
    return dict(label=[label] if label else [], units=units, sign=sign)


def T(key, field, value, doc="d1", **kw):
    return dict(key=key, doc=doc, field=field, value=value, **kw)


def E(key, value, page=1, printed=None, bbox=None):
    d = dict(key=key, value=value, page=page, printed_text=printed)
    if bbox:
        d["bbox"] = bbox
    return d


SUM = [dict(id="sum", terms=[["a", 1], ["b", 1], ["t", -1]], const=0, tol=0.5, kind="within_doc")]


def changed(res):
    return {f["key"]: f["new"] for f in res["fixes"]}


def tier2_keys(res):
    return {k for t in res["tier2"] for k in t["keys"]}


# ---------------------------------------------------------------- identities parse

def test_identities_parse():
    ids = parse_identities([dict(id="x", terms=[["a", 1], ["b", -2.5]], const=3, tol=1, kind="cross_doc")], {"a", "b"})
    assert ids[0].terms == [("a", 1.0), ("b", -2.5)] and ids[0].const == 3.0 and ids[0].kind == "cross_doc"
    with pytest.raises(ValueError):
        parse_identities([dict(id="x", terms=[["a", 1]])], {"b"})               # unknown key
    with pytest.raises(ValueError):
        parse_identities([dict(id="x", terms=[["a"]])], {"a"})                  # malformed term
    dup = parse_identities([dict(id="x", terms=[["a", 1]]), dict(id="x", terms=[["a", 1]]), dict(id="x", terms=[["a", 1]])], {"a"})
    assert [i.id for i in dup] == ["x", "x#2", "x#3"]                     # qcore5: duplicate ids are suffixed, not refused
    with pytest.raises(ValueError):
        parse_identities([dict(id="x", terms=[["a", 1]], kind="sideways")], {"a"})
    from qcore9.identities import Checks
    ch = Checks(ids)
    assert ch.run({"a": 1, "b": 0})[0]["failed"] is True          # 1 - 0 + 3 = 4 > 1
    assert ch.run({"a": -0.5, "b": 1})[0]["failed"] is False      # -0.5 - 2.5 + 3 = 0
    assert ch.run({"a": 1})[0]["gap"] is None                     # a blank skips the identity


# ---------------------------------------------------------------- the guard

def _guard_bundle(alpha_on_page1, alpha_in_table):
    p1 = [("Summary", 72, 60, "l")] + _row("Alpha", [alpha_on_page1], 100)
    p2 = [("Table", 72, 60, "l")] + _row("Alpha", [alpha_in_table], 100) + _row("Beta", ["200"], 120) + _row("Total", ["300"], 140)
    return make_bundle({"d1": [p1, p2]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                       [T("a", "a", 100), T("b", "b", 200), T("t", "t", 300)], SUM)


def test_guard_withholds_a_read_that_breaks_a_tied_identity():
    # The cited page's "Alpha" line prints 150; using it would break the tied sum, and the field's best
    # line in the whole document is ambiguous (two "Alpha" lines), so nothing backs it: tier 2, unchanged.
    b = _guard_bundle("150", "100")
    ext = [E("a", 100, 1, "Alpha"), E("b", 200, 2, "Beta"), E("t", 300, 2, "Total")]
    res = qcore.run(b, ext)
    assert "a" not in changed(res)
    held = [t for t in res["tier2"] if t["keys"] == ["a"]]
    assert held and held[0]["evidence"]["suggested_fix"]["found_by"] in ("label_withheld", "label_unverified")   # qcore5: unverified (two Alpha lines)
    assert res["values"]["a"] == 100


def test_guard_whole_document_exception():
    # The page itself disagrees (Alpha 110 + Beta 200 != Total 300) and the model entered the consistent 100.
    # The field's one line prints 110, a one-glyph read of 100, and 100 is printed nowhere: qcore5 (as qcore4) lets the
    # read stand although the sum breaks; the failing sum goes to tier 2 (the planted issue is routed through it).
    p1 = [("Table", 72, 60, "l")] + _row("Alpha", ["110"], 100) + _row("Beta", ["200"], 120) + _row("Total", ["300"], 140)
    b = make_bundle({"d1": [p1]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                    [T("a", "a", 110), T("b", "b", 200), T("t", "t", 300)], SUM,
                    meta=dict(planted_document_issues=[dict(keys=["a", "b", "t"], kind="inconsistent_total")]))
    ext = [E("a", 100, 1, "Alpha"), E("b", 200, 1, "Beta"), E("t", 300, 1, "Total")]
    res = qcore.run(b, ext)
    assert changed(res) == {"a": 110}
    card = qcore.score(b, ext, res)
    assert card["totals"]["fixed"] == 1 and card["totals"]["broke"] == 0
    assert card["planted_issues"][0]["outcome"] == "routed"     # the sum now fails: a document finding for tier 2


# ---------------------------------------------------------------- swap

def test_swap_fix_confirmed_by_the_page():
    # Labels the page doesn't carry (no label read), citations at the true spots, values swapped.
    # sum: a + b = t; cross: a = c (another document). Swapping a and b is the one fix that ties both.
    rows = _row("First line", ["100"], 100) + _row("Second line", ["250"], 120) + _row("Total line", ["350"], 140)
    other = _row("Carried figure", ["100"], 100)
    ids = SUM + [dict(id="cross", terms=[["a", 1], ["c", -1]], const=0, tol=0.5, kind="cross_doc")]
    b = make_bundle({"d1": [rows], "d2": [other]}, dict(a=F("zzz qqq"), b=F("yyy www"), t=F("Total line"), c=F("Carried figure")),
                    [T("a", "a", 100), T("b", "b", 250), T("t", "t", 350), T("c", "c", 100, doc="d2")], ids)
    import pdfplumber
    with pdfplumber.open(os.path.join(b, "docs", "d1.pdf")) as pdf:
        words = pdf.pages[0].extract_words()
    box = {w["text"]: [w["x0"], w["top"], w["x1"], w["bottom"]] for w in words}
    ext = [E("a", 250, 1, "First line", box["100"]), E("b", 100, 1, "Second line", box["250"]),
           E("t", 350, 1, "Total line"), E("c", 100, 1, "Carried figure")]
    res = qcore.run(b, ext)
    assert changed(res) == {"a": 100, "b": 250}
    assert {f["kind"] for f in res["fixes"]} == {"swap"}
    assert res["failing_after"] == []
    assert qcore.score(b, ext, res)["totals"]["fixed"] == 2
    # Without page text the same swap is only a suggestion for tier 2.
    res2 = qcore.run(b, ext, "notext")
    assert not res2["fixes"] and {"a", "b"} <= tier2_keys(res2)


# ---------------------------------------------------------------- sign

def test_sign_fix_from_the_page():
    rows = _row("Gain", ["1,500"], 100) + _row("Loss", ["(400)"], 120) + _row("Net", ["1,100"], 140)
    ids = [dict(id="net", terms=[["g", 1], ["l", 1], ["n", -1]], const=0, tol=0.5, kind="within_doc")]
    b = make_bundle({"d1": [rows]}, dict(g=F("Gain"), l=F("Loss"), n=F("Net")),
                    [T("g", "g", 1500), T("l", "l", -400), T("n", "n", 1100)], ids)
    ext = [E("g", 1500, 1, "Gain"), E("l", 400, 1, "Loss"), E("n", 1100, 1, "Net")]
    res = qcore.run(b, ext)
    assert changed(res) == {"l": -400}
    assert res["fixes"][0]["kind"] == "sign" and res["fixes"][0]["reason"] == "wrong_sign"


def test_declared_sign_is_not_taken_from_the_page():
    # A field stored positive though printed in parentheses (schema sign "pos") keeps its sign.
    rows = _row("Paid in", ["(400)"], 100) + _row("Plain", ["50"], 120)
    b = make_bundle({"d1": [rows]}, dict(p=F("Paid in", sign="pos"), q=F("Plain")),
                    [T("p", "p", 400), T("q", "q", 50)], [])
    res = qcore.run(b, [E("p", 400, 1, "Paid in"), E("q", 50, 1, "Plain")])
    assert not res["fixes"] and not res["tier2"]


# ---------------------------------------------------------------- scale

def test_scale_page_confirms_but_never_replaces():
    rows = [("(in thousands of U.S. dollars)", 72, 70, "l")] + _row("Revenue", ["1,234"], 100) + _row("Costs", ["1,000"], 120)
    b = make_bundle({"d1": [rows]}, dict(r=F("Revenue"), c=F("Costs")),
                    [T("r", "r", 1234000), T("c", "c", 1000000)], [])
    # Copied the printed thousands: routed to tier 2 as a scale issue, never rescaled.
    ext = [E("r", 1234, 1, "Revenue"), E("c", 1000000, 1, "Costs")]
    res = qcore.run(b, ext)
    assert not res["fixes"]
    assert any(t["reason"] == "scale" and t["keys"] == ["r"] for t in res["tier2"])
    # A full-unit value the page confirms at its scale is left alone and not flagged.
    assert "c" not in tier2_keys(res)
    assert qcore.score(b, ext, res)["totals"]["tier2"] == 1


# ---------------------------------------------------------------- nulls

def test_null_value_skips_identities_and_goes_to_tier2():
    rows = _row("Alpha", ["100"], 100) + _row("Beta", ["200"], 120) + _row("Total", ["300"], 140)
    # Beta's label is not on the page, so the page pass can't fill the blank.
    b = make_bundle({"d1": [rows]}, dict(a=F("Alpha"), b=F("qqq zzz"), t=F("Total")),
                    [T("a", "a", 100), T("b", "b", 200), T("t", "t", 300)], SUM)
    ext = [E("a", 100, 1, "Alpha"), E("b", None, 1), E("t", 300, 1, "Total")]
    res = qcore.run(b, ext)
    assert res["failing_before"] == [] and not res["fixes"]          # the partial sum is skipped, not failed
    miss = [t for t in res["tier2"] if t["reason"] == "missing"]
    assert miss and miss[0]["keys"][0] == "b" and {"a", "t"} <= set(miss[0]["keys"])   # numbers it starves
    assert qcore.score(b, ext, res)["errors"][0]["outcome"] == "tier2"


def test_null_value_filled_from_its_own_line():
    rows = _row("Alpha", ["100"], 100) + _row("Beta", ["200"], 120) + _row("Total", ["300"], 140)
    b = make_bundle({"d1": [rows]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")),
                    [T("a", "a", 100), T("b", "b", 200), T("t", "t", 300)], SUM)
    res = qcore.run(b, [E("a", 100, 1, "Alpha"), E("b", None, 1), E("t", 300, 1, "Total")])
    assert changed(res) == {"b": 200}
