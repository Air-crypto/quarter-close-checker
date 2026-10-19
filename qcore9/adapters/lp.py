"""LP / private-equity synthetic fund -> generic qcore bundle (the only LP-specific code in qcore).

    python -m qcore9.adapters.lp convert <out/synth/fundNN> <out/qcore/lp/fundNN>
    python -m qcore9.adapters.lp extraction <run dir with docMM/answer.json> <out/synth/fundNN> <bundle> <extraction.json>

What it does:
- records: the live icx records of correct.jsonl (a revised capital account supersedes the original; for a
  company with several securities in the FS schedule, icx keeps the first line), numeric ones only.
  Superseded records are left out of the bundle: no identity uses them and the frozen scorer ignores them.
- fields: "<doc_type>.<field>", plus "[<row label>]" for a schedule line whose label is not the company
  (each security line is its own field). Labels are the printed row labels (deduplicated by their words).
  A company's position is found by its entity (the company); its column is the printed Cost / Fair Value
  header. Narrative figures ("has invested $75.0M") get a "_rounded" unit, so they are never read off the page.
- column: the header text actually printed above the cell (found from the correct citation with the frozen
  checker's header families), so qcore can place the column by header position without LP vocabulary.
- sign: "pos" for a field stored positive though printed negative (paid-in capital); "any" otherwise.
- identities: every icx + qclose check that runs on the correct extraction, instantiated as explicit
  [key, coefficient] lists by probing each input (two step sizes; a check whose response is not linear in
  some input, such as call % x commitment, is not expressible and is listed in meta.json instead).
- meta: planted_document_issues with the keys of the checks each issue fails on the correct extraction.
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))           # quarter-close/
V2 = os.path.join(ROOT, "v2")
CANONICAL = {"qtd": "Quarter", "ytd": "Year to Date", "itd": "Inception to Date", "lp": "Limited Partners",
             "gp": "General Partner", "total": "Total", "cost": "Cost", "carrying": "Fair Value"}
NUMERIC_UNITS = ("USD", "fraction", "count", "x")


def _v2():
    """The frozen checker (read only): its pipeline (icx checks + qclose extensions) and header families."""
    if V2 not in sys.path:
        sys.path.insert(0, V2)
    from qclose import pipeline, columns, verdict, synth_eval   # noqa: F401
    return pipeline, columns, verdict, synth_eval


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _numeric_unit(u):
    return (u or "").startswith(NUMERIC_UNITS)


def doc_id(file: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", file.lower().replace(".pdf", "")).strip("_")


def _label_words(s):
    return tuple(w for w in re.findall(r"[a-z]+", (s or "").lower().replace("_", " ")) if len(w) > 2 and w not in ("pv", "the", "and"))


def _header_text(columns, lines, page, top, col):
    """The printed header cell above (page, top) naming `col`'s family, or None."""
    fam = columns.FAMILY_OF.get(col)
    if not fam:
        return None
    best = None
    for ln in lines:
        if ln.page != page or ln.top >= top - 1 or re.search(r"\d{3}", ln.text):
            continue
        cells = {}
        for g in columns._groups(ln.words):
            t = " ".join(w.text for w in g)
            c = columns._classify(t, fam)
            if c and c not in cells:
                cells[c] = t
        if len(cells) >= 2 and col in cells and (best is None or ln.top > best[0]):
            best = (ln.top, cells[col])
    return best[1] if best else None


def _column_text(columns, doc, rec, col, fallback=True):
    """The header cell printed above the record's cell (this page, else the nearest earlier page with one)."""
    lines = doc.lines
    page, bbox = rec.get("page"), rec.get("bbox")
    if not page or not bbox:
        return CANONICAL[col] if fallback else None
    t = _header_text(columns, lines, page, bbox[1], col)
    pg = page - 1
    while t is None and pg >= 1:
        t = _header_text(columns, lines, pg, 1e9, col)
        pg -= 1
    return t or (CANONICAL[col] if fallback else None)


