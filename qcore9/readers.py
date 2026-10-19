"""Two noisy readers (the extraction model and the OCR of the same page image), reconciled through the identities.

A cell where the two readers agree is never changed. Where they disagree, the candidates are the OCR read, its
OCR-confusion variants (one glyph swapped for a look-alike: 3/8, 1/7, 0/6/8, 5/6, ...; a comma read as a dot) and the
digit-wise mixes of the two reads (each digit taken from one reader). `reconcile` first applies the OCR reads jointly
under the guard (`joint`), then searches the cells still open; it accepts a set of changes only when it is the unique
best explanation:
  - nothing that ties now fails (never break an identity that held);
  - every changed cell lies on an identity that ties after the change and failed (or could not run) before, and the
    change is bigger than that identity's tolerance;
  - no identity that could not run before runs and fails after it;
  - among all such change sets of up to 3 cells in a connected group, it ties the most identities, then changes the
    fewest cells, then uses the most direct reads (the OCR read itself before a variant). A tie on all three is
    ambiguous and goes to tier 2.
"""

from __future__ import annotations

import itertools
import re
from typing import Dict, List, Optional, Set, Tuple

from .identities import Checks, _num

# glyph look-alikes in OCR of degraded scans (both directions are listed where both occur)
CONFUSABLE = {
    "0": "689", "1": "74", "2": "7", "3": "85", "4": "1", "5": "638", "6": "058", "7": "12", "8": "3065", "9": "08",
}
LETTER_DIGIT = {"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "i": "1", "|": "1", "]": "1", "!": "1",
                "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6", "b": "6", "T": "7", "g": "9", "q": "9", "A": "4"}


def _digits(v: float, d: int = 0) -> str:
    return f"{abs(v):.{d}f}".replace(".", "") if d else str(int(round(abs(v))))


def _value(ds: str, d: int, neg: bool) -> Optional[float]:
    if not ds or not ds.isdigit():
        return None
    v = int(ds) / (10 ** d) if d else int(ds)
    return -v if neg else v


def merges(a: float, b: float, d: int = 0) -> Set[float]:
    """Values whose every digit is a or b's digit at that place (same length, same sign), other than a and b."""
    if a is None or b is None or (a < 0) != (b < 0):
        return set()
    x, y = _digits(a, d), _digits(b, d)
    if len(x) != len(y):
        return set()
    diff = [n for n, (p, q) in enumerate(zip(x, y)) if p != q]
    if len(diff) < 2 or len(diff) > 4:
        return set()
    out = set()
    for pick in itertools.product((0, 1), repeat=len(diff)):
        s = list(x)
        for n, c in zip(diff, pick):
            s[n] = y[n] if c else x[n]
        v = _value("".join(s), d, a < 0)
        if v is not None:
            out.add(v)
    return out - {a, b}


def confusions(v: float, d: int = 0) -> Set[float]:
    """One digit of v replaced by a look-alike."""
    x = _digits(v, d)
    out = set()
    for n, c in enumerate(x):
        for r in CONFUSABLE.get(c, ""):
            if n == 0 and r == "0" and len(x) > 1:
                continue
            w = _value(x[:n] + r + x[n + 1:], d, v < 0)
            if w is not None:
                out.add(w)
    return out - {v}


def token_variants(tok: str, parse) -> Set[float]:
    """Readings of an OCR token with look-alike letters read as digits, or a dot before three final digits read as a
    thousands comma ('72.634' -> 72634)."""
    out = set()
    t = tok.strip()
    fixed = "".join(LETTER_DIGIT.get(c, c) if (n and t[n - 1].isdigit()) or (n + 1 < len(t) and t[n + 1].isdigit()) else c
                    for n, c in enumerate(t))
    for cand in {fixed, re.sub(r"[.:;](?=\d{3}(\D|$))", ",", fixed)}:
        try:
            v = parse(cand)
        except Exception:
            v = None
        if v is not None:
            out.add(v)
    return out


def _status(checks: Checks, values: Dict[str, float]) -> Dict[int, str]:
    return {c["n"]: ("none" if c["gap"] is None else "fail" if c["failed"] else "tie") for c in checks.run(values)}


def joint(checks: Checks, values: Dict[str, float], reads: Dict[str, float]) -> Dict[str, float]:
    """The OCR reads applied together (a misaligned block of rows needs every row changed at once), then pruned to a
    fixed point: while an identity that tied fails, the reads feeding the most such identities are dropped; a read that
    lies on no identity that ties now and did not tie before (by a change bigger than its tolerance) is dropped. The reads left are accepted."""
    before = _status(checks, values)
    keep = dict(reads)
    for _ in range(len(reads) + 1):
        now = dict(values)
        now.update(keep)
        st = _status(checks, now)
        broken = [n for n in checks.touching(keep) if before[n] == "tie" and st[n] != "tie"]
        broken += [n for n in checks.touching(keep) if before[n] == "none" and st[n] == "fail"]
        if broken:
            hits: Dict[str, int] = {}
            for n in broken:
                for k in checks.inputs[n]:
                    if k in keep:
                        hits[k] = hits.get(k, 0) + 1
            most = max(hits.values())
            for k in [k for k, m in hits.items() if m == most]:
                del keep[k]
            continue
        unsupported = [k for k in keep if not any(
            st[n] == "tie" and before[n] != "tie"
            and (not _num(values.get(k)) or abs(checks.coef(k, n, now) * (keep[k] - values[k])) > checks.ids[n].tol)
            for n in checks.by_key.get(k, ()))]
        if not unsupported:
            break
        for k in unsupported:
            del keep[k]
    return keep


