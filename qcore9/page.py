"""What the page prints: the cited spot, the field's own line (label read), column, scale and sign.

Everything here works from the PDF text layer alone. In "notext" mode every read returns
None (arithmetic only).
"""

from __future__ import annotations

import re
from statistics import median
from typing import Dict, List, Optional, Tuple

from . import pdftext
from .pdftext import amount, parse_amount

_NUMLIKE = re.compile(r"\d|^[-–—]$")
_RANGE_TOK = re.compile(r"\d{1,3}\s?[-–]\s?\d{1,3}")
SCALE_RX = re.compile(r"\bin\s+(thousands|millions|billions)\b|\(\s*(thousands|millions|billions)\b[^()]*\)|\(\s*\$\s*0{3}'?s?\s*\)|\$\s*0{3}'?s?(?![\d,])|\(\s*0{3}'?s\s*\)"
                      r"|\b0{3}'?s\s+omitted|\b(thousands|millions|billions)\s+of\s+(u\.?\s?s\.?\s+)?dollars", re.I)   # qcore5: billions
_NEG_TOKEN = re.compile(r"^\$?\(|^-\$?\d|^\$?-\d|\d\)?-$")
_DASH = re.compile(r"^[-–—]$")
_WORD = re.compile(r"[A-Za-z]{2,}")


def _strip_punct(t: str) -> str:
    """'(28,488),' -> '(28,488)': sentence punctuation after a number in prose."""
    return re.sub(r"(?<=[\d)%])[,.;:]+$", "", t)


def prose(ln, words, nums, anywhere: bool = False) -> bool:
    """Running prose, not a table row: after the label starts (anywhere on the line, for a record with no
    column), some value number is followed by at least two words of three or more letters."""
    alpha = split_words(words)[0]
    ws = ln.words
    starts = [n for n, w in enumerate(ws) if alpha and any(t in alpha for t in _norm(w.text))]
    if not starts:
        return False
    boxes = {n[2] for n in nums}
    for n in range(0 if anywhere else starts[0], len(ws)):
        if (ws[n].x0, ws[n].top, ws[n].x1, ws[n].bottom) in boxes:
            if sum(bool(re.fullmatch(r"[A-Za-z]{3,}[,.;:]?", x.text)) for x in ws[n + 1:]) >= 2:
                return True
    return False


# ---- small helpers (pure)

_UNREAD = ("unread",)    # _spot: the label's line was found but no single number could be read from it

_YEAR = re.compile(r"^\(?(19|20)\d{2}[),.;:]?$")


_MONTH = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?$", re.I)


def value_row(ln) -> bool:
    """A line printing any number that is not a year or a day after a month name ('June 30,'): a table row, not a
    header line."""
    ws = ln.words
    for n, w in enumerate(ws):
        t = w.text.strip("$")
        if not re.search(r"\d", t) or _YEAR.match(t):
            continue
        if re.fullmatch(r"\d{1,2}[,.]?", t) and n and _MONTH.match(ws[n - 1].text):
            continue
        return True
    return False


def data_row(text: str) -> bool:
    """A line printing an amount (a run of three digits that is not a year), so not a header line. A date header
    ('June 27, 2026 September 27, 2025') prints only days and years."""
    return any(re.search(r"\d{3}", t) and not _YEAR.match(t) for t in text.split())


def label_words(label: Optional[str]) -> List[str]:
    return [w for w in re.findall(r"[a-z]+", (label or "").lower().replace("_", " ")) if len(w) > 2 and w not in ("the", "and")]


ID_MARK = "#"


def label_ids(label: Optional[str]) -> List[str]:
    """The label's identity tokens that contain a digit ('59263' of '59263 Invoice', '2207' of 'KIS-2207',
    '2024' of 'of 2024, a change of'), marked with '#' so they sit beside the label words."""
    out = []
    for w in (label or "").split():
        core = w.strip("()[],.;:'\"")
        if not any(c.isdigit() for c in core) or _AMOUNT_WORD.fullmatch(core):
            continue          # an amount printed in the label ('Investments at cost: $164,000,000.') is not an identity
        for t in _norm(core):
            if any(c.isdigit() for c in t) and ID_MARK + t not in out:
                out.append(ID_MARK + t)
    return out


_AMOUNT_WORD = re.compile(r"[-–(]?[$€£¥]\S*|[-–]?\d{1,3}([,.]\d{3})+([.,]\d+)?%?|[-–]?\d+[.,]\d+%?|[-–]?\d+%")


def entity_words(entity: Optional[str]) -> List[str]:
    return label_words(entity) + label_ids(entity) if entity else []


def split_words(words) -> Tuple[List[str], List[str]]:
    """(label words, identity tokens without their mark)."""
    return [w for w in words if not w.startswith(ID_MARK)], [w[1:] for w in words if w.startswith(ID_MARK)]


_ELLIPSIS = re.compile(r"\.\.\.|…")


def ids_ok(ids: List[str], line, truncation: bool = True) -> bool:
    """Does the line print every identity token? A label cut short ('Hex bolt, grade 8, 1/2-13 x 2" (b...') needs
    only the tokens before the cut: those it prints must be a prefix of the label's tokens."""
    text = line if isinstance(line, str) else line.text
    have = set(_norm(text))
    if all(t in have for t in ids):
        return True
    if not truncation or not _ELLIPSIS.search(text):
        return False
    k = 0
    while k < len(ids) and ids[k] in have:
        k += 1
    return not any(t in have for t in ids[k:])


_PURE_NUM = re.compile(r"[-–(]?[$€£¥]?\d[\d,]*(\.\d+)?\)?%?")


def _ids_in_label(ids: List[str], line) -> bool:
    """qcore9: every identity token is spelled on the line in a word with letters or marks ('SKU-204', '#3310'), or as a
    bare number that no other bare number precedes on the line (the row's label part, '3310 05/14 87.50'), not only
    as one of the row's values."""
    ws = line.words if hasattr(line, "words") else []
    for t in ids:
        ok = False
        seen_num = False
        for w in ws:
            txt = w.text.strip()
            pure = bool(_PURE_NUM.fullmatch(txt))
            if t in _norm(txt) and (not pure or not seen_num):
                ok = True
                break
            seen_num = seen_num or pure
        if not ok:
            return False
    return True


def own_line(words: List[str], line: Optional[str]) -> bool:
    """Is this line the field's own line? Most (60%) of the label's words appear on it, and it prints the
    label's identity tokens."""
    alpha, ids = split_words(words or [])
    if not line or not (alpha or ids):
        return False
    if ids and not ids_ok(ids, line):
        return False
    if not alpha:
        return True
    have = set(re.findall(r"[a-z]+", line.lower()))
    return sum(w in have for w in alpha) / len(alpha) >= 0.6