def _field_family(field):
    return "cost" if field.endswith("_cost") else "carrying" if field.endswith(("_fair_value", "_carrying")) else None


def live_records(pipeline, rows):
    """Indices of the records icx's Facts keeps (first per key, a revised statement over an original)."""
    from icx.records import Record
    recs = []
    for d in rows:
        e = {k: v for k, v in d.items() if k != "key"}
        if e.get("bbox"):
            e["bbox"] = tuple(e["bbox"])
        recs.append(Record(**e))
    F = pipeline.Facts(recs)
    live = {id(r) for r in F.by_key.values()}
    return recs, [n for n, r in enumerate(recs) if id(r) in live]


def instantiate(pipeline, recs, live_idx):
    """Each check that runs on the correct records -> (check, {key: coef}, const), or (check, None, reason)."""
    F = pipeline.Facts(recs)
    base = [c for c in pipeline.run_checks(F)]
    by_key = {recs[n].to_json()["key"]: n for n in live_idx}
    # Probe every live number against every check that runs: a check can depend on a number it doesn't
    # list among its inputs (icx's fs.assets_sum omits total assets), and the identity must say so.
    runs = [m for m, c in enumerate(base) if c.status != "skip" and c.diff is not None]
    uses = {k: runs for k, n in by_key.items() if _num(recs[n].value)}
    coef = collections.defaultdict(dict)
    bent = set()
    for k, ms in uses.items():
        n = by_key[k]
        seen = []
        for step in (1000.0, -3700.0):
            rs = list(recs)
            r = copy.copy(rs[n])
            r.value = r.value + step
            rs[n] = r
            now = pipeline.run_checks(pipeline.Facts(rs))
            d = {}
            for m in ms:
                g = now[m].diff if m < len(now) and now[m].id == base[m].id and now[m].period == base[m].period else None
                if g is None:
                    bent.add(m)
                    continue
                if abs(g - base[m].diff) > 1e-9:
                    d[m] = (g - base[m].diff) / step
            seen.append(d)
        for m in set(seen[0]) | set(seen[1]):
            a, b = seen[0].get(m), seen[1].get(m)
            if a is None or b is None or abs(a - b) > 1e-6 * max(1.0, abs(a)):
                bent.add(m)
            elif abs(a) > 1e-12:
                coef[m][k] = a
    # A check can respond linearly to each input alone yet not be linear (call % x commitment): move every
    # input at once, by different steps, and compare with the sum of the single-input responses.
    for trial in (0, 1):
        rs = list(recs)
        steps = {}
        for j, k in enumerate(sorted(uses)):
            n = by_key[k]
            r = copy.copy(rs[n])
            steps[k] = (577.0 + 131.0 * j) * (1 if trial == 0 else -0.37)
            r.value = r.value + steps[k]
            rs[n] = r
        now = pipeline.run_checks(pipeline.Facts(rs))
        for m, c in enumerate(base):
            if c.status == "skip" or c.diff is None or m in bent:
                continue
            if m >= len(now) or now[m].id != c.id or now[m].diff is None:
                bent.add(m)
                continue
            want = c.diff + sum(a * steps[k] for k, a in coef.get(m, {}).items())
            if abs(now[m].diff - want) > 1e-6 * max(1.0, abs(want)) + 1e-6:
                bent.add(m)
    out = []
    for m, c in enumerate(base):
        if c.status == "skip" or c.diff is None:
            continue
        if m in bent:
            out.append((c, None, "not linear in its inputs"))
            continue
        terms = coef.get(m, {})
        if not terms:
            out.append((c, None, "no live numeric input"))
            continue
        const = c.diff - sum(a * float(recs[by_key[k]].value) for k, a in terms.items())
        out.append((c, terms, round(const, 6)))
    return out, base


KIND = {"document": "within_doc", "cross_document": "cross_doc", "cross_period": "cross_period"}


