"""Identity-localized flags: which cells of the failing identities can be wrong.

`candidates` (used for the flags) drops only cells shown right: confirmed by the page, or the one unconfirmed input of
an identity that ties over confirmed numbers. `likely` (a hint in the evidence) is the minimum-size explanation below.

For each connected group of failing identities (identities sharing an input), a set S of cells explains the failures
when changing only the cells of S can make every failing identity of the group tie while every identity through S
that ties now over page-confirmed numbers still ties: the linear system over S, solved by least squares, leaves each
such identity within its tolerance, and S hits every failing identity of the group. Cells the page confirms (`pinned`)
are not changed. An identity that ties over unconfirmed numbers clears nothing (two wrong numbers can cancel). The candidates are the union of the minimum-size explaining sets (sizes 1 to 3; Fellegi-Holt error
localization). When no set of up to 3 cells explains a group, or every input is pinned, the group is not narrowed.
"""

from __future__ import annotations

import itertools
from typing import Dict, Iterable, List, Optional, Set

from .identities import Checks, _num


def _groups(checks: Checks, failing: List[int]) -> List[List[int]]:
    out, seen = [], set()
    by_key: Dict[str, List[int]] = {}
    for n in failing:
        for k in checks.inputs[n]:
            by_key.setdefault(k, []).append(n)
    for n in failing:
        if n in seen:
            continue
        comp, todo = [], [n]
        seen.add(n)
        while todo:
            m = todo.pop()
            comp.append(m)
            for k in checks.inputs[m]:
                for j in by_key.get(k, ()):
                    if j not in seen:
                        seen.add(j)
                        todo.append(j)
        out.append(sorted(comp))
    return out


def _explains(checks: Checks, values: Dict[str, float], S: Iterable[str], pinned: Set[str] = frozenset()) -> Optional[Dict[str, float]]:
    """New values for S that make the identities through S tie, or None. The identities that bind are the failing
    ones and those that tie now with every input outside S confirmed by the page (`pinned`): an identity that ties
    over unconfirmed numbers may tie only because two of them are wrong together, so it clears nothing."""
    import numpy as np
    S = list(S)
    rows = []
    for n in sorted(checks.touching(S)):
        gap = checks.gap(n, values)
        if gap is None:
            continue
        if abs(gap) <= checks.ids[n].tol and not all(k in pinned or k in S for k in checks.inputs[n]):
            continue
        rows.append(n)
    A, g, tol = [], [], []
    for n in rows:
        gap = checks.gap(n, values)
        A.append([checks.coef(k, n, values) for k in S])
        g.append(-gap)
        tol.append(checks.ids[n].tol)
    if not A:
        return None
    A, g = np.array(A, dtype=float), np.array(g, dtype=float)
    d, *_ = np.linalg.lstsq(A, g, rcond=None)
    res = A @ d - g
    if any(abs(r) > t for r, t in zip(res, tol)):
        return None
    if any(abs(x) < 1e-9 for x in d):
        return None          # a cell that need not change is not part of a minimal set
    new = {k: values[k] + x for k, x in zip(S, d)}
    # product terms are linear in each factor alone; re-check exactly
    now = dict(values)
    now.update(new)
    if any(checks.failed(n, now) for n in rows):
        return None
    return new


def cleared(checks: Checks, values: Dict[str, float], pinned: Set[str]) -> Set[str]:
    """Cells shown right: confirmed by the page (`pinned`), or the one unconfirmed input of an identity that ties now
    while all its other inputs are confirmed (a wrong number there would make it fail)."""
    out = set(pinned)
    for n in range(len(checks.ids)):
        g = checks.gap(n, values)
        if g is None or abs(g) > checks.ids[n].tol:
            continue
        free = [k for k in checks.inputs[n] if k not in pinned]
        if len(free) == 1 and abs(checks.coef(free[0], n, values)) > 1e-9:
            out.add(free[0])
    return out


def candidates(checks: Checks, values: Dict[str, float], pinned: Set[str], max_size: int = 3,
               max_sets: int = 4000) -> Dict[str, Set[str]]:
    """{failing identity id: candidate cells}: the inputs not shown right (`cleared`). Every input when all are cleared
    (the document disagrees with itself)."""
    ok = cleared(checks, values, pinned)
    out: Dict[str, Set[str]] = {}
    for n in range(len(checks.ids)):
        if checks.failed(n, values):
            c = {k for k in checks.inputs[n] if k not in ok}
            out[checks.cid[n]] = c or set(checks.inputs[n])
    return out


def likely(checks: Checks, values: Dict[str, float], pinned: Set[str], max_size: int = 3,
           max_sets: int = 4000) -> Dict[str, Set[str]]:
    """{failing identity id: the union of the minimum-size explaining sets} (a ranking hint; not used to drop cells,
    since two errors can hide behind a one-cell explanation). A group with no set of up to 3 cells maps to all inputs."""
    failing = [n for n in range(len(checks.ids)) if checks.failed(n, values)]
    out: Dict[str, Set[str]] = {}
    for comp in _groups(checks, failing):
        cells = sorted({k for n in comp for k in checks.inputs[n] if _num(values.get(k))})
        free = [k for k in cells if k not in pinned]
        found: List[tuple] = []
        tried = 0
        for size in range(1, max_size + 1):
            for S in itertools.combinations(free, size):
                tried += 1
                if tried > max_sets:
                    break
                if not all(any(k in S for k in checks.inputs[n]) for n in comp):
                    continue
                if _explains(checks, values, S, pinned) is not None:
                    found.append(S)
            if found or tried > max_sets:
                break
        allin = {k for n in comp for k in checks.inputs[n]}
        cand = {k for S in found for k in S} if found else allin
        for n in comp:
            out[checks.cid[n]] = cand
    return out
