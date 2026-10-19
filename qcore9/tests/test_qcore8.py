"""Tests for the qcore8 changes (see ../../qcore8/CHANGES.md; carried into qcore9): robust rounding unit, weak labels without a misread exception,
relaxed admission of long sums, pooled transposed templates, cross-document chains and accumulations.

    python -m pytest qcore9/tests -q        (from the quarter-close folder)
"""

from __future__ import annotations

import collections

from .test_qcore import E, F, T, _row, changed, make_bundle, qcore, tier2_keys  # noqa: F401
from qcore9.auto import Discovery, Stat, robust_unit


# ---------------------------------------------------------------- the rounding unit of a column of round amounts

def test_robust_unit():
    # 3,500 1,500 6,000 ... beside 39,288 and 1,277: two numbers show units, so the unit is 1 (qcore7 voted 100)
    assert robust_unit(collections.Counter({100: 5, 1000: 2, 1: 2})) == 1
    # a single odd number among many round ones does not set the unit
    assert robust_unit(collections.Counter({100: 9, 1: 1})) == 100
    # a tenth of a large set is needed
    assert robust_unit(collections.Counter({1: 3, 100: 40})) == 100
    assert robust_unit(collections.Counter({1: 5, 100: 40})) == 1
    assert robust_unit(collections.Counter({0.01: 1})) == 0.01


# ---------------------------------------------------------------- relaxed admission of long sums

def _st(nterms, hold_page=0, hold=0, fail=0, fail_conf=0, big=0, nontriv=0, targets=()):
    s = Stat(hold_page=hold_page, hold=hold, fail=fail, fail_conf=fail_conf, big=big, nontriv=nontriv, targets=set(targets))
    s.nterms = nterms
    return s


def test_relaxed_admission():
    R = Discovery.relaxed
    assert R(_st(5, hold=2, fail=2, big=2))                        # a total of four lines failing as often as it holds
    assert not R(_st(5, hold=2, fail=3, big=2))
    assert R(_st(11, hold=2, fail=4, big=2))                       # six terms or more: up to twice as many failures
    assert not R(_st(11, hold=2, fail=5, big=2))
    assert not R(_st(6, hold=2, fail=1, big=1))                    # needs two large holds
    assert R(_st(3, hold=3, fail=1, big=2)) and not R(_st(3, hold=3, fail=2, big=2))
    assert not R(_st(2, hold=9, big=9))                            # two-term ties are never relaxed
    assert not R(_st(6, hold_page=1, hold=3, fail_conf=1, big=3))  # the page refutes it once per page confirmation
    assert R(_st(6, hold_page=4, hold=0, fail_conf=1, big=3))
    # small whole numbers: six nonzero holds over three targets, at most a fifth failing
    assert R(_st(3, hold=8, fail=2, big=0, nontriv=6, targets="abc"))
    assert not R(_st(3, hold=8, fail=2, big=0, nontriv=6, targets="ab"))
    assert not R(_st(3, hold=8, fail=3, big=0, nontriv=6, targets="abc"))


def test_pooled_templates():
    assert Discovery._pooled([(("N", "amount", "Q"), 1), (("LAG", ("N", "amount", "Q")), -1)])
    assert not Discovery._pooled([(("N", "amount", "Q"), 1), (("c", 2), -1)])


# ---------------------------------------------------------------- end to end: a carried balance across statements

def _statements(n=5, bad=None):
    docs, truth, ext = {}, [], []
    bal = 10_250
    for i in range(n):
        d = f"s{i}"
        dep, wd = 4_310 + 97 * i, 3_125 + 41 * i
        close = bal + dep - wd
        p = [("Account statement", 72, 50, "l"), ("Amount", 400, 70, "l")] \
            + _row("Opening balance", [f"{bal:,}"], 100) + _row("Deposits", [f"{dep:,}"], 120) \
            + _row("Withdrawals", [f"{wd:,}"], 140) + _row("Closing balance", [f"{close:,}"], 160)
        docs[d] = [p]
        for f, v in (("opening", bal), ("deposits", dep), ("withdrawals", wd), ("closing", close)):
            truth.append(T(f"{d}|{f}", f, v, doc=d, column="Amount", period=f"2026-{i + 1:02d}"))
            x = v + (900 if bad == (i, f) else 0)
            ext.append(E(f"{d}|{f}", x, 1, f"{f} {x:,}"))
        bal = close
    fields = dict(opening=F("Opening balance"), deposits=F("Deposits"), withdrawals=F("Withdrawals"), closing=F("Closing balance"))
    return make_bundle(docs, fields, truth, []), ext