def convert(fund_dir: str, out_dir: str) -> dict:
    pipeline, columns, verdict, synth_eval = _v2()
    from icx import pdfio
    rows = [json.loads(l) for l in open(os.path.join(fund_dir, "correct.jsonl")) if l.strip()]
    meta_in = json.load(open(os.path.join(fund_dir, "meta.json")))
    recs, live_idx = live_records(pipeline, rows)
    bundle_pdf = os.path.join(fund_dir, "bundle")
    os.makedirs(os.path.join(out_dir, "docs"), exist_ok=True)
    docs = {}
    for n in live_idx:
        f = rows[n]["file"]
        if f not in docs:
            docs[f] = doc_id(f)
    for f in sorted({d["file"] for d in rows}):
        shutil.copyfile(os.path.join(bundle_pdf, f), os.path.join(out_dir, "docs", doc_id(f) + ".pdf"))
        docs.setdefault(f, doc_id(f))
    loaded = {}

    def pdf(f):
        if f not in loaded:
            loaded[f] = pdfio.load(os.path.join(bundle_pdf, f))
        return loaded[f]

    # printed-sign conventions: stored positive while printed negative
    neg_print = collections.defaultdict(lambda: [0, 0])
    for n in live_idx:
        d = rows[n]
        if _num(d["value"]):
            raw = d.get("raw_value") or ""
            printed_neg = bool(re.search(r"^\s*\$?\(|^\s*-|\)\s*$", raw))
            st = neg_print[(d["doc_type"], d["field"])]
            st[0] += d["value"] < 0
            st[1] += printed_neg and d["value"] > 0
    fields = {}
    truth = []
    for n in live_idx:
        d = rows[n]
        if not _numeric_unit(d.get("unit")) or not _num(d["value"]):
            continue
        col = d.get("column")
        entity, column = None, None
        name = f"{d['doc_type']}.{d['field']}"
        label = d.get("row_label") or d["field"]
        if col in CANONICAL:
            column = _column_text(columns, pdf(d["file"]), d, col)
        elif col:
            entity = col
            fam = _field_family(d["field"])
            if fam:
                column = _column_text(columns, pdf(d["file"]), d, fam)
            if _label_words(label) == _label_words(col):
                label = None                       # the row is found by its entity
            else:
                name = f"{name}[{label}]"         # one schedule line per field
        elif _field_family(d["field"]) and d.get("page") and d.get("bbox"):
            # A total of a Cost / Fair Value table ("Total investments"): the frozen checker places it by the
            # family its field names; here, by the header printed above it on the same page, if any.
            column = _column_text(columns, pdf(d["file"]), d, _field_family(d["field"]), fallback=False)
        units = d.get("unit") or "number"
        if d["field"].startswith("narrative"):
            label = None
            if "round" not in units:
                units = f"{units}_rounded"
        st = neg_print[(d["doc_type"], d["field"])]
        sign = "pos" if (d["field"] in verdict.POSITIVE_FIELDS or (st[1] and not st[0])) else "any"
        f = fields.setdefault(name, dict(label=[], units=units, sign=sign))
        if label and _label_words(label) and all(_label_words(label) != _label_words(x) for x in f["label"]):
            f["label"].append(label)
        truth.append(dict(key=recs[n].to_json()["key"], doc=docs[d["file"]], field=name, column=column, period=d.get("period"),
                          entity=entity, value=d["value"], page=d.get("page"), bbox=d.get("bbox"),
                          source=dict(file=d["file"], icx_field=d["field"], icx_column=col, row_label=d.get("row_label"),
                                     icx_unit=d.get("unit"))))
    keys = {t["key"] for t in truth}
    inst, base = instantiate(pipeline, recs, live_idx)
    idents, dropped, seen = [], [], collections.Counter()
    for c, terms, const in inst:
        cid = f"{c.id}@{c.period}"
        seen[cid] += 1
        if seen[cid] > 1:
            cid = f"{cid}#{seen[cid]}"
        if terms is None:
            dropped.append(dict(id=cid, why=const, inputs=c.inputs, status_on_correct=c.status))
            continue
        terms = {k: a for k, a in terms.items() if k in keys}
        idents.append(dict(id=cid, terms=[[k, a] for k, a in terms.items()], const=const, tol=float(getattr(c, "tol", 1.0)),
                           kind=KIND.get(c.scope, "within_doc")))
    # planted document issues -> the keys of the checks each fails on the correct extraction
    issue_checks = synth_eval._issue_checks(fund_dir, [c.to_json() for c in base])
    issues = []
    for iss, checks in issue_checks:
        ks = sorted({k for _, _, inp in checks for k in inp if k in keys})
        issues.append(dict(keys=ks, kind=iss["kind"], period=iss.get("period"), detail=iss.get("detail"),
                           checks=[f"{a}@{b}" for a, b, _ in checks]))
    for iss in meta_in.get("planted_document_issues", []):
        if iss["kind"] not in synth_eval._ISSUE_CHECKS:
            issues.append(dict(keys=[], kind=iss["kind"], period=iss.get("period"), detail=iss.get("detail"),
                               note="no check covers it (a revised statement supersedes the original)"))
    schema = dict(domain="lp_fund_reporting", fields=fields)
    meta = dict(planted_document_issues=issues, source=os.path.relpath(fund_dir, ROOT), fund=meta_in.get("fund"),
                style=meta_in.get("style"), adapter="qcore.adapters.lp",
                superseded_records_left_out=len(rows) - len(live_idx),
                non_numeric_records_left_out=sum(1 for n in live_idx if not (_numeric_unit(rows[n].get("unit")) and _num(rows[n]["value"]))),
                identities_not_expressible=dropped,
                failing_on_truth=[i["id"] for i in idents if abs(sum(a * next(t["value"] for t in truth if t["key"] == k)
                                                                    for k, a in i["terms"]) + i["const"]) > i["tol"]])
    json.dump(schema, open(os.path.join(out_dir, "schema.json"), "w"), indent=1)
    with open(os.path.join(out_dir, "truth.jsonl"), "w") as fh:
        for t in truth:
            fh.write(json.dumps(t) + "\n")
    json.dump(idents, open(os.path.join(out_dir, "identities.json"), "w"), indent=0)
    json.dump(meta, open(os.path.join(out_dir, "meta.json"), "w"), indent=1)
    return dict(records=len(truth), fields=len(fields), identities=len(idents), not_expressible=len(dropped),
                failing_on_truth=len(meta["failing_on_truth"]), issues=len(issues), docs=len(docs))