def reconcile(checks: Checks, values: Dict[str, float], alts: Dict[str, List[float]], max_change: int = 3,
              max_eval: int = 20000) -> Tuple[Dict[str, float], List[List[Dict[str, float]]]]:
    """(accepted {key: new value}, ambiguous groups [[rival change sets]]). `alts` lists each disagreeing cell's
    candidates, most direct first; `values` holds the model's values (a blank cell is absent).
    First the direct reads together (`joint`), then, for the cells still open, the search over single reads, look-alike
    readings and digit mixes described above."""
    first = joint(checks, values, {k: a[0] for k, a in alts.items() if a})
    if first:
        values = dict(values)
        values.update(first)
        alts = {k: a for k, a in alts.items() if k not in first}
    acc, amb = _search(checks, values, alts, max_change, max_eval)
    acc.update(first)
    return acc, amb


def _valid(checks: Checks, values: Dict[str, float], before: Dict[int, str], ch: Dict[str, float]) -> int:
    """Identities newly tied by the change set `ch`, or -1 when it breaks a tie, makes an identity that could not run
    fail, or changes a cell no newly tied identity passes through."""
    now = dict(values)
    now.update(ch)
    gain, support = 0, set()
    for n in checks.touching(ch):
        gp = checks.gap(n, now)
        st = "none" if gp is None else "fail" if abs(gp) > checks.ids[n].tol else "tie"
        if (before[n] == "tie" and st != "tie") or (before[n] == "none" and st == "fail"):
            return -1
        if st == "tie" and before[n] != "tie":
            gain += 1
            # an identity vouches only for a change bigger than its own tolerance (a one-unit OCR slip can move a gap
            # across the rounding slack without fixing anything)
            support.update(k for k in ch if k in checks.inputs[n]
                           and (not _num(values.get(k)) or abs(checks.coef(k, n, now) * (ch[k] - values[k])) > checks.ids[n].tol))
    return gain if gain and support == set(ch) else -1


def _search(checks: Checks, values: Dict[str, float], alts: Dict[str, List[float]], max_change: int,
            max_eval: int) -> Tuple[Dict[str, float], List[List[Dict[str, float]]]]:
    """Per identity that fails (or can't run) and passes through open cells: the change sets over its open inputs that
    make it tie (`_valid`), minimal (no valid subset); a set of two or three cells uses their direct OCR reads only. The one with the fewest cells, then the most direct reads, is
    taken when it is unique; rival sets go to tier 2. Sets taken for different identities must agree on shared cells
    and pass `_valid` together. Repeated until nothing changes."""
    accepted: Dict[str, float] = {}
    ambiguous: List[List[Dict[str, float]]] = []
    seen_amb = set()
    values = dict(values)
    alts = {k: list(a[:8]) for k, a in alts.items() if a}
    for _ in range(10):
        before = _status(checks, values)
        picks: Dict[int, Dict[str, float]] = {}
        for n, st in before.items():
            if st == "tie":
                continue
            open_ = [k for k in checks.inputs[n] if k in alts and k not in accepted]
            if not open_:
                continue
            top = 3 if len(open_) <= 6 else 2 if len(open_) <= 12 else 1
            found: List[tuple] = []
            evals = 0
            for size in range(1, min(top, max_change, len(open_)) + 1):
                for ks in itertools.combinations(open_, size):
                    if any(set(ks) >= set(f[1]) for f in found):
                        continue      # a valid subset exists: not minimal
                    # several cells at once: their direct OCR reads only (look-alikes in two cells can fit any gap)
                    opts = [list(enumerate(alts[k]))[: (len(alts[k]) if size == 1 else 1)] for k in ks]
                    for choice in itertools.product(*opts):
                        evals += 1
                        if evals > max_eval:
                            break
                        ch = {k: v for k, (_, v) in zip(ks, choice)}
                        now = dict(values)
                        now.update(ch)
                        if checks.failed(n, now) or checks.gap(n, now) is None:
                            continue
                        if _valid(checks, values, before, ch) >= 0:
                            found.append(((size, sum(r for r, _ in choice)), tuple(ks), ch))
                    if evals > max_eval:
                        break
            if not found:
                continue
            best = min(f[0] for f in found)
            win = [f[2] for f in found if f[0] == best]
            if len(win) == 1:
                picks[n] = win[0]
            elif n not in seen_amb:
                seen_amb.add(n)
                ambiguous.append(win[:4])
        if not picks:
            break
        merged: Dict[str, float] = {}
        clash = set()
        for ch in picks.values():
            for k, v in ch.items():
                if k in merged and abs(merged[k] - v) > 1e-9:
                    clash.add(k)
                merged[k] = v
        merged = {k: v for k, v in merged.items() if k not in clash}
        if not merged or _valid(checks, values, before, merged) < 0:
            # take the picks one at a time instead
            merged = {}
            for ch in picks.values():
                if set(ch) & clash:
                    continue
                trial = dict(merged)
                trial.update(ch)
                if _valid(checks, values, before, trial) >= 0:
                    merged = trial
        if not merged:
            break
        accepted.update(merged)
        values.update(merged)
    return accepted, ambiguous
