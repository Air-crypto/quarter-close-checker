"""qcore6 zero-config mode: a schema and identities discovered from the document set itself.

    run(bundle, extraction, identities="auto", schema="auto")

Nothing here reads identities.json, schema.json or truth values. The inputs are the bundle's record list (key, doc,
field, column, period, entity: the extraction task), the extraction, and the page (text layer or OCR words) when the
mode has one.

Auto schema (`auto_schema`)
  - labels: the text before the first amount in the extraction's printed_text for the field's keys (most common), else
    the text left of the cited number on the page line; none (qcore then finds the row by entity or field name);
  - units: money when the extraction stores the printed number times a power of 1000 (the page states a unit), or the
    line prints a currency mark in a document with no unit statement; otherwise a plain number;
  - number format: dot-grouped thousands ("16.500") as well as comma-grouped in the printed strings -> "grouped";
  - sign "any", scale 1, label_read "replace".

Identity discovery (`discover`)
  Each document is laid out as a grid: a row per field (or per named entity: an item, a region), a column per printed
  column (or period); a field over named entities also gives an aggregate row (the sum over the entities). Candidates:
  - vertical sums (R1): a row equals a signed sum of the rows just above it that are still open (rows already summed
    into a subtotal are replaced by it: hierarchical subtotals, A - B differences, roll-forwards begin + changes = end);
  - horizontal sums (R2): the same on the transposed grid (a Total column);
  - products (R3): one column times another equals a third, per row (count x unit price = amount);
  - equal cells (R5): the same number printed on two rows, in one document or two (net income on two statements,
    cash at the end of the period and on the balance sheet), matched by period.
  A candidate is a template over row (column) names; its instances are the columns (rows) where all its cells exist,
  in every document that has those names. Each instance is evaluated on the source's own values: the page read of
  each cell where the page reads it, else the extraction (`hold_page`: every cell read off the page and it holds;
  `hold`: holds on the page reads or on the extraction; `fail_confirmed`: the page reads every cell, agrees with the
  extraction, and it fails; `fail`). A template is ADMITTED when it holds in at least 2 instances with at most
  max(1, 10%) failing, and, when some instance is read entirely off the page, at least one such instance holds; a
  template with a single holding instance is admitted only with a total-like label cue on its target ("Total",
  "Net", "Subtotal", "Balance", ...) and at least 3 significant digits. Admitted templates are instantiated on their
  holding and failing instances (a failing instance read entirely off the page only when the template holds elsewhere
  in the same document).
"""

from __future__ import annotations

import collections
import itertools
import re
from dataclasses import dataclass, field as dfield
from typing import Dict, List, Optional, Tuple

from . import bundle as B
from .identities import _num

CUE = re.compile(r"\b(total|totals|subtotal|sub-total|net|sum|balance|ending|end of|gross|grand|all)\b", re.I)
_LEAD_CUE = re.compile(r"\s*(total|totals|subtotal|sub-total|net|grand)\b", re.I)


def cue_of(label) -> bool:
    """A total-like label. qcore7: a field name used as the label ('gross_amount', 'TotalAssets') is split into words."""
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(label or "")).replace("_", " ")
    return bool(CUE.search(t))


_AMOUNT_TOK = re.compile(r"[-–(]*[$€£¥]?\s?\d[\d,.]*\)?%?[-]?")
_CURRENCY = re.compile(r"[$€£¥]|\b(USD|EUR|GBP|IDR|Rp)\b")
_STOP = {"the", "and", "for", "from", "with", "total", "net", "other", "less", "per", "end", "beginning", "period", "year",
         "months", "ended", "three", "six", "nine", "twelve", "of", "in", "to", "on", "at", "by", "or", "as", "a", "an"}
MAXSPAN = 25
TIGHT_MAX = 8          # qcore9: the longest sum held to one and a half units (Discovery.stats)


# ---------------------------------------------------------------------------------------------- auto schema

def label_of_printed(text: Optional[str]) -> Optional[str]:
    """'Products $ 78,678 $ 66,613' -> 'Products'; 'Trading assets (included $32,431 pledged) 1,062,072 802,873' ->
    'Trading assets (included $32,431 pledged)': the printed line without its trailing run of amounts (an amount inside
    the label stays in it); a printed string that is only a number -> None."""
    toks = (text or "").split()
    while toks and (toks[-1] in ("$", "(", ")", "€", "£", "-", "–", "—") or (_AMOUNT_TOK.fullmatch(toks[-1].strip()) and re.search(r"\d", toks[-1]))):
        toks.pop()
    lab = " ".join(toks).strip(" :.-–$")
    return lab if re.search(r"[A-Za-z]{2,}", lab) else None


def _amounts(text: str, fmt: str) -> List[float]:
    from .pdftext import amount
    out = []
    for t in (text or "").split():
        if re.search(r"\d", t):
            v = amount(t.strip().rstrip(",;:"), fmt)
            if v is not None:
                out.append(v)
    return out


def detect_format(extraction: List[dict]) -> str:
    comma = dot = 0
    for a in extraction or []:
        for t in str(a.get("printed_text") or "").split() + [str(a.get("value") or "")]:
            if re.fullmatch(r"\(?-?\$?\d{1,3}(,\d{3})+(\.\d+)?\)?", t):
                comma += 1
            elif re.fullmatch(r"\(?-?\$?\d{1,3}(\.\d{3})+(,\d+)?\)?", t):
                dot += 1
    if dot and dot >= 0.2 * max(1, comma):
        return "grouped"
    return "us"


def auto_schema(bun: B.Bundle, extraction: List[dict], pages=None, cites=None) -> Dict[str, B.Field]:
    """Fields with labels and units taken from the extraction's printed strings (and the page, when there is one)."""
    by = {str(a["key"]): a for a in extraction or [] if isinstance(a, dict) and "key" in a}
    fmt = bun.number_format
    labs: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    ratio: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    cur: Dict[str, int] = collections.Counter()
    docs_of: Dict[str, set] = collections.defaultdict(set)
    labs_doc: Dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in bun.records:
        docs_of[r.field].add(r.doc)
        a = by.get(r.key)
        if not a:
            continue
        pt = str(a.get("printed_text") or "")
        lab = label_of_printed(pt)
        if lab is None and pages is not None and cites is not None and cites.get(r.key) and cites[r.key].bbox:
            try:
                ln = pages.line_at(r.doc, cites[r.key].page, cites[r.key].bbox)
            except Exception:
                ln = None
            lab = label_of_printed(ln)
        if lab and not (r.entity and _names_entity(lab, r.entity)):
            labs[r.field][lab] += 1
            labs_doc[(r.doc, r.field)][lab] += 1
        v = a.get("value")
        if isinstance(v, str):
            from .pdftext import amount
            v = amount(v, fmt)
        if _num(v) and v != 0:
            for p in _amounts(pt, fmt):
                if p == 0:
                    continue
                for s in (1.0, 1e3, 1e6, 1e9):
                    if abs(abs(p) * s - abs(v)) <= 0.5 * s:
                        ratio[r.field][s] += 1
                        break
        if _CURRENCY.search(pt):
            cur[r.field] += 1
    keys_of: Dict[str, list] = collections.defaultdict(list)
    for r in bun.records:
        keys_of[r.field].append(r)
    if pages is not None and cites is not None and pages.mode != "notext":
        # qcore7: printed_text without the number (a bare label, as some extractors quote it) gives no scale vote; the
        # page then votes: the stored number is printed on its cited page at the page's stated unit (in millions)
        for f, rs in keys_of.items():
            if ratio[f]:
                continue
            for r in rs[:40]:
                c = cites.get(r.key)
                v = c.value if c is not None else None
                if not _num(v) or v == 0:
                    continue
                s = _page_ratio(pages, r.doc, c.page, v)
                if s:
                    ratio[f][s] += 1
    out = {}
    signs = _sign_convention(pages, bun, by, fmt, cites) if pages is not None and cites is not None and pages.mode != "notext" else {}
    single = set()      # fields whose one printed label the page confirms across the field's documents
    weak = set()        # qcore7: fields with a label the page fits once per document, unconfirmed by extracted numbers
    wordset = lambda l: frozenset(re.findall(r"[a-z]{2,}", l.lower()))
    split = {f for (d, f), c in labs_doc.items() if len({wordset(l) for l in c}) > 1}
    variants = _label_variants(pages, bun, by, fmt, labs_doc) if pages is not None and pages.mode != "notext" else None
    for r in bun.records:
        if r.field in out:
            continue
        rows_named = sum(1 for x in keys_of[r.field] if x.entity)
        if rows_named * 2 > len(keys_of[r.field]):
            labs[r.field] = collections.Counter()          # rows found by their entity (an item's name), not by a field label
            if variants:
                variants.pop(r.field, None)
        lab = [labs[r.field].most_common(1)[0][0]] if labs[r.field] else []
        if lab and (pages is None or pages.mode == "notext"):
            # qcore7: with no page to check it, a printed label quoted for under a third of the field's numbers (or once)
            # is not the field's label ('9,025.00 (Cash)' once in 199 receipts): the field name stands in
            n_ = labs[r.field].most_common(1)[0][1]
            if n_ < 2 or 3 * n_ < len(keys_of[r.field]):
                lab = []
        if pages is not None and pages.mode != "notext":
            if r.field in split:
                lab = []      # the field's cells carry different labels in one document (beginning / ending balance)
            full = _label_prints(pages, keys_of[r.field], lab[0], by, fmt) if lab else None
            weak_lab = None
            if lab and not full and not (variants or {}).get(r.field) and r.field not in split:
                weak_lab = _weak_label(pages, keys_of[r.field], lab[0])
            lab = [full] if full else (variants or {}).get(r.field, [])
            if weak_lab:
                lab = [weak_lab]
                weak.add(r.field)
            if full and getattr(_label_prints, "support", 0) >= 3:
                single.add(r.field)     # confirmed in two or more documents: trusted in every document (the read's own
                                        # locality picks the line); a label confirmed in one document only is checked there
        rs = ratio[r.field].most_common(1)
        sc = rs[0][0] if rs else 1.0
        if sc == 1.0 and ratio[r.field]:
            # qcore7: a field stored at the page's unit in most cells and copied as printed in others (555 for
            # 555,000,000 under 'in millions'): the stored unit still holds where a quarter of the cells show it
            tot_ = sum(ratio[r.field].values())
            big_ = [(n_, s_) for s_, n_ in ratio[r.field].items() if s_ > 1 and 4 * n_ >= tot_]
            if big_:
                sc = max(big_)[1]
        stated = False
        if pages is not None:
            try:
                stated = any(pages._scales(d) for d in docs_of[r.field] if pages.has_text(d))
            except Exception:
                stated = False
        money = sc > 1 or (sc == 1 and cur[r.field] > 0 and not stated)
        out[r.field] = B.Field(r.field, lab, "currency" if money else "number", signs.get(r.field, "any"), 1.0,
                               weak=r.field in weak)
    bun.doc_fields = {}
    if pages is not None and pages.mode != "notext":
        bun.doc_fields = _doc_labels(pages, bun, by, fmt, labs_doc, {k: v for k, v in out.items() if k not in single and k not in weak})
    return out