def _coerce(v, unit):
    _, _, _, synth_eval = _v2()
    return synth_eval._coerce(v, unit)


def extraction(run_dir: str, fund_dir: str, bundle_dir: str) -> list:
    """A model run (docMM/answer.json, with fields.json beside it) -> qcore extraction over the bundle's keys."""
    truth = [json.loads(l) for l in open(os.path.join(bundle_dir, "truth.jsonl")) if l.strip()]
    ans = {}
    for d in sorted(os.listdir(run_dir)):
        a, f = os.path.join(run_dir, d, "answer.json"), os.path.join(run_dir, d, "fields.json")
        if os.path.exists(a) and os.path.exists(f):
            doc = json.load(open(f))["document"]
            try:
                ans[doc] = {x["key"]: x for x in json.load(open(a))}
            except Exception:
                continue
    out = []
    for t in truth:
        a = ans.get(t["source"]["file"], {}).get(t["key"])
        if a is None:
            continue
        out.append(dict(key=t["key"], value=_coerce(a.get("value"), t["source"].get("icx_unit")),
                        page=a.get("page"), printed_text=a.get("printed_text")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qcore9.adapters.lp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("fund_dir")
    c.add_argument("out_dir")
    e = sub.add_parser("extraction")
    e.add_argument("run_dir")
    e.add_argument("fund_dir")
    e.add_argument("bundle_dir")
    e.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "convert":
        print(json.dumps(convert(a.fund_dir, a.out_dir)))
    else:
        ext = extraction(a.run_dir, a.fund_dir, a.bundle_dir)
        json.dump(ext, open(a.out, "w"), indent=0)
        print(len(ext))


if __name__ == "__main__":
    main()
