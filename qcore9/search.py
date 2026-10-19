"""The repair search: every small fix built only from numbers the extraction already has.

- swap two values from the same document (wrong row, column or field)
- swap a value with the same line in the adjacent period (wrong period)
- flip a value's sign

A fix counts only if the failing set shrinks strictly: it makes at least one failing identity tie
and makes no identity fail that did not fail before (Fail(x') is a proper subset of Fail(x)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Hashable, List, Optional, Tuple

from .identities import Checks, _num


@dataclass
class Fix:
    kind: str                     # swap | period_swap | sign
    a: Hashable
    b: Optional[Hashable] = None
    fixes: List[str] = field(default_factory=list)   # failing identity ids this makes tie

    @property
    def ident(self) -> Tuple:
        return (self.kind, frozenset((self.a, self.b)) if self.b is not None else self.a)


def candidates(values: Dict[str, float], group: Dict[str, str], checks: Checks,
               periods: Optional[Dict[str, List[str]]] = None, tol: float = 0.5, declared: Optional[Dict[str, str]] = None):
    """(failing ids, every single fix that makes at least one of them tie and breaks nothing). `declared` (qcore5):
    {key: "pos" | "neg"} for fields with a declared sign; a flip away from the declared sign is not a candidate (with a
    product term, flipping a count's sign ties a line as well as flipping its unit price does)."""
    declared = declared or {}
    periods = periods or {}
    base = checks.run(values)
    failing = {c["cid"] for c in base if c["failed"]}
    if not failing:
        return failing, []
    by_group: Dict[str, List[str]] = {}
    for k, g in group.items():
        if _num(values.get(k)):
            by_group.setdefault(g, []).append(k)

    found: Dict[Tuple, Fix] = {}
    seen = set()

    def test(fix: Fix, changed):
        if fix.ident in seen:
            return
        seen.add(fix.ident)
        now = checks.failing_after(values, failing, changed)
        fixed, broke = failing - now, now - failing
        if fixed and not broke:
            fix.fixes = sorted(fixed)
            found[fix.ident] = fix

    for c in base:
        if not c["failed"]:
            continue
        for a in c["inputs"]:
            if not _num(values.get(a)):
                continue
            for b in by_group.get(group.get(a), []):
                if b != a and abs(values[a] - values[b]) > tol:
                    test(Fix("swap", a, b), {a: values[b], b: values[a]})
            for b in periods.get(a, []):
                if _num(values.get(b)) and abs(values[a] - values[b]) > tol:
                    test(Fix("period_swap", a, b), {a: values[b], b: values[a]})
            if abs(values[a]) > tol and not (declared.get(a) == "pos" and values[a] > 0) \
                    and not (declared.get(a) == "neg" and values[a] < 0):
                test(Fix("sign", a), {a: -values[a]})
    return failing, list(found.values())
