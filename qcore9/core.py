"""Tier 1: repair an extraction from the page and the identities, and route the rest.

Three parts, in order:
  1. the label read: each record's own printed line (best label match, in its column), applied
     per document jointly under the guard (a read may not break an identity that tied, unless the
     field's one best line in the whole document prints the same number);
  2. the repair search (swap / period swap / sign), accepted only when the failing set shrinks
     strictly, ranked by page confirmation, then by how many failures it explains;
  3. the misread repair (a digit-level variant that closes the failing identities), applied only
     when the field's own line prints it; then routing of everything left to tier 2.
"""

from __future__ import annotations

import collections
import itertools
import os
import re
from dataclasses import dataclass, field as dfield
from typing import Dict, List, Optional

from . import bundle as B
from .identities import Checks, _num
from .page import (Pages, split_words, at_scale, copied_scale, decimals, entity_words, fragment, label_ids, label_words, misread, overlaps, own_line,
                   same)
from .pdftext import amount, parse_amount
from .readers import confusions, merges, reconcile, token_variants
from .search import Fix, candidates


@dataclass
class Finding:
    kind: str                 # misassignment | wrong_value | document | review
    summary: str
    reason_code: Optional[str]
    checks: List[str]
    keys: List[str]
    fix: Optional[dict] = None
    page_confirms: Optional[bool] = None


@dataclass
class Cite:
    value: Optional[float]
    page: Optional[int]
    bbox: Optional[list]
    printed_text: Optional[str] = None


# OCR word confidence above which an OCR read that disagrees with a checked cell is flagged anyway. Off (above 1): on dev
# (haiku_poor, tesseract) 0.9 and 0.97 added 6 and 0 flags and caught no error the identities missed.
OCR_SURE = 1.01


def _strip_tok(t: str) -> str:
    return (t or "").strip().replace("$", "").rstrip(".,;:")


def money(v) -> str:
    if v is None:
        return "blank"
    if abs(v - round(v)) < 1e-9:
        return f"{v:,.0f}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _coerce(v, fmt: str = "us"):
    if v is None or _num(v):
        return v
    try:
        return parse_amount(str(v), fmt)
    except Exception:
        return None


def cite(bun: B.Bundle, pages: Pages, extraction: List[dict]) -> Dict[str, Cite]:
    """The extraction per key, with each answered number placed on its answered page."""
    by = {}
    for a in extraction or []:
        if isinstance(a, dict) and "key" in a:
            by[str(a["key"])] = a
    out = {}
    for r in bun.records:
        a = by.get(r.key)
        if a is None:
            out[r.key] = Cite(None, None, None)
            continue
        v = _coerce(a.get("value"), bun.number_format)
        page = a.get("page")
        try:
            page = int(page) if page is not None else None
        except (TypeError, ValueError):
            page = None
        bb = a.get("bbox")
        if (isinstance(bb, (list, tuple)) and len(bb) == 4 and all(_num(x) for x in bb) and page
                and pages.has_text(r.doc) and _num(v)):
            bbox = [float(x) for x in bb]     # optional: the extractor's own citation box
        else:
            bbox = pages.locate(r.doc, page, v, a.get("printed_text")) if _num(v) else None
        out[r.key] = Cite(v, page, bbox, a.get("printed_text"))
    return out


