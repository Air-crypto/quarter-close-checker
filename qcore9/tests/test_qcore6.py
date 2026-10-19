"""Tests for the qcore6 changes (see ../CHANGES.md): zero-config identity discovery and schema, and the re-read tier.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from .test_qcore import E, F, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from .test_qcore4 import ocr_from_pdf
from qcore9 import bundle as B
from qcore9.auto import label_of_printed, detect_format


def _ids(res, source=None):
    out = []
    for i in res.get("identity_list") or []:
        if source is None or i.get("source") == source:
            out.append({k: c for k, c in i["terms"]})
    return out


# ---------------------------------------------------------------- discovery

def _table(vals, cols=("2026", "2025")):
    """A two-column table: Alpha + Beta = Total in each column (vals: {row: [col values as printed]})."""
    xs = (400, 480)
    items = [("Table", 72, 60, "l"), (cols[0], 380, 80, "l"), (cols[1], 460, 80, "l")]
    for n, (lab, nums) in enumerate(vals.items()):
        items += _row(lab, nums, 100 + 20 * n, xs)
    return items


def _sum2_bundle(page_vals):
    truth = []
    for f, lab in (("a", "Alpha"), ("b", "Beta"), ("t", "Total")):
        for c, v in zip(("2026", "2025"), {"a": (1200, 1100), "b": (300, 250), "t": (1500, 1350)}[f]):
            truth.append(T(f"{f}{c}", f, v, column=c))
    return make_bundle({"d1": [_table(page_vals)]}, dict(a=F("Alpha"), b=F("Beta"), t=F("Total")), truth, [])


def _ext(vals):
    return [E(k, v, 1, None) for k, v in vals.items()]


def test_discovers_a_column_sum_validated_on_the_page():
    b = _sum2_bundle({"Alpha": ["1,200", "1,100"], "Beta": ["300", "250"], "Total": ["1,500", "1,350"]})
    ext = _ext({"a2026": 1200, "a2025": 1100, "b2026": 300, "b2025": 250, "t2026": 1800, "t2025": 1350})
    res = qcore.run(b, ext, "text", identities="auto")
    sums = _ids(res, "vsum")
    assert {"a2026": 1.0, "b2026": 1.0, "t2026": -1.0} in sums and {"a2025": 1.0, "b2025": 1.0, "t2025": -1.0} in sums
    assert changed(res) == {"t2026": 1500}
    assert res["coverage"]["in_identity"] == 6


def test_no_identity_is_invented_when_the_page_disagrees():
    # the page's numbers do not add up in either column: the extraction alone (which does) cannot make a sum
    b = _sum2_bundle({"Alpha": ["1,200", "1,100"], "Beta": ["900", "950"], "Total": ["1,500", "1,350"]})
    ext = _ext({"a2026": 1200, "a2025": 1100, "b2026": 300, "b2025": 250, "t2026": 1500, "t2025": 1350})
    res = qcore.run(b, ext, "text", identities="auto")
    assert not _ids(res, "vsum")


def test_hand_and_both_identities():
    b = _sum2_bundle({"Alpha": ["1,200", "1,100"], "Beta": ["300", "250"], "Total": ["1,500", "1,350"]})
    ext = _ext({"a2026": 1200, "a2025": 1100, "b2026": 300, "b2025": 250, "t2026": 1500, "t2025": 1350})
    assert qcore.run(b, ext, "text")["coverage"]["in_identity"] == 0         # the bundle has no hand identities
    assert qcore.run(b, ext, "text", identities="both")["coverage"]["in_identity"] == 6


def test_discovers_equal_ties_across_documents():
    # the same labelled number printed in two statements (net income on the income statement and the cash flow)
    is_ = [("Income statement", 72, 60, "l")] + _row("Net income", ["12,345", "11,111"], 100, (400, 480))
    cf = [("Cash flows", 72, 60, "l")] + _row("Net income", ["12,345", "11,111"], 100, (400, 480))
    truth = [T("is26", "ni_is", 12345, doc="is", column="2026"), T("is25", "ni_is", 11111, doc="is", column="2025"),
             T("cf26", "ni_cf", 12345, doc="cf", column="2026"), T("cf25", "ni_cf", 11111, doc="cf", column="2025")]
    b = make_bundle({"is": [is_], "cf": [cf]}, dict(ni_is=F("Net income"), ni_cf=F("Net income")), truth, [])
    ext = _ext({"is26": 12345, "is25": 11111, "cf26": 12345, "cf25": 11111})
    res = qcore.run(b, ext, "text", identities="auto")
    eq = _ids(res, "equal")
    assert {"is26": 1.0, "cf26": -1.0} in eq or {"cf26": 1.0, "is26": -1.0} in eq


def test_discovers_quantity_times_price():
    rows = [("Invoice", 72, 60, "l")]
    data = [("Widget", 3, 25.0, 75.0), ("Gadget", 4, 12.5, 50.0), ("Gizmo", 2, 40.0, 80.0)]
    truth = []
    for n, (name, q, p, a) in enumerate(data):
        rows += _row(name, [str(q), f"{p:,.2f}", f"{a:,.2f}"], 100 + 20 * n, (380, 450, 520))
        truth += [T(f"q{n}", "qty", q, entity=name), T(f"p{n}", "price", p, entity=name), T(f"m{n}", "amount", a, entity=name)]
    b = make_bundle({"d1": [rows]}, dict(qty=F("", "count"), price=F(""), amount=F("")), truth, [])
    ext = [E(t["key"], t["value"], 1, None) for t in truth]
    res = qcore.run(b, ext, "text", identities="auto")
    prods = [i for i in res["identity_list"] if i.get("source") == "product"]
    assert prods and all(i["products"] for i in prods)


# ---------------------------------------------------------------- zero-config schema

def test_label_of_printed_and_format():
    assert label_of_printed("Total revenue 1,234 1,100") == "Total revenue"
    assert label_of_printed("Cash, end of period $ 1,234") == "Cash, end of period"
    assert label_of_printed("1,234") is None
    assert detect_format([dict(printed_text="Total 1.234.567")] * 3 + [dict(printed_text="Total 1,234")]) == "grouped"


def test_zero_config_schema_from_printed_text():
    b = _sum2_bundle({"Alpha": ["1,200", "1,100"], "Beta": ["300", "250"], "Total": ["1,500", "1,350"]})
    os.remove(os.path.join(b, "schema.json"))
    os.remove(os.path.join(b, "identities.json"))
    pt = {"a": "Alpha", "b": "Beta", "t": "Total"}
    vals = {"a2026": 1200, "a2025": 1100, "b2026": 300, "b2025": 250, "t2026": 1800, "t2025": 1350}
    ext = [E(k, v, 1, f"{pt[k[0]]} $ {v:,}") for k, v in vals.items()]
    ctx = {}
    res = qcore.core.run(b, ext, "text", identities="auto", schema="auto", _ctx=ctx)
    f = ctx["bun"].fields
    assert f["a"].labels == ["Alpha"] and f["t"].labels == ["Total"] and f["a"].units == "currency"
    assert changed(res) == {"t2026": 1500}


def test_zero_config_label_ambiguous_in_a_document_is_not_read():
    # 'Total' also fits 'Sub total': in this document the label names two lines and the extracted number is printed on
    # neither, so the field is not read by label there (no change; an unverifiable read is never applied)
    rows = [("Receipt", 72, 60, "l")] + _row("Sub total", ["90"], 100) + _row("Total", ["99"], 120)
    other = [("Receipt", 72, 60, "l")] + _row("Total", ["50"], 100)
    third = [("Receipt", 72, 60, "l")] + _row("Total", ["70"], 100)
    truth = [T("t1", "total", 99, doc="r1"), T("t2", "total", 50, doc="r2"), T("t3", "total", 70, doc="r3")]
    b = make_bundle({"r1": [rows], "r2": [other], "r3": [third]}, dict(total=F("Total")), truth, [])
    os.remove(os.path.join(b, "schema.json"))
    ext = [E("t1", 95, 1, "Total 95"), E("t2", 50, 1, "Total 50"), E("t3", 70, 1, "Total 70")]
    ctx = {}
    res = qcore.core.run(b, ext, "text", identities="hand", schema="auto", _ctx=ctx)
    assert "t1" not in changed(res)
    bun = ctx["bun"]
    assert bun.field_of(bun.records[0]).no_label


# ---------------------------------------------------------------- re-read tier

def _single_bundle():
    rows = [("Table", 72, 60, "l")] + _row("Other assets", ["4,321"], 100) + _row("Land", ["800"], 120)
    return make_bundle({"d1": [rows]}, dict(x=F("Other assets"), y=F("Land")), [T("x", "x", 4321), T("y", "y", 800)], [])


def test_reread_agreeing_with_ocr_needs_an_identity():
    b = _single_bundle()
    ocr = ocr_from_pdf(b)
    ext = [E("x", 4821), E("y", 800)]
    base = qcore.run(b, ext, "ocr", ocr)
    assert "x" not in changed(base) and "x" in tier2_keys(base)       # OCR alone does not change it
    # nor does OCR plus a re-read: the two readers can share a glyph confusion on a poor scan (no identity here)
    res = qcore.run(b, ext, "ocr", ocr, reread=[dict(key="x", printed_text="4,321", row_label="Other assets")])
    assert "x" not in changed(res) and "x" in tier2_keys(res)
    assert [r["outcome"] for r in res["rereads"] if r["key"] == "x"] == ["disagrees"]   # with the extraction
    # a re-read of another row (the crop sat on the wrong line) is no evidence
    res = qcore.run(b, ext, "ocr", ocr, reread=[dict(key="x", printed_text="4,321", row_label="Land")])
    assert "x" not in changed(res)
    assert [r["outcome"] for r in res["rereads"] if r["key"] == "x"] == ["wrong_cell"]


def test_reread_agreeing_with_the_extraction_stops_the_ocr():
    b = _single_bundle()
    ocr = ocr_from_pdf(b, {"4,321": "4,821"})
    ext = [E("x", 4321), E("y", 800)]
    res = qcore.run(b, ext, "ocr", ocr, reread=[dict(key="x", printed_text="4,321")])
    assert "x" not in changed(res) and res["values"]["x"] == 4321


def test_reread_disagreeing_is_flagged_with_every_read():
    b = _single_bundle()
    ocr = ocr_from_pdf(b)
    ext = [E("x", 4321), E("y", 800)]
    res = qcore.run(b, ext, "ocr", ocr, reread=[dict(key="x", printed_text="4,391")])
    assert "x" not in changed(res)
    item = [t for t in res["tier2"] if t["reason"] == "reread_disagrees"]
    assert item and item[0]["evidence"]["reads"]["reread"] == "4,391" and item[0]["evidence"]["reads"]["model"] == 4321


def test_reread_never_changes_by_a_power_of_1000():
    b = _single_bundle()
    ocr = ocr_from_pdf(b)
    ext = [E("x", 4321000), E("y", 800)]
    res = qcore.run(b, ext, "ocr", ocr, reread=[dict(key="x", printed_text="4,321")])
    assert "x" not in changed(res)


def test_reread_in_notext_never_changes_a_value():
    b = _single_bundle()
    ext = [E("x", 4821), E("y", 800)]
    res = qcore.run(b, ext, "notext", reread=[dict(key="x", printed_text="4,321")])
    assert not res["fixes"] and "x" in tier2_keys(res)


def _png_of(bundle):
    img = os.path.join(bundle, "img")
    for f in os.listdir(os.path.join(bundle, "docs")):
        d = f[:-4]
        os.makedirs(os.path.join(img, d), exist_ok=True)
        subprocess.run(["pdftoppm", "-r", "100", "-png", "-f", "1", "-l", "1", "-singlefile", os.path.join(bundle, "docs", f),
                        os.path.join(img, d, "page-1")], check=True)
    return img


@pytest.mark.skipif(not any(os.access(os.path.join(p, "pdftoppm"), os.X_OK) for p in os.environ.get("PATH", "").split(":")),
                    reason="pdftoppm not installed")
def test_requests_and_simulator(tmp_path):
    b = _single_bundle()
    ocr = ocr_from_pdf(b)
    img = _png_of(b)
    ext = [E("x", 4821), E("y", 800)]
    from qcore9.reread import make_requests, simulate
    man = make_requests(b, ext, "ocr", str(tmp_path), img, ocr)
    reqs = [json.loads(l) for l in open(tmp_path / "requests.jsonl")]
    assert man["requests"] == len(reqs) >= 1 and reqs[0]["key"] == "x"
    assert (tmp_path / reqs[0]["crop"]).exists() and reqs[0]["size"][1] <= 300
    assert "row_label" in reqs[0]["question"] and '"Other assets"' in reqs[0]["question"]
    ans = simulate(str(tmp_path), b, digit=0.0, null=0.0, spot=False)
    rows = [json.loads(l) for l in open(ans)]
    assert rows[0]["printed_text"] == "4,321" and rows[0]["row_label"] == "Other assets"
    res = qcore.run(b, ext, "ocr", ocr, reread=ans)        # answers.jsonl next to requests.jsonl: keys by req_id
    assert "x" not in changed(res) and "x" in tier2_keys(res)      # no identity through x: tier 2 with the reads


# ---------------------------------------------------------------- a scan's text layer

def test_scan_text_layer_needs_digit_corroboration():
    # The same page as a scan (a full-page image under the text layer): the text layer is an OCR of the image, so the
    # weak corroborations (the extracted number printed nowhere, a blank filled) do not apply; a digit misread does.
    from PIL import Image
    from reportlab.pdfgen import canvas
    rows = [("Table", 72, 60, "l")] + _row("Other assets", ["4,321"], 100) + _row("Land", ["800"], 120)
    truth = [T("x", "x", 4321), T("y", "y", 800)]
    plain = make_bundle({"d1": [rows]}, dict(x=F("Other assets"), y=F("Land")), truth, [])
    scan = make_bundle({"d1": [rows]}, dict(x=F("Other assets"), y=F("Land")), truth, [])
    img = os.path.join(scan, "page.png")
    Image.new("RGB", (612, 792), (255, 255, 255)).save(img)
    c = canvas.Canvas(os.path.join(scan, "docs", "d1.pdf"), pagesize=(612, 792))
    c.drawImage(img, 0, 0, 612, 792)
    c.setFont("Helvetica", 10)
    for text, x, top, align in rows:
        (c.drawRightString if align == "r" else c.drawString)(x, 792 - top - 8, text)
    c.showPage()
    c.save()
    far = [E("x", 9999), E("y", 800)]           # 9,999 is printed nowhere: plain text layer takes the read
    assert changed(qcore.run(plain, far)) == {"x": 4321}
    assert "x" not in changed(qcore.run(scan, far))
    near = [E("x", 4821), E("y", 800)]          # a one-digit misread: taken on the scan too
    assert changed(qcore.run(scan, near)) == {"x": 4321}


def test_near_numbers_are_a_glyph_apart():
    from qcore9.core import _near
    assert _near(5571e6, 5671e6) and _near(64525, 54525) and _near(-2753, -2751) and _near(80, -80)
    assert not _near(122607, 41005) and not _near(2432, 45637)


def test_crop_row_check_reads_the_label_either_side():
    from qcore9.pdftext import Word, Line
    from qcore9.reread import _Rows
    ln = Line(1, 100.0, [Word("$", 300, 305, 100, 110), Word("500", 306, 330, 100, 110), Word("CBS", 340, 360, 100, 110),
                         Word("NEWS", 362, 390, 100, 110)])
    other = Line(1, 112.0, [Word("Other", 10, 40, 112, 122), Word("assets", 42, 80, 112, 122), Word("1,234", 300, 330, 112, 122)])
    R = _Rows([ln, other])
    assert R.label_score(1, 100, 110, 306, [["cbs", "news"]]) == 1.0           # the name right of its amount
    assert R.label_score(1, 112, 122, 300, [["cbs", "news"]]) == 0.0           # the next row is not it
    assert R.numbers(1, 100, 110, 0) == [[300, 100, 330, 110]]                 # '$' joins its number
    assert [c[1] for c in R.label_lines(1, [["other", "assets"]])] == [0]
