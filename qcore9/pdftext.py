"""Page text: watermark-free lines of words with boxes (pdfplumber, top-left origin, 1-based pages).

Same reading as the frozen checker's text tool: rotated characters (a per-recipient stamp)
are dropped, words are pdfplumber words, and words whose tops lie within 3 points form a line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: float = 1.0          # OCR confidence in [0, 1]; 1 for a PDF text layer


@dataclass
class Line:
    page: int
    top: float
    words: List[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Doc:
    path: str
    n_pages: int
    lines: List[Line]


def _is_watermark(obj) -> bool:
    if obj.get("object_type") != "char":
        return False
    m = obj.get("matrix") or (1, 0, 0, 1, 0, 0)
    return abs(m[1]) > 1e-3


def _group_lines(words, page_no, y_tol=3.0) -> List[Line]:
    lines: List[Line] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        word = Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
        if lines and abs(lines[-1].top - word.top) <= y_tol:
            lines[-1].words.append(word)
        else:
            lines.append(Line(page_no, word.top, [word]))
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
    return lines


@lru_cache(maxsize=1024)
def load(path: str) -> Doc:
    import pdfplumber
    lines: List[Line] = []
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            clean = page.filter(lambda o: not _is_watermark(o))
            words = clean.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
            lines.extend(_group_lines(words, i + 1))
    return Doc(path, n, lines)


@lru_cache(maxsize=1024)
@lru_cache(maxsize=1024)
def scanned(path: str) -> bool:
    """Does an image cover at least half of some page (a scanned page, its text layer an OCR)?"""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                area = float(pg.width) * float(pg.height) or 1.0
                for im in pg.images:
                    w = max(0.0, min(float(im["x1"]), float(pg.width)) - max(0.0, float(im["x0"])))
                    h = max(0.0, min(float(im["bottom"]), float(pg.height)) - max(0.0, float(im["top"])))
                    if w * h >= 0.5 * area:
                        return True
    except Exception:
        return False
    return False


def load_ocr(path: str) -> Doc:
    """Page text from an OCR file written by `qcore5.ocr` (words in points, top-left origin, with confidences),
    grouped into lines like a text layer."""
    d = json.load(open(path))
    lines: List[Line] = []
    pages = d.get("pages") or []
    for pg in pages:
        ws = []
        for t, x0, top, x1, bottom, conf in pg.get("words") or []:
            t = str(t).strip()
            if t:
                ws.append(dict(text=t, x0=float(x0), x1=float(x1), top=float(top), bottom=float(bottom), conf=float(conf)))
        lines.extend(_group_ocr_lines(ws, int(pg["page"])))
    return Doc(path, max([int(p["page"]) for p in pages] or [0]), lines)


def _group_ocr_lines(words, page_no) -> List[Line]:
    """Words whose vertical centres lie within 40% of the median word height form a line (OCR boxes vary in height
    more than a text layer's)."""
    if not words:
        return []
    hs = sorted(w["bottom"] - w["top"] for w in words)
    tol = max(1.5, 0.4 * hs[len(hs) // 2])
    lines: List[Line] = []
    centres: List[float] = []
    for w in sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
        c = (w["top"] + w["bottom"]) / 2
        word = Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], w["conf"])
        if lines and abs(centres[-1] - c) <= tol:
            lines[-1].words.append(word)
        else:
            lines.append(Line(page_no, word.top, [word]))
            centres.append(c)
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
        ln.top = min(w.top for w in ln.words)
    return lines


NUMBER_FORMATS = ("us", "dot_thousands", "grouped")


def _plain(t: str, fmt: str) -> Optional[str]:
    """A digits string in the document's number format -> a float() string; None if it does not fit the format.
    us: '1,234.56' (comma thousands, dot decimal). dot_thousands: '1.234.567,89', '60.000' (dot thousands,
    comma decimal); every dot group after the first must have exactly three digits. grouped: either character groups
    thousands when exactly three digits follow it ('60.000', '60,000', '1.234.000'), and one or two digits after the
    last separator are decimals ('1.500,00', '45,000.00', '1.5'); the thousands separators must all be the same
    character and differ from the decimal one ('57,0000' and '1.234.56' are unreadable)."""
    if fmt == "grouped":
        parts = re.split(r"([.,])", t)
        nums, seps = parts[0::2], parts[1::2]
        if not seps:
            return t
        if not nums[0]:
            return None
        dec = ""
        if len(nums[-1]) in (1, 2):
            dec, dsep = nums[-1], seps[-1]
            nums, seps = nums[:-1], seps[:-1]
            if dsep in seps:
                return None
        if seps:
            if len(set(seps)) > 1 or any(len(g) != 3 for g in nums[1:]) or not (1 <= len(nums[0]) <= 3):
                return None
        if not all(g.isdigit() for g in nums) or (dec and not dec.isdigit()):
            return None
        return "".join(nums) + ("." + dec if dec else "")
    if fmt == "dot_thousands":
        if t.count(",") > 1:
            return None
        whole, _, dec = t.partition(",")
        if "." in whole:
            head, *groups = whole.split(".")
            if not head or len(head) > 3 or any(len(g) != 3 for g in groups):
                return None
            whole = head + "".join(groups)
        return whole + ("." + dec if dec else "")
    return t.replace(",", "")


def parse_amount(tok: str, fmt: str = "us") -> Optional[float]:
    """'1,234' -> 1234, '(1,234)' / '-1,234' -> -1234, '-' -> 0, '12.5%' -> 12.5, '1.3x' -> 1.3.
    fmt "dot_thousands": '1.234.567,89' -> 1234567.89, '60.000' -> 60000."""
    t = tok.strip().replace("$", "").replace(" ", "")
    if t in ("-", "–", "—", ""):
        return 0.0
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    if fmt == "us":
        t = t.strip("()-").replace(",", "").rstrip("%x")
    else:
        t = _plain(t.strip("()-").rstrip("%x"), fmt)
    if t is None:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


_TRAIL_MINUS = re.compile(r"^\$?\(?[\d,]+(\.\d+)?\)?-$")
_TRAIL_MINUS_ANY = re.compile(r"^\$?\(?\d[\d.,]*\)?-$")


def amount(text: str, fmt: str = "us") -> Optional[float]:
    """parse_amount, plus a trailing minus ('1,234-') read as negative."""
    t = text.replace("$", "")
    v = parse_amount(t, fmt)
    if v is not None and v > 0 and (_TRAIL_MINUS if fmt == "us" else _TRAIL_MINUS_ANY).match(t.strip()):
        return -v
    return v