def diagnose(bun: B.Bundle, pages: Pages, cites: Dict[str, Cite], max_rounds: int = 40, info: Optional[dict] = None) -> List[Finding]:
    recs = {r.key: r for r in bun.records}
    order = [r.key for r in bun.records]
    fld = {k: bun.field_of(recs[k]) for k in order}
    checks = Checks(bun.identities)
    values: Dict[str, float] = {k: cites[k].value for k in order if _num(cites[k].value)}
    group = {k: recs[k].doc for k in order}
    lines_: Dict[tuple, List[tuple]] = {}
    for n, k in enumerate(order):
        r = recs[k]
        lines_.setdefault((r.field, r.column, r.entity), []).append((r.period or "", n, k))
    periods: Dict[str, List[str]] = {}
    for rows in lines_.values():
        rows.sort()
        for n, (_, _, k) in enumerate(rows):
            periods[k] = [rows[m][2] for m in (n - 1, n + 1) if 0 <= m < len(rows)]

    def run(vals):
        return checks.run(vals)

    def failing_now():
        return checks.failing(values)

    def is_money(k):
        return B.is_money(fld[k].units)

    word_cache: Dict[str, List[List[str]]] = {}
    raw_cache: Dict[str, List[str]] = {}

    def word_sets(k):
        """Per label: its words (three letters or more) and its identity tokens (those with a digit, '#'-marked)."""
        if k not in word_cache:
            r, f = recs[k], fld[k]
            ws, raws, seen = [], [], set()
            for lab in f.labels:
                w = label_words(lab) + label_ids(lab)
                if w and tuple(w) not in seen:
                    seen.add(tuple(w))
                    ws.append(w)
                    raws.append(lab)
            if not ws and not getattr(f, "no_label", False):
                src = r.entity if r.entity else r.field
                w = entity_words(src) if r.entity else label_words(src)
                if w:
                    ws.append(w)
                    raws.append(src)
            word_cache[k], raw_cache[k] = ws, raws
        return word_cache[k]

    shared_cache: Dict[str, bool] = {}

    def shared(k):
        """A record with no column and no labels of its own, found by its entity, whose entity row another field of
        the same document also reads (a count, a unit price and an amount on one item row).
        Its line then can't tell which of the numbers is this record's, so the page is not read for it."""
        if not shared_cache:
            sig = collections.defaultdict(set)
            key = lambda j: (recs[j].doc, recs[j].period, recs[j].entity, tuple(map(tuple, word_sets(j))))
            by_entity = lambda j: recs[j].column is None and recs[j].entity and not fld[j].labels
            for j in order:
                if by_entity(j):
                    sig[key(j)].add(recs[j].field)
            for j in order:
                shared_cache[j] = bool(by_entity(j)) and len(sig[key(j)]) > 1
        return shared_cache.get(k, False)

    def owns(k, line):
        sets_ = word_sets(k)
        if pages.q7 and recs[k].entity and len(sets_) > 1:
            # qcore9 (zero-config only): as the label read and rule (b) do, a field with many labels (one per day of a
            # census) owns only the lines of the labels that name the record's entity, when any do: 'Thu 14 May' is not
            # the own line of Sunday's admissions
            ea, ei = split_words(entity_words(recs[k].entity))
            sets_ = [w for w in sets_ if (ea and set(ea) <= set(w)) or (ei and set(ei) <= set(w))] or sets_
        return not shared(k) and any(own_line(w, line) for w in sets_)

    cache: Dict[str, Optional[float]] = {}
    scale_cache: Dict[str, float] = {}
    own_cache: Dict[str, Optional[tuple]] = {}
    row_cache: Dict[str, Optional[tuple]] = {}

    def row_read(k):
        """qcore5 line-level read of an entity row whose numbers belong to several label-less fields (an item's count,
        unit price and amount): the row's numbers are matched to the row's fields one to one so that every identity
        among those fields holds, preferring the match that agrees with the most extracted values. The read is the
        number the one best match gives this field; (value, page, bbox), or None."""
        if k in row_cache:
            return row_cache[k]
        r = recs[k]
        group = [j for j in order if recs[j].doc == r.doc and recs[j].entity == r.entity and recs[j].period == r.period and shared(j)]
        for j in group:
            row_cache[j] = None
        pg = next((cites[j].page for j in group if cites[j].page), None) or 1
        base = lambda e: re.sub(r"\s*#\d+\s*$", "", (e or "").split(" / ")[-1]).lower()
        same_name = sorted({recs[j].entity for j in order if recs[j].doc == r.doc and recs[j].entity and base(recs[j].entity) == base(r.entity)},
                           key=lambda e: [int(x) for x in re.findall(r"#(\d+)", e)])
        try:
            nums = pages.entity_row(r.doc, pg, r.entity, same_name)
        except Exception:
            nums = None
        if not nums or len(group) > 5 or len(nums) > 6:
            return None
        gset = set(group)
        inner = [n for n in range(len(checks.ids)) if checks.inputs[n] and set(checks.inputs[n]) <= gset]
        best, picks = -1, {}
        m = min(len(group), len(nums))
        for fs in itertools.permutations(group, m) if len(nums) < len(group) else [tuple(group)]:
            for perm in itertools.permutations(range(len(nums)), m):
                vals = {j: nums[i][0] for j, i in zip(fs, perm)}
                if any(checks.gap(n, vals) is not None and checks.failed(n, vals) for n in inner):
                    continue
                agree = sum(bool(same(vals[j], cites[j].value)) for j in fs if _num(cites[j].value)) \
                    + sum(1 for n in inner if checks.gap(n, vals) is not None)
                sig = tuple(sorted((j, vals[j]) for j in fs))
                if agree > best:
                    best, picks = agree, {sig: dict(zip(fs, perm))}
                elif agree == best:
                    picks.setdefault(sig, dict(zip(fs, perm)))
        if len(picks) != 1 or best <= 0:
            return None          # no match, or matches that give different numbers
        for j, i in next(iter(picks.values())).items():
            row_cache[j] = nums[i]
        return row_cache[k]

    def printed(k):
        if k not in cache:
            c = cites[k]
            try:
                cache[k] = pages.page_value(recs[k].doc, c.page, c.bbox) if c.page else None
            except Exception:
                cache[k] = None
        return cache[k]

    def stored(k, sc):
        """The page's scale relative to the stored value. A field that declares its own scale (schema "scale": 1000,
        values stored as printed in thousands) is stored at the page's scale, so the page compares one to one."""
        fs = fld[k].scale or 1.0
        if fs == 1.0:
            return sc
        return 1.0 if sc in (1.0, fs) else sc / fs

    def scale_of(k):
        if k not in scale_cache:
            c, sc = cites[k], 1.0
            if c.page and c.bbox and is_money(k):
                try:
                    sc = stored(k, pages.page_scale(recs[k].doc, c.page, c.bbox[1]))
                except Exception:
                    sc = 1.0
            scale_cache[k] = sc
        return scale_cache[k]

    def matches(k, v):
        return at_scale(printed(k), v, scale_of(k))

    def readable(k):
        return pages.has_text(recs[k].doc) and not B.is_rounded(fld[k].units) and bool(word_sets(k)) and not shared(k)

    others_cache: Dict[tuple, list] = {}

    def others(k):
        """The label words of every other row of k's document (another field, or the same field for another entity)."""
        r = recs[k]
        sig = (r.doc, r.field, r.entity)
        if not others_cache:
            for j in order:
                others_cache.setdefault(("doc", recs[j].doc), []).append(j)
        if sig not in others_cache:
            seen = {}
            for j in others_cache[("doc", r.doc)]:
                rj = recs[j]
                if (rj.field, rj.entity) != (r.field, r.entity):
                    for w in word_sets(j):
                        a = frozenset(split_words(w)[0])
                        if a:
                            seen[a] = True
            others_cache[sig] = list(seen)
        return others_cache[sig]

    def spot(k, page):
        r = recs[k]
        try:
            return pages.label_spot(r.doc, page, word_sets(k), r.entity, r.column, is_money(k), raw_cache.get(k), others(k))
        except Exception:
            return None

    def own(k):
        """(number on the field's own line, page, bbox, token, scale), or None."""
        if k not in own_cache:
            res = None
            if shared(k) and pages.has_text(recs[k].doc) and not B.is_rounded(fld[k].units):
                sp = row_read(k)
                if sp is not None:
                    doc = recs[k].doc
                    tok = pages.token_at(doc, sp[1], sp[2]) or ""
                    res = (sp[0], sp[1], sp[2], tok, stored(k, pages.page_scale(doc, sp[1], sp[2][1])) if is_money(k) else 1.0)
            elif readable(k):
                sp = spot(k, cites[k].page)
                if sp is not None:
                    doc = recs[k].doc
                    tok = pages.token_at(doc, sp[1], sp[2]) or ""
                    res = (sp[0], sp[1], sp[2], tok, stored(k, pages.page_scale(doc, sp[1], sp[2][1])) if is_money(k) else 1.0)
            own_cache[k] = res
        return own_cache[k]

    def own_says(k, v):
        o = own(k)
        return None if o is None else at_scale(o[0], v, o[4])

    def sign_shown(k):
        o = own(k)
        if o is None or o[0] == 0 or fld[k].sign != "any":
            return 0
        if o[0] < 0:
            return -1
        return 1 if pages.prints_negatives(recs[k].doc) else 0

    def label(k):
        r = recs[k]
        bits = [r.field] + [x for x in (r.entity, r.column, r.period) if x]
        return f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]

    def confirms(fx):
        """The cited spots print the repaired numbers, or the fields' own lines do (a citation placed by the wrong
        value points at the wrong spot)."""
        if fx.kind == "sign":
            p, sc = printed(fx.a), scale_of(fx.a)
            if fld[fx.a].sign in ("pos", "neg") and (values[fx.a] > 0) == (fld[fx.a].sign == "pos"):
                return False      # never confirmed away from the field's declared sign
            if p is not None and abs(p) > 0.5 and abs(p * sc + values[fx.a]) <= 0.5 * sc \
                    and (p < 0 or pages.prints_negatives(recs[fx.a].doc)):
                return True       # a plain number shows a positive sign only where the document marks negatives
            sh = sign_shown(fx.a)
            return bool(sh) and bool(own_says(fx.a, values[fx.a])) and (sh < 0) == (values[fx.a] > 0)
        if abs(abs(values[fx.a]) - abs(values[fx.b])) <= 0.5:
            return False          # equal digits, opposite signs: the page's digits can't tell a swap from two sign errors
        if matches(fx.a, values[fx.b]) and matches(fx.b, values[fx.a]):
            return True
        return bool(own_says(fx.a, values[fx.b]) and own_says(fx.b, values[fx.a]))

    def reason(fx):
        if fx.kind == "sign":
            return "wrong_sign"
        ra, rb = recs[fx.a], recs[fx.b]
        if fx.kind == "period_swap" or ra.period != rb.period:
            return "wrong_period"
        return "wrong_column" if ra.field == rb.field else "wrong_field"

    def describe(fx):
        if fx.kind == "sign":
            return f"{label(fx.a)} has the wrong sign ({money(values[fx.a])})"
        return f"{label(fx.a)} ({money(values[fx.a])}) and {label(fx.b)} ({money(values[fx.b])}) are swapped"

    findings: List[Finding] = []
    reviewed: set = set()

    def facts_of(f):
        return {f.a} | ({f.b} if f.b is not None else set())

    def apply(fx):
        ok = confirms(fx)
        n = len(fx.fixes)
        summary = (f"{describe(fx)}. Fixing that makes {n} failing identit{'y' if n == 1 else 'ies'} tie and breaks nothing else."
                   + (" The page confirms it." if ok else ""))
        findings.append(Finding("misassignment", summary, reason(fx), fx.fixes,
                                [k for k in sorted(facts_of(fx), key=lambda k: k != fx.a)],
                                dict(kind=fx.kind, a=fx.a, b=fx.b, a_value=values[fx.a], b_value=values.get(fx.b) if fx.b is not None else None), ok))
        if fx.kind == "sign":
            values[fx.a] = -values[fx.a]
        else:
            values[fx.a], values[fx.b] = values[fx.b], values[fx.a]

    if info is not None:
        # the page confirms a value: its own line prints it
        # (the cited spot alone is not evidence: a citation is placed where the answered number is printed)
        info["page_confirms"] = lambda k, v: bool(own_says(k, v))
        info["own"] = own

    ocr_alts: Dict[str, List[float]] = {}

    def ocr_candidates(k, v, new, sc):
        """The OCR read, its look-alike readings and its digit-wise mixes with the model's value, most direct first;
        in stored units, with the field's declared sign, the model's value left out."""
        o = own(k)
        tok, p = o[3], new / sc
        d = decimals(v / sc) if v is not None else decimals(p)
        out = [p]
        out += sorted(token_variants(_strip_tok(tok), lambda t: amount(t, pages.fmt)) - {p}, key=lambda x: abs(x - p))
        out += sorted(confusions(p, d), key=lambda x: abs(x - p))
        if v is not None:
            out += sorted(merges(v / sc, p, d), key=lambda x: abs(x - p))
            if (v < 0) != (p < 0):
                out += [-x for x in list(out)]
        res = []
        for x in out:
            x = abs(x) if fld[k].sign == "pos" else -abs(x) if fld[k].sign == "neg" else x
            x = x * sc
            if (v is None or not same(x, v) or (x < 0) != (v < 0)) and all(abs(x - y) > 1e-9 for y in res) and (v is None or x != v):
                res.append(x)
        return res[:12]

    if pages.ocr or pages.mode == "text":
        # an OCR'd unit line ('In millions') is often unreadable: the scale is the one under which the OCR's own-line
        # numbers and the model's values agree most often in the document (at least 3 cells, twice the runner-up).
        votes: Dict[str, collections.Counter] = {}
        for k in order:
            if not is_money(k) or not readable(k) or not _num(values.get(k)) or values[k] == 0:
                continue
            if not pages.ocr:
                continue          # text mode: no own-line vote (see below)
            sp = spot(k, cites[k].page)
            if sp is None or not sp[0]:
                continue
            for s_ in (1.0, 1e3, 1e6):
                sc_ = stored(k, s_)
                if abs(abs(sp[0]) * sc_ - abs(values[k])) <= 0.5 * sc_:
                    votes.setdefault(recs[k].doc, collections.Counter())[s_] += 1
        if not pages.ocr:
            # text mode, a document with no unit statement (a table cut from a filing whose 'in millions' sits elsewhere): the
            # scale under which the model's money values are printed somewhere in the document most often. It is not a
            # declared scale, so it only lets a number count as printed; reads and changes stay at the page's own scale
            for doc in sorted({recs[k].doc for k in order}):
                if pages._scales(doc) or not pages.has_text(doc):
                    continue
                cnt2 = collections.Counter()
                for k in order:
                    if recs[k].doc != doc or not is_money(k) or not _num(values.get(k)) or B.is_rounded(fld[k].units):
                        continue
                    for s_ in (1.0, 1e3, 1e6, 1e9):
                        x_ = values[k] / stored(k, s_)
                        if abs(x_) >= 10 and abs(x_ * 100 - round(x_ * 100)) < 1e-6 and pages.find_value(doc, x_):
                            cnt2[s_] += 1
                top2 = cnt2.most_common(2)
                if top2 and top2[0][0] != 1.0 and top2[0][1] >= 3 and (len(top2) == 1 or top2[0][1] >= 2 * top2[1][1]):
                    # used only to find where a number is printed ('printed nowhere'); never to read or change a value
                    pages.print_scale[doc] = top2[0][0]
        for doc, cnt in votes.items():
            top_ = cnt.most_common(2)
            if top_[0][1] >= 3 and (len(top_) == 1 or top_[0][1] >= 2 * top_[1][1]):
                pages.doc_scale[doc] = top_[0][0]

    if info is not None and info.get("reads_only"):
        return []             # qcore6: the page reads only (own(k)), for identity discovery and the re-read tier

    # qcore6: re-reads of single cells by a third reader (a cheap model shown a crop of the cell), {key: printed number or
    # None}. A re-read that agrees with the page read (text layer or OCR) of the same cell, against the extraction, makes
    # that read a candidate change under the same gate as a text-layer read; a re-read that agrees with the extraction
    # keeps the OCR from changing the cell.
    rr_num: Dict[str, Optional[float]] = {}
    rr_wrong_row: set = set()
    for k_, a_ in ((info or {}).get("rereads") or {}).items():
        if k_ in recs:
            t_ = (a_.get("printed_text") or "").strip().replace("$", "").replace(" ", "")
            rr_num[k_] = amount(t_, pages.fmt) if t_ and re.search(r"\d", t_) else None
            rl_ = a_.get("row_label")
            if rl_ not in (None, "", "null") and rr_num[k_] is not None:
                # the re-reader names the row it read: a row that is not this record's (the crop was placed on another
                # line) makes the re-read no evidence for this cell
                sets_ = list(word_sets(k_)) + ([entity_words(recs[k_].entity)] if recs[k_].entity else [])
                seen_ = set(re.findall(r"[a-z]+", str(rl_).lower()))
                need_ = [{x for x in w_ if re.fullmatch(r"[a-z]+", x)} for w_ in sets_]
                if any(need_) and not any(n_ and n_ <= seen_ for n_ in need_):     # every word of the row's label
                    rr_num[k_] = None
                    rr_wrong_row.add(k_)
            cl_ = a_.get("column_label")
            if cl_ not in (None, "", "null") and rr_num[k_] is not None and recs[k_].column:
                # a column heading with another year than the record's column (2025 read for 2026): another column's cell
                ys_, yc_ = set(re.findall(r"(?:19|20)\d\d", str(cl_))), set(re.findall(r"(?:19|20)\d\d", str(recs[k_].column)))
                if ys_ and yc_ and not (ys_ & yc_):
                    rr_num[k_] = None
                    rr_wrong_row.add(k_)
    rr_ok: set = set()          # the re-read agrees with the page read of the cell, which differs from the extraction
    rr_model: set = set()       # the re-read agrees with the extraction
    if info is not None:
        info["rr_num"] = rr_num
        info["rr_wrong_row"] = rr_wrong_row
        info["rr_ok"] = rr_ok
        info["rr_model"] = rr_model

    ext_by_doc: Dict[str, set] = {}
    for j in order:
        if _num(cites[j].value):
            ext_by_doc.setdefault(recs[j].doc, set()).add(round(cites[j].value, 6))

    def rr_agrees_page(k, o):
        x = rr_num.get(k)
        if x is None or o is None or abs(x - o[0]) > 1e-9 * max(1.0, abs(o[0])):
            return False
        if pages.ocr and x != 0 and round(o[0] * o[4], 6) in ext_by_doc.get(recs[k].doc, set()):
            return False      # the OCR (and the re-read of its spot) may sit on another cell the extraction placed elsewhere
        return True

    def cited_sign_shown(k, v, o):
        """qcore9: the extraction's cited spot is another cell than the own-line read `o` and prints v's digits with v's
        own sign, in a document that marks negatives (so a plain number there reads positive)."""
        c = cites[k]
        if not c.page or not c.bbox or not _num(v) or not pages.prints_negatives(recs[k].doc):
            return False
        if c.page == o[1] and o[2] and abs(c.bbox[0] - o[2][0]) < 1.0 and abs(c.bbox[1] - o[2][1]) < 1.0:
            return False
        p = printed(k)
        return _num(p) and p != 0 and bool(at_scale(p, v, scale_of(k))) and (p < 0) == (v < 0)

    def rr_agrees_value(k, v, sc):
        x = rr_num.get(k)
        return x is not None and v is not None and bool(at_scale(x, v, sc))

    # ---- 1. the label read --------------------------------------------------------------------
    proposals: Dict[str, List[tuple]] = {}
    held: Dict[str, Dict[str, List[str]]] = {}

    def hold(why, k):
        held.setdefault(why, {}).setdefault(recs[k].doc, []).append(k)

    for k in order:
        r, v = recs[k], values.get(k)
        missing = cites[k].value is None
        if (v is None and not missing) or not (readable(k) or (shared(k) and own(k) is not None)):
            continue
        o = own(k)
        if o is None:
            continue
        p, sc = o[0], o[4]
        if sc != 1:
            if v is not None and copied_scale(p, v, sc):
                hold("scale", k)
                continue
            if v is not None and abs(v / sc - round(v / sc)) > 1e-9:
                # the value carries more precision than the page: the page can confirm it but not replace it
                if not at_scale(p, v, sc) and not fragment(p, v):
                    hold("scale_value", k)
                continue
            # the value is stated at the page's precision (a whole number of the page's unit), or blank: the page's
            # number times its unit replaces it like any other read
            p = p * sc
        if v is not None and same(p, v):
            sh = sign_shown(k)
            if abs(v) > 0.5 and sh and (v < 0) != (sh < 0):
                if pages.ocr and rr_agrees_page(k, o) and not rr_agrees_value(k, v, sc):
                    rr_ok.add(k)
                    proposals.setdefault(r.doc, []).append((k, v, -v, "sign"))
                elif pages.ocr and rr_agrees_value(k, v, sc):
                    rr_model.add(k)
                    ocr_alts[k] = [-v]      # a sign is a glyph (a dash): the re-read may share the extractor's misread
                elif pages.ocr:
                    ocr_alts[k] = [-v]
                elif pages.q7 and cited_sign_shown(k, v, o):
                    # qcore9 (zero-config only): the document prints this number twice with opposite signs (a check
                    # amount '(87.50)' in the transaction list and '87.50' in the checks-paid list), and the
                    # extraction's own citation is the copy printed with the extracted sign. The page does not say which
                    # copy is the record's, so the label read is no evidence of a sign error (an identity may still
                    # flip it, repair search)
                    pass
                else:
                    proposals.setdefault(r.doc, []).append((k, v, -v, "sign"))
            elif abs(v) > 0.5 and not sh and v < 0 < p and fld[k].sign == "any" and is_money(k):
                hold("sign_unclear", k)
            continue
        if fragment(p, v):
            continue
        new = abs(p) if fld[k].sign == "pos" else -abs(p) if fld[k].sign == "neg" else p
        if k in rr_num:
            if rr_agrees_page(k, o) and not rr_agrees_value(k, v, sc):
                rr_ok.add(k)
                proposals.setdefault(r.doc, []).append((k, v, new, "value"))
                continue
            if rr_agrees_value(k, v, sc):
                rr_model.add(k)
                if pages.ocr and not (v is not None and _near(p, v)):
                    continue      # the extraction and the re-read agree, the OCR read another number: it does not change the cell
                # a glyph or two apart: the re-reader may share the extractor's misread; the identities pick, as without it
        if pages.ocr:
            ocr_alts[k] = ocr_candidates(k, v, new, sc)
            continue
        proposals.setdefault(r.doc, []).append((k, v, new, "value"))

    if info is not None:
        info["ocr_alts"] = ocr_alts
    if pages.ocr and ocr_alts:
        # two readers: the model's value and the OCR read disagree on these cells; the identities pick (readers.reconcile)
        before_ = failing_now()
        took, rivals_ = reconcile(checks, values, ocr_alts)
        for k, new in took.items():
            values[k] = new
        tied_now = failing_now()
        for k, new in took.items():
            v, o = cites[k].value, own(k)
            ids_ = sorted(c for c in before_ - tied_now if k in checks.inputs[checks.cid.index(c)]) if before_ else []
            n_ = len([c for c in run(values) if k in c["inputs"] and not c["failed"] and c["gap"] is not None])
            how = "its OCR read" if at_scale(o[0], new, o[4]) else "a look-alike reading of its OCR read" \
                if v is None or not at_scale(o[0], v, o[4]) else "its OCR read"
            if v is not None and abs(abs(v) - abs(new)) <= 0.5 and v != new:
                findings.append(Finding("misassignment", f"{label(k)} has the wrong sign ({money(v)}): the OCR of its own line prints it "
                                        f"{'negative' if v > 0 else 'positive'} ({o[3]}), and with that sign {n_} identit{'y' if n_ == 1 else 'ies'} through it tie.",
                                        "wrong_sign", ids_, [k], dict(kind="sign", a=k, b=None, a_value=v, b_value=None, found_by="two_readers",
                                                                      page=o[1], bbox=list(o[2])), True))
                continue
            findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(v)}; the OCR of its own line reads {o[3]!r}. "
                                    f"{money(new)} ({how}) is the one reading that makes {n_} identit{'y' if n_ == 1 else 'ies'} through it tie "
                                    "and breaks nothing.", "misread", ids_, [k],
                                    dict(kind="value", a=k, a_value=v, b_value=new, found_by="two_readers", page=o[1], bbox=list(o[2])), True))
        for win in rivals_:
            ks = sorted({k for ch in win for k in ch})
            alts_ = "; ".join(", ".join(f"{label(k)} as {money(x)}" for k, x in ch.items()) for ch in win)
            cids_ = sorted({c["cid"] for c in run(values) if c["failed"] and set(c["inputs"]) & set(ks)})
            findings.append(Finding("review", f"The model's and the OCR's readings disagree here, and several readings would each make "
                                    f"the identities tie, so a person should pick: {alts_}.", None, cids_, ks))
            reviewed |= set(cids_)
        # disagreements the identities can't settle: an identity that ties with the model's numbers and also with the OCR's
        # (two or more cells read differently whose differences cancel)
        open_ = {k for k in ocr_alts if k not in took and not any(k in ch for w in rivals_ for ch in w)}
        both_ways = set()
        for c in run(values):
            d_ = [k for k in c["inputs"] if k in open_]
            if c["gap"] is None or c["failed"] or len(d_) < 2:
                continue
            trial = dict(values)
            trial.update({k: ocr_alts[k][0] for k in d_})
            g_ = checks.gap(c["n"], trial)
            if g_ is not None and abs(g_) <= checks.ids[c["n"]].tol:
                findings.append(Finding("review", f"The model and the OCR read {len(d_)} numbers here differently "
                                        f"({'; '.join(f'{label(k)}: {money(values.get(k))} vs {money(ocr_alts[k][0])}' for k in d_[:4])}), "
                                        "and the identity ties either way, so it can't tell which reader is right.",
                                        "readers_disagree", [c["cid"]], d_))
                both_ways |= set(d_)
        # placement conflicts: the OCR prints this cell's number on another row of the document, or prints another row's
        # number on this cell's row. Moving numbers between rows of one sum keeps it tying, so the identities can't
        # settle where they belong.
        model_by_doc: Dict[str, List[tuple]] = {}
        for j in order:
            if _num(values.get(j)):
                model_by_doc.setdefault(recs[j].doc, []).append((j, values[j]))
        ocr_by_doc: Dict[str, List[tuple]] = {}
        for j in open_:
            ocr_by_doc.setdefault(recs[j].doc, []).append((j, ocr_alts[j][0]))
        placed = []
        for k in sorted(open_ - both_ways):
            if not _num(values.get(k)):
                continue
            doc = recs[k].doc
            moved = any(j != k and abs(abs(x) - abs(ocr_alts[k][0])) <= 0.5 for j, x in model_by_doc.get(doc, ())) or \
                any(j != k and abs(abs(x) - abs(values[k])) <= 0.5 for j, x in ocr_by_doc.get(doc, ()))
            if moved:
                placed.append(k)
        for doc in sorted({recs[k].doc for k in placed}):
            ks = [k for k in placed if recs[k].doc == doc]
            cids_ = sorted({c["cid"] for c in run(values) if set(c["inputs"]) & set(ks)})
            findings.append(Finding("review", f"The model and the OCR place {len(ks)} number(s) on different rows "
                                    f"({'; '.join(f'{label(k)}: model {money(values[k])}, OCR {money(ocr_alts[k][0])}' for k in ks[:4])}"
                                    f"{' ...' if len(ks) > 4 else ''}); the identities can't tell which placement is right.",
                                    "readers_disagree", cids_, ks))
            both_ways |= set(ks)
        # disagreements nothing settles: a cell no identity can check, or one whose identities tie only with the model's
        # number while the OCR reads its own line with high confidence (models make numbers add up)
        checked = {k for c in run(values) if c["gap"] is not None for k in c["inputs"]}
        for k in ocr_alts:
            if k in took or k in both_ways or any(k in ch for w in rivals_ for ch in w) or cites[k].value is None:
                continue
            o = own(k)
            w_ = pages.word_at(recs[k].doc, o[1], o[2])
            if k in checked and (w_ is None or w_.conf < OCR_SURE):
                continue
            findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(cites[k].value)}, but the OCR of its own line reads "
                                    f"{o[3]!r} and no identity can check either reading. Not changed.", "misread", [], [k],
                                    dict(kind="value", a=k, a_value=cites[k].value, b_value=ocr_alts[k][0], found_by="ocr_disagrees"), None))

    def whole_doc_backs(k, new):
        sp = spot(k, None)
        if sp is not None and is_money(k):
            sc = stored(k, pages.page_scale(recs[k].doc, sp[1], sp[2][1]))
            sp = (sp[0] * sc,) + tuple(sp[1:])
        return sp is not None and abs(sp[0] - new) <= 0.5 if abs(new) >= 1 else sp is not None and abs(sp[0] - new) < 1e-6

    def unset(k, v):
        if v is None:
            values.pop(k, None)
        else:
            values[k] = v

    withheld: List[tuple] = []
    conflicts: List[tuple] = []
    unverified: List[tuple] = []
    cancel: Dict[str, set] = {}
    audit: List[dict] = []
    if info is not None:
        info["label_reads"] = audit

    def jump(v, new, k=None):
        """qcore5: the read changes the value's magnitude by a power of 1000 (a unit scale dropped or added): exactly, or
        with a misread digit besides (829,000,000 -> 329). In a document that states no unit but whose numbers the
        extraction stores at a voted scale (Pages.print_scale), any change of more than ~2.5 orders of magnitude counts:
        the page is read at scale 1 there, so a read can't be trusted to carry the unit."""
        if not _num(v) or not _num(new) or abs(v) < 1e-9 or abs(new) < 1e-9:
            return False
        r = abs(new) / abs(v)
        pw = (1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9)
        if any(abs(r / f - 1) <= 0.005 for f in pw) or any(misread(v, new * f) for f in pw):
            return True
        if k is not None and not pages._scales(recs[k].doc) and pages.print_scale.get(recs[k].doc, 1.0) != 1.0:
            return r >= 300 or r <= 1 / 300
        return False

    def effect(changes):
        """(identities through the changed cells that tie after and did not before, by a change larger than their
        tolerance; identities that tied, or could not run, and fail after) for `changes` applied to the current values."""
        ns = checks.touching(changes)
        pre = {n: checks.gap(n, values) for n in ns}
        old = {k: values.get(k) for k in changes}
        values.update(changes)
        post = {n: checks.gap(n, values) for n in ns}
        for k, v in old.items():
            unset(k, v)
        tie = lambda n, g: g is not None and abs(g) <= checks.ids[n].tol
        newly = {n for n in ns if tie(n, post[n]) and not tie(n, pre[n])
                 and (pre[n] is None or abs(pre[n] - post[n]) > checks.ids[n].tol)
                 and (pre[n] is not None or any(abs(checks.coef(k, n, values) * x) > checks.ids[n].tol for k, x in changes.items()))}
        broken = {n for n in ns if post[n] is not None and not tie(n, post[n]) and (pre[n] is None or tie(n, pre[n]))}
        return newly, broken

    def agreement(k):
        """qcore5: how the page read and the extraction agree on the other cells of k's column and of k's row in its
        document: (column agree, column disagree, row agree, row disagree). A column whose cells all disagree with the
        extraction more likely means the page read sits in the wrong column (a stacked or multi-block table) than that
        the extraction is wrong throughout."""
        r = recs[k]
        ca = cd = ra = rd = 0
        for j in order:
            if j == k or recs[j].doc != r.doc or not _num(cites[j].value):
                continue
            col, row = recs[j].column == r.column, (recs[j].field, recs[j].entity) == (r.field, r.entity)
            if not (col or row):
                continue
            s_ = own_says(j, cites[j].value)
            if s_ is None:
                continue
            if not s_:
                # a read that is a digit misread of the extracted number still places the cell right (the extraction
                # misread it); a column shift reads other numbers altogether
                oj = own(j)
                s_ = bool(misread(cites[j].value, oj[0] * oj[4]) or misread(oj[0] * oj[4], cites[j].value))
            if col:
                ca, cd = ca + bool(s_), cd + (not s_)
            if row:
                ra, rd = ra + bool(s_), rd + (not s_)
        return ca, cd, ra, rd

    def completion(ns, fixed_keys):
        """{key: value}: for the identities n in `ns` that have exactly one input outside `fixed_keys` whose page cell, or a
        number printed on its own line (on its cited page, or a digit misread of its value), makes n tie, that input and
        number -- each kept only when it breaks no identity that ties; None when there is none."""
        out: Dict[str, float] = {}
        for n in ns:
            g = checks.gap(n, {**values, **out})
            if g is None or abs(g) <= checks.ids[n].tol:
                continue
            opts = []
            for j in checks.inputs[n]:
                if j in fixed_keys or j in out or not _num(values.get(j)):
                    continue
                a = checks.coef(j, n, values)
                if not a:
                    continue
                d = decimals(values[j])
                x = values[j] - g / a
                x = round(x, d) if d else float(round(x))
                if x == values[j] or jump(values[j], x, j):
                    continue
                ok_ = bool(own_says(j, x))
                if not ok_ and own_says(j, values[j]) is not True and not pages.ocr:
                    ok_ = any(owns(j, h[2]) and (misread(values[j], x) or h[0] == cites[j].page)
                              for h in pages.find_value(recs[j].doc, x, cites[j].page, word_sets(j)[0] if word_sets(j) else []))
                if ok_:
                    opts.append((j, x))
            if len(opts) != 1:
                continue
            j, x = opts[0]
            trial = {**values, **out, j: x}
            if any(checks.failed(m, trial) for m in checks.touching([j]) if m != n and not checks.failed(m, {**values, **out})):
                continue
            out[j] = x
        return out or None

    def strict(k, v, new, kind):
        """qcore5 rule (b): the page read of this exact cell is unambiguous and corroborated. Returns (ok, why)."""
        if pages.mode == "ocr" and k in rr_ok:
            # qcore6: an OCR read that only a re-read backs is not enough on its own. Real re-reads of poor scans make the
            # same glyph confusions as the OCR (6/5, 0/8, 2/0, 1/7): on the dev SEC scans, 11 extracted numbers were
            # replaced by a misread that OCR and re-read shared. Such a read changes a value only when an identity through
            # the cell then ties (rule a); otherwise it goes to tier 2 with every read
            return False, "reread_needs_identity"
        if pages.mode == "text" and pages.scanned(recs[k].doc) and not (
                kind != "sign" and _num(v) and _num(new) and (misread(v, new) or misread(new, v))):
            # qcore6: a scan's text layer is an OCR of the image, a second reader: only a read tied to this very cell by
            # its digits (a digit misread) corroborates; a sign (an OCR'd dash, or a convention the extraction does not
            # store), a blank fill, the extracted number seen elsewhere or printed nowhere, or a whole-row read, do not
            # (the OCR layer may have put another column's number on the line); those need an identity (rule a)
            return False, "scan_text_layer"
        if getattr(fld[k], "weak", False):
            # qcore7: a zero-config label that no extracted number confirmed: its read needs an identity (rule a).
            # qcore8: no digit-misread exception either. A weak label can fit the wrong one of two tables that print the
            # same row label (set E: a pension table's label wrapped over two lines, the other table's on one line), and
            # a one-glyph difference (4 -> 9) is then a coincidence, not corroboration
            return False, "weak_label"
        if shared(k):
            return (own(k) is not None, "entity_row_read")       # a row read, unique by values under the line identities
        r = recs[k]
        res = None
        sets_ = list(zip(word_sets(k), raw_cache.get(k) or [None] * len(word_sets(k))))
        ent_ = entity_words(r.entity)
        if ent_:
            # as the label read: with an entity, only the labels that name the entity are read, when any do
            ea, ei = split_words(ent_)
            naming = [x for x in sets_ if (ea and set(ea) <= set(x[0])) or (ei and set(ei) <= set(x[0]))]
            sets_ = naming or sets_
        for words, raw in sets_:
            try:
                c_ = pages.strict_cell(r.doc, words, r.entity, r.column, is_money(k), raw, tuple(others(k)))
            except Exception:
                c_ = dict(ok=False, why="error")
            if c_.get("ok"):
                if res is not None:
                    return False, "several_labels_read"
                res = c_
        if res is None:
            return False, "cell_ambiguous"
        o = own(k)
        if o is None or tuple(res["cell"][2]) != tuple(o[2]):
            return False, "cell_ambiguous"
        # the row's other records (other columns) must be placed on other cells of the row: two records placed on one
        # cell mean the header can't separate the columns (stacked blocks, a header line misread as a cell name)
        for j in order:
            if j == k or recs[j].doc != r.doc or (recs[j].field, recs[j].entity) != (r.field, r.entity) or recs[j].column == r.column:
                continue
            for words, raw in zip(word_sets(j), raw_cache.get(j) or [None] * len(word_sets(j))):
                try:
                    cj = pages.strict_cell(r.doc, words, r.entity, recs[j].column, is_money(j), raw, tuple(others(j)))
                except Exception:
                    cj = {}
                if cj.get("ok") and tuple(cj["cell"][2]) == tuple(o[2]):
                    return False, "two_columns_one_cell"
        # the read equals the extracted number of another column of this row: the read may sit in that column (a header
        # the reader placed wrong), or the two cells are swapped, which the identity search handles
        for j in order:
            if j == k or recs[j].doc != r.doc or (recs[j].field, recs[j].entity) != (r.field, r.entity) or recs[j].column == r.column:
                continue
            xj = cites[j].value
            if _num(xj) and abs(xj) > 0.5 and at_scale(o[0], xj, o[4]) and not (v is not None and at_scale(o[0], v, o[4])):
                oj = own(j)
                if oj is not None and (oj[1], tuple(oj[2])) != (o[1], tuple(o[2])) and (
                        at_scale(oj[0], xj, oj[4]) or (_num(v) and at_scale(oj[0], v, oj[4]))):
                    if pages.q7 and _num(v) and at_scale(oj[0], v, oj[4]) and not at_scale(oj[0], xj, oj[4]):
                        # qcore7: both cells read as the other's extracted number. A model that swapped two columns and a
                        # header the reader placed the wrong way round look the same on the page: the page alone does
                        # not decide; an identity must (rule a)
                        return False, "columns_swapped_or_misplaced"
                    continue      # that column reads on its own cell: its own number there, or k's (the two are swapped)
                return False, "read_is_another_columns_value"
        ca, cd, ra, rd = agreement(k)
        both = bool(res.get("both"))
        row_ok = ra >= 1 and rd == 0
        if r.column and ca < cd and not (both or row_ok) and not (kind == "sign" or (v is not None and (misread(v, new) or misread(new, v)))):
            return False, "column_disagrees"
        # a column read is checked against the extraction: another cell of its column agrees, or the row's other columns all
        # agree with their reads; or both header readings place it. Otherwise only a read whose digits tie it to this very
        # cell (a sign, or a digit misread) is taken
        col_ok = not r.column or ca >= 1 or row_ok or both
        if kind == "sign":
            return True, "unambiguous"
        sc = o[4]
        if v is not None and (misread(v, new) or misread(new, v)):
            return True, "digit_misread"
        if not col_ok:
            return False, "column_unchecked"
        if v is None:
            return True, "unambiguous"
        on_row = [x for x in res["nums"] if tuple(x[2]) != tuple(o[2]) and at_scale(x[0], v, sc)]
        if on_row:
            return True, "extracted_value_elsewhere_on_row"
        hits_ = [h for h in pages.find_value(r.doc, v) if not (h[0] == o[1] and tuple(h[1]) == tuple(o[2]))]
        if any(h[0] == o[1] for h in hits_):
            return True, "extracted_value_elsewhere_on_page"
        if any(abs(h[0] - o[1]) == 1 for h in hits_):
            return True, "extracted_value_on_next_page"
        if not hits_ and not any(h[0] == o[1] and tuple(h[1]) == tuple(o[2]) for h in pages.find_value(r.doc, v)):
            # the extracted number is printed nowhere in the document (not at any scale the document states): the
            # extraction has no source on the page, while the cell's read is unambiguous
            return True, "extraction_printed_nowhere"
        if k in rr_ok:
            return True, "reread_agrees"      # qcore6: a third reader (the re-read) reads the cell as the page does
        return False, "uncorroborated"

    base_all = run(values)
    before_all = {c["cid"] for c in base_all if c["failed"]}
    tied_all = {c["cid"] for c in base_all if not c["failed"] and c["gap"] is not None}
    accepted_all: List[tuple] = []
    sure: List[tuple] = []
    for doc, props in proposals.items():
        base_ = run(values)
        before_ = {c["cid"] for c in base_ if c["failed"]}
        tied = {c["cid"] for c in base_ if not c["failed"] and c["gap"] is not None}
        if bun.label_read == "backed":
            # schema "label_read": "backed" (qcore3/qcore4 rule, unchanged): applied jointly under the guard, then a value
            # read is kept only when an identity through it ties now and did not before (it failed, or a blank kept it
            # from running), and the identity's other inputs are confirmed by their own lines; otherwise it is only
            # suggested (tier 2). qcore5 adds only the scale screen.
            keep_ = []
            for prop in props:
                if prop[3] == "value" and jump(prop[1], prop[2], prop[0]):
                    hold("scale_jump", prop[0])
                else:
                    keep_.append(prop)
            props = keep_
            for k, v, new, kind in props:
                values[k] = new
            backed = {n for n, prop in enumerate(props) if whole_doc_backs(prop[0], prop[2])}
            while props:
                broken = [c for c in run(values) if c["failed"] and c["cid"] in tied]
                if not broken:
                    break
                hits = collections.Counter(n for c in broken for n, prop in enumerate(props) if prop[0] in c["inputs"] and n not in backed)
                if not hits:
                    break
                most = max(hits.values())
                drop = {n for n, m in hits.items() if m == most}
                for n in drop:
                    unset(props[n][0], props[n][1])
                    withheld.append(props[n])
                keep = [n for n in range(len(props)) if n not in drop]
                backed = {keep.index(n) for n in backed if n in keep}
                props = [props[n] for n in keep]
            while True:
                mine = {prop[0] for prop in props}
                now_tied = {k for c in run(values) if not c["failed"] and c["gap"] is not None and c["cid"] not in tied
                            and all(j in mine or own_says(j, values.get(j)) for j in c["inputs"]) for k in c["inputs"]}
                # qcore6: on a scan (its text layer an OCR) a sign read needs an identity too (an OCR'd dash, or a
                # convention the extraction does not store)
                out = [prop for prop in props if (prop[3] == "value" or (prop[3] == "sign" and pages.mode == "text" and
                                                                         pages.scanned(recs[prop[0]].doc)))
                       and prop[0] not in now_tied]
                if not out:
                    break
                for prop in out:
                    unset(prop[0], prop[1])
                    conflicts.append(prop[:3])
                props = [prop for prop in props if prop not in out]
            accepted_all += [(prop, "identity_backed") for prop in props]
        else:
            # qcore5: a read changes a value only (a) when it makes an identity through the cell tie that did not tie
            # before, breaking nothing, or (b) when the page read of that exact cell is unambiguous and corroborated
            # (strict); never by a power of 1000. Everything else is a tier 2 suggestion with the read as evidence.
            pend = []
            for prop in props:
                k, v, new, kind = prop
                if kind == "value" and jump(v, new, k):
                    hold("scale_jump", k)
                    audit.append(dict(key=k, old=v, new=new, kind=kind, decision="scale_jump"))
                else:
                    pend.append(prop)
            accepted_, rivals_ = [], []
            for _ in range(20):
                wins = []
                for prop in pend:
                    nt, br = effect({prop[0]: prop[2]})
                    if nt and not br:
                        wins.append((prop, nt))
                if not wins:
                    break
                cnt_ = collections.Counter(n for _, nt in wins for n in nt)
                took_any = False
                for prop, nt in wins:
                    if any(cnt_[n] > 1 for n in nt):
                        rivals_.append(prop)          # another read alone also ties this identity: they can't both be right
                        pend.remove(prop)
                        continue
                    nt2, br2 = effect({prop[0]: prop[2]})
                    if nt2 and not br2:
                        values[prop[0]] = prop[2]
                        basis_ = "identity_ties"
                        if pages.q7:
                            # qcore9 (zero-config only): a read that rule (b) takes on its own rests on the page even
                            # when a discovered identity also ties with it; without this, finding one more identity
                            # through the cell turned qcore8's page fixes into unbacked ones (auto_unbacked)
                            ok_b, why_b = strict(*prop)
                            if ok_b:
                                basis_ = why_b
                        accepted_.append((prop, basis_))
                        pend.remove(prop)
                        took_any = True
                if not took_any:
                    break
            # two reads that together make an identity tie (neither alone does), the one such pair for that identity
            fail_ids = [n for n in range(len(checks.ids)) if not (checks.gap(n, values) is not None and abs(checks.gap(n, values)) <= checks.ids[n].tol)]
            for n in fail_ids:
                mine = [prop for prop in pend if prop[0] in checks.inputs[n]]
                if len(mine) < 2 or len(mine) > 8:
                    continue
                good = []
                for a_, b_ in itertools.combinations(mine, 2):
                    nt, br = effect({a_[0]: a_[2], b_[0]: b_[2]})
                    if n in nt and not br:
                        good.append((a_, b_))
                if len(good) == 1:
                    for prop in good[0]:
                        values[prop[0]] = prop[2]
                        accepted_.append((prop, "identity_ties_pair"))
                        pend.remove(prop)
                elif len(good) > 1:
                    for pr in good:
                        for prop in pr:
                            if prop in pend:
                                pend.remove(prop)
                                rivals_.append(prop)
            for prop in pend:
                k, v, new, kind = prop
                ok_, why_ = strict(k, v, new, kind)
                if ok_:
                    sure.append((prop, why_))
                else:
                    unverified.append(prop + (why_,))
                    audit.append(dict(key=k, old=v, new=new, kind=kind, decision="tier2", why=why_))
            for prop in rivals_:
                unverified.append(prop + ("rival_reads",))
                audit.append(dict(key=prop[0], old=prop[1], new=prop[2], kind=prop[3], decision="tier2", why="rival_reads"))
            accepted_all += accepted_
    # (b) reads, and the reads whose cell is unambiguous but that nothing else corroborates (no column evidence, or no trace
    # of the extracted number: 'weak'), are applied jointly across the documents (two misreads that cancel in a sum, or a
    # cross-document tie, are corrected together) under the guard: while an identity that tied fails, the one other input
    # whose page cell makes it tie again completes the correction, or else the reads feeding the most such identities are
    # withheld (weak reads first). A weak read is kept only when it lies on an identity that ties after and either did
    # not tie before or has another input changed by a corroborated read.
    weak = [(u[:4], "identity_ties_group") for u in unverified if u[4] in ("column_unchecked", "uncorroborated", "column_disagrees", "cell_ambiguous")]
    if sure or weak:
        weak_keys = {prop[0] for prop, _ in weak}
        sure_keys = {prop[0] for prop, _ in sure}
        unverified[:] = [u for u in unverified if u[0] not in weak_keys]
        base_j = {c["cid"]: c for c in run(values)}
        tied_now = {cid for cid, c in base_j.items() if not c["failed"] and c["gap"] is not None}
        joint = sure + weak
        for (k, v, new, kind), _ in joint:
            values[k] = new
        rel_ = set(checks.touching([prop[0] for prop, _ in joint]))

        def run_rel():
            out_r = []
            for n in sorted(rel_):
                g = checks.gap(n, values)
                out_r.append(dict(cid=checks.cid[n], failed=g is not None and abs(g) > checks.ids[n].tol, inputs=checks.inputs[n], gap=g,
                                  tol=checks.ids[n].tol, n=n))
            return out_r
        tried_ = set()
        prot_memo: Dict[tuple, bool] = {}

        def protected(prop, why_):
            """A strict read is kept even when it breaks an identity that tied, when its cell is unambiguous, its digits tie
            it to the extracted number (a sign, or a one-glyph misread) or the extraction was blank, and the extracted
            number is printed nowhere in the document: the extraction then has no source on the page, so an identity
            that tied with it tied by accident (or the documents disagree with each other); the identity is left failing
            and goes to tier 2."""
            k, v, new, kind = prop
            if why_ not in ("digit_misread", "unambiguous"):
                return False
            key_ = (k, v, new)
            if key_ not in prot_memo:
                prot_memo[key_] = v is None or not pages.find_value(recs[k].doc, v)
            return prot_memo[key_]
        for _ in range(500):
            if not joint:
                break
            now_ = run_rel()
            broken = [c for c in now_ if c["failed"] and c["cid"] in tied_now]
            if not broken:
                live = {prop[0] for prop, _ in joint}
                changed_sure = live & sure_keys
                ok_weak = set()
                for c in now_:
                    if c["failed"] or c["gap"] is None:
                        continue
                    ins = set(c["inputs"])
                    if c["cid"] not in tied_now or (ins & changed_sure):
                        ok_weak |= ins & weak_keys
                out_ = [n for n, (prop, _) in enumerate(joint) if prop[0] in weak_keys and prop[0] not in ok_weak]
                if not out_:
                    break
                for n in out_:
                    prop, why_ = joint[n]
                    unset(prop[0], prop[1])
                    unverified.append(prop + ("uncorroborated",))
                joint = [x for n, x in enumerate(joint) if n not in set(out_)]
                continue
            # an identity that tied only because two numbers were wrong together: the one other input whose page
            # cell (or a number printed on its own line) makes it tie again completes the correction
            comp = completion([c["n"] for c in broken], {prop[0] for prop, _ in joint})
            if comp and set(comp) & tried_:
                comp = None           # a completion already tried and since withheld is not tried again
            tried_ |= set(comp or ())
            if comp:
                for j, x in comp.items():
                    old_ = values.get(j)
                    values[j] = x
                    rel_ |= set(checks.touching([j]))
                    joint.append(((j, old_, x, "value"), "identity_completion"))
                    unverified[:] = [u for u in unverified if u[0] != j]
                    audit[:] = [a_ for a_ in audit if a_["key"] != j]
                continue
            hits = collections.Counter(n for c in broken for n, (prop, _) in enumerate(joint) if prop[0] in c["inputs"] and not protected(*joint[n]))
            if not hits:
                break
            # a read that is the only changed input of an identity it breaks is contradicted directly: those go first
            jk = {prop[0]: n for n, (prop, _) in enumerate(joint)}
            sole = collections.Counter()
            for c in broken:
                mine_ = [jk[k] for k in c["inputs"] if k in jk]
                if len(mine_) == 1 and not protected(*joint[mine_[0]]):
                    sole[mine_[0]] += 1
            weak_hits = {n: m for n, m in (sole or hits).items() if joint[n][0][0] in weak_keys}
            pool = weak_hits or sole or hits
            most = max(pool.values())
            drop = {n for n, m in pool.items() if m == most}
            for n in drop:
                prop, why_ = joint[n]
                unset(prop[0], prop[1])
                if prop[0] in weak_keys:
                    unverified.append(prop + ("uncorroborated",))
                    continue
                withheld.append(prop)
                audit.append(dict(key=prop[0], old=prop[1], new=prop[2], kind=prop[3], decision="withheld", why=why_,
                                  broken=[c["cid"] for c in broken if prop[0] in c["inputs"]]))
                for c in broken:
                    if prop[0] in c["inputs"]:
                        cancel.setdefault(c["cid"], set()).add(prop[0])
            joint = [x for n, x in enumerate(joint) if n not in drop]
        # the reads set aside above (withheld, or not corroborated) are tried again identity by identity: the reads on
        # the inputs of an identity that fails now are applied together when that makes it tie, some input of it keeps
        # its value, and no identity that ties breaks -- rule (a) for a group of cells
        pool = {}
        for prop in withheld:
            pool[prop[0]] = (prop, "w")
        for u in unverified:
            if u[4] in ("column_unchecked", "uncorroborated", "column_disagrees", "cell_ambiguous"):
                pool.setdefault(u[0], (u[:4], "u"))
        for _ in range(10):
            took = False
            cand_ids = sorted({n for k in pool for n in checks.touching([k])})
            for n in cand_ids:
                ks = [k for k in checks.inputs[n] if k in pool]
                if not ks or len(ks) > 12:
                    continue
                if not any(k not in pool and _num(values.get(k)) for k in checks.inputs[n]):
                    continue
                ch_ = {k: pool[k][0][2] if pool[k][0][3] == "value" else -pool[k][0][1] for k in ks}
                nt, br = effect(ch_)
                if br or not nt or not all(any(k in checks.inputs[m] for m in nt) for k in ks):
                    continue
                for k in ks:
                    prop, src = pool.pop(k)
                    values[k] = ch_[k]
                    joint.append(((prop[0], prop[1], ch_[k], prop[3]), "identity_ties_group"))
                    if src == "w":
                        withheld[:] = [w_ for w_ in withheld if w_[0] != k]
                    unverified[:] = [u for u in unverified if u[0] != k]
                took = True
            if not took:
                break
        for prop, why_ in joint:
            audit[:] = [a_ for a_ in audit if a_["key"] != prop[0]]
        accepted_all += joint

    for prop, why_ in accepted_all:
        audit.append(dict(key=prop[0], old=prop[1], new=prop[2], kind=prop[3], decision="tier1", why=why_))
    fixed = sorted(before_all - failing_now())
    for (k, v, new, kind), why_ in accepted_all:
        o = own(k)
        ids_ = [c for c in fixed if k in checks.inputs[checks.cid.index(c)]]
        tail = (f"; with the page corrections, {len(ids_)} failing identit{'y' if len(ids_) == 1 else 'ies'} through it tie." if ids_ else ".")
        basis = (" The page cell is unambiguous and the read is corroborated (" + why_.replace("_", " ") + ")."
                 if not why_.startswith("identity") else "")
        if kind == "sign":
            findings.append(Finding("misassignment", f"{label(k)} has the wrong sign ({money(v)}): its own line prints it "
                                    f"{'negative' if v > 0 else 'positive'} ({o[3] if o else ''}){tail}{basis}", "wrong_sign", ids_, [k],
                                    dict(kind="sign", a=k, b=None, a_value=v, b_value=None, found_by="label", basis=why_,
                                         page=o[1] if o else None, bbox=list(o[2]) if o else None), True))
        else:
            findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(v)}, but its own line prints {money(new)}{tail}{basis}",
                                    "misread", ids_, [k], dict(kind="value", a=k, a_value=v, b_value=new, found_by="label", basis=why_,
                                                               page=o[1] if o else None, bbox=list(o[2]) if o else None), True))
    for k, v, new, kind, why_ in unverified:
        if v is None and why_ != "rival_reads":
            continue          # a blank stays blank and is reported with the other blanks
        o = own(k)
        what = "sign" if kind == "sign" else "number"
        findings.append(Finding("wrong_value" if kind == "value" else "review",
                                f"{label(k)} was extracted as {money(v)}, but the page read of its cell gives "
                                f"{money(-v if kind == 'sign' else new)}. No identity backs the read and the cell is not unambiguous on the page "
                                f"({why_.replace('_', ' ')}), so the {what} was not changed.", "label_unverified", [], [k],
                                dict(kind=kind, a=k, a_value=v, b_value=(None if kind == "sign" else new), found_by="label_unverified",
                                     why=why_, page=o[1] if o else None, bbox=list(o[2]) if o else None), None))
    for k, v, new in conflicts:
        if v is None:
            continue          # a blank stays blank and is reported with the other blanks
        findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(v)}, but its own line reads {money(new)}; no identity "
                                "backs the read, and this schema accepts label reads only with identity support. Not changed.",
                                "misread", [], [k], dict(kind="value", a=k, a_value=v, b_value=new, found_by="label_conflict"), None))
    for k, v, new, kind in withheld:
        if v is None:
            continue
        findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(v)}, and its own line reads {money(-v if kind == 'sign' else new)}, but using "
                                "that number would break identities that tie, so the read itself may be wrong. Not changed.",
                                "misread", [], [k], dict(kind="value", a=k, a_value=v, b_value=new, found_by="label_withheld"), None))
    for cid, ks in sorted(cancel.items()):
        # qcore5: an identity that ties only while page reads of its cells are withheld may tie because several of its
        # numbers are wrong together (a sign convention or a shift applied to a whole block): all its inputs are reviewed
        n = checks.cid.index(cid)
        if checks.failed(n, values):
            continue
        findings.append(Finding("review", f"This identity ties with the extracted numbers, but the page reads {len(ks)} of its cells differently "
                                f"({', '.join(label(k) for k in sorted(ks)[:3])}{' ...' if len(ks) > 3 else ''}) and using those reads alone "
                                "would break it: several of its numbers may be wrong together. A person should look.", "label_withheld",
                                [cid], list(checks.inputs[n])))

    # ---- 2. the repair search -------------------------------------------------------------------
    for _ in range(max_rounds):
        failing, cands = candidates(values, group, checks, periods, declared={k: fld[k].sign for k in order if fld[k].sign in ('pos', 'neg')})
        cands = [f for f in cands if set(f.fixes) - reviewed]

        def contradicted(f):
            if f.kind == "sign":
                sh = sign_shown(f.a)
                return bool(sh) and bool(own_says(f.a, values[f.a])) and (values[f.a] < 0) == (sh < 0)
            return any((matches(x, values[x]) and not matches(x, values[y])) or (own_says(x, values[x]) and not own_says(x, values[y]))
                       for x, y in ((f.a, f.b), (f.b, f.a)))
        cands = [f for f in cands if not contradicted(f)]
        if not cands:
            break
        score = {id(f): (confirms(f), len(f.fixes)) for f in cands}
        best = max(score.values())
        top = [f for f in cands if score[id(f)] == best]
        accepted, contested = [], []
        for f in top:
            rivals = [g for g in top if g is not f and (facts_of(g) & facts_of(f) or set(g.fixes) & set(f.fixes))]
            (contested if rivals else accepted).append(f)
        for f in accepted:
            apply(f)
        if contested:
            cids = sorted(set(c for f in contested for c in f.fixes) - reviewed)
            if cids:
                alts = "; ".join(describe(f) for f in contested[:4])
                findings.append(Finding("review", f"{len(contested)} different fixes would each make these identities tie, so a person "
                                        f"should pick: {alts}.", None, cids, sorted({k for f in contested for k in facts_of(f)})))
                reviewed |= set(cids)
        if not accepted and not contested:
            break

    # ---- 3. misread repair ----------------------------------------------------------------------
    base = run(values)
    before = {c["cid"] for c in base if c["failed"]}
    if before:
        done = {k for f in findings for k in f.keys}
        props2: Dict[str, List[float]] = {}
        for c in base:
            if not c["failed"] or c["cid"] in reviewed or c["gap"] is None:
                continue
            for k in c["inputs"]:
                a = checks.coef(k, c["n"], values)
                if a and k in values and k not in done:
                    props2.setdefault(k, []).append(values[k] - c["gap"] / a)
        repairs = []
        for k, vs in props2.items():
            d = decimals(values[k])
            v = round(vs[0], d) if d else round(vs[0])
            if any(abs(x - v) > (1 if not d else 0.5 * 10 ** -d) for x in vs) or not misread(values[k], v):
                continue
            now = checks.failing_after(values, before, {k: v})
            if before - now and not now - before:
                repairs.append((k, v, before - now))
        for k, v, fixed in repairs:
            if fixed & reviewed:
                continue
            rivals = [x for x in repairs if x[0] != k and x[2] & fixed]
            r = recs[k]
            if own_says(k, values[k]):
                continue      # the field's own cell prints the extracted number: the document is off, not the read
            taken = [(cites[j].page, cites[j].bbox) for j in order if j != k and cites[j].bbox and recs[j].doc == r.doc]
            page_new = any(owns(k, h[2]) and not any(pg == h[0] and overlaps(bb, h[1]) for pg, bb in taken)
                           for h in pages.find_value(r.doc, v, cites[k].page, word_sets(k)[0] if word_sets(k) else []))
            page_old = any(owns(k, h[2]) for h in pages.find_value(r.doc, values[k], cites[k].page, word_sets(k)[0] if word_sets(k) else []))
            if shared(k) and own(k) is not None:
                page_new, page_old = bool(at_scale(own(k)[0], v, own(k)[4])), bool(at_scale(own(k)[0], values[k], own(k)[4]))
            if pages.ocr and not page_new and own(k) is not None:
                o = own(k)
                sc_ = o[4] if is_money(k) else 1.0
                page_new = any(abs(x - v) <= 0.5 * sc_ for x in ocr_candidates(k, values[k], o[0] * sc_, sc_))
            if page_old and not page_new:
                continue
            if rivals:
                alts = "; ".join(f"{label(j)} as {money(w)}" for j, w, _ in [(k, v, fixed)] + rivals[:3])
                findings.append(Finding("review", f"Several single misreads would each make these identities tie, so a person should pick: {alts}.",
                                        None, sorted(fixed | {c for x in rivals for c in x[2]}), [k] + [x[0] for x in rivals]))
                reviewed |= fixed | {c for x in rivals for c in x[2]}
                continue
            n = len(fixed)
            findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(values[k])}, but {money(v)} makes {n} failing "
                                    f"identit{'y' if n == 1 else 'ies'} tie and breaks nothing; it looks like a misread digit."
                                    + (" The page prints that number on this line." if page_new else " The page text can't confirm it."),
                                    "misread", sorted(fixed), [k], dict(kind="value", a=k, a_value=values[k], b_value=v, found_by="arithmetic"),
                                    True if page_new else None))
            if page_new:
                values[k] = v     # a tier 2 suggestion is not applied: later stages see the extracted number
            reviewed |= fixed

    # ---- what the repair couldn't explain --------------------------------------------------------
    covered = {k for f in findings for k in f.keys}
    left = [c for c in run(values) if c["failed"] and c["cid"] not in reviewed]
    wrong: Dict[str, List[str]] = {}
    by_label = {f.fix["a"] for f in findings if f.fix and f.fix.get("found_by") == "label"}
    on_page = lambda k: True if k in by_label else matches(k, values.get(k))
    for c in left:
        bad = [k for k in c["inputs"] if on_page(k) is False]
        if bad:
            for k in bad:
                wrong.setdefault(k, []).append(c["cid"])
        elif all(on_page(k) for k in c["inputs"]) and c["inputs"]:
            if pages.ocr:
                findings.append(Finding("review", "The model and the OCR agree on every number here, yet it does not tie and no swap, "
                                        "period move or sign flip makes it tie: both may have misread the same blurred digits, or the "
                                        "document disagrees. A person should look.", "readers_agree", [c["cid"]], list(c["inputs"])))
            else:
                findings.append(Finding("document", "Every number here matches the page, and no swap, period move or sign flip makes it tie. "
                                        "The document itself disagrees.", "source_error", [c["cid"]], list(c["inputs"])))
        else:
            keys = list(c["inputs"])
            tot = _agg_total(checks.ids[c["n"]]) if getattr(bun, "q7", False) and len(keys) > 6 else None
            if tot:
                keep = [tot] + [k for k in keys if k != tot and on_page(k) is not True]
                findings.append(Finding("review", f"The lines do not add up to this total (off by {money(abs(c['gap']))}); no swap, "
                                        "period move or sign flip makes it tie, and the page can't settle it. A person should look "
                                        "at the total and the lines the page does not confirm.", None, [c["cid"]], keep))
            else:
                findings.append(Finding("review", "No swap, period move or sign flip makes this tie, and the page can't settle it. "
                                        "A person should look.", None, [c["cid"]], keys))
    tied_ok = {k for c in run(values) if c["gap"] is not None and not c["failed"] for k in c["inputs"]}
    for k in list(values):
        if k in wrong or k in covered or (pages.ocr and k in tied_ok):
            continue          # (ocr: an identity that ties backs the model's reading against the OCR's)
        if matches(k, values[k]) is False:
            wrong[k] = []
    for k in list(wrong):
        if scale_of(k) != 1:
            hold("scale" if copied_scale(printed(k), values[k], scale_of(k)) else "scale_value", k)
            del wrong[k]
    paired = set()
    items = list(wrong.items())
    for n, (i, ci) in enumerate(items):
        for j, cj in items[n + 1:]:
            if i in paired or j in paired or group[i] != group[j]:
                continue
            if matches(i, values[j]) and matches(j, values[i]):
                paired |= {i, j}
                fx = Fix("swap", i, j)
                seen = (" No identity catches it; only the page does." if not (ci or cj)
                        else f" {len(set(ci) | set(cj))} identities fail because of it.")
                nt_, br_ = effect({i: values[j], j: values[i]})
                # qcore5: a page-found swap changes values only when it makes a failing identity tie and breaks nothing
                findings.append(Finding("misassignment" if not pages.ocr and nt_ and not br_ else "review",
                                        f"{describe(fx)}. The page prints each number at the other's spot.{seen}",
                                        reason(fx), sorted(set(ci) | set(cj)), [i, j],
                                        dict(kind="swap", a=i, b=j, a_value=values[i], b_value=values[j], found_by="page"), True))
    for k, cids in wrong.items():
        if k in paired:
            continue
        findings.append(Finding("wrong_value", f"{label(k)} was extracted as {money(values[k])}, but the page prints {money(printed(k))} "
                                f"at that spot. No swap explains it.", "misread", cids, [k],
                                dict(kind="value", a=k, a_value=values[k], b_value=printed(k)),
                                None if pages.ocr or not _num(printed(k)) or not (lambda e_: e_[0] and not e_[1])(effect({k: printed(k)})) else True))
    why_text = dict(scale="copied as printed on a page that states its amounts in {unit}; the schema stores whole units. Not changed.",
                    scale_value="does not match its own line, which is printed in {unit}. A rounded page figure can't replace it. Not changed.",
                    scale_jump="read off its own line at a different unit scale (a power of 1000 away from the extracted value); the page's unit statement does not prove it. Not changed.",
                    sign_unclear="stored negative where its own line prints a plain number, and the document never marks negatives. Not changed.")
    for why, docs in held.items():
        for doc, idx in docs.items():
            done_ = {f.fix.get(x) for f in findings if f.fix for x in ("a", "b")}
            idx = [k for k in dict.fromkeys(idx) if k not in done_]
            if not idx:
                continue
            units = {scale_of(k) if scale_of(k) != 1 else (own(k) or (0, 0, 0, 0, 1.0))[4] for k in idx}
            text = why_text[why].format(unit="billions" if 1e9 in units else "millions" if 1e6 in units else "thousands")
            names = ", ".join(label(k) for k in idx[:4]) + (f" and {len(idx) - 4} more" if len(idx) > 4 else "")
            findings.append(Finding("review", f"{names}: {text}", "scale" if why.startswith("scale") else why, [], idx))
    # blanks
    flagged = {k for f in findings for k in f.keys}
    blank: Dict[str, List[str]] = {}
    for k in order:
        if cites[k].value is None and k not in values and k not in flagged:
            blank.setdefault(recs[k].doc, []).append(k)
    if blank:
        empty = {k for ks in blank.values() for k in ks}
        ran = {k for c in run(values) if c["gap"] is not None for k in c["inputs"]}
        starving: Dict[str, set] = {}
        for c in run({**values, **{k: 0.0 for k in empty}}):
            hit = set(c["inputs"]) & empty
            if hit:
                for k in c["inputs"]:
                    if k not in empty and k in values:
                        starving.setdefault(k, set()).update(hit)
        for doc, ks in blank.items():
            mine = set(ks)
            starved = [j for j in order if j in starving and starving[j] & mine and j not in ran and j not in flagged]
            more = (f" Without them, no identity can verify {len(starved)} other number(s)." if starved else "")
            findings.append(Finding("review", f"{len(ks)} number(s) left empty: {', '.join(label(k) for k in ks[:4])}"
                                    f"{' ...' if len(ks) > 4 else ''}. The page pass couldn't fill them.{more}", "missing", [], ks + starved))
            flagged |= set(starved)
    # numbers printed nowhere in their document
    flagged = {k for f in findings for k in f.keys}
    for k in order:
        v = values.get(k)
        r = recs[k]
        if (k in flagged or v is None or abs(v) < 100 or v != round(v) or B.is_rounded(fld[k].units)
                or not pages.has_text(r.doc) or (pages.ocr and k in tied_ok) or pages.find_value(r.doc, v)):
            continue
        if pages.ocr and any(pages.find_value(r.doc, x) for x in confusions(v, decimals(v))):
            continue          # the OCR prints a look-alike of it: the OCR may have misread it
        findings.append(Finding("review", f"{label(k)} was extracted as {money(v)}, but that number isn't printed anywhere in its document.",
                                "misread", [], [k]))
    return findings