def _page_ratio(pages, doc, page, v) -> Optional[float]:
    """qcore7: the power of 1000 at which `v` is printed on its page under the page's own unit statement: s when the page
    states unit s > 1 and prints v / s (to half a unit), 1 when it prints v itself; None otherwise, or when both."""
    if not page or not pages.has_text(doc):
        return None
    from .pdftext import amount
    got = set()
    for ln in pages.lines(doc):
        if ln.page != page:
            continue
        sc = pages.page_scale(doc, ln.page, ln.top)
        for w in ln.words:
            p = amount(w.text, pages.fmt) if any(ch.isdigit() for ch in w.text) else None
            if p is None or p == 0:
                continue
            if abs(abs(p) - abs(v)) <= 0.5:
                got.add(1.0)
            elif sc > 1 and abs(abs(p) * sc - abs(v)) <= 0.5 * sc:
                got.add(sc)
    return next(iter(got)) if len(got) == 1 else None


def _sign_convention(pages, bun, by, fmt, cites) -> Dict[str, str]:
    """{field: 'pos' | 'neg'} for fields stored with a fixed sign whatever the page prints (a receipt's change printed
    '-16,000' and stored 16000; a discount printed '3,901' and stored -3901): in at least two documents the page prints
    the cited number with the other sign, and nine in ten of the field's extracted numbers have the one sign. A page sign
    is then not taken for the field (as with a declared sign in a hand schema)."""
    from .pdftext import parse_amount
    flip = {"pos": collections.defaultdict(set), "neg": collections.defaultdict(set)}
    npos, nneg = collections.Counter(), collections.Counter()
    for r in bun.records:
        a = by.get(r.key)
        v = a.get("value") if a else None
        if isinstance(v, str):
            from .pdftext import amount
            v = amount(v, fmt)
        if not _num(v) or v == 0:
            continue
        npos[r.field] += v > 0
        nneg[r.field] += v < 0
        c = cites.get(r.key)
        if c is None or not c.bbox or not c.page:
            continue
        try:
            w = pages.word_at(r.doc, c.page, c.bbox)
        except Exception:
            w = None
        if w is None:
            continue
        t = w.text.replace("$", "").strip()
        neg = t.startswith("-") or t.startswith("(") or t.endswith(")") or t.endswith("-")
        p = parse_amount(t, fmt)
        if p is None:
            continue
        if neg and v > 0:
            flip["pos"][r.field].add(r.doc)
        elif not neg and v < 0:
            flip["neg"][r.field].add(r.doc)
    out = {}
    for f in set(npos) | set(nneg):
        n = npos[f] + nneg[f]
        if len(flip["pos"][f]) >= 2 and npos[f] >= 0.9 * n:
            out[f] = "pos"
        elif len(flip["neg"][f]) >= 2 and nneg[f] >= 0.9 * n:
            out[f] = "neg"
    return out


def _fit_lines(pages, doc, lab):
    from .page import label_words, own_line
    ws = label_words(lab)
    if not ws:
        return []
    try:
        return [ln for ln in pages.lines(doc) if own_line(ws, ln.text)]
    except Exception:
        return []


def _weak_label(pages, recs, lab: str) -> Optional[str]:
    """qcore7: the extraction's printed label for a field when the page fits it (every word) on exactly one line that
    prints a number, in every document of the field that has page text, and the printed label of those lines is the
    same; that printed label, else None. No extracted number confirms it (they may all be wrong), so the field is
    `weak`: its page reads change a value only through an identity or as a digit misread of the extracted number."""
    from .page import label_words
    if len(label_words(lab)) < 2:
        return None       # one word ('Total', 'Goodwill' aside) fits too many rows to be trusted without a number
    docs = sorted({r.doc for r in recs})
    seen = set()
    for d in docs:
        if not pages.has_text(d):
            continue
        fit = [ln for ln in _fit_lines_all(pages, d, lab) if re.search(r"\d", ln.text)]
        if len(fit) != 1:
            return None
        full = label_of_printed(fit[0].text)
        if not full:
            return None
        seen.add(full)
    return next(iter(seen)) if len(seen) == 1 else None


def _fit_lines_all(pages, doc, lab):
    """qcore7: the lines that fit the label (as _fit_lines) and print every one of its words."""
    from .page import label_words
    need = set(label_words(lab))
    return [ln for ln in _fit_lines(pages, doc, lab) if need <= set(re.findall(r"[a-z]+", ln.text.lower()))]


def _prints(ln, vals, fmt) -> bool:
    from .pdftext import amount
    for w in ln.words:
        p = amount(w.text.strip().rstrip(",;:"), fmt) if re.search(r"\d", w.text) else None
        if p is not None and p != 0 and any(abs(abs(p) * sc - abs(v)) <= 0.5 * sc for v in vals for sc in (1.0, 1e3, 1e6, 1e9)):
            return True
    return False


def _prints_big(ln, vals, fmt) -> bool:
    """qcore8: the line prints one of the numbers with three significant digits at least, as printed, at a scale of 1,
    1e3, 1e6 or 1e9 (_prints also matches 0.25 to 119,963 at a scale of 1e6, within half the scale)."""
    from .pdftext import amount
    for w in ln.words:
        p = amount(w.text.strip().rstrip(",;:"), fmt) if re.search(r"\d", w.text) else None
        if p is None or p == 0 or abs(p) / (unit(p) or 1) < 100:
            continue
        if any(abs(abs(p) * sc - abs(v)) <= 0.5 * sc for v in vals for sc in (1.0, 1e3, 1e6, 1e9)):
            return True
    return False


def _vals(rs, by, fmt):
    from .pdftext import amount
    out = []
    for r in rs:
        a = by.get(r.key)
        v = a.get("value") if a else None
        if isinstance(v, str):
            v = amount(v, fmt)
        if _num(v) and v != 0:
            out.append(v)
    return out


def _label_variants(pages, bun, by, fmt, labs_doc) -> Dict[str, List[str]]:
    """{field: printed labels} when no one label covers the field (receipts print 'TOTAL', 'Total Due', 'GRAND TOTAL').
    A printed label (from the extraction's printed_text) is kept when, in at least two documents, a page line with its
    words prints that document's extracted number for the field, and it is not validated more often for another field
    (a 'Subtotal' line that prints the total on receipts without tax still belongs to subtotal)."""
    recs_of = collections.defaultdict(list)
    for r in bun.records:
        recs_of[(r.doc, r.field)].append(r)
    ok: Dict[tuple, set] = collections.defaultdict(set)      # (field, normalized label) -> docs
    shown: Dict[tuple, str] = {}
    for (doc, fname), labs in labs_doc.items():
        if not pages.has_text(doc):
            continue
        vals = _vals(recs_of[(doc, fname)], by, fmt)
        if not vals:
            continue
        for lab in labs:
            if any(_prints(ln, vals, fmt) for ln in _fit_lines(pages, doc, lab)):
                lab = re.sub(r"^\s*[\d.,]+\s*[xX@]?\s+", "", lab).strip(" ,;:") or lab   # a count before the label ('3 Items,')
                nl = " ".join(re.findall(r"[a-z]+", lab.lower()))
                if nl:
                    ok[(fname, nl)].add(doc)
                    shown.setdefault((fname, nl), lab)
    by_label = collections.defaultdict(dict)
    for (fname, nl), docs in ok.items():
        by_label[nl][fname] = len(docs)
    out = collections.defaultdict(list)
    for (fname, nl), docs in sorted(ok.items(), key=lambda kv: -len(kv[1])):
        if len(docs) < 2 or max(by_label[nl].values()) > len(docs):
            continue
        out[fname].append(shown[(fname, nl)])
    return dict(out)


def _doc_labels(pages, bun, by, fmt, labs_doc, fields) -> Dict[tuple, B.Field]:
    """{(doc, field): Field}: per document, the field's labels are kept only when they fit one line of the document
    ('TOTAL' also fits 'SUB TOTAL' and 'TOTAL QTY'). When they fit several, the one fitting line that prints the
    field's extracted number decides, if its own printed label fits that line alone; otherwise the field has no label
    in that document and its cells are not read by label there (they stay as extracted unless an identity says
    otherwise)."""
    out = {}
    recs_of = collections.defaultdict(list)
    for r in bun.records:
        recs_of[(r.doc, r.field)].append(r)
    for (doc, fname), rs in recs_of.items():
        f = fields.get(fname)
        if f is None or not pages.has_text(doc):
            continue
        labs = list(f.labels) or ([fname.replace("_", " ")] if not any(r.entity for r in rs) else [])
        if not labs:
            continue
        seen, lines = set(), []
        for lab in labs:
            for ln in _fit_lines(pages, doc, lab):
                if id(ln) not in seen:
                    seen.add(id(ln))
                    lines.append(ln)
        if len(lines) <= 1:
            continue
        vals = _vals(rs, by, fmt)
        hits = [ln for ln in lines if vals and _prints(ln, vals, fmt)]
        got = None
        if hits and getattr(bun, "q8", True):
            # qcore8: several lines fit the label ('Total assets' also fits 'Total current assets'), so qcore7's test
            # (the printed label fits one line with all its words) fails for every total whose words recur in a subtotal.
            # The one line whose printed label is exactly one of the field's labels, when only one line of the
            # document is and it prints one of the field's extracted numbers, decides
            flat = lambda t: re.sub(r"\W+", "", (t or "").lower())
            want = {flat(lab) for lab in labs if flat(lab)}
            exact = [ln for ln in lines if flat(label_of_printed(ln.text)) in want]
            # the line must print an extracted number of the field with three significant digits at least (a share count
            # field whose extraction holds one stray 0.25 matches an earnings-per-share line by that number alone)
            if len(exact) == 1 and exact[0] in hits and _prints_big(exact[0], vals, fmt) \
                    and not any(r.entity and _names_entity(exact[0].text, r.entity) for r in rs):
                cand_ = label_of_printed(exact[0].text)
                # the label read must then land on that line: it is the one best line for the label's words (a long
                # label whose words another, shorter line also prints scores that line higher, and would read it)
                from .page import label_ids, label_words
                try:
                    _, best_ = pages._label_lines(doc, None, label_words(cand_) + label_ids(cand_), B.is_money(f.units))
                except Exception:
                    best_ = []
                # and the first token after the label must not be a footnote marker ('Total other plans (6) $ 86 ...': a
                # small number in parentheses right after the label reads as a cell and shifts every column by one)
                tail_ = exact[0].text.strip()[len(cand_):].split()
                marker = bool(tail_) and re.fullmatch(r"\(\d{1,2}\)|\d{1,2}\)|\*+", tail_[0]) is not None
                if [x[0] for x in best_] == [exact[0]] and not marker:
                    got = cand_
        if len(hits) == 1:
            full = label_of_printed(hits[0].text)
            # qcore7: the printed label must fit one line with every one of its words ('Deferred income tax assets' also
            # fits 'Deferred income tax liabilities' at 60% of its words, but the label read takes the full match)
            if full and len(_fit_lines_all(pages, doc, full)) == 1 and not any(r.entity and _names_entity(full, r.entity) for r in rs):
                got = full
        out[(doc, fname)] = B.Field(fname, [got] if got else [], f.units, f.sign, f.scale, no_label=not got)
    return out