def same(printed: Optional[float], value) -> Optional[bool]:
    """Same digits (signs and % scaling are conventions)."""
    if printed is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return any(abs(abs(printed) - abs(v)) <= 0.5 for v in (value, value * 100)) or abs(abs(printed) - abs(value * 100)) < 0.01


def at_scale(printed: Optional[float], value, scale: float) -> Optional[bool]:
    if printed is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if scale == 1:
        return same(printed, value)
    return abs(abs(printed) * scale - abs(value)) <= 0.5 * scale


def _print_unit(text: str) -> float:
    """qcore9: the unit of the last printed digit of a number token ('0.40' -> 0.01, '1,234' -> 1)."""
    m = re.search(r"\d[\d,]*(?:\.(\d+))?", text or "")
    return 10.0 ** -len(m.group(1)) if m and m.group(1) else 1.0


def _at_scale_p(printed, value, scale, unit) -> bool:
    """qcore9: at_scale held to the printed precision (half the last printed digit, times the page's scale); a stored
    value below one unit of the page must be the printed number times the scale, digit for digit (SEC's '(0.40)' in
    millions is 400,000; a stored 412.6 is not '0.4' in thousands)."""
    if not at_scale(printed, value, scale) or abs(abs(printed) * scale - abs(value)) > 0.5 * unit * scale * 1.0001:
        return False
    q = abs(value) / (unit * scale)
    return abs(value) >= scale or abs(q - round(q)) < 1e-6


def copied_scale(printed: Optional[float], value, scale: float) -> bool:
    return scale != 1 and bool(same(printed, value)) and isinstance(value, (int, float)) and abs(value) >= 1 \
        and not at_scale(printed, value, scale)


def decimals(v) -> int:
    """Decimal places of a number as extracted (0 to 4)."""
    if not isinstance(v, float) or v == int(v):
        return 0
    t = f"{abs(v):.4f}".rstrip("0")
    return len(t.split(".")[1]) if "." in t else 0


def misread(a, b) -> bool:
    """Could b be what was printed where a was read? Same sign, |b| >= 100, one or two digits
    different at the same length, or one digit added or dropped. Digits are compared at a's precision
    (147.3 and 138.6 differ in three digits)."""
    if a is None or b is None or (a < 0) != (b < 0) or abs(b) < 100 or abs(a) < 1:
        return False
    d = decimals(a)
    x, y = (f"{abs(a):.{d}f}".replace(".", ""), f"{abs(b):.{d}f}".replace(".", "")) if d else \
        (str(int(round(abs(a)))), str(int(round(abs(b)))))
    if x == y:
        return False
    if len(x) == len(y):
        return sum(p != q for p, q in zip(x, y)) <= 2
    if abs(len(x) - len(y)) == 1:
        long, short = (x, y) if len(x) > len(y) else (y, x)
        return any(long[:k] + long[k + 1:] == short for k in range(len(long)))
    return False


def fragment(printed, value) -> bool:
    """Is the printed read a piece of the number ('371' of 128,806,371)?"""
    if printed is None or not isinstance(value, (int, float)):
        return False
    p, v = str(int(round(abs(printed)))), str(int(round(abs(value))))
    return 3 <= len(p) < len(v) and (v.startswith(p) or v.endswith(p))


def overlaps(a, b, slack: float = 0.5) -> bool:
    if not a or not b:
        return False
    return a[0] < b[2] + slack and b[0] < a[2] + slack and a[1] < b[3] + slack and b[1] < a[3] + slack


def ratio_token(text: str) -> bool:
    t = text.strip().rstrip(".,;:)")
    return "%" in t or bool(re.search(r"\dx$", t, re.I))