# ---- routing

def sign_convention(f: Finding, bun: B.Bundle) -> bool:
    """A sign flip that restores a field's declared sign (schema sign pos / neg)."""
    fx = f.fix or {}
    if f.kind != "misassignment" or fx.get("kind") != "sign":
        return False
    r = next((x for x in bun.records if x.key == fx.get("a")), None)
    if r is None:
        return False
    s, v = bun.field_of(r).sign, fx.get("a_value") or 0
    return (s == "pos" and v < 0) or (s == "neg" and v > 0)


_ID_BASIS = ("identity_ties", "identity_ties_pair", "identity_completion", "identity_ties_group")
_LABEL_NUM = re.compile(r"(?<![\d.,/–-])(\d{1,3})\s?[-–]\s?(\d{1,3})(?![\d.,/–-])|(?<![\d.,])((?:19|20)\d\d)(?![\d.,])")


def label_values(bun: B.Bundle, final: Dict[str, object], info: dict, mode: str) -> List[dict]:
    """qcore7: a value that is a number of the cell's own row or column label (an age band '30-34' read as 30 or 34, the
    year of the column heading or period read as the value) is a label read as a number: tier 2 `value_is_label`, unless
    the text layer prints that very value in the cell (`page_confirms`)."""
    conf = info.get("page_confirms") if mode == "text" else None
    out = []
    for r in bun.records:
        v = final.get(r.key)
        if not _num(v) or v == 0 or abs(v - round(v)) > 1e-9:
            continue
        cands = set()
        for t in (r.entity, r.column, r.period):
            for m in _LABEL_NUM.finditer(str(t or "")):
                if m.group(3):
                    cands.add(int(m.group(3)))
                else:
                    a, b = m.group(1), m.group(2)
                    cands |= {int(a), int(b), int(a + b)}
        if abs(v) not in cands:
            continue
        try:
            if conf is not None and conf(r.key, v):
                continue
        except Exception:
            pass
        out.append(dict(keys=[r.key], reason="value_is_label", evidence=dict(
            summary=f"{r.field} ({r.entity or ''} {r.column or ''} {r.period or ''}) is {v:g}, a number printed in its own row or "
                    f"column label: a label read as the value?", finding="review", checks=[])))
    return out