def _names_entity(lab: str, entity: str) -> bool:
    """True when a printed label is the entity's own row label ('1 Peanut & Cheese' for the entity 'Peanut & Cheese #1'):
    then it names the row, not the field, and the field gets no label from it (the entity words find the row)."""
    from .page import entity_words
    ew = set(w.lower() for w in entity_words(entity) or [])
    lw = set(w.lower() for w in re.findall(r"[A-Za-z]{2,}", lab))
    if not ew:
        return lab.strip().lower() == str(entity).strip().lower()
    return len(ew & lw) >= max(1, (len(ew) + 1) // 2)


def _label_prints(pages, recs, lab: str, by: dict, fmt: str) -> Optional[str]:
    """The field's label as the page prints it, or None. The page lines that carry the extraction's label words and print
    the field's extracted number are found for each key; their printed label (the line without its trailing amounts:
    'Trading assets (included assets pledged of $267,907 and $165,927)' for the extraction's 'Trading assets') is the
    label when one such label covers at least half of the field's keys that have a number. Otherwise the extraction's
    label is not trusted (its printed_text may name another line of the page)."""
    from .page import label_words, own_line
    from .pdftext import amount
    ws = label_words(lab)
    if not ws:
        return None
    hit = tot = 0
    found = collections.Counter()
    where = collections.defaultdict(set)       # full label -> distinct page lines that confirm it
    need = set(w.lower() for w in ws if re.fullmatch(r"[A-Za-z]+", w))
    for r in recs:
        a = by.get(r.key)
        v = a.get("value") if a else None
        if isinstance(v, str):
            v = amount(v, fmt)
        if not _num(v) or v == 0 or not pages.has_text(r.doc):
            continue
        tot += 1
        hits = []
        for ln in pages.lines(r.doc):
            # every label word on the line ('Net realized gains' is not 'Net unrealized gains')
            if not own_line(ws, ln.text) or not need <= set(re.findall(r"[a-z]+", ln.text.lower())):
                continue
            for w in ln.words:
                p = amount(w.text.strip().rstrip(",;:"), fmt) if re.search(r"\d", w.text) else None
                if p is not None and p != 0 and any(abs(abs(p) * s - abs(v)) <= 0.5 * s for s in (1.0, 1e3, 1e6, 1e9)):
                    hits.append(ln)
                    break
        # one line only: a number printed on two lines with the label's words (a '1' on two loan types' rows) confirms neither
        ok = len({h.text for h in hits}) == 1
        if ok:
            full = label_of_printed(hits[0].text)
            if full:
                found[full] += 1
                where[full].add((r.doc, hits[0].text))
        hit += ok
    if tot == 0:
        _label_prints.support = 0
        return lab
    top = found.most_common(2)
    best = top[:1]
    _label_prints.support = best[0][1] if best else 0
    if best:
        docs_best = {d for d, _ in where[best[0][0]]}
        if any(lab_ != best[0][0] and docs_best & {d for d, _ in w_} for lab_, w_ in where.items()):
            return None   # another label confirmed for the field's cells in the same document (beginning and ending balances)
    return best[0][0] if best and best[0][1] * 2 >= tot else None


# ---------------------------------------------------------------------------------------------- discovery

_UNIT: Dict[float, Optional[float]] = {}


def unit(v: float) -> Optional[float]:
    if not _num(v) or v == 0:
        return None
    u_ = _UNIT.get(v)          # qcore8: memoized (same result)
    if u_ is None:
        if len(_UNIT) > 1_000_000:
            _UNIT.clear()
        u_ = _UNIT[v] = _unit(v)
    return u_


def _unit(v: float) -> float:
    for e in range(12, -7, -1):
        u = 10.0 ** e
        if abs(v) >= u * 0.999 and abs(v / u - round(v / u)) < 1e-6:
            return u
    return 1e-6


def robust_unit(us: "collections.Counter") -> float:
    """qcore8: the rounding unit of a set of printed numbers, from the units of their last nonzero digits: the finest unit
    that at least two of them (or a tenth) show. qcore7 took the most common unit, so a column of round amounts (3,500,
    1,500, 6,000 beside a total of 39,288) voted a unit of 100 and gave a sum a tolerance 100 times too wide (set E,
    a debt total read from the wrong column then still 'tied'). Trailing zeros are not rounding."""
    tot = sum(us.values())
    need = min(tot, max(2, 0.1 * tot))
    acc = 0
    for u in sorted(us):
        acc += us[u]
        if acc >= need:
            return u
    return max(us)


def _named(entity: Optional[str]) -> bool:
    return bool(entity) and len(re.findall(r"[A-Za-z]", str(entity))) >= 2


@dataclass
class Grid:
    doc: str
    rows: List[tuple]                       # row ids in reading order
    cells: Dict[tuple, Dict[tuple, str]]    # row -> col -> key (an aggregate row: col -> tuple of keys)
    cols: List[tuple]
    label: Dict[tuple, str]


@dataclass
class Stat:
    hold_page: int = 0
    hold: int = 0
    fail: int = 0
    fail_conf: int = 0
    big: int = 0               # holding instances whose target has >= 3 significant digits and >= 2 nonzero terms
    page_full: int = 0         # instances read entirely off the page
    docs_hold: set = dfield(default_factory=set)
    matter: set = dfield(default_factory=set)
    negligible: bool = False
    nontriv: int = 0           # qcore8: holding instances with every term nonzero
    fail_slip: int = 0         # qcore9: failing instances whose gap one slip of one cell explains (Discovery._one_slip)
    loose: int = 0             # qcore9: instances that hold only on the half unit per term (Discovery.stats, tight)
    tight: Optional[float] = None
    targets: set = dfield(default_factory=set)
    tol: Optional[float] = None
    unit: Optional[float] = None

    @property
    def nh(self):
        return self.hold_page + self.hold

    @property
    def nf(self):
        return self.fail + self.fail_conf


class Discovery:
    def __init__(self, bun: B.Bundle, E: Dict[str, float], P: Dict[str, float], labels: Dict[str, str], mode: str,
                 kinds=("vsum", "hsum", "product", "equal"), pos: Optional[Dict[str, tuple]] = None):
        self.pos = pos or {}
        self.bun, self.E, self.P, self.mode, self.kinds = bun, E, P, mode, kinds
        self.S = {k: (P[k] if k in P else E.get(k)) for k in set(E) | set(P)}
        self.recs = {r.key: r for r in bun.records}
        self.labels = labels
        self.grids: Dict[str, Grid] = {}
        by_doc = collections.defaultdict(list)
        for r in bun.records:
            by_doc[r.doc].append(r)
        for doc, rs in by_doc.items():
            self.grids[doc] = self._grid(doc, rs)
        self.row_docs: Dict[tuple, set] = collections.defaultdict(set)
        for doc, g in self.grids.items():
            for row in g.rows:
                self.row_docs[row].add(doc)
        self.memo: Dict[tuple, Stat] = {}
        self.out: List[dict] = []
        self.seen: set = set()

    # ---- the grid
    def _grid(self, doc, rs) -> Grid:
        rows, cells, cols, label = [], {}, [], {}
        last_of = {}
        for r in rs:
            if _named(r.entity):
                row, col = ("e", r.entity), (r.field, r.column if r.column is not None else r.period)
            else:
                row, col = ("f", r.field, r.entity), (None, r.column if r.column is not None else r.period)
            if row not in cells:
                rows.append(row)
                cells[row] = {}
                lab = self.labels.get(r.field) or r.field
                label[row] = (str(r.entity) + " " if row[0] == "e" else "") + (lab if row[0] == "f" else "")
                if row[0] == "f" and r.entity:
                    label[row] = lab + " " + str(r.entity)
            n_ = 0
            while col in cells[row]:
                # the same field twice in one column (a balance at the beginning and at the end of the period): the
                # second occurrence is a row of its own
                n_ += 1
                row = row[:3] + (n_,)
                if row not in cells:
                    rows.append(row)
                    cells[row] = {}
                    label[row] = label[row[:3]] if row[:3] in label else ""
            cells[row][col] = r.key
            if col not in cols:
                cols.append(col)
            if row[0] == "e":
                last_of[r.field] = row
        # aggregate rows: the sum of a field over the named entities, per column, placed after the field's last entity row
        agg: Dict[tuple, Dict[tuple, list]] = collections.defaultdict(lambda: collections.defaultdict(list))
        for row in rows:
            if row[0] != "e":
                continue
            for (f, c), k in cells[row].items():
                agg[("agg", f)][(None, c)].append(k)
        for arow, cs in agg.items():
            if sum(len(v) for v in cs.values()) < 2:
                continue
            at = rows.index(last_of[arow[1]]) + 1 if arow[1] in last_of else len(rows)
            rows.insert(at, arow)
            cells[arow] = {c: tuple(v) for c, v in cs.items()}
            label[arow] = "sum of " + (self.labels.get(arow[1]) or arow[1])
            for c in cs:
                if c not in cols:
                    cols.append(c)
        rows = self._reading_order(rows, cells)
        return Grid(doc, rows, cells, cols, label)

    def _reading_order(self, rows, cells):
        """Rows in the order the page prints them (median page/top of their cells), when the page places most of them;
        a row the page does not place keeps its place after the row before it in the record list."""
        def at(row):
            ps = sorted(self.pos[k] for c, k in cells[row].items() if not isinstance(k, tuple) and k in self.pos)
            return ps[len(ps) // 2] if ps else None
        real = [r for r in rows if r[0] != "agg"]
        placed = {r: at(r) for r in real}
        if not real or sum(v is not None for v in placed.values()) < 0.8 * len(real):
            return rows
        key, prev = {}, (0, -1e9)
        for n, r in enumerate(rows):
            if r[0] == "agg":
                key[r] = prev + (n,)
                continue
            p = placed.get(r)
            if p is None:
                key[r] = prev + (n,)
            else:
                prev = tuple(p)
                key[r] = prev + (n,)
        return sorted(rows, key=lambda r: key[r])

    # ---- values
    def _val(self, src, k):
        if isinstance(k, tuple):
            vs = [src.get(j) for j in k]
            return sum(vs) if all(_num(v) for v in vs) else None
        v = src.get(k)
        return v if _num(v) else None

    def _keys(self, k):
        return list(k) if isinstance(k, tuple) else [k]

    def _null(self, row_cells) -> bool:
        """A row printing no nonzero number (dashes, blanks): it adds nothing to a sum, so it is left out of spans."""
        for k in row_cells.values():
            for src in (self.S, self.E):
                v = self._val(src, k)
                if v not in (None, 0):
                    return False
        return True

    def _page_full(self, ks):
        return all(j in self.P for k in ks for j in self._keys(k))

    def _confirmed(self, ks):
        return all(j in self.P and _num(self.E.get(j)) and abs(self.P[j] - self.E[j]) <= 1e-6 * max(1, abs(self.E[j]))
                   for k in ks for j in self._keys(k))

    # ---- instances of a linear template [(row, coef)] in one grid: one per common column; all single-valued rows: one
    def instances(self, g: Grid, terms: List[tuple], transpose=False):
        cells = self._cells(g, transpose)
        if any(t not in cells for t, _ in terms):
            return []
        sets = [set(cells[t]) for t, _ in terms]
        # the target's columns where some other term is printed; a term with no cell in such a column is not printed
        # there (a dash) and counts as zero
        common = sets[0] & set.union(*sets[1:]) if len(sets) > 1 else set()
        if common:
            order = g.rows if transpose else g.cols
            out = []
            for c in order:
                if c in common:
                    inst = [(cells[t][c], a, t) for t, a in terms if c in cells[t]]
                    if len(inst) >= 2:
                        out.append(inst)
            return out
        if all(len(s) == 1 for s in sets) and not transpose:
            return [[(next(iter(cells[t].values())), a, t) for t, a in terms]]
        return []

    def _cells(self, g, transpose):
        if not transpose:
            return g.cells
        key = ("T", g.doc)
        if key not in self._tcache:
            t = collections.defaultdict(dict)
            for row, cs in g.cells.items():
                if row[0] == "agg":
                    continue
                for c, k in cs.items():
                    t[c][row] = k
            if getattr(self, "q8", True):
                # qcore8: a column is named by its field and its heading without dates ('Q1 2023' and 'Q1 2024' -> 'q') when
                # that name is unique in the document; the same table in every document then pools into one template
                # (qcore7 checked a Total column only within its own document, so a table with two extraction errors
                # lost the template). A name shared by two columns of the document keeps the column's own heading.
                sig = {c: ("N", c[0], self._dur(c[1])) for c in t}
                cnt = collections.Counter(sig.values())
                t = {(sig[c] if cnt[sig[c]] == 1 else c): v for c, v in t.items()}
                self._torder[g.doc] = [(sig[c] if cnt[sig[c]] == 1 else c) for c in g.cols if c in sig]
                # lagged columns (qcore8): in a table of named rows (days, transactions, items), the same column one row up
                # ('LAG', c): running balances and roll-forwards (closing = opening + in - out; opening = last closing)
                ent = [r for r in g.rows if r[0] == "e"]
                if len(ent) >= 3 and len(ent) * 2 >= len([r for r in g.rows if r[0] != "agg"]):
                    for c in list(t):
                        prev, lag = None, {}
                        for r in g.rows:
                            if r in t[c]:
                                if prev is not None:
                                    lag[r] = t[c][prev]
                                prev = r
                        if len(lag) >= 2:
                            t[("LAG", c)] = lag
                for c in t:
                    self.col_docs[c].add(g.doc)
            else:
                self._torder[g.doc] = [c for c in g.cols if c in t]
            self._tcache[key] = t
        return self._tcache[key]

    _tcache: Dict = {}

    def status(self, inst, tol, products=()):
        ks = [e[0] for e in inst] + [k for p in products for k in p[:2]]
        def gap(src):
            s = 0.0
            for k, a, *_ in inst:
                v = self._val(src, k)
                if v is None:
                    return None
                s += a * v
            for k1, k2, a in products:
                x, y = self._val(src, k1), self._val(src, k2)
                if x is None or y is None:
                    return None
                s += a * x * y
            return s
        gE, gS = gap(self.E), gap(self.S)
        if gE is None and gS is None:
            return None
        vals = [self._val(self.S, k) for k in ks] + [self._val(self.E, k) for k in ks]
        if all(v in (None, 0) for v in vals):
            return None
        hS = gS is not None and abs(gS) <= tol
        hE = gE is not None and abs(gE) <= tol
        full = self._page_full(ks)
        if hS and full:
            return "hold_page"
        if hS or hE:
            return "hold"
        if full and self._confirmed(ks):
            return "fail_conf"
        return "fail"

    def tol_of(self, insts, nterms, products=()):
        us = collections.Counter()
        for inst in insts:
            ks = [e[0] for e in inst] + [k for p in products for k in p[:2]]
            for k in ks:
                for src in (self.P, self.E):
                    for j in self._keys(k):
                        u = unit(src.get(j)) if _num(src.get(j)) else None
                        if u:
                            us[u] += 1
        if not us:
            return None
        u = robust_unit(us)
        self._u = u
        return 0.5 * u * max(1, nterms - 1) * 1.0001 + 1e-9

    def big(self, inst):
        tv = self._val(self.S, inst[0][0])
        if tv is None:
            tv = self._val(self.E, inst[0][0])
        if tv is None or tv == 0:
            return False
        u = unit(tv) or 1
        nz = sum(1 for k, *_ in inst[1:] if (self._val(self.S, k) or self._val(self.E, k) or 0) != 0)
        return abs(tv) / u >= 100 and nz >= (1 if len(inst) == 2 else 2)

    def _colfield(self, inst):
        """The kind of column an instance sits in: the field of its target cell when the rows are named entities (a count
        column and a rate column of one table follow different rules), else None."""
        k = inst[0][0]
        k = k[0] if isinstance(k, tuple) else k
        r = self.recs[k]
        return r.field if _named(r.entity) else None

    def stats(self, tkey, terms, transpose=False, docs=None, colfield=None):
        """Stat over every grid that has all the template's rows (columns)."""
        if tkey in self.memo:
            return self.memo[tkey]
        st = Stat()
        if docs is None:
            ds = None
            for t, _ in terms:
                d_ = self.row_docs.get(t) if not transpose else self.col_docs.get(t)
                if d_ is not None:
                    ds = set(d_) if ds is None else ds & d_
            docs = sorted(ds) if ds is not None else sorted(self.grids)
        per = []
        for d in docs:
            per += [(d, i) for i in self.instances(self.grids[d], terms, transpose)]
        if colfield is not None:
            per = [(d, i) for d, i in per if self._colfield(i) == colfield]
        tol = self.tol_of([i for _, i in per], len(terms))
        st.tol = tol
        st.nterms = len(terms)
        st.unit = self._u if tol is not None else None
        st.per = []
        if tol is None:
            self.memo[tkey] = st
            return st
        # qcore9: a sum of four or more printed figures may be off by its rounding (half a unit per term), but a
        # statement that foots is off by a unit at most: a template that holds within one and a half units somewhere is
        # checked to that everywhere (an instance off by two to seven units is then a failing check, a last-digit
        # misread in a long column); otherwise the half unit per term stands
        tight = 1.5 * self._u * 1.0001 + 1e-9 if self.mode == "notext" and 4 <= len(terms) <= TIGHT_MAX and tol > 1.5 * self._u * 1.0001 else None
        if tight is not None:
            exact_ = 0.5 * self._u * 1.0001 + 1e-9
            ss = [(self.status(i, exact_), self.status(i, tight)) for _, i in per]
            # only a template that foots exactly somewhere and is never off by one unit (figures rounded separately: a
            # budget report's thousands with one decimal) is held to it
            # (and only sums of large numbers: counts of a few people with a unit of one hold a sum that misses a small
            # term within half a unit per term, and tightening it fails them all)
            if not any(a in ("hold", "hold_page") for a, _ in ss) or any(a not in ("hold", "hold_page") and b in ("hold", "hold_page")
                                                                        for a, b in ss) \
                    or not all(self.big(i) for (_, i), (a, _) in zip(per, ss) if a in ("hold", "hold_page")):
                tight = None
        st.tight = tight
        for d, inst in per:
            s = self.status(inst, tol)
            if tight is not None and s in ("hold", "hold_page") and self.status(inst, tight) == "fail":
                # admitted on the half unit per term, emitted as a failing check (not where the page reads every cell
                # as extracted: the statement itself is off by its rounding there)
                st.per.append((d, inst, "fail"))
                st.loose += 1
            else:
                st.per.append((d, inst, s))
            if s is None:
                continue
            if self._page_full([e[0] for e in inst]):
                st.page_full += 1
            setattr(st, s, getattr(st, s) + 1)
            if s == "fail" and self._one_slip(inst, tol):
                st.fail_slip += 1
            for k, a, *_ in inst:
                for src in (self.S, self.E):
                    v = self._val(src, k)
                    if v is not None and abs(a * v) > 0.5 * self._u:
                        st.matter.add(_[0] if _ else k)
            if s in ("hold_page", "hold"):
                st.docs_hold.add(d)
                if self.big(inst):
                    st.big += 1
                vs = [self._val(self.S, k) if self._val(self.S, k) is not None else self._val(self.E, k) for k, *_ in inst]
                if all(v not in (None, 0) for v in vs):
                    st.nontriv += 1
                    st.targets.add(round(vs[0], 6))
                for k, a, *_ in []:
                    v = self._val(self.S, k)
                    if v is None:
                        v = self._val(self.E, k)
                    if v is not None and abs(a * v) > tol:
                        st.matter.add(_[0] if _ else k)
        # a term whose value never exceeds the tolerance where the template holds is not evidence (a 0.124 share
        # beside a 14,134 count)
        st.negligible = bool(st.nh) and any(t not in st.matter for t, _ in terms)
        self.memo[tkey] = st
        return st

    def admit(self, st: Stat, cue: bool, relax: bool = True, slip: bool = False) -> bool:
        if st.tol is None or st.negligible:
            return False
        if st.page_full and not st.hold_page:
            return False          # the page reads it whole somewhere and never confirms it
        if st.nh >= 2 and st.big >= 1 and st.nf <= max(1, int(0.1 * (st.nh + st.nf))):
            return True
        if relax and self.relaxed(st):
            st.relaxed = True
            return True
        if relax and slip and self.mode == "notext" and self.slipped(st):
            st.relaxed = True
            return True
        # a total-like target that holds where the page reads every cell: admitted with as many failing instances as
        # holding ones at most (a two-column table with one extraction error)
        if cue and st.big >= 1 and st.nh >= 1 and st.nf <= st.nh and (st.hold_page >= 1 or (not st.page_full and st.nf == 0)
                                                                     or (not self.P and st.nh >= st.nf)):
            return True
        # a total-like target that holds in many documents of the set but not all (documents that print other lines
        # into the total): admitted, instantiated only where it holds
        return cue and st.big >= 3 and st.nh >= 5 and st.nh >= 2 * st.nf

    @staticmethod
    def relaxed(st) -> bool:
        """qcore8: a sum of three or more terms that holds exactly in at least two (three) places with large numbers is not
        a coincidence, however often it fails elsewhere: at 5% extraction errors a ten-line total fails in 40% of its
        instances, and qcore7's 10% failure limit dropped exactly the templates whose failures were the errors. Admitted
        when it holds in two places at least and fails at most twice as often (six or more terms), as often (four or five
        terms), or half as often (three terms, three holds), and the page, where it reads every cell, confirms it at least
        four times as often as it refutes it."""
        n = getattr(st, "nterms", 0)
        if st.fail_conf * 4 > st.hold_page:
            return False
        if n >= 3 and st.big == 0 and st.nontriv >= 6 and len(st.targets) >= 3 and 5 * st.nf <= st.nh + st.nf:
            return True       # small whole numbers (backorder = ordered - shipped) that hold, nonzero, in six places
        if n >= 6:
            return st.big >= 2 and st.nh >= 2 and st.nf <= 2 * st.nh     # six or more terms holding twice: no coincidence
        if n >= 4:
            return st.big >= 2 and st.nh >= 2 and st.nf <= st.nh
        if n == 3:
            return st.big >= 2 and st.nh >= 3 and 2 * st.nf <= st.nh
        return False

    @staticmethod
    def slipped(st) -> bool:
        """qcore9: a sum of two or more terms that holds exactly, with large numbers, in at least as many places as it
        fails, where every failure is one slip of one cell (Discovery._one_slip: one digit, or the sign). A financial
        statement prints two or three columns, so one extraction error in one column left qcore8 one holding instance
        against one failure, too few for a total whose label does not say 'total' ('Cash flows from investing
        activities'). A coincidental sum fails by an arbitrary amount, not by one digit of one of its cells."""
        return getattr(st, "nterms", 0) >= 3 and st.big >= 1 and st.nh >= 1 and st.fail_conf == 0 and st.fail >= 1 \
            and st.fail_slip == st.fail and st.nf <= st.nh

    def _one_slip(self, inst, tol) -> bool:
        """qcore9: the failing instance's gap is closed by changing one of its cells by one slip: one digit replaced (the
        same number of digits at the template's rounding unit, at least three), or the sign flipped. Read off the page
        where it reads the cell, else the extraction."""
        u = getattr(self, "_u", None)
        if not u:
            return False
        for src in (self.S, self.E):
            vs = [self._val(src, k) for k, *_ in inst]
            if any(v is None for v in vs):
                continue
            g = sum(a * v for (k, a, *_), v in zip(inst, vs))
            if abs(g) <= tol:
                continue
            for (k, a, *_), v in zip(inst, vs):
                if not a or isinstance(k, tuple):
                    continue
                w = v - g / a
                if abs(v) > tol and abs(w + v) <= tol:
                    return True       # the sign
                x, y = round(abs(v) / u), round(abs(w) / u)
                if (v < 0) != (w < 0) or abs(abs(w) - y * u) > tol:
                    continue
                sx, sy = str(x), str(y)
                if x >= 100 and len(sx) == len(sy) and sum(p != q for p, q in zip(sx, sy)) == 1:
                    return True       # one digit
        return False

    @staticmethod
    def regular(st):
        """qcore7: a template that holds in many places and fails in few (at least four holds, at least twice the failures): its
        failing instances are emitted in any document, as checks. Discovered identities change a value only with
        independent backing (core.auto_unbacked), so such an instance mostly adds a flag."""
        return st.nh >= 4 and st.nh >= 2 * st.nf

    # ---- qcore7: a total equal to the sum of a field over its entities, in either reading order
    def aggties(self, g: Grid):
        found = 0
        aggs = [r for r in g.rows if r[0] == "agg"]
        for A in aggs:
            for R in g.rows:
                if R[0] == "agg" or (R[0] == "e"):
                    continue
                ca, cr = set(g.cells[A]), set(g.cells[R])
                if not (ca & cr) and not (len(ca) == 1 and len(cr) == 1):
                    continue
                terms = [(R, -1), (A, 1)]
                tkey = ("aggtie", A, R)
                st = self.stats(tkey, terms)
                if not st.per or not self.admit(st, cue_of(g.label.get(R, ""))):
                    continue
                found += self.emit("aggtie", st, g.doc)
        return found

    @staticmethod
    def exact(st):
        """A template that holds nearly everywhere it applies (at most one failing instance, none of them read whole off
        the page): its failing instances are instantiated even in documents where it holds nowhere else."""
        return st.nh >= 3 and st.nf <= 1 and st.fail_conf == 0

    def emit(self, kind, st, doc, transpose=False, products=None):
        """Instantiate an admitted template in one document."""
        n = 0
        for d, inst, s in st.per:
            if d != doc:
                continue
            if s in ("fail", "fail_conf") and doc not in st.docs_hold and not self.exact(st) and not (s == "fail" and self.regular(st)) \
                    and not (s == "fail" and getattr(st, "relaxed", False)):
                continue          # a failing instance in a document where the template never holds: only for an exact template
            terms = []
            net = collections.Counter()
            for k, a, *_ in inst:
                for j in self._keys(k):
                    terms.append((j, float(a)))
                    net[j] += float(a)
            if sum(1 for c in net.values() if abs(c) > 1e-9) < 2:
                continue          # qcore7: a tautology (every term cancels): checks nothing
            prods = [(p[0], p[1], p[2]) for p in (products or {}).get(id(inst), [])]
            sig = (tuple(sorted(terms)), tuple(sorted(prods)))
            if sig in self.seen:
                continue
            tol = st.tol if getattr(st, "tight", None) is None else st.tight
            if s in ("fail", "fail_conf") and getattr(st, "unit", None) and len(inst) >= 3 and getattr(st, "tight", None) is None:
                # off by at most one unit of the last printed digit per input: the printed figures were rounded separately
                # (a total of unrounded parts); its tolerance covers the gap (a misread of the last digit there is missed)
                gs = [sum(a * v for (k, a, *_), v in zip(inst, vs)) for vs in
                      ([self._val(src, k) for k, *_ in inst] for src in (self.S, self.E)) if all(v is not None for v in vs)]
                if gs and min(abs(g) for g in gs) <= st.unit * (len(inst) - 1) * 1.0001:
                    tol = min(abs(g) for g in gs) + 0.5 * st.unit
            self.seen.add(sig)
            self.out.append(dict(id=f"auto.{kind}.{doc}.{len(self.out)}", terms=[list(t) for t in terms], products=[list(p) for p in prods],
                                 const=0.0, tol=tol, kind="within_doc" if len({self.recs[t[0]].doc for t in terms}) == 1 else "cross_doc",
                                 source=kind, support=dict(hold_page=st.hold_page, hold=st.hold, fail=st.fail, fail_confirmed=st.fail_conf),
                                 instance_status=s))
            n += 1
        return n

    # ---- R1 / R2: hierarchical sums
    @staticmethod
    def _patterns(L):
        if L <= 4:
            pats = list(itertools.product((1, -1), repeat=L))
            pats.sort(key=lambda p: (sum(x < 0 for x in p), p[0] < 0))
            return pats
        return [(1,) * L, (1,) + (-1,) * (L - 1)]

    def sums(self, g: Grid, transpose=False):
        kind = "hsum" if transpose else "vsum"
        cells = self._cells(g, transpose)
        order = [c for c in (self._torder.get(g.doc, g.cols) if transpose else g.rows) if c in cells]
        lab = (lambda r: " ".join(str(x) for x in r if x)) if transpose else (lambda r: g.label.get(r, ""))
        active: List[tuple] = []
        found = 0
        subt = set()       # qcore9: rows found to be sums (section totals) in this grid
        for row in order:
            if len(cells[row]) == 0:
                continue
            mycols = set(cells[row])
            single = len(mycols) == 1
            compat = [a for a in active if ((set(cells[a]) & mycols) or (single and len(cells[a]) == 1 and not transpose))
                      and not self._null(cells[a])]
            # qcore8: a row of rates or shares (every number below 1) beside a row of amounts (10 or more) is not a term of
            # its sum: an invoice prints the tax rate between the subtotal and the tax, a budget report a variance % column
            # between the variance and the year-to-date columns
            big_row = self._rowmax(cells[row]) >= 10
            if big_row:
                compat = [a for a in compat if self._rowmax(cells[a]) >= 1]
            best = {}
            spans = [[a] for a in compat if a[0] == "agg"]       # a total of the entity rows above: equal to their sum
            spans += [compat[-L:] for L in range(2, min(len(compat), MAXSPAN) + 1)]
            cue = cue_of(lab(row))
            for span in spans:
                L = len(span)
                if row[0] == "agg" and any(isinstance(c, tuple) and c[0] == row[1] for a in span for c in cells[a]):
                    # qcore7: an aggregate row (a field summed over its entities) equal to a sum of its own entity rows is
                    # a tautology (qcore6 emitted these: x - x = 0 with a wide tolerance; coverage without a check)
                    continue
                for pat in self._patterns(L) + self._more_patterns(g, row, span, transpose):
                    self._try(g, row, list(zip(span, pat)), transpose, kind, cue, best)
            if transpose and ("LAG", row) in cells:
                # qcore8: a running total (balance = last balance + deposits - withdrawals; closing = opening + in - out)
                lagr = ("LAG", row)
                for L in range(1, min(len(compat), 6) + 1):
                    span = compat[-L:]
                    for pat in (self._patterns(L) if L <= 4 else self._more_patterns(g, row, span, transpose, lag=lagr)):
                        self._try(g, row, [(lagr, 1)] + list(zip(span, pat)), transpose, kind, cue, best, lag=True)
            if not best and not transpose and cue and len(compat) >= 2:
                self._cue_sums(g, row, compat, kind, cue, best, lab, subt)
            if best:
                subt.add(row)
                for cf, (_, span, st, _l) in best.items():
                    found += self.emit(kind, st, g.doc)
                top = min(best.values(), key=lambda x: x[0])
                if not top[3]:
                    active = [a for a in active if a not in top[1]]
            elif getattr(self, "q8", True) and compat:
                found += self._sparse(g, row, compat, transpose, kind, cue)
            active.append(row)
        if transpose:
            found += self._lag_ties(g, cells, order)
        return found

    def _rowmax(self, row_cells) -> float:
        m = 0.0
        for k in row_cells.values():
            for src in (self.S, self.E):
                v = self._val(src, k)
                if v is not None:
                    m = max(m, abs(v))
        return m

    def _try(self, g, row, sterms, transpose, kind, cue, best, lag=False, strict=False):
        """Score one candidate (target row = signed span) for every column field; keeps the best per field in `best`."""
        terms = [(row, -1)] + list(sterms)
        cfs = {self._colfield(i) for i in self.instances(g, terms, transpose)} if not transpose else {None}
        for cf in cfs:
            tkey = (kind, tuple(sorted(((t, a) for t, a in terms), key=repr)), g.doc if transpose and not self._pooled(terms) else None, cf)
            st = self.stats(tkey, terms, transpose, docs=[g.doc] if transpose and not self._pooled(terms) else None, colfield=cf)
            pat = [a for _, a in sterms]
            # qcore9: one slipped instance is evidence only for a sum whose signs were not fitted to the document's own
            # instances (spans of up to four terms try every pattern; longer ones only all-plus)
            plain = (len(sterms) <= 4 or all(x > 0 for x in pat)) and not transpose and not lag
            if not self.admit(st, cue, relax=not strict, slip=plain):
                continue
            score = (st.nf, -st.nh, sum(x < 0 for x in pat), len(sterms))
            if cf not in best or score < best[cf][0]:
                best[cf] = (score, [t for t, _ in sterms if not (isinstance(t, tuple) and t and t[0] == "LAG")], st, lag)

    @staticmethod
    def _pooled(terms) -> bool:
        """A transposed template whose columns are all named by signature (or lagged ones) is checked in every document."""
        def pooled(t):
            return isinstance(t, tuple) and t and (t[0] == "N" or (t[0] == "LAG" and isinstance(t[1], tuple) and t[1] and t[1][0] == "N"))
        return all(pooled(t) for t, _ in terms)

    def _inst_vals(self, g, terms, transpose):
        """[(target value, [term values])] for the instances of this document (page read, else extraction)."""
        out = []
        for inst in self.instances(g, terms, transpose):
            vals = {}
            for k, a, t in inst:
                v = self._val(self.S, k)
                if v is None:
                    v = self._val(self.E, k)
                vals[t] = v
            out.append(vals)
        return out

    def _more_patterns(self, g, row, span, transpose, lag=None):
        """qcore8: sign patterns beyond qcore7's (all of them up to four terms; all-plus or first-plus-rest-minus beyond)
        for spans of five to eight terms: every pattern that holds exactly on two of this document's instances (or its
        only one), found from the values first (receipts - issues - adjustments: begin + in + in - out - out = end)."""
        L = len(span)
        if L < 5 or L > 8 or not getattr(self, "q8", True):
            return []
        terms = [(row, -1)] + [(a, 1) for a in span] + ([(lag, 1)] if lag else [])
        ivs = self._inst_vals(g, terms, transpose)
        ivs = [iv for iv in ivs if iv.get(row) is not None and all(iv.get(a) is not None for a in span)
               and (lag is None or iv.get(lag) is not None)]
        if not ivs:
            return []
        us = collections.Counter(unit(v) for iv in ivs for v in iv.values() if v)
        if not us:
            return []
        tol = 0.5 * robust_unit(us) * L * 1.0001 + 1e-9
        legacy = set(self._patterns(L))
        out = []
        for pat in itertools.product((1, -1), repeat=L):
            if pat in legacy:
                continue
            n = sum(1 for iv in ivs if abs(sum(p * iv[a] for p, a in zip(pat, span)) + (iv[lag] if lag else 0) - iv[row]) <= tol)
            if n >= min(2, len(ivs)):
                out.append(pat)
        return out[:4]

    def _cue_sums(self, g, row, compat, kind, cue, best, lab, subt=()):
        """qcore9: a total-like row that no contiguous span explains, as the sum of the last two to five total-like rows
        above it plus up to two plain rows printed right above it (net change in cash = operating + investing +
        financing totals + the exchange-rate line; total liabilities and equity = total liabilities + a mezzanine line
        + total equity) -- the section totals whose own line items are still in the active list because an extraction
        error hid their sum. All plus, or every sign pattern up to three terms (operating income = total revenue -
        total expenses). Admitted as any sum (holds, or one slip per failing instance); the terms then leave the active
        list as for a contiguous span (an all-plus one; a difference of totals, equity = total assets - total
        liabilities, leaves them for the totals below). A section total is a row found to be a sum above, or one whose
        label starts with a total word ('Total ...', 'Net cash ...'; not '..., net')."""
        contiguous = {tuple(compat[-L:]) for L in range(1, len(compat) + 1)}
        sect = lambda a: a in subt or bool(_LEAD_CUE.match(lab(a) or ""))
        for k in (0, 1, 2):
            tail = compat[len(compat) - k:] if k else []
            if any(cue_of(lab(a)) for a in tail):
                continue
            head = [a for a in compat[:len(compat) - k] if sect(a)]
            for m in range(2, 6):
                if len(head) < m:
                    break
                span = head[-m:] + tail
                if tuple(span) in contiguous:
                    continue
                L = len(span)
                pats = self._patterns(L) if L <= 3 else [(1,) * L]
                for pat in pats:
                    self._try(g, row, list(zip(span, pat)), False, kind, cue, best, lag=any(x < 0 for x in pat))

    def _sparse(self, g, row, compat, transpose, kind, cue) -> int:
        """qcore8: a target that no contiguous span explains: two or three of the last eight rows above it (remaining =
        annual - year to date, three columns apart; a total two lines below its subtotal). Found from the values (two holding
        instances in this document, or its only one), admitted only on qcore7's strict rule (holds in two places, fails in
        at most a tenth), never displaces the rows above from later sums, and one per target."""
        K = compat[-8:]
        if len(K) < 3:
            return 0
        contiguous = {tuple(K[-L:]) for L in range(1, len(K) + 1)}
        cand = []
        allterms = [(row, -1)] + [(a, 1) for a in K]
        ivs = self._inst_vals(g, allterms, transpose)
        for n in (2, 3):
            for sub in itertools.combinations(K, n):
                if tuple(sub) in contiguous:
                    continue
                for pat in itertools.product((1, -1), repeat=n):
                    hits = tot = 0
                    for iv in ivs:
                        if iv.get(row) is None or any(iv.get(a) is None for a in sub):
                            continue
                        vs = [iv[a] for a in sub] + [iv[row]]
                        if not all(vs):
                            continue
                        tot += 1
                        u = max(unit(v) or 1e-6 for v in vs)
                        hits += abs(sum(p * iv[a] for p, a in zip(pat, sub)) - iv[row]) <= 0.5 * u * n * 1.0001
                    if hits >= 2 or (hits == 1 and tot == 1):
                        cand.append(list(zip(sub, pat)))
        if not cand or len(cand) > 6:
            return 0          # nothing, or too many coincidences to trust any
        best = {}
        for st_ in cand:
            self._try(g, row, st_, transpose, kind, cue, best, strict=True)
        n = 0
        for cf, (_, span, st, _l) in best.items():
            if st.big >= 2:
                n += self.emit(kind + "_sparse", st, g.doc)
        return n

    def _lag_ties(self, g, cells, order) -> int:
        """qcore8: one column equal to another column one row up (opening census = last midnight census; beginning balance =
        last ending balance) in a table of named rows: holds in two places at least, fails in at most a tenth."""
        n = 0
        for a in order:
            for b in order:
                lagb = ("LAG", b)
                if a == b or lagb not in cells or a not in cells:
                    continue
                terms = [(a, -1), (lagb, 1)]
                ivs = self._inst_vals(g, terms, True)
                if sum(1 for iv in ivs if iv.get(a) and iv.get(lagb) is not None and abs(iv[a] - iv[lagb]) < 1e-9) < 1:
                    continue
                pooled = self._pooled(terms)
                tkey = ("lagtie", a, b, None if pooled else g.doc)
                st = self.stats(tkey, terms, True, docs=None if pooled else [g.doc])
                if st.tol is None or st.negligible or st.nh < 2 or st.big < 2 or st.nf > max(1, int(0.1 * (st.nh + st.nf))) or st.fail_conf:
                    continue
                n += self.emit("lagtie", st, g.doc)
        return n

    # ---- R3: products, per row
    def products(self):
        found = 0
        tmpl = collections.defaultdict(list)     # (ca, cb, cc) -> [(doc, row)]
        for d, g in self.grids.items():
            for row in g.rows:
                if row[0] == "agg":
                    continue
                cs = [c for c in g.cells[row]]
                if len(cs) < 3:
                    continue
                for a, b in itertools.combinations(cs, 2):
                    for c in cs:
                        if c in (a, b):
                            continue
                        tmpl[(a, b, c)].append((d, row))
        for (a, b, c), where in tmpl.items():
            if len(where) < 2:
                continue
            st, per = Stat(), []
            insts = []
            for d, row in where:
                cs = self.grids[d].cells[row]
                insts.append((d, cs[a], cs[b], cs[c]))
            us = [unit(self.S.get(x[3])) or unit(self.E.get(x[3])) for x in insts]
            us = [u for u in us if u]
            if not us:
                continue
            tol = 0.5 * robust_unit(collections.Counter(us)) * 1.0001 + 1e-9
            st, per = self._prod_eval(insts, lambda d, ka, kb, kc: tol)
            if not (st.big >= 2 and self._padmit(st)):
                # qcore8: a rate printed rounded (12.3%, 0.0663) times a base: the product is off by up to half the rate's
                # last unit times the base (variance % x budget = variance; occupancy x beds = census). The rate's
                # rounding is carried into the tolerance, per instance; only for a factor whose numbers are all below 1
                rf = self._rate_factor(insts)
                if rf is None:
                    continue
                ur = robust_unit(collections.Counter(u for x in insts for u in [unit(self._val(self.S, x[rf]) or self._val(self.E, x[rf]) or 0)] if u))
                other = 2 if rf == 1 else 1

                def tolf(d, ka, kb, kc, ur=ur, other=other):
                    v = self._val(self.S, (None, ka, kb)[other]) or self._val(self.E, (None, ka, kb)[other]) or 0
                    return tol + 0.5 * ur * abs(v) * 1.0001
                st, per = self._prod_eval(insts, tolf)
                if not (st.big >= 2 and self._padmit(st)):
                    continue
            for d, inst, prods, s, tol_i in per:
                if s in ("fail", "fail_conf") and d not in st.docs_hold and not self.exact(st) and not (s == "fail" and st.big >= 3):
                    continue
                sig = (tuple(sorted(inst)), tuple(sorted(prods)))
                if sig in self.seen:
                    continue
                self.seen.add(sig)
                self.out.append(dict(id=f"auto.product.{d}.{len(self.out)}", terms=[list(t) for t in inst], products=[list(p) for p in prods],
                                     const=0.0, tol=tol_i, kind="within_doc", source="product",
                                     support=dict(hold_page=st.hold_page, hold=st.hold, fail=st.fail, fail_confirmed=st.fail_conf),
                                     instance_status=s))
                found += 1
        return found

    def row_products(self) -> int:
        """qcore8: a row equal to a rate row times another row of the same column (tax = tax rate x taxable subtotal), the
        three rows named by their fields, so the template is pooled over the documents that print them. Found from the
        values (the product ties within half a unit of the target plus the rate's rounding times the base); admitted as
        products are (_padmit) with large factors in three places at least."""
        cand = collections.defaultdict(set)
        for d, g in self.grids.items():
            frows = [row for row in g.rows if row[0] == "f"]
            if len(frows) < 3:
                continue
            bycol = collections.defaultdict(dict)
            for row in frows:
                for c, k in g.cells[row].items():
                    bycol[c][row] = k
            for c, m in bycol.items():
                vals = {row: self._val(self.S, k) for row, k in m.items()}
                vals = {row: v for row, v in vals.items() if v is not None and v != 0}
                rates = [row for row, v in vals.items() if abs(v) < 1]
                if not rates or len(vals) < 3:
                    continue
                for ra in rates:
                    for rb, vb in vals.items():
                        if rb == ra or abs(vb) < 10:
                            continue
                        prod = vals[ra] * vb
                        ur = unit(vals[ra]) or 1e-6
                        for rc, vc in vals.items():
                            if rc in (ra, rb) or abs(vc) < 1:
                                continue
                            tol = 0.5 * (unit(vc) or 1) * 1.0001 + 0.5 * ur * abs(vb) * 1.0001
                            if abs(vc - prod) <= tol and abs(vc) / (unit(vc) or 1) >= 10:
                                cand[(ra[1:], rb[1:], rc[1:])].add(d)
        rmap = {d: {row[1:]: row for row in g.rows if row[0] == "f"} for d, g in self.grids.items()}
        found = 0
        for t, docs in cand.items():
            if len(docs) < 2:
                continue
            insts = []
            for d in sorted(self.grids):
                rs_ = [rmap[d].get(x) for x in t]
                if None in rs_:
                    continue
                ca, cb, cc = (self.grids[d].cells[r_] for r_ in rs_)
                for c in ca:
                    if c in cb and c in cc:
                        insts.append((d, ca[c], cb[c], cc[c]))

            def tolf(d, ka, kb, kc):
                va = self._val(self.S, ka) if self._val(self.S, ka) is not None else self._val(self.E, ka)
                vb = self._val(self.S, kb) if self._val(self.S, kb) is not None else self._val(self.E, kb)
                vc = self._val(self.S, kc) if self._val(self.S, kc) is not None else self._val(self.E, kc)
                return 0.5 * (unit(vc) or 1) * 1.0001 + 0.5 * (unit(va) or 1e-6) * abs(vb or 0) * 1.0001 + 1e-9
            st, per = self._prod_eval(insts, tolf)
            if not (st.big >= 3 and self._padmit(st)):
                continue
            for d, inst, prods, s_, tol_i in per:
                if s_ in ("fail", "fail_conf") and d not in st.docs_hold and not self.exact(st) and not (s_ == "fail" and st.big >= 3):
                    continue
                sig = (tuple(sorted(inst)), tuple(sorted(prods)))
                if sig in self.seen:
                    continue
                self.seen.add(sig)
                self.out.append(dict(id=f"auto.rowprod.{d}.{len(self.out)}", terms=[list(x) for x in inst], products=[list(p) for p in prods],
                                     const=0.0, tol=tol_i, kind="within_doc", source="rowprod",
                                     support=dict(hold_page=st.hold_page, hold=st.hold, fail=st.fail, fail_confirmed=st.fail_conf),
                                     instance_status=s_))
                found += 1
        return found

    def _padmit(self, st) -> bool:
        """Products: qcore7's rule, or (qcore8) a product that holds with large factors in three places at least and fails
        in at most half as many (at 5% extraction errors a quantity x price x amount row fails one time in seven)."""
        if self.admit(st, False, relax=False):
            return True
        if st.tol is None or (st.page_full and not st.hold_page) or st.fail_conf * 4 > st.hold_page:
            return False
        return st.big >= 3 and st.nh >= 3 and 2 * st.nf <= st.nh

    def _prod_eval(self, insts, tolf):
        st, per = Stat(), []
        for d, ka, kb, kc in insts:
            prods = [(ka, kb, 1.0)]
            inst = [(kc, -1.0)]
            t_ = tolf(d, ka, kb, kc)
            s = self.status(inst, t_, prods)
            if s is None:
                continue
            per.append((d, inst, prods, s, t_))
            if self._page_full([ka, kb, kc]):
                st.page_full += 1
            setattr(st, s, getattr(st, s) + 1)
            if s in ("hold", "hold_page"):
                st.docs_hold.add(d)
                x, y = self._val(self.S, ka), self._val(self.S, kb)
                if x not in (None, 0, 1, -1) and y not in (None, 0, 1, -1):
                    st.big += 1
        st.tol = max([p[4] for p in per] or [0.0]) or None
        return st, per

    def _rate_factor(self, insts) -> Optional[int]:
        """1 or 2 (the index of factor a or b in the instance tuples) when every nonzero number of that factor (two at least)
        is a fraction below 1 in magnitude (a rate or share); None otherwise."""
        for i in (1, 2):
            vs = [self._val(self.S, x[i]) if self._val(self.S, x[i]) is not None else self._val(self.E, x[i]) for x in insts]
            vs = [v for v in vs if v is not None and v != 0]
            if len(vs) >= 2 and all(abs(v) < 1 for v in vs):
                return i
        return None

    # ---- R5: the same number printed on two rows
    def _words(self, k):
        r = self.recs[k]
        t = (self.labels.get(r.field) or r.field.split(":")[-1]) + " " + str(r.entity or "")
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
        return {w for w in re.findall(r"[a-z]{4,}", t.lower()) if w not in _STOP}

    @staticmethod
    def _dur(col):
        s = re.sub(r"\d", " ", str(col or "").lower())
        s = re.sub(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|"
                   r"jun|jul|aug|sep|sept|oct|nov|dec)\b", " ", s)
        return " ".join(re.findall(r"[a-z]+", s))

    @staticmethod
    def _same_years(a, b):
        ya, yb = set(re.findall(r"(?:19|20)\d\d", str(a))), set(re.findall(r"(?:19|20)\d\d", str(b)))
        return not ya or not yb or ya == yb

    def equal(self):
        recs = self.recs
        rowof = {}
        for d, g in self.grids.items():
            for row in g.rows:
                if row[0] == "agg":
                    continue
                for c, k in g.cells[row].items():
                    rowof[k] = (d, row)
        buckets = collections.defaultdict(set)
        for src in (self.S, self.E):
            for k, v in src.items():
                if _num(v) and v != 0 and k in rowof:
                    u = unit(v) or 1
                    if abs(v) / u >= 100:
                        buckets[round(v, 6)].add(k)
        pairs = collections.defaultdict(set)
        for v, ks in buckets.items():
            ks = sorted(ks)
            if len(ks) > 12:
                continue
            for a, b in itertools.combinations(ks, 2):
                ra, rb = recs[a], recs[b]
                if rowof[a] == rowof[b]:
                    continue
                if ra.period != rb.period:
                    continue
                if ra.column is not None and rb.column is not None and ra.column != rb.column:
                    da, db = self._dur(ra.column), self._dur(rb.column)
                    if da and db and da != db or not self._same_years(ra.column, rb.column):
                        continue
                if ra.field != rb.field and not (self._words(a) & self._words(b)):
                    continue
                pr = tuple(sorted((rowof[a], rowof[b]), key=repr))
                pairs[pr].add((a, b) if rowof[a] == pr[0] else (b, a))
        found = 0
        for (ra_, rb_), seeds in pairs.items():
            ga, gb = self.grids[ra_[0]], self.grids[rb_[0]]
            ca, cb = ga.cells[ra_[1]], gb.cells[rb_[1]]
            insts = []
            for c1, k1 in ca.items():
                for c2, k2 in cb.items():
                    r1, r2 = recs[k1], recs[k2]
                    if r1.period != r2.period:
                        continue
                    if r1.column is not None and r2.column is not None and r1.column != r2.column:
                        d1, d2 = self._dur(r1.column), self._dur(r2.column)
                        if d1 and d2 and d1 != d2 or not self._same_years(r1.column, r2.column):
                            continue
                    insts.append([(k1, 1.0), (k2, -1.0)])
            if not insts:
                continue
            tol = self.tol_of(insts, 2)
            if tol is None:
                continue
            st, per = Stat(), []
            for inst in insts:
                s = self.status(inst, tol)
                if s is None:
                    continue
                per.append((inst, s))
                if self._page_full([e[0] for e in inst]):
                    st.page_full += 1
                setattr(st, s, getattr(st, s) + 1)
                if s in ("hold", "hold_page") and self.big(inst):
                    st.big += 1
                if s == "fail" and self._one_slip(inst, tol):
                    st.fail_slip += 1
            st.tol = tol
            k0 = next(iter(seeds))
            shared_words = len(self._words(k0[0]) & self._words(k0[1]))
            same_field = recs[k0[0]].field == recs[k0[1]].field
            if not self.P and ra_[0] == rb_[0]:
                # no page: a value the extraction copied onto a second row would tie with itself. qcore9: two total rows
                # of one statement, not adjacent, equal in two or more columns that print different numbers (total
                # assets and total liabilities and equity) are not a copy
                g_ = self.grids[ra_[0]]
                vals_ = {round(self._val(self.E, i[0][0]) or 0, 6) for i, s_ in per if s_ in ("hold", "hold_page")}
                if not (st.nh >= 2 and st.nf == 0 and st.big >= 2 and len(vals_) == st.nh
                        and cue_of(g_.label.get(ra_[1], "")) and cue_of(g_.label.get(rb_[1], ""))
                        and abs(g_.rows.index(ra_[1]) - g_.rows.index(rb_[1])) > 1):
                    continue
            ok = (self.admit(st, False) and st.nf == 0) or (st.nh == 1 and st.nf == 0 and st.big >= 1 and (shared_words >= 2 or same_field)
                                            and (st.hold_page >= 1 or not st.page_full))
            # qcore9: a tie (net income on the income statement and the cash-flow statement) that holds in one period
            # and is off by one slip of one cell (a digit, or the sign) in the others: the extraction misread it there
            ok = ok or (st.nh >= 1 and st.big >= 1 and st.fail >= 1 and st.fail == st.fail_slip and st.fail_conf == 0
                        and st.nf <= st.nh and (same_field or shared_words >= 1))
            if not ok:
                continue
            for inst, s in per:
                if s == "fail_conf" and not st.nh:
                    continue
                if s == "fail_conf" and st.nh < 2:
                    continue
                sig = (tuple(sorted(inst)), ())
                if sig in self.seen:
                    continue
                self.seen.add(sig)
                self.out.append(dict(id=f"auto.equal.{recs[inst[0][0]].doc}.{len(self.out)}", terms=[list(t) for t in inst], products=[],
                                     const=0.0, tol=tol, kind="within_doc" if recs[inst[0][0]].doc == recs[inst[1][0]].doc else "cross_doc",
                                     source="equal", support=dict(hold_page=st.hold_page, hold=st.hold, fail=st.fail, fail_confirmed=st.fail_conf),
                                     instance_status=s))
                found += 1
        return found

    # ---- qcore8: the same number carried from one document to a later one (closing -> next opening)
    def chains(self) -> int:
        """A cell carried into a later document under another period (a closing balance as the next statement's opening
        balance, a meter's current reading as the next bill's previous reading, a year-to-date column as next month's prior
        figure). Cells are named by their row and their column heading without dates; a template is (cell A, cell B, n):
        A's value in a document equals B's value in the n-th later document that prints B. Found from equal values (at least
        three significant digits); admitted when it holds in at least two document pairs, with large numbers, and fails in
        at most a third of them and never where the page reads both cells and agrees with the extraction."""
        docs = sorted(self.grids)
        pos_ = {d: n for n, d in enumerate(docs)}
        at = collections.defaultdict(dict)        # sig -> {doc: key}
        sig_of = {}
        dup = set()
        for d in docs:
            g = self.grids[d]
            for row in g.rows:
                if row[0] == "agg":
                    continue
                for c, k in g.cells[row].items():
                    sg = (row, c[0], self._dur(c[1]))
                    if d in at[sg]:
                        dup.add(sg)
                    at[sg][d] = k
                    sig_of[k] = sg
        for sg in dup:
            at.pop(sg, None)
        seq = {sg: sorted(m, key=pos_.get) for sg, m in at.items()}
        buckets = collections.defaultdict(set)
        for src in (self.S, self.E):
            for k, v in src.items():
                if _num(v) and v != 0 and k in sig_of and sig_of[k] in at:
                    u = unit(v) or 1
                    if abs(v) / u >= 100:
                        buckets[round(v, 6)].add(k)
        cand = collections.defaultdict(int)
        for v, ks in buckets.items():
            if len(ks) > 8:
                continue
            for a, b in itertools.combinations(sorted(ks), 2):
                ra, rb = self.recs[a], self.recs[b]
                if ra.doc == rb.doc or ra.period == rb.period:
                    continue           # one document, or one period: equal() handles those
                if pos_[ra.doc] > pos_[rb.doc]:
                    a, b, ra, rb = b, a, rb, ra
                sa, sb = sig_of[a], sig_of[b]
                if sa == sb:
                    continue
                later = [d for d in seq[sb] if pos_[d] > pos_[ra.doc]]
                if rb.doc not in later:
                    continue
                cand[(sa, sb, later.index(rb.doc) + 1)] += 1
        n = 0
        for (sa, sb, step), cnt in cand.items():
            if cnt < 2:
                continue
            insts = []
            for d in seq[sa]:
                later = [x for x in seq[sb] if pos_[x] > pos_[d]]
                if len(later) >= step:
                    insts.append([(at[sa][d], 1.0), (at[sb][later[step - 1]], -1.0)])
            tol = self.tol_of(insts, 2)
            if tol is None:
                continue
            st, per = Stat(), []
            for inst in insts:
                s_ = self.status(inst, tol)
                if s_ is None:
                    continue
                per.append((inst, s_))
                setattr(st, s_, getattr(st, s_) + 1)
                if s_ in ("hold", "hold_page") and self.big(inst):
                    st.big += 1
            if st.nh < 2 or st.big < 2 or st.fail_conf or 3 * st.nf > st.nh:
                continue
            for inst, s_ in per:
                sig = (tuple(sorted(inst)), ())
                if sig in self.seen:
                    continue
                self.seen.add(sig)
                self.out.append(dict(id=f"auto.chain.{self.recs[inst[0][0]].doc}.{len(self.out)}", terms=[list(t) for t in inst], products=[],
                                     const=0.0, tol=tol, kind="cross_doc", source="chain",
                                     support=dict(hold_page=st.hold_page, hold=st.hold, fail=st.fail, fail_confirmed=st.fail_conf),
                                     instance_status=s_))
                n += 1
        return n

    # ---- qcore8: period accumulation across documents (year to date = last report's year to date + this period)
    def accum(self) -> int:
        """In a document X and the previous document Y that prints the same rows (by name), a column of X equal, row by
        row, to a column of Y plus another column of X: year to date = previous year to date + quarter (an income statement's
        nine months = six months + three months; a budget report's YTD actual = last month's YTD actual + this month).
        Columns are triples (X1, X2, Y1) found from the values (holding on two rows at least); a triple is admitted when it
        holds on at least three rows with large numbers and fails on at most half as many."""
        docs = sorted(self.grids)
        rows_of = {d: {r for r in self.grids[d].rows if r[0] != "agg"} for d in docs}
        n = 0
        for i, X in enumerate(docs):
            gx = self.grids[X]
            Y = None
            for j in range(i - 1, -1, -1):
                if len(rows_of[docs[j]] & rows_of[X]) >= max(3, len(rows_of[X]) // 2):
                    Y = docs[j]
                    break
            if Y is None:
                continue
            gy = self.grids[Y]
            shared = [r for r in gx.rows if r in rows_of[Y]]

            def colmap(g, r):
                out = {}
                for c, k in g.cells[r].items():
                    v = self._val(self.S, k)
                    if v is None:
                        v = self._val(self.E, k)
                    out[c] = (k, v)
                return out
            vx = {r: colmap(gx, r) for r in shared}
            vy = {r: colmap(gy, r) for r in shared}
            cx = sorted({c for r in shared for c in vx[r]}, key=repr)
            cy = sorted({c for r in shared for c in vy[r]}, key=repr)
            if len(cx) < 2 or len(cx) * len(cx) * len(cy) > 4000:
                continue
            for c1 in cx:
                for c2 in cx:
                    if c1 == c2 or (c1[0] is not None and c1[0] == c2[0] and False):
                        continue
                    for c3 in cy:
                        hits = 0
                        insts = []
                        for r in shared:
                            a, b, c = vx[r].get(c1), vx[r].get(c2), vy[r].get(c3)
                            if not (a and b and c) or a[0] == b[0]:
                                continue
                            insts.append([(a[0], -1.0), (b[0], 1.0), (c[0], 1.0)])
                            if None not in (a[1], b[1], c[1]) and a[1] and b[1] and c[1]:
                                us = [unit(x) or 1 for x in (a[1], b[1], c[1])]
                                hits += abs(b[1] + c[1] - a[1]) <= max(us) * 1.0001
                        if hits < 2:
                            continue
                        tol = self.tol_of(insts, 3)
                        if tol is None:
                            continue
                        st, per = Stat(), []
                        for inst in insts:
                            s_ = self.status(inst, tol)
                            if s_ is None:
                                continue
                            per.append((inst, s_))
                            setattr(st, s_, getattr(st, s_) + 1)
                            if s_ in ("hold", "hold_page") and self.big(inst):
                                st.big += 1
                        if st.nh < 3 or st.big < 2 or 2 * st.nf > st.nh or st.fail_conf * 4 > max(1, st.hold_page):
                            continue
                        for inst, s_ in per:
                            sig = (tuple(sorted(inst)), ())
                            if sig in self.seen:
                                continue
                            self.seen.add(sig)
                            self.out.append(dict(id=f"auto.accum.{X}.{len(self.out)}", terms=[list(t) for t in inst], products=[],
                                                 const=0.0, tol=tol, kind="cross_doc", source="accum",
                                                 support=dict(hold_page=st.hold_page, hold=st.hold, fail=st.fail, fail_confirmed=st.fail_conf),
                                                 instance_status=s_))
                            n += 1
        return n

    def run(self) -> List[dict]:
        self._tcache = {}
        self._torder = {}
        self.col_docs = collections.defaultdict(set)
        if "hsum" in self.kinds:
            for d in sorted(self.grids):
                self._cells(self.grids[d], True)
        rep = collections.Counter()
        for d in sorted(self.grids):
            g = self.grids[d]
            if "vsum" in self.kinds:
                rep["vsum"] += self.sums(g, False)
            if "hsum" in self.kinds:
                rep["hsum"] += self.sums(g, True)
            if "vsum" in self.kinds:
                rep["aggtie"] += self.aggties(g)
        if "product" in self.kinds:
            rep["product"] += self.products()
            if getattr(self, "q8", True):
                rep["rowprod"] += self.row_products()
        if "equal" in self.kinds:
            rep["equal"] += self.equal()
            if getattr(self, "q8", True):
                rep["chain"] += self.chains()
                rep["accum"] += self.accum()
        self.report = dict(rep)
        return self.out


def page_reads(bun: B.Bundle, pages, cites) -> Dict[str, float]:
    """{key: the page's read of the key's own cell, in stored units} (qcore's own-line read; none in notext)."""
    from .core import diagnose
    import copy
    b0 = copy.copy(bun)
    b0.identities = []
    info = {"reads_only": True}
    diagnose(b0, pages, cites, info=info)
    own = info.get("own")
    out = {}
    for r in bun.records:
        try:
            o = own(r.key)
        except Exception:
            o = None
        if o is not None and _num(o[0]):
            out[r.key] = (float(o[0]) * float(o[4] or 1.0), (r.doc, o[1], tuple(o[2])))
    spots = collections.Counter(v[1] for v in out.values())
    # a page spot read for two keys (two labels matched to one line) says nothing about either
    P = {k: v[0] for k, v in out.items() if spots[v[1]] == 1}
    pos = {k: (v[1][1], v[1][2][1]) for k, v in out.items() if spots[v[1]] == 1}
    for k, c in cites.items():
        if k not in pos and c.page and c.bbox:
            pos[k] = (c.page, c.bbox[1])
    return P, pos


def discover(bun: B.Bundle, pages, cites, kinds=("vsum", "hsum", "product", "equal")) -> Tuple[List[B.Identity], dict]:
    E = {k: c.value for k, c in cites.items() if _num(c.value)}
    P, pos = page_reads(bun, pages, cites) if pages.mode != "notext" else ({}, {})
    labels = {n: (f.labels[0] if f.labels else "") for n, f in bun.fields.items()}
    D = Discovery(bun, E, P, labels, pages.mode, kinds, pos)
    rows = D.run()
    ids = B.parse_identities(rows, {r.key for r in bun.records})
    for i, row in zip(ids, rows):
        i.source = row.get("source")
        i.support = row.get("support")      # qcore7: hold / fail counts of the template (strong_auto)
    info = dict(report=D.report, n=len(rows), page_reads=len(P), rows=rows)
    return ids, info
