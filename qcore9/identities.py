"""Identities over extracted values: sum(coef * value[key]) + sum(coef * value[k1] * value[k2]) + const = 0 within tol.
The product terms (qcore5) state a line's count x unit price = amount with the count itself as a value.

An identity with a blank (missing or null) term is skipped, never failed: a sum over a partial
set of its terms says nothing. Check results use the shape the repair search expects:
{"cid", "failed", "inputs", "gap", "tol"}.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from .bundle import Identity


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Checks:
    def __init__(self, identities: List[Identity]):
        self.ids = list(identities)
        self.cid = [i.id for i in self.ids]
        self.inputs = [list(dict.fromkeys([k for k, _ in i.terms] + [k for p in i.products for k in p[:2]])) for i in self.ids]
        self.by_key: Dict[str, List[int]] = {}
        for n, i in enumerate(self.ids):
            for k in self.inputs[n]:
                self.by_key.setdefault(k, []).append(n)

    def gap(self, n: int, values: Dict[str, float]) -> Optional[float]:
        s = self.ids[n].const
        for k, c in self.ids[n].terms:
            v = values.get(k)
            if not _num(v):
                return None
            s += c * v
        for k1, k2, c in self.ids[n].products:
            a, b = values.get(k1), values.get(k2)
            if not (_num(a) and _num(b)):
                return None
            s += c * a * b
        return round(s, 6)

    def failed(self, n: int, values) -> bool:
        g = self.gap(n, values)
        return g is not None and abs(g) > self.ids[n].tol

    def run(self, values: Dict[str, float]) -> List[dict]:
        out = []
        for n, i in enumerate(self.ids):
            g = self.gap(n, values)
            out.append(dict(cid=i.id, failed=g is not None and abs(g) > i.tol, inputs=self.inputs[n], gap=g, tol=i.tol, n=n))
        return out

    def failing(self, values) -> Set[str]:
        return {self.cid[n] for n in range(len(self.ids)) if self.failed(n, values)}

    def touching(self, keys: Iterable[str]) -> Set[int]:
        out = set()
        for k in keys:
            out.update(self.by_key.get(k, ()))
        return out

    def failing_after(self, values, failing: Set[str], changed: Dict[str, float]) -> Set[str]:
        """Failing ids after applying `changed` to `values` (values itself is left as it was)."""
        old = {k: values.get(k, _MISSING) for k in changed}
        values.update(changed)
        try:
            now = set(failing)
            for n in self.touching(changed):
                if self.failed(n, values):
                    now.add(self.cid[n])
                else:
                    now.discard(self.cid[n])
        finally:
            for k, v in old.items():
                if v is _MISSING:
                    values.pop(k, None)
                else:
                    values[k] = v
        return now

    def coef(self, key: str, n: int, values: Optional[Dict[str, float]] = None) -> float:
        """d gap / d value[key]: the linear coefficient, plus each product term's coefficient times its other factor."""
        a = sum(c for k, c in self.ids[n].terms if k == key)
        for k1, k2, c in self.ids[n].products:
            for x, y in ((k1, k2), (k2, k1)):
                if x == key:
                    v = (values or {}).get(y)
                    if _num(v):
                        a += c * v
        return a

    def linear(self, n: int) -> bool:
        return not self.ids[n].products


_MISSING = object()