def _agg_total(i) -> Optional[str]:
    """qcore7: the total of a discovered aggregate tie (source `aggtie`: a total equal to the sum of a field over the
    document's entities), or None. When such a tie with many lines fails and no single change explains it, only the total
    and the lines the page does not confirm are flagged (with the identity as evidence), not the lines the page reads
    as extracted."""
    if getattr(i, "source", None) != "aggtie":
        return None
    net: Dict[str, float] = {}
    for k, c in i.terms:
        net[k] = net.get(k, 0.0) + c
    neg = [k for k, c in net.items() if c < -1e-9]
    pos = [k for k, c in net.items() if c > 1e-9]
    return neg[0] if len(neg) == 1 and len(pos) > 1 else pos[0] if len(pos) == 1 and len(neg) > 1 else None


def strong_auto(i) -> bool:
    """qcore7: a discovered identity validated on the page: its template held with every cell read off the page in at
    least two places and never failed where the page read every cell."""
    s = getattr(i, "support", None) or {}
    return s.get("hold_page", 0) >= 2 and not s.get("fail_confirmed", 0) and s.get("fail", 0) <= 1


def auto_unbacked(findings: List[Finding], bun: B.Bundle, cites: Dict[str, Cite], checks: Checks) -> set:
    """qcore7 (identities auto / both): the indexes of tier 1 findings whose change rests on discovered identities alone
    without independent backing. A change rests on identities when it was found by the repair search, the misread
    repair, the two-reader reconcile, a page swap, or a label read taken because an identity ties (rule a); a label read
    taken as unambiguous and corroborated (rule b) rests on the page. Its backing identities are those through its cells
    that tie with all tier 1 changes applied and fail with this one reverted. It is kept when one of them is hand-written,
    two of them are discovered, or one discovered identity is strong (`strong_auto`)."""
    tier1 = [(n, f) for n, f in enumerate(findings) if tier(f, bun) == 1]
    final = {k: c.value for k, c in cites.items()}
    for _, f in tier1:
        final.update(new_values(f))
    vals = {k: v for k, v in final.items() if _num(v)}
    out = set()
    for n, f in tier1:
        fx = f.fix or {}
        by = fx.get("found_by", "search")
        if by == "label" and fx.get("basis") not in _ID_BASIS:
            continue
        if by in ("label_unverified", "label_conflict", "label_withheld"):
            continue
        ch = new_values(f)
        rev = dict(vals)
        for k in ch:
            o = cites[k].value if k in cites else None
            if _num(o):
                rev[k] = o
            else:
                rev.pop(k, None)
        back = [m for m in checks.touching(ch) if not checks.failed(m, vals) and checks.gap(m, vals) is not None
                and checks.failed(m, rev)]
        hand = [m for m in back if getattr(checks.ids[m], "source", None) is None]
        auto_ = [m for m in back if getattr(checks.ids[m], "source", None) is not None]
        if hand or _independent(auto_, checks, set(ch)) >= 2 or any(strong_auto(checks.ids[m]) for m in auto_):
            continue
        out.add(n)
    return out