def test_chain_of_balances_flags_a_misread_opening():
    b, ext = _statements(5, bad=(3, "opening"))
    res = qcore.run(b, ext, "notext", identities="auto", schema="hand")
    src = collections.Counter(i["source"] for i in res["identity_list"])
    assert src.get("chain", 0) >= 1, src
    assert "s3|opening" in tier2_keys(res) or changed(res).get("s3|opening") == ext[12]["value"] - 900
    assert all(k == "s3|opening" for k in changed(res))           # nothing else moves
    clean, ext0 = _statements(5)
    res0 = qcore.run(clean, ext0, "notext", identities="auto", schema="hand")
    assert not res0["fixes"]


# ---------------------------------------------------------------- end to end: year to date = last report's + this month

def _monthly(n=4, bad=None):
    rows = ["Salaries", "Rent", "Utilities", "Supplies", "Travel"]
    docs, truth, ext = {}, [], []
    ytd = {r: 0 for r in rows}
    for i in range(n):
        d = f"m{i}"
        p = [("Monthly expense report", 72, 50, "l"), ("Month Year to date", 380, 70, "l")]
        for j, r in enumerate(rows):
            m = 1_000 + 137 * j + 53 * i + 7 * j * i
            ytd[r] += m
            p += _row(r, [f"{m:,}", f"{ytd[r]:,}"], 100 + 20 * j, (400, 480))
            for col, v in (("Month", m), ("Year to date", ytd[r])):
                k = f"{d}|{r}|{col}"
                truth.append(T(k, "expense", v, doc=d, entity=r, column=col, period=f"2026-{i + 1:02d}"))
                x = v + (90 if bad == (i, r, col) else 0)
                ext.append(E(k, x, 1, f"{r} {x:,}"))
        docs[d] = [p]
    return make_bundle(docs, dict(expense=F(None)), truth, []), ext


def test_accumulation_across_reports():
    b, ext = _monthly(4, bad=(2, "Rent", "Year to date"))
    res = qcore.run(b, ext, "notext", identities="auto", schema="hand")
    src = collections.Counter(i["source"] for i in res["identity_list"])
    assert src.get("accum", 0) >= 1, src
    k = "m2|Rent|Year to date"
    assert k in tier2_keys(res) or k in changed(res)
    assert all(x == k for x in changed(res))
    clean, ext0 = _monthly(4)
    assert not qcore.run(clean, ext0, "notext", identities="auto", schema="hand")["fixes"]


# ---------------------------------------------------------------- end to end: tax = tax rate x taxable amount (rows)

def _taxed(n=5, bad=None):
    docs, truth, ext = {}, [], []
    for i in range(n):
        d = f"v{i}"
        base = 1_250.40 + 311.7 * i
        rate = (0.06625, 0.0798, 0.0725, 0.06625, 0.0875)[i % 5]
        tax = round(base * rate, 2)
        p = [("Invoice", 72, 50, "l")] + _row("Taxable amount", [f"{base:,.2f}"], 100) + _row("Tax rate", [f"{100 * rate:.3f}%"], 120) \
            + _row("Sales tax", [f"{tax:,.2f}"], 140)
        docs[d] = [p]
        for f, v in (("taxable", base), ("tax_rate", rate), ("tax", tax)):
            truth.append(T(f"{d}|{f}", f, v, doc=d, period=f"2026-0{i + 1}"))
            ext.append(E(f"{d}|{f}", (0.0898 if bad == (i, f) else v), 1))
        docs[d] = [p]
    fields = dict(taxable=F("Taxable amount"), tax_rate=F("Tax rate", units="fraction", sign="pos"), tax=F("Sales tax"))
    return make_bundle(docs, fields, truth, []), ext


def test_row_product_with_a_rate():
    b, ext = _taxed(5, bad=(2, "tax_rate"))
    res = qcore.run(b, ext, "notext", identities="auto", schema="hand")
    src = collections.Counter(i["source"] for i in res["identity_list"])
    assert src.get("rowprod", 0) >= 3, src
    assert "v2|tax_rate" in tier2_keys(res) and not res["fixes"]
    clean, ext0 = _taxed(5)
    res0 = qcore.run(clean, ext0, "notext", identities="auto", schema="hand")
    assert not res0["fixes"] and not res0["tier2"]
