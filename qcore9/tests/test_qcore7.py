"""Tests for the qcore7 changes (carried into qcore9) (see ../CHANGES.md): zero-config safety (label cells, independent backing of discovered
identities, value-is-label flags), weak labels and page scale votes in the auto schema, aggregate ties, no tautologies.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

from .test_qcore import E, F, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from qcore9 import bundle as B
from qcore9.auto import cue_of, Discovery
from qcore9.core import auto_unbacked, Finding, Cite, label_values, strong_auto
from qcore9.identities import Checks


# ---------------------------------------------------------------- label cells (age bands, years) are never values

def _bands_bundle():
    p = [("Main points", 72, 50, "l"),
         ("- North West recorded the most births (5,310), and the age band with the largest number was 30-34 (5,261).", 72, 70, "l"),
         ("Region Under 20 20-24 25-29 30-34 Total", 72, 100, "l"),
         ("All regions 499 2,541 4,072 5,261 12,373", 72, 120, "l")]
    truth = [T("nw", "narrative_count", 5310, entity="North West"), T("band", "narrative_count", 5261, entity="30-34")]
    return make_bundle({"d1": [p]}, dict(narrative_count=F(None, "count")), truth, [])


def test_age_band_cell_is_not_read_from_the_header_or_another_entity():
    b = _bands_bundle()
    ext = [E("nw", 5310, 1, "North West recorded the most births (5,310)"), E("band", 5261, 1, "was 30-34 (5,261)")]
    for ids, sch in (("auto", "auto"), ("auto", "hand"), ("both", "hand")):
        res = qcore.run(b, ext, "text", identities=ids, schema=sch)
        assert "band" not in changed(res), (ids, sch, res["fixes"])


def test_pages_label_cells_rules():
    from qcore9.page import Pages
    b = _bands_bundle()
    bun = B.load(b)
    pg = Pages(bun, "text")
    pg.q7 = True
    lines = pg.lines("d1")
    head = [ln for ln in lines if ln.text.startswith("Region")][0]
    assert pg._label_cells(head) and pg._value_words(head, False) == []
    prose = [ln for ln in lines if "North West" in ln.text][0]
    n_first = next(i for i, w in enumerate(prose.words) if "5,310" in w.text)
    n_band = next(i for i, w in enumerate(prose.words) if "5,261" in w.text)
    from qcore9.page import entity_words
    assert not pg._entity_before(prose, n_first, entity_words("30-34"))     # the band is named after the region's number
    assert pg._entity_before(prose, n_band, entity_words("30-34"))
    assert pg._entity_before(prose, n_first, entity_words("North West"))
    pg6 = Pages(bun, "text")                                                  # q7 off: qcore6's reads
    assert pg6._value_words(head, False)


def test_value_is_label_flag():
    truth = [T("y", "count", 120, column="2025"), T("b", "count", 77, entity="30-34"), T("d", "qty", 8, period="2025-08-14")]
    b = make_bundle({"d1": [[("x", 72, 60, "l")]]}, dict(count=F(None, "count"), qty=F(None, "count")), truth, [])
    bun = B.load(b)
    out = label_values(bun, {"y": 2025, "b": 34, "d": 8}, {}, "notext")
    assert {k for t in out for k in t["keys"]} == {"y", "b"}               # a date's month/day is not a label number
    assert all(t["reason"] == "value_is_label" for t in out)
    res = qcore.run(b, [E("y", 2025), E("b", 34), E("d", 8)], "notext", identities="auto", schema="auto")
    assert {"y", "b"} <= tier2_keys(res)
    res = qcore.run(b, [E("y", 2025), E("b", 34), E("d", 8)], "notext")     # hand mode: unchanged (no such flag)
    assert not any(t["reason"] == "value_is_label" for t in res["tier2"])


# ---------------------------------------------------------------- discovered identities need independent backing

def test_auto_unbacked():
    ids = B.parse_identities([dict(id="auto.vsum.d.0", terms=[["a", 1], ["b", 1], ["t", -1]], tol=0.5),
                              dict(id="auto.vsum.d.1", terms=[["t", 1], ["c", -1]], tol=0.5),
                              dict(id="hand", terms=[["x", 1], ["y", -1]], tol=0.5)], None)
    ids[0].source, ids[0].support = "vsum", dict(hold_page=1, hold=3, fail=1, fail_confirmed=0)
    ids[1].source, ids[1].support = "equal", dict(hold_page=0, hold=2, fail=0, fail_confirmed=0)
    bun = None              # tier() reads the bundle only for a misassignment's sign convention
    cites = {k: Cite(v, 1, None) for k, v in dict(a=100, b=200, t=300, c=999, x=5, y=6).items()}
    f1 = Finding("wrong_value", "", "misread", [], ["b"], dict(kind="value", a="b", a_value=200, b_value=210, found_by="arithmetic"), True)
    cites["t"] = Cite(310, 1, None)
    checks = Checks(ids)
    assert auto_unbacked([f1], bun, cites, checks) == {0}                  # one weak discovered identity: not applied
    ids[0].support = dict(hold_page=2, hold=3, fail=0, fail_confirmed=0)
    assert strong_auto(ids[0]) and auto_unbacked([f1], bun, cites, checks) == set()
    f2 = Finding("wrong_value", "", "misread", [], ["y"], dict(kind="value", a="y", a_value=6, b_value=5, found_by="arithmetic"), True)
    assert auto_unbacked([f2], bun, cites, checks) == set()                # a hand identity backs it
    f3 = Finding("wrong_value", "", "misread", [], ["b"], dict(kind="value", a="b", a_value=200, b_value=210, found_by="label",
                                                              basis="digit_misread"), True)
    ids[0].support = dict(hold_page=0, hold=2, fail=1, fail_confirmed=0)
    assert auto_unbacked([f3], bun, cites, checks) == set()                # rule (b) page read: not identity-backed


# ---------------------------------------------------------------- discovery: aggregate ties, no tautologies, cues

def _receipts_bundle(n_docs=6, bad=()):
    docs, truth, ext = {}, [], []
    for d in range(n_docs):
        items = [("Item A", 1000 + 10 * d), ("Item B", 2500 + d), ("Item C", 730 + 7 * d)]
        tot = sum(v for _, v in items)
        page = [("Invoice", 72, 50, "l")] + [x for n, (lab, v) in enumerate(items) for x in _row(lab, [f"{v:,}"], 80 + 20 * n)] \
            + _row("Gross amount", [f"{tot:,}"], 160)
        did = f"r{d}"
        docs[did] = [page]
        truth.append(T(f"{did}|gross", "gross_amount", tot, doc=did))     # listed before the items: not a sum of rows above
        ext.append(E(f"{did}|gross", tot))
        for lab, v in items:
            truth.append(T(f"{did}|amt|{lab}", "sub_amount", v, doc=did, entity=lab))
            ext.append(E(f"{did}|amt|{lab}", v + (900 if (d in bad and lab == "Item B") else 0)))
    return make_bundle(docs, dict(sub_amount=F(None), gross_amount=F(None)), truth, []), ext


def test_aggregate_tie_flags_in_notext_and_no_tautology():
    b, ext = _receipts_bundle(6, bad=(2,))
    res = qcore.run(b, ext, "notext", identities="auto", schema="auto")
    src = {i["source"] for i in res["identity_list"]}
    assert "aggtie" in src
    for i in res["identity_list"]:
        net = {}
        for k, c in i["terms"]:
            net[k] = net.get(k, 0) + c
        assert sum(1 for c in net.values() if abs(c) > 1e-9) >= 2           # no x - x = 0
    assert "r2|amt|Item B" in tier2_keys(res) and not res["fixes"]


def test_cue_of_field_names():
    assert cue_of("gross_amount") and cue_of("TotalAssets") and cue_of("Net income") and not cue_of("sub_amount")
    assert Discovery.regular(type("S", (), dict(nh=8, nf=4))()) and not Discovery.regular(type("S", (), dict(nh=8, nf=5))())


# ---------------------------------------------------------------- auto schema: page scale votes, weak labels

def test_page_scale_vote():
    p = [("Balance sheet (in millions)", 72, 50, "l"), ("2026 2025", 400, 70, "l")] \
        + _row("Deferred income tax assets", ["1,090", "1,206"], 100, (400, 480)) \
        + _row("Deferred income tax liabilities", ["2,779", "2,406"], 120, (400, 480))
    truth = [T("dta26", "dta", 1090e6, column="2026"), T("dta25", "dta", 1206e6, column="2025"),
             T("dtl26", "dtl", 2779e6, column="2026"), T("dtl25", "dtl", 2406e6, column="2025")]
    b = make_bundle({"d1": [p]}, dict(dta=F("Deferred income tax assets"), dtl=F("Deferred income tax liabilities")), truth, [])
    # the extractor quotes bare labels (no numbers): the scale is voted from the page
    ext = [E("dta26", 1098e6, 1, "Deferred income tax assets"), E("dta25", 1206e6, 1, "Deferred income tax assets"),
           E("dtl26", 2779e6, 1, "Deferred income tax liabilities"), E("dtl25", 2406e6, 1, "Deferred income tax liabilities")]
    ctx = {}
    res = qcore.run(b, ext, "text", identities="auto", schema="auto", _ctx=ctx)
    assert ctx["bun"].fields["dta"].units == "currency" and ctx["bun"].fields["dtl"].units == "currency"
    assert changed(res) == {"dta26": 1090e6}                                  # 1,098 -> 1,090: a digit misread on its own line


def test_weak_label():
    p = [("Schedule", 72, 50, "l"), ("2026 2025", 400, 70, "l")] \
        + _row("Deferred income tax assets", ["1,090", "1,206"], 100, (400, 480)) \
        + _row("Deferred income tax liabilities", ["2,779", "2,406"], 120, (400, 480))
    truth = [T("dta26", "dta", 1090, column="2026"), T("dta25", "dta", 1206, column="2025"),
             T("dtl26", "dtl", 2779, column="2026"), T("dtl25", "dtl", 2406, column="2025")]
    b = make_bundle({"d1": [p]}, dict(dta=F("Deferred income tax assets"), dtl=F("Deferred income tax liabilities")), truth, [])
    # both asset cells misread: no extracted number confirms the label; it fits one line with all its words
    ext = [E("dta26", 1098, 1, "Deferred income tax assets"), E("dta25", 1286, 1, "Deferred income tax assets"),
           E("dtl26", 2779, 1, "Deferred income tax liabilities"), E("dtl25", 2406, 1, "Deferred income tax liabilities")]
    ctx = {}
    res = qcore.run(b, ext, "text", identities="auto", schema="auto", _ctx=ctx)
    f = ctx["bun"].fields["dta"]
    assert f.weak and f.labels == ["Deferred income tax assets"]
    # qcore8 (changed from qcore7): a weak label's read needs an identity even for a digit misread; tier 2 instead
    assert changed(res) == {}
    assert {"dta26", "dta25"} <= {k for t in res["tier2"] for k in t["keys"]}
    ext[0] = E("dta26", 2779, 1, "Deferred income tax assets")                # not a misread: needs an identity
    res = qcore.run(b, ext, "text", identities="auto", schema="auto")
    assert "dta26" not in changed(res)


# ---------------------------------------------------------------- a number inside the row's label text is not a cell

def test_number_in_label_text_is_not_a_column_cell():
    p = [("Balance sheet", 72, 50, "l"), ("June 30, 2026 December 31, 2025", 380, 70, "l"),
         ("Property, equipment and right-of-use assets, net of accumulated depreciation of", 72, 100, "l")] \
        + _row("$2,878 and $2,756, respectively", ["2,541", "2,303"], 112, (400, 480))
    truth = [T("p26", "ppe", 2541, column="June 30, 2026"), T("p25", "ppe", 2303, column="December 31, 2025")]
    b = make_bundle({"d1": [p]}, dict(ppe=F("Property, equipment and right-of-use assets, net")), truth, [])
    ext = [E("p26", 2541, 1, "Property, equipment and right-of-use assets, net 2,541 2,303"),
           E("p25", 2303, 1, "Property, equipment and right-of-use assets, net 2,541 2,303")]
    for ids in ("auto", "both"):
        assert not changed(qcore.run(b, ext, "text", identities=ids)), ids
    from qcore9.page import Pages
    pg = Pages(B.load(b), "text")
    ln = [x for x in pg.lines("d1") if "respectively" in x.text][0]
    bb = lambda t: next((w.x0, w.top, w.x1, w.bottom) for w in ln.words if w.text == t)
    assert pg._in_text(ln, (2756, 1, bb("$2,756,"))) and pg._in_text(ln, (2878, 1, bb("$2,878")))
    assert not pg._in_text(ln, (2541, 1, bb("2,541"))) and not pg._in_text(ln, (2303, 1, bb("2,303")))