def _independent(ms, checks: Checks, changed: set) -> int:
    """qcore7: how many of these identities check the change independently: two that share most of their other inputs
    (a total and its repeat, both equal to the same sum of lines) count once."""
    groups: List[set] = []
    for m in ms:
        rest = set(checks.inputs[m]) - changed
        for g in groups:
            if len(rest & g) * 2 > min(len(rest), len(g)):
                g |= rest
                break
        else:
            groups.append(set(rest))
    return len(groups)


def tier(f: Finding, bun: B.Bundle) -> int:
    if f.kind == "wrong_value":
        return 1 if f.page_confirms else 2
    if f.kind == "misassignment" and (f.page_confirms is not False or sign_convention(f, bun)):
        return 1
    return 2


def new_values(f: Finding) -> Dict[str, float]:
    fx = f.fix or {}
    if fx.get("kind") == "sign":
        return {fx["a"]: -fx["a_value"]}
    if fx.get("kind") == "value":
        return {fx["a"]: fx["b_value"]}
    return {fx["a"]: fx["b_value"], fx["b"]: fx["a_value"]}


def localize_flags(checks: Checks, after: Dict[str, float], t2: List[dict], info: dict, mode: str):
    """Identity-localized flags: a tier 2 item about failing identities names only the cells that can explain them
    (localize.candidates); the full list stays in evidence["all_keys"]. Cells the text layer confirms are pinned; in ocr
    mode nothing is pinned. Returns (items, (cells flagged before, cells flagged after))."""
    from .localize import candidates, likely
    conf = info.get("page_confirms")
    pinned = set()
    if mode == "text" and conf is not None:     # ocr: two readers often misread the same blurred glyph alike, so agreement pins nothing
        for k, v in after.items():
            try:
                if conf(k, v):
                    pinned.add(k)
            except Exception:
                pass
    cand = candidates(checks, after, pinned)
    hint = likely(checks, after, pinned)
    pos = {c: n for n, c in enumerate(checks.cid)}
    before_keys = {k for t in t2 for k in t["keys"]}
    out = []
    for t in t2:
        cids = (t.get("evidence") or {}).get("checks") or []
        keys = t["keys"]
        if cids and all(c in cand for c in cids):
            allowed = set().union(*(cand[c] for c in cids))
            inside = set().union(*(set(checks.inputs[pos[c]]) for c in cids))
            nk = [k for k in keys if k in allowed or k not in inside]
            if nk and len(nk) < len(keys):
                t = dict(t, keys=nk, evidence=dict(t["evidence"], all_keys=keys))
            lk = set().union(*(hint.get(c, set()) for c in cids)) & set(t["keys"])
            if lk and len(lk) < len(t["keys"]):
                t = dict(t, evidence=dict(t["evidence"], most_likely=sorted(lk)))
        out.append(t)
    return out, (len(before_keys), len({k for t in out for k in t["keys"]}))