def _norm(s: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def column_match(group_text: str, column: str) -> int:
    """How well a header cell names the record's column: 3 same words, 2 the cell contains the
    column's words, 1 the column contains the cell's words, 0 no match."""
    g, c = _norm(group_text), _norm(column)
    if not g or not c:
        return 0
    if g == c:
        return 3
    if set(c) <= set(g):
        return 2
    if set(g) <= set(c):
        return 1
    return 0


def _groups(words, gap: float = 6.0):
    out = []
    for w in words:
        if out and w.x0 - out[-1][-1].x1 <= gap:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _text_groups(ln, left: Optional[float], fmt: str = "us"):
    """Header cells of a line: word groups that are not numbers, right of `left` when given."""
    gs = []
    for g in _groups(ln.words):
        t = " ".join(w.text for w in g)
        if _NUMLIKE.search(t) and parse_amount(t.replace(" ", ""), fmt) is not None:
            continue
        if left is not None and max(w.x1 for w in g) <= left:
            continue
        gs.append((t, max(w.x1 for w in g), min(w.x0 for w in g)))
    return gs


def _numbers(ln, fmt: str = "us"):
    return [w for w in ln.words if _NUMLIKE.search(w.text) and parse_amount(w.text.replace("$", ""), fmt) is not None]


_VOCAB_BASE = {"thousands", "millions", "except", "share", "per", "amounts", "dollars", "months", "ended", "three", "six", "nine",
               "twelve", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december", "total", "net"}


def _lev(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, or cap + 1 once it exceeds cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        lo = i
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            lo = min(lo, cur[j])
        if lo > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _snap(word: str, vocab: set, memo: dict) -> str:
    """Each run of three or more letters of an OCR'd word replaced by the one vocabulary word within a small edit distance
    (1 for 3 letters, 2 up to 9, 3 beyond), keeping its case style; unchanged when none or several are that close.
    A word of letters with one digit has the digit read as its look-alike letter first; other words with digits are
    left alone."""
    if re.search(r"\d", word):
        letters = re.sub(r"[^A-Za-z]", "", word)
        if len(letters) < 2 or len(re.sub(r"\D", "", word)) > 1:
            return word
        word = word.translate(_DIGIT_LETTER)      # '5ex' -> 'Sex' (then snapped to 'Six')
    return re.sub(r"[A-Za-z]{3,}", lambda m: _snap_run(m.group(0), vocab, memo), word)


_DIGIT_LETTER = str.maketrans({"5": "S", "0": "O", "1": "l", "8": "B", "6": "G"})


def _snap_number(word: str, nums: set) -> str:
    """A short number on a header line (a year or a day, no separators) one digit away from exactly one of the
    document's column numbers ('7026' -> '2026', '38,' -> '30,')."""
    m = re.fullmatch(r"(\(?)(\d{2,4})([.,;:)]*)", word)
    if not m or m.group(2) in nums:
        return word
    t = m.group(2)
    hits = [n for n in nums if len(n) == len(t) and sum(a != b for a, b in zip(n, t)) == 1]
    return m.group(1) + hits[0] + m.group(3) if len(hits) == 1 else word


def _snap_run(core: str, vocab: set, memo: dict) -> str:
    low = core.lower()
    if low in vocab:
        return core
    if low not in memo:
        cap = 1 if len(low) <= 3 else 2 if len(low) <= 9 else 3
        best, hits = cap + 1, []
        for v in vocab:
            if abs(len(v) - len(low)) > cap:
                continue
            d = _lev(low, v, cap)
            if d < best:
                best, hits = d, [v]
            elif d == best:
                hits.append(v)
        memo[low] = hits[0] if best <= cap and len(hits) == 1 else None
    rep = memo[low]
    if rep is None:
        return core
    return rep.upper() if core.isupper() else rep.capitalize() if core[0].isupper() else rep


_DOT_GROUPS = re.compile(r"\(?\$?[1-9]\d{0,2}([.,:;]\d{3})+\)?")


def ocr_number(t: str, fmt: str) -> str:
    """OCR number cleanup. A one-sided parenthesis is completed. For the us format, ':' or ';' between digits, or a '.'
    before a group of three digits in a token whose every separator sits before three digits, is a thousands comma
    ('72.634' -> '72,634', '22:855' -> '22,855')."""
    if re.fullmatch(r"\$?\d[\d,.]*\)", t):
        t = "(" + t           # a parenthesis lost on one side ('13,858)', '(4,054') still marks a negative
    elif re.fullmatch(r"\(\$?\d[\d,.]*", t):
        t = t + ")"
    if fmt != "us" or not _DOT_GROUPS.fullmatch(t):
        return t
    return re.sub(r"[.:;](?=\d{3})", ",", t)


def ocr_clean(doc, vocab: set, fmt: str, nums: set = frozenset()):
    """An OCR page text with specks dropped (words of punctuation only, and low-confidence words of one or two characters
    that are not digits or a dash), its words snapped to the document's vocabulary, the short numbers of header lines
    snapped to the column numbers (`nums`), and its number tokens cleaned."""
    memo: dict = {}
    lines = []
    for ln in doc.lines:
        header = not any(re.search(r"\d[.,:;]\d{3}", w.text) or re.search(r"\d{5}", w.text) for w in ln.words)
        ws = []
        for w in ln.words:
            if not re.search(r"[A-Za-z0-9$%&\-–—]", w.text) or (len(w.text) <= 2 and w.conf < 0.3 and not re.fullmatch(r"\d+|[-–—$]", w.text)):
                continue          # specks read as punctuation or stray letters
            t = _snap(w.text, vocab, memo)
            if header and nums:
                t = _snap_number(t, nums)
            ws.append(pdftext.Word(ocr_number(t, fmt), w.x0, w.x1, w.top, w.bottom, w.conf))
        if ws:
            lines.append(pdftext.Line(ln.page, min(w.top for w in ws), ws))
    return pdftext.Doc(doc.path, doc.n_pages, lines)


class Pages:
    """Page reads for one bundle. mode "notext": no page text at all. mode "ocr": the page text is an OCR read of the
    page images (`ocr_dir`/<doc>.json, written by qcore5.ocr), a second noisy reader rather than a trusted layer."""

    q7 = False      # qcore7 zero-config page rules (set by run() when anything is discovered); off: qcore6's reads

    def __init__(self, bundle, mode: str = "text", ocr_dir: Optional[str] = None):
        self.bundle, self.mode = bundle, mode
        self.ocr_dir = ocr_dir
        self.doc_scale: Dict[str, float] = {}     # ocr: the scale both readers agree on, per document
        self.print_scale: Dict[str, float] = {}   # text (qcore5): a voted scale for a document with no unit statement, used
                                                  # only to find where a number is printed
        self.fmt = getattr(bundle, "number_format", "us") or "us"
        self._memo: Dict[tuple, object] = {}

    @property
    def ocr(self) -> bool:
        return self.mode == "ocr"

    # ---- documents

    def doc(self, doc_id: str):
        if self.mode == "ocr":
            k = ("ocrdoc", doc_id)
            if k not in self._memo:
                import os
                raw = pdftext.load_ocr(os.path.join(self.ocr_dir, f"{doc_id}.json"))
                self._memo[k] = ocr_clean(raw, self._vocab(doc_id), self.fmt, self._column_numbers(doc_id))
            return self._memo[k]
        return pdftext.load(self.bundle.pdf(doc_id))

    def _column_numbers(self, doc_id: str) -> set:
        return {t for r in self.bundle.records if r.doc == doc_id for t in re.findall(r"\d{2,4}", r.column or "")}

    def _vocab(self, doc_id: str) -> set:
        """The words this document's records can be looked up by: their fields' labels, columns and entities."""
        words = set(_VOCAB_BASE)
        for r in self.bundle.records:
            if r.doc != doc_id:
                continue
            f = self.bundle.field_of(r)
            for t in [r.column, r.entity, r.field if not (f and f.labels) else None] + list(f.labels if f else []):
                for w in re.findall(r"[A-Za-z]+", t or ""):
                    if len(w) >= 3:
                        words.add(w.lower())
        return words

    def word_at(self, doc_id, page, bbox):
        """The word whose box is exactly `bbox`, or None."""
        if not bbox or not page or not self.has_text(doc_id):
            return None
        x0, top, x1, _ = bbox
        for ln in self.lines(doc_id):
            if ln.page == page:
                for w in ln.words:
                    if abs(w.x0 - x0) < 0.05 and abs(w.top - top) < 0.05 and abs(w.x1 - x1) < 0.05:
                        return w
        return None

    def scanned(self, doc_id: str) -> bool:
        """qcore6: the PDF is a scan (an image covers half of a page or more) whose text layer is an OCR of it, not the
        document's own text: its reads are a second reader, like OCR mode's."""
        k = ("scanned", doc_id)
        if k not in self._memo:
            self._memo[k] = pdftext.scanned(self.bundle.pdf(doc_id))
        return self._memo[k]

    def has_text(self, doc_id: str) -> bool:
        if self.mode == "notext":
            return False
        k = ("text", doc_id)
        if k not in self._memo:
            try:
                self._memo[k] = bool(self.doc(doc_id).lines)
            except Exception:
                self._memo[k] = False
        return self._memo[k]

    def lines(self, doc_id: str):
        ls = self.doc(doc_id).lines
        if self.q7 and ls and getattr(ls[0], "doc", None) != doc_id:
            for ln in ls:
                ln.doc = doc_id         # qcore7: a line knows its document (_year_label)
        return ls

    # ---- the cited spot

    def page_value(self, doc_id, page, bbox) -> Optional[float]:
        if not bbox or not page or not self.has_text(doc_id):
            return None
        x0, top, x1, bottom = bbox
        hits = [w for ln in self.lines(doc_id) if ln.page == page for w in ln.words
                if w.x1 > x0 - 1 and w.x0 < x1 + 1 and w.bottom > top - 1 and w.top < bottom + 1]
        if sum(bool(re.search(r"\d", w.text)) for w in hits) > 1:
            return None       # two printed numbers at the spot (adjacent amounts, or overlapping words): no single read
        text = "".join(w.text for w in sorted(hits, key=lambda w: w.x0)).replace("$", "")
        return amount(text, self.fmt) if text else None

    def line_at(self, doc_id, page, bbox) -> Optional[str]:
        if not bbox or not self.has_text(doc_id):
            return None
        x0, top, x1, bottom = bbox
        for ln in self.lines(doc_id):
            if ln.page == page and any(w.x1 > x0 - 1 and w.x0 < x1 + 1 and w.bottom > top - 1 and w.top < bottom + 1 for w in ln.words):
                return ln.text
        return None

    def token_at(self, doc_id, page, bbox) -> Optional[str]:
        x0, top, x1, _ = bbox
        for ln in self.lines(doc_id):
            if ln.page == page:
                for w in ln.words:
                    if abs(w.x0 - x0) < 0.05 and abs(w.top - top) < 0.05 and abs(w.x1 - x1) < 0.05:
                        return w.text
        return None

    def locate(self, doc_id, page, value, printed_text) -> Optional[list]:
        """Place an extraction's citation: where the answered number is printed on the answered page,
        preferring the line that carries the model's printed_text."""
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not page or not self.has_text(doc_id):
            return None
        cands = []
        for ln in self.lines(doc_id):
            if ln.page != page:
                continue
            txt = " ".join(w.text for w in ln.words)
            for w in ln.words:
                p = parse_amount(w.text.replace("$", ""), self.fmt) if any(c.isdigit() for c in w.text) else None
                if p is not None and any(abs(abs(p) - abs(x)) <= 0.5 for x in (value, value * 100)):
                    cands.append((0 if printed_text and printed_text[:25] in txt else 1, (w.x0, w.top, w.x1, w.bottom)))
        return list(sorted(cands)[0][1]) if cands else None

    # ---- scale and sign

    def _scales(self, doc_id) -> Dict[int, List[tuple]]:
        k = ("scales", doc_id)
        if k not in self._memo:
            out: Dict[int, List[tuple]] = {}
            prev = None
            for ln in self.lines(doc_id):
                m = SCALE_RX.search(ln.text)
                if not m and prev is not None and prev.page == ln.page and 0 < ln.top - prev.top <= 20:
                    # qcore5: a unit statement broken over two lines ('... by separate components (in' / 'millions):')
                    m2 = SCALE_RX.search(prev.text + " " + ln.text)
                    m = m2 if m2 and m2.end() > len(prev.text) + 1 and m2.start() < len(prev.text) else None
                if m:
                    g_ = m.group(0).lower()
                    out.setdefault(ln.page, []).append((ln.top, 1e9 if "billion" in g_ else 1e6 if "million" in g_ else 1e3))
                prev = ln
            self._memo[k] = out
        return self._memo[k]

    def page_scale(self, doc_id, page, top=None) -> float:
        """1, 1e3 or 1e6: the nearest unit statement above `top` ('in thousands', '$000'), else the first on the page.
        A page with none continues the last statement of the nearest earlier page that has one (a statement whose
        title and unit line sit on the page before, or a table continued over pages)."""
        if not page or not self.has_text(doc_id):
            return 1.0
        if self.mode == "ocr" and doc_id in self.doc_scale:
            return self.doc_scale[doc_id]
        hits = self._scales(doc_id).get(page)
        if not hits:
            for pg in range(page - 1, 0, -1):
                prev = self._scales(doc_id).get(pg)
                if prev:
                    return max(prev)[1]
            return 1.0
        above = [h for h in hits if top is None or h[0] <= top + 1]
        return (max(above) if above else min(hits))[1]

    def prints_negatives(self, doc_id) -> bool:
        if not self.has_text(doc_id):
            return False
        k = ("neg", doc_id)
        if k not in self._memo:
            self._memo[k] = any(_NEG_TOKEN.search(w.text.strip()) and re.search(r"\d", w.text)
                                for ln in self.lines(doc_id) for w in ln.words)
        return self._memo[k]

    def find_value(self, doc_id, value, page=None, words=None) -> List[tuple]:
        """Every spot in the document printing this number: (page, bbox, line text), own line first, then the cited page."""
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not self.has_text(doc_id):
            return []
        hits = []
        scales = self._scales(doc_id)
        for ln in self.lines(doc_id):
            sc = self.page_scale(doc_id, ln.page, ln.top) if scales else 1.0
            ps = self.print_scale.get(doc_id, 1.0) if not scales else 1.0
            for w in ln.words:
                v = amount(w.text, self.fmt)
                # qcore9: a percentage is never a scaled amount, and a scaled number matches to its printed precision:
                # '0.4' on a page in thousands is 400 give or take 50, not anything from -100 to 900 (at_scale's half
                # unit of the page), which let '0.4' match a stored 357 in a report kept at the page's own unit
                scaled_ok = "%" not in w.text
                pu = _print_unit(w.text)
                if v is not None and (v != 0 or value == 0) and (same(v, value) or (sc != 1 and v != 0 and scaled_ok
                                                                                    and _at_scale_p(v, value, sc, pu))
                                                                 or (ps != 1 and v != 0 and scaled_ok and _at_scale_p(v, value, ps, pu))):
                    hits.append((ln.page, (w.x0, w.top, w.x1, w.bottom), ln.text))
        return sorted(hits, key=lambda h: (not own_line(words or [], h[2]), h[0] != page))

    # ---- columns

    def header(self, doc_id, page, top, column, left=None):
        """(top, right edges, rank, (x0, x1) of the matching cell) of the nearest header line above `top` with at least two header cells,
        one of which (uniquely) names `column`; or None."""
        best = None
        for ln in self.lines(doc_id):
            if ln.page != page or ln.top >= top - 1 or data_row(ln.text):
                continue
            cells = _text_groups(ln, left, self.fmt)
            if len(cells) < 2:
                continue
            sc = [column_match(t, column) for t, _, _ in cells]
            m = max(sc)
            if m == 0 or sc.count(m) > 1:
                continue
            k = sc.index(m)
            edges = [x for _, x, _ in cells]
            if best is None or ln.top > best[0]:
                best = (ln.top, edges, k, (cells[k][2], cells[k][1]))
        return best

    def _header_like(self, doc_id, page, top, left=None) -> bool:
        return any(ln.page == page and ln.top < top - 1 and not data_row(ln.text) and len(_text_groups(ln, left, self.fmt)) >= 2
                   for ln in self.lines(doc_id))

    def find_header(self, doc_id, page, top, column, left=None):
        """(header, page it is on): above the row on this page, or, for a table continued from earlier pages,
        the last header on the nearest earlier page that has a header line (another header there ends the search)."""
        h = self.header(doc_id, page, top, column, left)
        if h:
            return h, page
        if self._header_like(doc_id, page, top, left):
            return None, None
        for pg in range(page - 1, 0, -1):
            h = self.header(doc_id, pg, 1e9, column, left)
            if h:
                return h, pg
            if self._header_like(doc_id, pg, 1e9, left):
                return None, None
        return None, None

    def has_header(self, doc_id, page, top, column, left=None) -> bool:
        return self.find_header(doc_id, page, top, column, left)[0] is not None

    def _learned_edges(self, doc_id, page, n, top_from=None) -> Optional[List[float]]:
        rows = []
        for ln in self.lines(doc_id):
            if ln.page != page or (top_from is not None and ln.top <= top_from):
                continue
            ws = _numbers(ln, self.fmt)
            if len(ws) == n:
                rows.append(sorted(w.x1 for w in ws))
        if len(rows) < 2:
            return None
        return [median(r[k] for r in rows) for k in range(n)]

    def pick(self, doc_id, ln, nums, column, left):
        """The number on row `ln` in `column`, placed by the header's position; None if none or two sit there.
        When a stacked header (`stacked`) also places it, both placements must agree."""
        if not nums:
            return None
        one = self._pick(doc_id, ln, nums, column, left)
        st = self.stacked(doc_id, ln, nums, column, left)
        if one is not None and st is not None and tuple(one[2]) != tuple(st[2]):
            return None
        return one if one is not None else st

    def _pick(self, doc_id, ln, nums, column, left):
        page, top = nums[0][1], nums[0][2][1]
        h, hp = self.find_header(doc_id, page, top, column, left)
        if not h or (hp == page and len(h[1]) < len(nums)):
            return None
        edges, k = list(h[1]), h[2]
        n = len(edges)
        span = h[3]
        if sum(m[2][0] < span[1] and span[0] < m[2][2] for m in nums) > 1:
            return None       # the column's header cell spans several numbers ('Three Months Ended' over two years)
        learned = self._learned_edges(doc_id, page, n, h[0] if hp == page else None)
        if learned and all(abs(a - b) <= 40 for a, b in zip(learned, sorted(edges))):
            edges = learned
        else:
            edges = sorted(edges)
        x = edges[k]
        gaps = [b - a for a, b in zip(edges, edges[1:])]
        tol = min(30.0, 0.4 * min(gaps)) if gaps else 30.0
        # the number must sit within the column's tolerance and under the header cell itself
        close = [m for m in nums if abs(m[2][2] - x) <= tol and m[2][0] < span[1] + 2 and span[0] - 2 < m[2][2]]
        return close[0] if len(close) == 1 else None

    def stacked(self, doc_id, ln, nums, column, left):
        """A header printed over several lines ('Three Months' / 'Ended June' / '2026 2025 2026 2025'). The leaf line
        is the nearest non-data line above the row that prints one cell (a year or a text cell right of the label)
        per number of the row; each leaf cell's column name joins the header cells above it that overlap its zone,
        up to the nearest data line. The record's column must name exactly one leaf best, with no year or day of
        that leaf missing from the column; the read is the row's number in the same position. None otherwise."""
        page, top = nums[0][1], nums[0][2][1]
        above = sorted([x for x in self.lines(doc_id) if x.page == page and x.top < top - 1], key=lambda x: -x.top)
        n = len(nums)
        if n < 2:
            return None
        leaf, rest, prev = None, [], None
        for x in above:
            if leaf is None:
                if value_row(x):
                    continue
                cells = self._leaf_cells(x, left)
                if len(cells) == n:
                    leaf, prev = cells, x.top
                elif len(cells) >= 2:
                    return None        # a header-like line with another number of cells: not this table's leaf
                continue
            if value_row(x) or len(rest) >= 4 or prev - x.top > 40:
                break
            if left is not None and any(min(w.x0 for w in g) < left - 2 and max(w.x1 for w in g) > left + 2 for g in _groups(x.words)):
                break          # qcore5: a line running across the row-label edge is a title or prose, not a header cell
            rest.append(x)
            prev = x.top
        if leaf is None:
            return None
        cuts = [(a[1] + b[0]) / 2 for a, b in zip(leaf, leaf[1:])]
        zones = [(lo, hi) for lo, hi in zip([-1e9] + cuts, cuts + [1e9])]
        names = [list(_norm(c[2])) for c in leaf]
        for x in rest:
            for t, x1, x0 in _text_groups(x, left, self.fmt):
                for j, (lo, hi) in enumerate(zones):
                    if x0 < hi and x1 > lo:
                        names[j] = _norm(t) + names[j]
        c = _norm(column)
        cset = set(c)
        sc = []
        for nm in names:
            if any(re.search(r"\d", t) and t not in cset for t in nm):
                sc.append(0)
                continue
            sc.append(column_match(" ".join(nm), column))
        m = max(sc)
        if m == 0 or sc.count(m) > 1:
            return None
        k = sc.index(m)
        ordered = sorted(nums, key=lambda v: v[2][0])
        return ordered[k]

    def strict_cell(self, doc_id, words, entity, column, money, raw=None, others=()):
        """qcore5: is this record's cell unambiguous on the page? The label must best-match exactly one line in the whole
        document (a label repeated in stacked blocks, sections or pages is not unique), the line must not be prose, and
        the column must be placed by the header (both header placements, where both apply, agreeing), naming every
        year and day of the record's column. Returns a dict: ok, why, cell (value, page, bbox), nums (the row's numbers)."""
        if not self.has_text(doc_id):
            return dict(ok=False, why="no_text")
        others = tuple(sorted({frozenset(o) for o in others if o}, key=sorted))
        memo = ("strict", doc_id, tuple(words), entity, column, money, others, raw)
        if memo in self._memo:
            return self._memo[memo]
        ent = entity_words(entity) if entity else []
        ent_ids = split_words(ent)[1]
        best, lines = self._label_lines(doc_id, None, words, money, ent_ids, others)
        if best[0] < 0.75 or not lines:
            res = dict(ok=False, why="row_not_found")
        else:
            if ent:
                lines = [x for x in lines if self._under(doc_id, x[0], ent)]
            if len(lines) > 1 and raw:
                flat = lambda t: re.sub(r"\s+", "", t.lower())
                exact = [x for x in lines if flat(raw) in flat(x[0].text)]
                if len(exact) == 1:
                    lines = exact
            if len(lines) != 1:
                res = dict(ok=False, why="row_not_unique", n=len(lines))
            else:
                ln, nums = lines[0]
                if prose(ln, words, nums, anywhere=not column):
                    pr = None if column else self._prose_read(ln, words, money, raw, ent)
                    res = dict(ok=pr is not None, why="" if pr is not None else "prose", cell=pr, nums=nums)
                elif not column:
                    res = dict(ok=len(nums) == 1, why="" if len(nums) == 1 else "several_numbers", cell=nums[0] if len(nums) == 1 else None, nums=nums)
                else:
                    left = self._label_end(ln, nums)
                    one = self._pick(doc_id, ln, nums, column, left)
                    st = self.stacked(doc_id, ln, nums, column, left)
                    cell = one if one is not None else st
                    if cell is None:
                        res = dict(ok=False, why="column_not_placed", nums=nums)
                    elif one is not None and st is not None and tuple(one[2]) != tuple(st[2]):
                        res = dict(ok=False, why="column_placements_disagree", nums=nums)
                    elif self.q7 and self._in_text(ln, cell):
                        res = dict(ok=False, why="number_in_text", nums=nums)
                    else:
                        res = dict(ok=True, why="", cell=cell, nums=nums, both=one is not None and st is not None)
        self._memo[memo] = res
        return res

    def _leaf_cells(self, x, left):
        """(x0, x1, text) cells of a header line right of the label: each year token alone, other word groups whole."""
        ws = [w for w in x.words if left is None or w.x0 >= left - 1]
        cells = []
        for g in _groups(ws):
            run = []
            for w in g:
                if _YEAR.match(w.text):
                    if run:
                        cells.append((run[0].x0, run[-1].x1, " ".join(r.text for r in run)))
                        run = []
                    cells.append((w.x0, w.x1, w.text))
                else:
                    run.append(w)
            if run:
                cells.append((run[0].x0, run[-1].x1, " ".join(r.text for r in run)))
        return sorted(cells)

    # ---- entity rows read whole (qcore5)

    def skew_lines(self, doc_id, page) -> List[List]:
        """The page's words grouped into printed rows along the page's own slope (a photographed receipt's rows run
        downhill): the slope within +-0.12 that makes the words' centre heights most concentrated, then words whose
        heights along that slope lie within half a median word height of the row's."""
        k = ("skew", doc_id, page)
        if k in self._memo:
            return self._memo[k]
        import numpy as np
        ws = [w for ln in self.lines(doc_id) if ln.page == page for w in ln.words]
        rows: List[List] = []
        if ws:
            cx = np.array([(w.x0 + w.x1) / 2 for w in ws])
            cy = np.array([(w.top + w.bottom) / 2 for w in ws])
            h = float(np.median([max(0.5, w.bottom - w.top) for w in ws]))
            best = (-1.0, 0.0)
            for s_ in np.arange(-0.12, 0.1201, 0.004):
                y = cy - s_ * cx
                hist, _ = np.histogram(y, bins=max(1, int((y.max() - y.min()) / (0.5 * h)) + 1))
                sc = float((hist.astype(float) ** 2).sum())
                if sc > best[0] + 1e-9 or (abs(sc - best[0]) <= 1e-9 and abs(s_) < abs(best[1])):
                    best = (sc, float(s_))
            y = cy - best[1] * cx
            order = sorted(range(len(ws)), key=lambda n: y[n])
            cur, ys = [], []
            for n in order:
                if cur and abs(y[n] - sum(ys) / len(ys)) > 0.5 * h:
                    rows.append(sorted(cur, key=lambda w: w.x0))
                    cur, ys = [], []
                cur.append(ws[n])
                ys.append(y[n])
            if cur:
                rows.append(sorted(cur, key=lambda w: w.x0))
        self._memo[k] = rows
        return rows

    def entity_row(self, doc_id, page, entity: str, same_name: List[str]):
        """(value, page, bbox) for each number printed on the row that names `entity` (left to right), or None.
        An entity ending in '#<n>' is the n-th of rows printed with the same name; `same_name` lists the document's
        entities with this entity's name, in order. 'A / B' names row B within row group A (a sub-item): row B is read. The row must be the one best match (or, for repeated names, the
        rows that match must be as many as the entities, taken top to bottom)."""
        if not self.has_text(doc_id) or not page:
            return None
        name = re.sub(r"\s*#\d+\s*$", "", (entity or "").split(" / ")[-1])
        alpha, ids = split_words(entity_words(name))
        if not alpha:
            return None
        scored = []
        for row in self.skew_lines(doc_id, page):
            text = " ".join(w.text for w in row)
            have = set(re.findall(r"[a-z]+", text.lower()))
            frac = sum(a in have for a in alpha) / len(alpha)
            if frac >= 0.75 and (not ids or ids_ok(ids, text)):
                extra = len([w for w in have if len(w) > 2 and w not in alpha])
                scored.append(((frac, -extra), row))
        if not scored:
            return None
        top = max(sc for sc, _ in scored)
        rows = [r for sc, r in scored if sc == top]
        if len(rows) > 1:
            if len(rows) != len(same_name) or entity not in same_name:
                return None
            rows.sort(key=lambda r: min(w.top for w in r))
            rows = [rows[same_name.index(entity)]]
        words = list(rows[0])
        # a continuation row right below (or above) that prints only numbers ('2 x 15,000  30,000' under the item's name)
        allrows = self.skew_lines(doc_id, page)
        at = next((n for n, r in enumerate(allrows) if r is rows[0]), None)
        # the row below first; the row above only when nothing follows below (the row above an item's name is
        # usually the previous item's continuation)
        for nb in ((at + 1, at - 1) if at is not None else ()):
            if 0 <= nb < len(allrows):
                nxt = allrows[nb]
                if not any(re.search(r"[A-Za-z]{3,}", w.text) for w in nxt) and any(re.search(r"\d", w.text) for w in nxt):
                    words += list(nxt)
                    break
        nums, drop = [], set(ids)
        for w in words:
            t = re.sub(r"^[xX](?=\d)", "", w.text.strip().lstrip("@").rstrip("xX"))
            toks = set(_norm(w.text))
            if toks & drop:
                drop -= toks          # the name's own number ('Es Teh 2'), leftmost occurrence
                continue
            if not re.search(r"\d", t) or toks & set(alpha):
                continue
            v = amount(_strip_punct(t), self.fmt)
            if v is not None:
                nums.append((v, page, (w.x0, w.top, w.x1, w.bottom)))
        return nums or None

    # ---- the label read

    def _entity_before(self, ln, n, ent) -> bool:
        """qcore7: in prose that names the record's entity, the number read must follow a mention of the entity with no
        other number between ('... was 30-34 (5,261)' for the age band 30-34, not the region's '(4,799)' earlier on the
        line). True when the line does not name the entity at all (the entity is implied: 'There were 16,088 ...')."""
        alpha, ids = split_words(ent)
        toks = set(alpha) | set(ids)
        if not toks:
            return True
        pos = [i for i, w in enumerate(ln.words) if toks & set(_norm(w.text))]
        if not pos:
            return True
        before = [i for i in pos if i < n]
        if not before:
            return False
        e = max(before)
        return not any(re.search(r"\d", x.text) and amount(_strip_punct(x.text).strip("()"), self.fmt) is not None
                       and not (set(_norm(x.text)) & toks) for x in ln.words[e + 1:n])

    @staticmethod
    def _in_text(ln, cell) -> bool:
        """qcore7: the cell placed under a column is a number inside the row's label text ('net of accumulated depreciation
        of $2,878 and $2,756, respectively  2,541  2,303'): it ends with a comma or semicolon, or the next word on the line
        is a lower-case word. Such a number is never a column cell."""
        bb = tuple(cell[2]) if cell and cell[2] else None
        if bb is None:
            return False
        ws = ln.words
        for n, w in enumerate(ws):
            if abs(w.x0 - bb[0]) < 0.01 and abs(w.x1 - bb[2]) < 0.01:
                if w.text.rstrip().endswith((",", ";")):
                    return True
                return n + 1 < len(ws) and bool(re.match(r"[a-z]{2,}", ws[n + 1].text))
        return False

    def _label_cells(self, ln) -> bool:
        """qcore7: a line of column labels that print numbers ('Under 20 20-24 25-29 30-34 35-39 40 and over'): two or
        more range tokens (age bands, size bands). Its numbers are labels, never values."""
        return sum(bool(_RANGE_TOK.fullmatch(w.text.strip("(),;:"))) for w in ln.words) >= 2

    def _year_label(self, ln, w) -> bool:
        """qcore7: a bare year (1900-2099, no thousands separator) in a document that groups thousands ('1,234'): a date
        or heading label, not a value."""
        t = w.text.strip("(),;:.")
        if not re.fullmatch(r"(19|20)\d\d", t):
            return False
        doc = getattr(ln, "doc", None)
        if doc is None:
            return False
        k = ("groups", doc)
        if k not in self._memo:
            self._memo[k] = any(re.search(r"\d,\d{3}\b", x.text) for l2 in self.lines(doc) for x in l2.words)
        return self._memo[k]

    def _value_words(self, ln, money, drop_ids=()):
        """(value, page, bbox) for each value number on a line. Not values: a lone dash with words after it (a bullet
        or 'Payment - ACH'), percentages and multiples for money, and the leftmost number token that spells each
        identity token (an ID such as '59263' or 'KIS-2207' printed in the row label)."""
        ws = ln.words
        drop = set()
        for t in drop_ids:
            for n, w in enumerate(ws):
                if t in _norm(w.text):
                    if _NUMLIKE.search(w.text):
                        drop.add(n)
                    break         # only the leftmost word spelling the token ('(08/14/2024)', not '14,375,000')
        out = []
        if self.q7 and self._label_cells(ln):
            return out
        for n, w in enumerate(ws):
            if n in drop or not re.search(r"\d|^[-–—]$", w.text) or (money and ratio_token(w.text)):
                continue
            if self.q7 and self._year_label(ln, w):
                continue
            if _DASH.match(w.text) and any(_WORD.search(x.text) for x in ws[n + 1:]):
                continue
            v = amount(_strip_punct(w.text), self.fmt)
            if v is not None:
                out.append((v / 100 if "%" in w.text else v, ln.page, (w.x0, w.top, w.x1, w.bottom)))
        return out

    def _label_lines(self, doc_id, page, words, money, drop_ids=(), others=()):
        """The lines that best match the label. A line is skipped when another row's label (`others`) is printed on
        it in full and names more than the words this label matches there: 'Total Item: 2' belongs to a
        'Total item' label, not to 'Total'."""
        alpha, ids = split_words(words)
        best, lines = (0.0, 0), []
        cand = []
        for ln in self.lines(doc_id):
            if page and ln.page != page:
                continue
            if ids and not ids_ok(ids, ln):
                continue
            have = [w for w in re.findall(r"[a-z]+", ln.text.lower()) if len(w) > 2]
            frac = sum(w in have for w in alpha) / len(alpha) if alpha else 1.0
            if others and alpha:
                hs = set(have)
                got = set(alpha) & hs
                if any(o <= hs and o > got for o in others):
                    continue
            score = (frac, -len([w for w in have if w not in alpha]))
            nums = self._value_words(ln, money, tuple(ids) + tuple(drop_ids))
            if not nums:
                continue
            if self.q7 and ids:
                cand.append((score, ln, nums, _ids_in_label(ids, ln)))
                continue
            if score > best:
                best, lines = score, [(ln, nums)]
            elif score == best:
                lines.append((ln, nums))
        if cand:
            # qcore9 (zero-config only): a line that spells the label's identity tokens in its label ('SKU-204 Hand
            # soap ...') is the row; a line that prints the token only as a number among its values ('SKU-199 Dish soap
            # 204 0 37 ...', an opening quantity of 204) is not, when the other kind exists
            if any(c[3] for c in cand):
                cand = [c for c in cand if c[3]]
            best = max(c[0] for c in cand)
            lines = [(c[1], c[2]) for c in cand if c[0] == best]
        return best, lines

    def _under(self, doc_id, ln, entity_words) -> bool:
        alpha, ids = split_words(entity_words)

        def have(x):
            if ids and not ids_ok(ids, x, truncation=False):
                return False
            return not alpha or sum(w in re.findall(r"[a-z]+", x.text.lower()) for w in alpha) / len(alpha) >= 0.75
        if have(ln):
            return True
        above = [x for x in self.lines(doc_id) if x.page == ln.page and x.top < ln.top - 1]
        if not above:
            return False
        head = max(above, key=lambda x: x.top)
        # a row-group heading prints no value of its own: a data row naming the entity is that entity's row, not a heading
        return have(head) and not self._value_words(head, False, tuple(ids))

    @staticmethod
    def _label_end(ln, nums) -> float:
        first = min(n[2][0] for n in nums)
        boxes = {n[2] for n in nums}
        rest = [w.x1 for w in ln.words if (w.x0, w.top, w.x1, w.bottom) not in boxes and w.x1 <= first]
        return max(rest) if rest else first

    def label_spot(self, doc_id, page, word_sets: List[List[str]], entity: Optional[str], column: Optional[str], money: bool,
                   raw_labels: Optional[List[str]] = None, others=()):
        """(value, page, bbox) printed on the field's own line, in its column; None unless exactly one spot.
        With an entity, only the label sets that name the entity are read when there are any (a field whose
        labels list several row names reads the row of its own entity)."""
        if not self.has_text(doc_id):
            return None
        others = tuple(sorted({frozenset(o) for o in others if o}, key=sorted))
        memo = ("spot", doc_id, page, tuple(map(tuple, word_sets)), entity, column, money, others, tuple(raw_labels or ()))
        if memo in self._memo:
            return self._memo[memo]
        sets = [w for w in word_sets if w]
        raws = list(raw_labels) if raw_labels and len(raw_labels) == len(word_sets) else [None] * len(word_sets)
        raws = [r for r, w in zip(raws, word_sets) if w]
        ent = entity_words(entity)
        if ent:
            ea, ei = split_words(ent)
            naming = [n for n, w in enumerate(sets) if (ea and set(ea) <= set(w)) or (ei and set(ei) <= set(w))]
            if naming:
                sets, raws = [sets[n] for n in naming], [raws[n] for n in naming]
        found, unread = set(), False
        for words, raw in zip(sets, raws):
            sp = self._spot(doc_id, page, words, entity, column, money, raw, others)
            if sp is _UNREAD:
                unread = True
            elif sp is not None:
                found.add((sp[0], sp[1], tuple(sp[2])))
        # a label that finds its line but can't read it may name the record's row (a field printed on two rows,
        # such as cash at the beginning and at the end of a period): the other labels' read is then not unique
        res = next(iter(found)) if len(found) == 1 and not unread else None
        self._memo[memo] = res
        return res

    def _spot(self, doc_id, page, words, entity, column, money, raw=None, others=()):
        ent = entity_words(entity) if entity else []
        ent_ids = split_words(ent)[1]

        def find(pg):
            best, lines = self._label_lines(doc_id, pg, words, money, ent_ids, others)
            if best[0] < 0.75:
                return []
            return [x for x in lines if self._under(doc_id, x[0], ent)] if ent else lines
        lines = find(page)
        if not lines and page:
            lines = find(None)
        if len(lines) > 1 and raw:
            # the label printed verbatim ('Total assets(a)') singles out its row among lines that match its words
            flat = lambda t: re.sub(r"\s+", "", t.lower())
            exact = [x for x in lines if flat(raw) in flat(x[0].text)]
            if len(exact) == 1:
                lines = exact
        if len(lines) > 1:
            def fits(x):
                if ent:
                    return False          # an entity's row must be the one best line
                if column:
                    h = self.header(doc_id, x[0].page, x[0].top, column, self._label_end(*x))
                    return bool(h) and len(x[1]) == len(h[1])
                return len(x[1]) == 1
            lines = [x for x in lines if fits(x)]
        if len(lines) != 1:
            return _UNREAD if lines else None
        ln, nums = lines[0]
        if prose(ln, words, nums, anywhere=not column):
            return None if column else self._prose_read(ln, words, money, raw, ent)
        if column:
            left = self._label_end(ln, nums)
            if ent and not self.has_header(doc_id, nums[0][1], nums[0][2][1], column, left):
                return _UNREAD
            got = self.pick(doc_id, ln, nums, column, left)
            return _UNREAD if got is None else got
        return nums[0] if len(nums) == 1 else _UNREAD

    def _prose_read(self, ln, words, money, raw=None, ent=()):
        """The number printed right after the label phrase in running prose ('... the most admissions (28,488), and
        ...'). The phrase's words must appear once, in order, with only short filler words between them; after it
        may come only the label's own trailing filler words ('a change of -4.5%'). A parenthesised number in prose
        is an aside, so it reads positive. None unless all of this holds."""
        alpha, ids = split_words(words)
        if not alpha:
            return None
        toks = [(n, t) for n, w in enumerate(ln.words) for t in _norm(w.text)]
        filler = lambda t: t not in alpha and (len(t) <= 2 or t in ("the", "and") or t in ids)
        ends = []
        for s0, (n0, t0) in enumerate(toks):
            if t0 != alpha[0]:
                continue
            k, j = 1, s0 + 1
            while k < len(alpha) and j < len(toks):
                if toks[j][1] == alpha[k]:
                    k += 1
                elif not filler(toks[j][1]):
                    break
                j += 1
            if k == len(alpha):
                ends.append(toks[j - 1][0])
        if len(ends) != 1:
            return None
        tail = []
        if raw:
            rt = _norm(raw)
            last = max(i for i, t in enumerate(rt) if t == alpha[-1]) if alpha[-1] in rt else len(rt) - 1
            tail = [t for t in rt[last + 1:] if not re.search(r"\d", t)]
        n = ends[0] + 1
        skipped = 0
        while n < len(ln.words) and skipped < len(tail) and not re.search(r"\d", ln.words[n].text) \
                and _norm(ln.words[n].text) and all(t in tail for t in _norm(ln.words[n].text)):
            n, skipped = n + 1, skipped + 1
        if n >= len(ln.words):
            return None
        w = ln.words[n]
        t = _strip_punct(w.text)
        if not re.search(r"\d", t) or (money and ratio_token(t)):
            return None
        if self.q7 and ent and not self._entity_before(ln, n, ent):
            return None
        paren = t.startswith("(") and t.endswith(")")
        v = amount(t[1:-1] if paren else t, self.fmt)
        if v is None:
            return None
        return (v / 100 if "%" in t else v, ln.page, (w.x0, w.top, w.x1, w.bottom))
