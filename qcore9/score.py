"""Score a tier 1 result against truth.jsonl, in the paper's outcome categories.

A tier 0 error is an extracted value (or blank) that differs from the truth (a blank where the truth
is 0 is not an error). Per error, tier 1's outcome is fixed (set to the true value), closer
(changed toward it), wrong (changed away), tier2 (a tier 2 item names the key) or missed. "Broke"
counts tier 1 changes to numbers the extraction had right. The truth is what the page prints,
so a planted document issue (meta.json) is correct as printed: tier 1 changing it counts as broke
or wrong, and each issue is also reported as routed (a tier 2 item names one of its keys),
changed (tier 1 wrote one of its keys) or silent.
"""

from __future__ import annotations

import collections
from typing import Dict, List

from . import bundle as B
from .identities import _num


def _same(a, b) -> bool:
    return _num(a) and _num(b) and abs(a - b) <= max(0.5, 1e-6 * abs(b))


def _coerce(v, fmt: str = "us"):
    from .core import _coerce as c
    return c(v, fmt)


def score(bundle_dir: str, extraction: List[dict], result: dict) -> dict:
    truth = B.truth(bundle_dir)
    bun = B.load(bundle_dir)
    meta = bun.meta
    ext = {str(a["key"]): _coerce(a.get("value"), bun.number_format) for a in extraction or [] if isinstance(a, dict) and "key" in a}
    by_doc: Dict[str, Dict[str, float]] = collections.defaultdict(dict)
    for k, d in truth.items():
        if _num(d.get("value")):
            by_doc[d["doc"]][k] = d["value"]
    errs = {}
    for k, d in truth.items():
        g = d.get("value")
        if not _num(g):
            continue
        v = ext.get(k)
        if _same(v, g) or (v is None and g == 0):
            continue
        if v is None:
            kind = "missing"
        elif _same(-v, g):
            kind = "wrong_sign"
        elif any(_same(v, x) for kk, x in by_doc[d["doc"]].items() if kk != k):
            kind = "misfiled (number from another field)"
        elif any(_same(v, x["value"]) for kk, x in truth.items() if _num(x.get("value")) and x["field"] == d["field"] and kk != k):
            kind = "misfiled (other period or document)"
        elif any(_same(v, g * s) for s in (100, 0.01, 1000, 0.001, 1e6)):
            kind = "scale"
        else:
            kind = "misread / other"
        errs[k] = dict(kind=kind, model=v, correct=g, doc=d["doc"])
    fixed = {}
    for f in result.get("fixes", []):
        fixed[f["key"]] = f["new"]
    esc = {k for t in result.get("tier2", []) for k in t.get("keys", [])}
    outcome = {}
    for k, e in errs.items():
        t = truth[k]["value"]
        if k in fixed and _same(fixed[k], t):
            outcome[k] = "fixed"
        elif k in fixed and _num(e["model"]) and abs(fixed[k] - t) < abs(e["model"] - t):
            outcome[k] = "closer"
        elif k in fixed:
            outcome[k] = "wrong"
        elif k in esc:
            outcome[k] = "tier2"
        else:
            outcome[k] = "missed"
    broke = [k for k in fixed if k not in errs and k in truth and _num(truth[k].get("value")) and not _same(fixed[k], truth[k]["value"])]
    issues = []
    for iss in meta.get("planted_document_issues", []) or []:
        keys = [k for k in iss.get("keys", []) if k in truth]
        changed = [k for k in keys if k in fixed and not _same(fixed[k], truth[k]["value"])]
        routed = [k for k in keys if k in esc]
        issues.append(dict(kind=iss.get("kind"), keys=keys, outcome="changed" if changed else "routed" if routed else
                           ("no keys" if not keys else "silent"), changed=changed))
    c = collections.Counter(outcome.values())
    totals = dict(numbers=sum(1 for d in truth.values() if _num(d.get("value"))), tier0_errors=len(errs),
                  fixed=c["fixed"], closer=c["closer"], wrong=c["wrong"], tier2=c["tier2"], missed=c["missed"],
                  broke=len(broke), tier2_items=len(result.get("tier2", [])), tier1_fixes=len(fixed),
                  planted_issues=len(issues), planted_issues_changed=sum(i["outcome"] == "changed" for i in issues),
                  planted_issues_routed=sum(i["outcome"] == "routed" for i in issues))
    return dict(totals=totals, by_kind=dict(collections.Counter(e["kind"] for e in errs.values())),
                errors=[dict(key=k, **e, outcome=outcome[k]) for k, e in errs.items()],
                broke=[dict(key=k, new=fixed[k], correct=truth[k]["value"]) for k in broke], planted_issues=issues)