def _near(a, b) -> bool:
    """Two numbers a glyph confusion apart: a digit misread (page.misread), a sign, or the same digit count with at least
    half of the digit positions equal."""
    from .page import misread
    if not (_num(a) and _num(b)):
        return False
    if misread(a, b) or misread(b, a) or a == -b:
        return True
    da, db = re.sub(r"\D", "", f"{abs(a):.0f}"), re.sub(r"\D", "", f"{abs(b):.0f}")
    return len(da) == len(db) and sum(x == y for x, y in zip(da, db)) * 2 >= len(da)


REREAD_CLEARABLE = ("misread", "label_unverified", "readers_disagree", "label_withheld")


def reread_routing(bun, pages, cites, checks, final, after, t2, info, rr, clear: bool = True):
    """qcore6: after tier 1, each re-read cell is compared with its final value. A re-read that disagrees with it is a
    tier 2 `reread_disagrees` item with every read as evidence (model, page, re-read). A re-read that agrees with it clears
    the cell from single-cell tier 2 items (OCR or page-read disagreements, unverified reads) when the page read is
    another number altogether (not a glyph or two apart, `_near`), unless an identity through the cell still fails."""
    rr_num = info.get("rr_num", {})
    own_ = info.get("own")
    failing = {checks.cid[n] for n in range(len(checks.ids)) if checks.failed(n, after)}
    fail_keys = {k for n in range(len(checks.ids)) if checks.cid[n] in failing for k in checks.inputs[n]}
    log, agree = [], set()
    recs = {r.key: r for r in bun.records}
    for k, x in rr_num.items():
        f = final.get(k)
        try:
            o = own_(k) if own_ else None
        except Exception:
            o = None
        ent = dict(key=k, reread=(rr.get(k) or {}).get("printed_text"), model=cites[k].value if k in cites else None, final=f,
                   page_read=(o[0] * o[4]) if o else None)
        if x is None:
            ent["outcome"] = "wrong_cell" if k in info.get("rr_wrong_row", ()) else "null"
        else:
            scs = (o[4],) if o else (1.0, 1e3, 1e6, 1e9)
            ok = (f is None and x == 0) or (_num(f) and any(at_scale(x, f, sc_) for sc_ in scs))
            ent["outcome"] = "agrees" if ok else "disagrees"
            if ok and o is not None and _num(f) and not _near(o[0] * o[4], f):
                # only a re-read that settles a disagreement the page reader can't have made by misreading glyphs clears
                # the flag: the page read is another number altogether (the reader read another cell). When the page read
                # is a glyph or two off the value, or there is no page read, the re-reader (often the same kind of model as
                # the extractor, reading the same poor image) shares the extractor's misreads too often (dev SEC scans:
                # 21 re-reads agreed with a wrong extracted value), so the flag stays
                agree.add(k)
            if not ok:
                t2.append(dict(keys=[k], reason="reread_disagrees", evidence=dict(
                    summary=f"{k}: the re-read of its cell gives {ent['reread']!r}, the model {money(ent['model']) if _num(ent['model']) else 'blank'}"
                            f", the page {money(ent['page_read']) if _num(ent['page_read']) else 'no read'}; the value is "
                            f"{money(f) if _num(f) else 'blank'}. Not changed by the re-read.",
                    finding="review", checks=sorted(c for c in failing if k in checks.inputs[checks.cid.index(c)]), reads=ent)))
        log.append(ent)
    if clear and agree:
        out = []
        for t in t2:
            ev = t.get("evidence") or {}
            if t.get("reason") in REREAD_CLEARABLE and not (set(ev.get("checks") or []) & failing):
                ks = [k for k in t["keys"] if not (k in agree and k not in fail_keys)]
                if not ks:
                    for k in t["keys"]:
                        log.append(dict(key=k, outcome="cleared", reason=t.get("reason")))
                    continue
                if len(ks) < len(t["keys"]):
                    t = dict(t, keys=ks)
            out.append(t)
        t2 = out
    info["reread_log"] = log
    return t2


