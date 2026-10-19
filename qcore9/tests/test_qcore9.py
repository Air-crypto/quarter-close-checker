"""Tests for the qcore9 changes (see ../CHANGES.md): a sign the citation shows is not flipped from another print, a row
whose label spells the identity tokens is the label read, scaled page matches need a whole unit and no percent sign,
and discovery admits sums whose failures are one slip of one cell.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import os
import types

from .test_qcore import E, F, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from qcore9 import bundle as B
from qcore9.auto import Discovery, Stat, _LEAD_CUE
from qcore9.page import Pages, _ids_in_label


def _boxes(b, doc="d1"):
    import pdfplumber
    with pdfplumber.open(os.path.join(b, "docs", f"{doc}.pdf")) as pdf:
        words = pdf.pages[0].extract_words()
    box = {}
    for w in words:
        box.setdefault(w["text"], [w["x0"], w["top"], w["x1"], w["bottom"]])
    return box


# ---------------------------------------------------------------- a statement that prints each check twice

def _checks():
    p = [("Account activity", 72, 50, "l"), ("Date Description Amount", 72, 70, "l")]
    p += _row("08/03 Deposit", ["1,250.00"], 90) + _row("08/05 Check 3310", ["(87.50)"], 110) + _row("08/09 Check 3311", ["(88.10)"], 130)
    p += [("Checks paid", 72, 170, "l"), ("Check Date Amount", 72, 190, "l")]
    p += [("3310 08/05", 72, 210, "l"), ("87.50", 400, 210, "r"), ("3311 08/09", 72, 230, "l"), ("88.10", 400, 230, "r")]
    truth = [T("dep", "deposit", 1250.0, entity="08/03"), T("c1", "check", -87.50, entity="3310"), T("c2", "check", -88.10, entity="3311")]
    b = make_bundle({"d1": [p]}, dict(deposit=F("Deposit"), check=F("Check")), truth, [])
    box = _boxes(b)
    ext = [E("dep", 1250.0, 1, "Deposit 1,250.00", box["1,250.00"]), E("c1", -87.50, 1, "Check 3310 (87.50)", box["(87.50)"]),
           E("c2", -88.10, 1, "Check 3311 (88.10)", box["(88.10)"])]
    return b, ext


def test_two_prints_with_opposite_signs_keep_the_cited_sign():
    # qcore8 zero-config flipped both checks to the checks-paid copy
    b, ext = _checks()
    for ids, sch in (("auto", "auto"), ("auto", "hand"), ("both", "hand"), ("hand", "hand")):
        res = qcore.run(b, ext, "text", identities=ids, schema=sch)
        assert not changed(res), (ids, sch, res["fixes"])


def test_sign_fix_from_the_page_still_applies_without_a_second_print():
    rows = _row("Gain", ["1,500"], 100) + _row("Loss", ["(400)"], 120) + _row("Net", ["1,100"], 140)
    ids = [dict(id="net", terms=[["g", 1], ["l", 1], ["n", -1]], const=0, tol=0.5, kind="within_doc")]
    b = make_bundle({"d1": [rows]}, dict(g=F("Gain"), l=F("Loss"), n=F("Net")),
                    [T("g", "g", 1500), T("l", "l", -400), T("n", "n", 1100)], ids)
    ext = [E("g", 1500, 1, "Gain"), E("l", 400, 1, "Loss"), E("n", 1100, 1, "Net")]
    for ids_mode in ("hand", "both"):
        assert changed(qcore.run(b, ext, identities=ids_mode)) == {"l": -400}


# ---------------------------------------------------------------- identity tokens in the label part of a row

def _line(*words):
    return types.SimpleNamespace(words=[types.SimpleNamespace(text=w) for w in words])


def test_ids_in_label():
    assert _ids_in_label(["204"], _line("SKU-204", "Hand", "1,250", "12"))     # tokens are words of the label
    assert _ids_in_label(["3310"], _line("3310", "08/22", "87.50"))                 # a bare number first on the line
    assert _ids_in_label(["3310"], _line("Check", "#3310", "87.50"))
    assert not _ids_in_label(["204"], _line("SKU-199", "Soap", "40", "204"))  # only as a value after another number
    assert not _ids_in_label(["204"], _line("SKU-199", "Soap", "40"))
    assert _ids_in_label([], _line("anything"))


# ---------------------------------------------------------------- scaled page matches

def test_scaled_match_needs_the_printed_precision_and_no_percent():
    p = [("Budget summary", 72, 50, "l"), ("(in thousands)", 72, 70, "l")] \
        + _row("Revenue", ["1,234"], 100) + _row("Margin", ["7.7%"], 120) + _row("Scrap sales", ["0.4"], 140) \
        + _row("Other", ["(0.40)"], 160)
    b = make_bundle({"d1": [p]}, dict(r=F("Revenue"), m=F("Margin", "fraction"), q=F("Scrap sales"), o=F("Other")),
                    [T("r", "r", 1_234_000), T("m", "m", 0.077), T("q", "q", 400), T("o", "o", -400)], [])
    pg = Pages(B.load(b), "text")
    assert pg.find_value("d1", 1_234_000)                    # the thousands scale still applies to amounts
    assert pg.find_value("d1", 1_234_400)                    # within the printed precision
    assert not pg.find_value("d1", 1_235_000)
    assert pg.find_value("d1", 1234)
    assert not pg.find_value("d1", 7700)                     # '7.7%' is not 7.7 thousand
    assert pg.find_value("d1", 400) and pg.find_value("d1", -400)   # a sub-unit amount printed as such
    assert not pg.find_value("d1", 331)                      # '0.4' (qcore8: anything from -100 to 900)
    assert not pg.find_value("d1", 368.8)                    # finer than the page prints: not this number


# ---------------------------------------------------------------- slip admission

def _st(nterms, hold=0, fail=0, fail_conf=0, big=0, fail_slip=0):
    s = Stat(hold=hold, fail=fail, fail_conf=fail_conf, big=big, fail_slip=fail_slip)
    s.nterms = nterms
    return s


def test_slipped():
    S = Discovery.slipped
    assert S(_st(3, hold=1, fail=1, big=1, fail_slip=1))           # one column holds, the other is one digit off
    assert not S(_st(3, hold=1, fail=1, big=1, fail_slip=0))       # an arbitrary gap: a coincidence
    assert not S(_st(2, hold=1, fail=1, big=1, fail_slip=1))       # a two-term tie is never slip-admitted
    assert not S(_st(3, hold=1, fail=2, big=1, fail_slip=2))       # more failures than holds
    assert not S(_st(3, hold=1, fail=1, fail_conf=1, big=1, fail_slip=1))
    assert not S(_st(3, hold=1, fail=1, big=0, fail_slip=1))       # needs a large holding instance


def _slipper(vals, u=1):
    return types.SimpleNamespace(_u=u, S={}, E=vals, _val=lambda src, k: Discovery._val(None, src, k))


def test_one_slip():
    inst = [("a", 1), ("b", 1), ("c", 1), ("t", -1)]
    one = Discovery._one_slip
    ok = dict(a=1_234, b=5_678, c=910, t=7_822)
    assert not one(_slipper(ok), inst, 1.5)                                    # holds: nothing to explain
    assert one(_slipper(dict(ok, b=5_478)), inst, 1.5)                          # one digit of one cell
    assert one(_slipper(dict(ok, c=-910)), inst, 1.5)                           # the sign of one cell
    assert not one(_slipper(dict(ok, b=5_400)), inst, 1.5)                      # two digits
    assert not one(_slipper(dict(ok, b=4_478)), inst, 1.5)                      # two digits again
    assert not one(_slipper(dict(ok, t=7_000)), inst, 1.5)
    assert not one(_slipper(dict(a=12, b=56, c=9, t=79)), inst, 1.5)            # small cells: a one-digit change is common


def test_lead_cue():
    for s in ("Total assets", "Net cash provided by operating activities", "Subtotal", "Grand total", "TOTALS"):
        assert _LEAD_CUE.match(s), s
    for s in ("Cash and equivalents", "Network equipment", "Totally owned"):
        assert not _LEAD_CUE.match(s), s


# ---------------------------------------------------------------- end to end: a balance sheet with a misread digit

def _balance(bad=None, cols=2):
    rows = [("Cash", (1_204, 988)), ("Receivables", (3_377, 3_102)), ("Inventory", (2_519, 2_640)),
            ("Total current assets", None), ("Property and equipment", (6_731, 6_410)), ("Total assets", None)]
    hdr = [("Balance sheet", 72, 50, "l"), ("2026 2025", 420, 70, "l")]
    vals = {}
    for j in range(cols):
        cur = sum(r[1][j] for r in rows[:3])
        vals[("Total current assets", j)] = cur
        vals[("Total assets", j)] = cur + rows[4][1][j]
        for lab, v in rows:
            if v:
                vals[(lab, j)] = v[j]
    p = list(hdr)
    truth, ext = [], []
    for i, (lab, _) in enumerate(rows):
        nums = [vals[(lab, j)] for j in range(cols)]
        p += _row(lab, [f"{n:,}" for n in nums], 100 + 20 * i, (440, 520))
        for j, n in enumerate(nums):
            col = ("2026", "2025")[j]
            k = f"{lab}|{col}"
            truth.append(T(k, lab.lower().replace(" ", "_"), n, column=col))
            x = n + (200 if bad == (lab, j) else 0)
            ext.append(E(k, x, 1, None))
    fields = {lab.lower().replace(" ", "_"): F(lab) for lab, _ in rows}
    return make_bundle({"d1": [p]}, fields, truth, []), ext


def test_balance_sheet_clean_stays_clean_and_a_slip_is_not_broken():
    b, ext = _balance()
    for mode in ("text", "notext"):
        res = qcore.run(b, ext, mode, identities="auto", schema="auto")
        assert not res["fixes"], (mode, res["fixes"])
    b, ext = _balance(bad=("Receivables", 1))
    for mode in ("text", "notext"):
        res = qcore.run(b, ext, mode, identities="auto", schema="auto")
        for k, v in changed(res).items():                                    # whatever moves, moves to the truth
            assert k == "Receivables|2025" and v == 3_102, (mode, res["fixes"])