def coverage(bun: B.Bundle) -> dict:
    """qcore6: the share of the bundle's numbers that sit in at least one identity."""
    inn = {k for i in bun.identities for k, _ in i.terms} | {k for i in bun.identities for p in i.products for k in p[:2]}
    n = len(bun.records)
    m = sum(1 for r in bun.records if r.key in inn)
    return dict(numbers=n, in_identity=m, share=round(m / n, 4) if n else 0.0)


def load_rereads(src) -> Dict[str, dict]:
    """qcore6 re-read answers: a path to answers.jsonl ({req_id, printed_text}) next to its requests.jsonl, or a list of
    {key, printed_text} / {req_id, key, printed_text} dicts. Returns {key: {"printed_text": str|None, "req_id": ...}}."""
    import json as _json
    rows = src
    if isinstance(src, str):
        rows = [_json.loads(l) for l in open(src) if l.strip()]
        reqp = os.path.join(os.path.dirname(os.path.abspath(src)), "requests.jsonl")
        if os.path.exists(reqp):
            reqs = {}
            for l in open(reqp):
                if l.strip():
                    q = _json.loads(l)
                    reqs[q["req_id"]] = q
            more = []
            for r in rows:
                if "key" not in r and r.get("req_id") in reqs:
                    r["key"] = reqs[r["req_id"]]["key"]
                    # one crop placed for several records (their cells coincide): the answer is each one's re-read; the
                    # row label and column heading checks then run per record
                    for k2 in reqs[r["req_id"]].get("also_keys") or []:
                        more.append(dict(r, key=k2))
            rows = rows + more
    out = {}
    for r in rows or []:
        if isinstance(r, dict) and r.get("key"):
            pt = r.get("printed_text")
            out[str(r["key"])] = dict(printed_text=None if pt in (None, "", "null", "None") else str(pt), req_id=r.get("req_id"),
                                      row_label=r.get("row_label"), column_label=r.get("column_label"))
    return out


def run(bundle_dir: str, extraction: List[dict], page_text_mode: str = "text", ocr_dir: Optional[str] = None,
        identities: str = "hand", schema: str = "hand", reread: Optional[object] = None, _ctx: Optional[dict] = None) -> dict:
    """Tier 1 over one bundle. Returns {"fixes", "tier2", "values", "failing_before", "failing_after", ...}.
    page_text_mode: "text" (the PDF text layer), "notext" (identities only) or "ocr" (page text from `ocr_dir`, an OCR
    read of the page images written by `python -m qcore5 ocr`, treated as a second noisy reader)."""
    if page_text_mode not in ("text", "notext", "ocr"):
        raise ValueError("page_text_mode is 'text', 'notext' or 'ocr'")
    if page_text_mode == "ocr" and not (ocr_dir and os.path.isdir(ocr_dir)):
        raise ValueError("page_text_mode 'ocr' needs ocr_dir, a folder of <doc>.json OCR files")
    if identities not in ("hand", "auto", "both") or schema not in ("hand", "auto"):
        raise ValueError("identities is 'hand', 'auto' or 'both'; schema is 'hand' or 'auto'")
    bun = B.load(bundle_dir, schema_file=schema == "hand", identities_file=identities != "auto")
    # qcore7: the new zero-config safety rules apply whenever something is discovered (identities auto / both, or schema
    # auto); with the hand files alone (the defaults) qcore7 runs qcore6's code paths unchanged
    bun.q7 = identities != "hand" or schema == "auto"
    auto_info = None
    if schema == "auto":
        from .auto import auto_schema, detect_format
        bun.number_format = detect_format(extraction)
        bun.fields = auto_schema(bun, extraction)
    pages = Pages(bun, page_text_mode, ocr_dir)
    pages.q7 = bun.q7
    cites = cite(bun, pages, extraction)
    if schema == "auto" and page_text_mode != "notext":
        bun.fields = auto_schema(bun, extraction, pages, cites)
        pages._memo = {k: v for k, v in pages._memo.items() if k[0] != "ocrdoc"}
    if identities in ("auto", "both"):
        from .auto import discover
        found, auto_info = discover(bun, pages, cites)
        if identities == "auto":
            bun.identities = found
        else:
            have = {(tuple(sorted(i.terms)), tuple(sorted(i.products))) for i in bun.identities}
            bun.identities = list(bun.identities) + [i for i in found if (tuple(sorted(i.terms)), tuple(sorted(i.products))) not in have]
    if schema == "auto" and page_text_mode == "text":
        # a set of scans (their text layer an OCR of the image) gets the backed label read: a page read changes a value
        # only with an identity through it (what a hand schema sets for such documents)
        docs_ = sorted({r.doc for r in bun.records})
        if docs_ and sum(pages.scanned(d) for d in docs_) * 2 >= len(docs_):
            bun.label_read = "backed"
    rr = load_rereads(reread) if reread is not None else None
    checks = Checks(bun.identities)
    start = {k: c.value for k, c in cites.items() if _num(c.value)}
    failing_before = sorted(checks.failing(start))
    info: dict = {}
    if rr is not None:
        info["rereads"] = rr
    findings = diagnose(bun, pages, cites, info=info)
    fixes, t2 = [], []
    final = {k: c.value for k, c in cites.items()}
    unbacked = auto_unbacked(findings, bun, cites, checks) if getattr(bun, "q7", False) and identities != "hand" else set()
    for n_, f in enumerate(findings):
        if n_ in unbacked:
            ev = dict(summary=f.summary + " Not applied: only discovered identities back it, and not two of them nor one "
                      "confirmed on the page in two places.", finding=f.kind, checks=f.checks, suggested_fix=f.fix)
            t2.append(dict(keys=list(new_values(f)), reason="auto_identity_unbacked", evidence=ev))
            continue
        if tier(f, bun) == 1:
            for k, v in new_values(f).items():
                fixes.append(dict(key=k, old=final.get(k), new=v, reason=f.reason_code or f.kind, kind=(f.fix or {}).get("kind"),
                                  found_by=(f.fix or {}).get("found_by", "search"), checks=f.checks, summary=f.summary))
                final[k] = v
        else:
            ev = dict(summary=f.summary, finding=f.kind, checks=f.checks)
            if f.fix:
                ev["suggested_fix"] = f.fix
            t2.append(dict(keys=f.keys, reason=f.reason_code or {"document": "suspected_document_error"}.get(f.kind, f.kind),
                           evidence=ev))
    after = {k: v for k, v in final.items() if _num(v)}
    if _ctx is not None:
        _ctx.update(bun=bun, pages=pages, cites=cites, info=info, checks=checks)
    if rr is not None:
        t2 = reread_routing(bun, pages, cites, checks, final, after, t2, info, rr)
    if bun.q7:
        t2 += label_values(bun, final, info, page_text_mode)
    t2, narrowed = localize_flags(checks, after, t2, info, page_text_mode)
    return dict(flags_before_localization=narrowed[0], flags_after_localization=narrowed[1], bundle=bundle_dir, mode=page_text_mode, ocr_dir=ocr_dir, fixes=fixes, tier2=t2, values=final,
                failing_before=failing_before, failing_after=sorted(checks.failing(after)), label_reads=info.get("label_reads", []),
                identities=len(bun.identities), records=len(bun.records),
                identity_list=[dict(id=i.id, terms=[list(t) for t in i.terms], products=[list(p) for p in i.products], tol=i.tol,
                                    source=getattr(i, "source", None)) for i in bun.identities] if identities != "hand" else None,
                auto=dict(report=auto_info["report"], n=auto_info["n"], page_reads=auto_info["page_reads"]) if auto_info else None,
                coverage=coverage(bun), rereads=info.get("reread_log"))
