"""qcore6 re-read tier: a cheap model re-reads only the cells the checker is unsure about.

    python -m qcore7 reread-requests <bundle> <extraction.json> --mode ocr|notext|text [--ocr-dir D] --images <page images>
                                     --out <reqdir> [--identities hand|auto|both] [--schema hand|auto] [--max-share 0.6]
    python -m qcore7 run <bundle> <extraction.json> --mode ... --reread <reqdir>/answers.jsonl
    python -m qcore7 reread-simulate <reqdir> <bundle> [--digit 0.05] [--null 0.03] [--seed 0] [--out answers.jsonl]

Requests. tier 1 runs first; the cells it is unsure about are, in this order: the cells named by tier 2 items about
identities that fail (smallest items first), cells named by single-cell tier 2 items (the page or OCR read disagrees,
an unverified read, a number printed nowhere), cells whose OCR read disagrees with the extraction while every identity
through them ties (agreeing or cancelling errors), and blanks. At most `max_share` x (numbers) requests are written.
Each request is one PNG crop (the cell's row with its label, the column header band above it when the page has one,
the cell outlined in red; a few hundred pixels tall) located from the page reader's own read of the cell, else the
spot where the extracted number is printed, else the row's label line; and one line of requests.jsonl:
{req_id, key, crop, question, row_label, column, reason, doc, page}. The question names the row label and the column
header and asks for the exact printed string or null. Neither the model's value nor the OCR read is in the question.

Answers: answers.jsonl next to requests.jsonl, one {req_id, printed_text} per line (printed_text null when the cell is
empty or unreadable). `run(..., reread=path)` uses them as a third reader (core.py: rr_*; README "Re-read tier").
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Dict, List, Optional

from . import bundle as B
from .identities import _num
from .page import data_row as _data_row

PRIORITY = {"identity": 0, "cell": 1, "agreeing": 2, "blank": 3}
SINGLE = ("misread", "label_unverified", "readers_disagree", "label_withheld", "sign_unclear", "review", None)


def _label(bun, r) -> str:
    f = bun.fields.get(r.field)
    lab = (f.labels[0] if f and f.labels else "") or ""
    if r.entity:
        lab = (lab + " " if lab else "") + str(r.entity)
    return lab or r.field


def select(res: dict, ctx: dict, max_share: float = 0.6) -> List[tuple]:
    """[(key, reason)] in priority order, capped at max_share x the number of records."""
    bun, info = ctx["bun"], ctx["info"]
    out: Dict[str, tuple] = {}

    def add(k, why, rank, size=0):
        if k not in out or (rank, size) < out[k][:2]:
            out[k] = (rank, size, why)
    for t in res["tier2"]:
        ev = t.get("evidence") or {}
        if t.get("reason") in ("scale",):
            continue
        if ev.get("checks"):
            for k in t["keys"]:
                add(k, t.get("reason") or "identity_fails", PRIORITY["identity"], len(t["keys"]))
        elif t.get("reason") == "missing":
            for k in t["keys"]:
                if ctx["cites"].get(k) is not None and ctx["cites"][k].value is None:
                    add(k, "blank", PRIORITY["blank"], len(t["keys"]))
        else:
            for k in t["keys"]:
                add(k, t.get("reason") or "review", PRIORITY["cell"], len(t["keys"]))
    flagged = set(out)
    fixed = {f["key"] for f in res["fixes"]}
    for k in (info.get("ocr_alts") or {}):
        if k not in flagged and k not in fixed:
            add(k, "ocr_disagrees_identities_tie", PRIORITY["agreeing"])
    cap = int(max_share * len(bun.records))
    order = sorted(out.items(), key=lambda x: (x[1][0], x[1][1], x[0]))
    return [(k, v[2]) for k, v in order[:cap]]


def _ocr_frame(ocr_dir, doc, page):
    try:
        d = json.load(open(os.path.join(ocr_dir, f"{doc}.json")))
        for p in d.get("pages") or []:
            if int(p.get("page")) == int(page):
                return float(p.get("width")), float(p.get("skew") or 0.0)
    except Exception:
        pass
    return None


def _pdf_width(pdf, page):
    try:
        import pdfplumber
        with pdfplumber.open(pdf) as d:
            return float(d.pages[page - 1].width)
    except Exception:
        return None


def crop(img_path: str, frame_w: float, skew: float, cell: Optional[list], row: list, header: Optional[list], out: str,
         max_h: int = 340, max_w: int = 1400, header_img: Optional[str] = None) -> Optional[tuple]:
    """Write the crop PNG: the column header band (from `header_img` when the header is on an earlier page) stitched
    above the row band, the cell outlined in red. Returns its (width, height) or None."""
    from PIL import Image, ImageDraw

    def load(p):
        im_ = Image.open(p).convert("RGB")
        return im_.rotate(skew, resample=Image.BICUBIC, fillcolor=(255, 255, 255)) if skew else im_
    im = load(img_path)
    s = im.width / frame_w
    h_ = max(6.0, row[3] - row[1])
    lo = min(row[0], cell[0]) if cell else row[0]
    hi = max(row[2], cell[2]) if cell else row[2]
    x0 = max(0, int((lo - 2.0 * h_) * s))
    x1 = min(im.width, int((hi + 2.0 * h_) * s))      # room right of the cell: a clipped last digit is a misread
    pad = 0.9 * h_
    top_ = min(row[1], cell[1]) if cell else row[1]
    bot_ = max(row[3], cell[3]) if cell else row[3]
    rb = (x0, max(0, int((top_ - pad) * s)), x1, min(im.height, int((bot_ + pad) * s)))
    if rb[2] - rb[0] < 10 or rb[3] - rb[1] < 4:
        return None
    strips = []
    if header is not None:
        him = load(header_img) if header_img else im
        hb = (x0, max(0, int((header[1] - 0.3 * h_) * s)), x1, min(him.height, int((header[3] + 0.3 * h_) * s)))
        if hb[3] > hb[1] + 3 and (header_img or hb[1] < rb[1]):
            if not header_img and hb[3] > rb[1]:
                hb = (hb[0], hb[1], hb[2], rb[1])
            if hb[3] > hb[1] + 3:
                strips.append(him.crop(hb))
    rs = im.crop(rb)
    if cell is not None:
        d = ImageDraw.Draw(rs)
        cx0, cy0, cx1, cy1 = [(cell[0] - 2.5) * s - rb[0], (cell[1] - 2.5) * s - rb[1], (cell[2] + 2.5) * s - rb[0], (cell[3] + 2.5) * s - rb[1]]
        d.rectangle([cx0, cy0, cx1, cy1], outline=(220, 0, 0), width=2)
    strips.append(rs)
    w = max(x.width for x in strips)
    H = sum(x.height for x in strips) + 4 * (len(strips) - 1)
    canvas = Image.new("RGB", (w, H), (255, 255, 255))
    y = 0
    for n, x in enumerate(strips):
        canvas.paste(x, (0, y))
        y += x.height
        if n < len(strips) - 1:
            ImageDraw.Draw(canvas).rectangle([0, y, w, y + 3], fill=(170, 170, 170))
            y += 4
    # legible: text rows at least ~22 px tall, then within the size limits
    up = min(3.0, max(1.0, 22.0 / max(1.0, h_ * s)))
    W2, H2 = canvas.width * up, canvas.height * up
    down = min(1.0, max_h / H2, max_w / W2)
    f = up * down
    if abs(f - 1) > 1e-3:
        canvas = canvas.resize((max(1, int(canvas.width * f)), max(1, int(canvas.height * f))), Image.LANCZOS)
    canvas.save(out)
    return canvas.size


def question(lab: str, col: Optional[str], outlined: bool = True) -> str:
    where = "the cell outlined in red" if outlined else f'the row shown, in the column "{col}"' if col else "the row shown"
    return (f'What number is printed in {where}? We expect the row "{lab}"' + (f' and the column "{col}"' if col and outlined else "")
            + '. Reply with JSON only: {"printed_text": the exact printed string (for example "1,234" or "(56)"), or null if the cell'
            + ' is empty or unreadable; "row_label": the row label printed at the left of that row, as printed; "column_label": the'
            + ' column heading printed above that cell, as printed, or null if none is shown}.')


_YEARISH = re.compile(r"^(19|20)\d\d\b|^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?$|^(quarter|months?|weeks?|year|ended)$", re.I)


def _box(ws) -> list:
    return [min(w.x0 for w in ws), min(w.top for w in ws), max(w.x1 for w in ws), max(w.bottom for w in ws)]


def _wmatch(w: str, have: set) -> bool:
    import difflib
    if w in have:
        return True
    return len(w) >= 4 and any(len(t) >= 3 and difflib.SequenceMatcher(None, w, t).ratio() >= 0.8 for t in have)


def _label_sets(bun, r) -> List[List[str]]:
    """The words a row label of this record prints: each label of its field (per document first), plus its entity's."""
    from .page import label_words, entity_words
    f = bun.field_of(r)
    e_ = re.sub(r"\s*#\d+\s*$", "", r.entity) if r.entity else None      # '#n' numbers repeated entities; not printed
    ent = [w.lstrip("#").lower() for w in entity_words(e_)] if e_ else []
    sets = [label_words(l) + ent for l in (f.labels or []) if label_words(l)]
    if not sets and ent:
        sets = [ent]
    if not sets and not r.entity:
        sets = [label_words(r.field)]
    return [x for x in sets if x]


class _Rows:
    """The page reader's words of one document, by page: row bands, label scores, number words."""

    def __init__(self, lines):
        self.by_page: Dict[int, list] = {}
        for ln in lines:
            self.by_page.setdefault(ln.page, []).extend(ln.words)
        self.lines = lines

    def band(self, page, y0, y1):
        h = max(4.0, y1 - y0)
        lo, hi = y0 - 0.1 * h, y1 + 0.1 * h      # OCR boxes of adjacent rows overlap: a word's centre must sit in the row
        return [w for w in self.by_page.get(page, ()) if lo <= (w.top + w.bottom) / 2 <= hi]

    def label_score(self, page, y0, y1, x_right, sets) -> float:
        """Share of the label's words printed on the row, either side of the cell (a log or schedule prints the program
        name right of its amount), or on a wrapped label line just above, left of `x_right`."""
        ws = self.band(page, y0, y1)
        h = max(4.0, y1 - y0)
        above = [w for w in self.by_page.get(page, ()) if y0 - 1.6 * h <= (w.top + w.bottom) / 2 < y0 - 0.1 * h]
        if above and not any(re.search(r"\d", w.text) for w in above):
            ws = ws + [w for w in above if w.x1 <= x_right + 2]   # a label wrapped onto two lines; the number prints on the second
        txt = " ".join(w.text for w in ws).lower()
        have = set(re.findall(r"[a-z]+", txt)) | set(re.findall(r"[a-z0-9][a-z0-9\-/]*", txt))
        best = 0.0
        for want in sets:
            best = max(best, sum(_wmatch(w, have) for w in want) / len(want))
        return best

    def numbers(self, page, y0, y1, x_left):
        """Boxes of the numbers printed on the row right of `x_left` (a '$', '(' or ')' beside a number joins it)."""
        groups = []
        for w in sorted(self.band(page, y0, y1), key=lambda w: w.x0):
            if w.x0 < x_left - 1:
                continue
            dig = bool(re.search(r"\d", w.text)) and not _YEARISH.match(w.text)
            if not dig and w.text.strip() not in ("$", "(", ")", "-", "$(", ")%"):
                groups.append(None)
                continue
            last = groups[-1] if groups else None
            if last and w.x0 - last[-1].x1 < 0.7 * max(1.0, w.bottom - w.top):
                last.append(w)
            else:
                groups.append([w])
        return [_box(g) for g in groups if g and any(re.search(r"\d", w.text) for w in g)]

    def label_lines(self, page, sets):
        """[(score, extra, page, row box)] of the rows on `page` (all pages when None) that print the label: score the
        share of the label's words on the row, extra the row label's words that are not the label's. A row the reader
        split into several lines (label, numbers) is one row."""
        rows = []
        for ln in sorted(self.lines, key=lambda l_: (l_.page, l_.top)):
            if (page is not None and ln.page != page) or not ln.words:
                continue
            b = _box(ln.words)
            for rw in rows:
                if rw[0] == ln.page and min(rw[1][3], b[3]) - max(rw[1][1], b[1]) > 0.5 * min(rw[1][3] - rw[1][1], b[3] - b[1]):
                    rw[1][:] = [min(rw[1][0], b[0]), min(rw[1][1], b[1]), max(rw[1][2], b[2]), max(rw[1][3], b[3])]
                    rw[2].extend(ln.words)
                    break
            else:
                rows.append([ln.page, list(b), list(ln.words)])
        out = []
        for pg, b, ws in rows:
            nums = [w for w in ws if re.search(r"\d", w.text) and not _YEARISH.match(w.text)]
            xr = min((w.x0 for w in nums), default=b[2])
            sc = self.label_score(pg, b[1], b[3], xr, sets)
            if sc >= 0.6:
                lab = set(re.findall(r"[a-z]{3,}", " ".join(w.text for w in ws if w.x1 <= xr + 2).lower())) - {"the", "and"}
                extra = min(len([t for t in lab if not any(_wmatch(t, {w}) for w in want)]) for want in sets)
                out.append((sc, extra, pg, b))
        return out


def _nearest(cands, x_right, tol):
    if not cands:
        return None
    c = min(cands, key=lambda b: abs(b[2] - x_right))
    return c if abs(c[2] - x_right) <= tol else None


def make_requests(bundle_dir: str, extraction: List[dict], mode: str, out_dir: str, images: str, ocr_dir: Optional[str] = None,
                  identities: str = "hand", schema: str = "hand", max_share: float = 0.6) -> dict:
    from .core import run, cite, diagnose
    from .page import Pages
    ctx: dict = {}
    res = run(bundle_dir, extraction, mode, ocr_dir if mode == "ocr" else None, identities=identities, schema=schema, _ctx=ctx)
    bun = ctx["bun"]
    recs = {r.key: r for r in bun.records}
    picks = select(res, ctx, max_share)
    # the page reader used to place the cells: the run's own (text / ocr), or for notext the OCR (else the text layer)
    if mode == "notext":
        lp = Pages(bun, "ocr", ocr_dir) if ocr_dir else Pages(bun, "text")
        lc = cite(bun, lp, extraction)
        inf = {"reads_only": True}
        import copy
        b0 = copy.copy(bun)
        b0.identities = []
        diagnose(b0, lp, lc, info=inf)
        own, cites = inf["own"], lc
    else:
        lp, own, cites = ctx["pages"], ctx["info"]["own"], ctx["cites"]
    loc_ocr = lp.mode == "ocr"
    os.makedirs(os.path.join(out_dir, "crops"), exist_ok=True)
    rows, skipped = [], []
    seen_cells: Dict[tuple, int] = {}
    for n, (k, why) in enumerate(picks):
        r = recs[k]
        try:
            o = own(k)
        except Exception:
            o = None
        page, cell = (o[1], list(o[2])) if o else (None, None)
        loc = "own" if o else None
        c = cites.get(k)
        alt = [c.page, [round(x, 1) for x in c.bbox]] if c is not None and c.bbox else None
        if page is None and alt:
            page, cell, loc = alt[0], list(alt[1]), "cite"
        try:
            lines = lp.lines(r.doc) if lp.has_text(r.doc) else []
        except Exception:
            lines = []
        if loc == "cite" and loc_ocr and cell:
            # the extraction's box is in PDF points; the page reader's words are in the OCR frame
            fo, wp = _ocr_frame(ocr_dir, r.doc, page), _pdf_width(bun.pdf(r.doc), page)
            if fo and wp and abs(fo[0] / wp - 1) > 0.05:
                cell = [x * fo[0] / wp for x in cell]
        if not lines:
            skipped.append(dict(key=k, why="no_page_words"))
            continue
        R_ = _Rows(lines)
        sets = _label_sets(bun, r)
        hint = cell
        placed = None
        # 1. the row check: the cell sits on a row whose label prints the record's label (OCR words, fuzzy)
        if cell is not None and sets and R_.label_score(page, cell[1], cell[3], cell[0], sets) >= 0.6:
            placed = (page, cell, loc)
        # 2. else the line that prints the label (on the cell's page, else the cited page, else the document: one line
        #    only), and on it the number under the column (the hinted cell's place, the column header, the column's other
        #    cells) or the row's only number
        if placed is None and sets:
            pg = page if page is not None else (cites[k].page if cites.get(k) is not None else None)
            cands = R_.label_lines(pg, sets) or ([] if pg is None else [])
            if not cands and pg is not None and loc != "own":
                cands = R_.label_lines(None, sets)
            if cands:
                cands.sort(key=lambda c_: (-c_[0], c_[1]))
                best = [c_ for c_ in cands if (c_[0], c_[1]) == (cands[0][0], cands[0][1])]
                if len(best) == 1:
                    _, _, pg2, rowb = best[0]
                    nums = R_.numbers(pg2, rowb[1], rowb[3], rowb[0])
                    nums = [b_ for b_ in nums if R_.label_score(pg2, b_[1], b_[3], b_[0], sets) >= 0.6]
                    xs = None
                    if hint is not None and pg2 == page:
                        xs = hint[2]
                    elif r.column:
                        col_x = []
                        for j, rj in recs.items():
                            if j != k and rj.doc == r.doc and rj.column == r.column:
                                try:
                                    oj = own(j)
                                except Exception:
                                    oj = None
                                if oj is not None and oj[1] == pg2:
                                    col_x.append(oj[2][2])
                        if col_x:
                            xs = sorted(col_x)[len(col_x) // 2]
                        else:
                            # no other cell of the column read on the page: the right edge of the header cell that names it
                            try:
                                h, hp = lp.find_header(r.doc, pg2, rowb[1], r.column)
                            except Exception:
                                h = None
                            if h:
                                xs = h[1][h[2]]
                            else:
                                # nor a header line naming it: the column's year, printed once on the nearest dated line above
                                yrs = set(re.findall(r"\b(?:19|20)\d\d\b", str(r.column)))
                                if len(yrs) == 1:
                                    ab = sorted((ln for ln in lp.lines(r.doc) if ln.page == pg2 and ln.top < rowb[1] - 1
                                                 and any(_YEARISH.match(w.text) for w in ln.words) and not _data_row(ln.text)),
                                                key=lambda l_: -l_.top)
                                    for ln in ab[:3]:
                                        hit = [w for w in ln.words if re.sub(r"\W", "", w.text) in yrs]
                                        if hit:
                                            xs = hit[0].x1 if len(hit) == 1 else None
                                            break
                    hgt = max(4.0, rowb[3] - rowb[1])
                    c2 = _nearest(nums, xs, 4.0 * hgt) if xs is not None else (nums[0] if len(nums) == 1 else None)
                    if c2 is not None:
                        placed = (pg2, c2, "label_line")
                    else:
                        skipped.append(dict(key=k, why="unplaced_no_cell_on_label_row"))
                        continue
                else:
                    skipped.append(dict(key=k, why="unplaced_several_label_rows"))
                    continue
        if placed is None:
            skipped.append(dict(key=k, why="unplaced_row_label_not_found" if sets else "unplaced_no_label"))
            continue
        page, cell, loc = placed
        cell = list(cell)
        hgt = max(4.0, cell[3] - cell[1])
        rowws = R_.band(page, cell[1], cell[3])
        line = _box(rowws) if rowws else list(cell)
        line = [min(line[0], cell[0]), min(line[1], cell[1]), max(line[2], cell[2]), max(line[3], cell[3])]
        # 3. the column header strip: the header lines that name the column (with the period and year lines just above
        #    and below them), else the nearest line above the row that prints a year, month or period. The place of the
        #    cell is not moved by the header (moving it on dev put as many cells off as on); the reader's column heading
        #    is checked by the run instead (core.py: a heading naming another year is a wrong cell)
        header, header_page, hdr_kind = None, page, "none"
        if r.column:
            try:
                h, hp = lp.find_header(r.doc, page, line[1], r.column)
            except Exception:
                h, hp = None, None
            if h:
                hl = [ln for ln in lp.lines(r.doc) if ln.page == hp and h[0] - 3.5 * hgt <= ln.top <= h[0] + 2.5 * hgt
                      and (hp != page or ln.top < line[1] - 1) and not _data_row(ln.text)]
                if hl:
                    hb = _box([w for ln in hl for w in ln.words])
                    header, header_page, hdr_kind = [line[0], hb[1], line[2], hb[3]], hp, "header" if hp == page else "header_prev_page"
            if header is None:
                # no header line names the column: the nearest line above the row that prints a year, month or period
                ab = [ln for ln in lp.lines(r.doc) if ln.page == page and ln.top < line[1] - 1
                      and any(_YEARISH.match(w.text) for w in ln.words) and not _data_row(ln.text)]
                if ab:
                    ln = max(ab, key=lambda l_: l_.top)
                    hb = _box(ln.words)
                    header, hdr_kind = [line[0], hb[1], line[2], hb[3]], "dated_line"
        img = os.path.join(images, r.doc, f"page-{page}.png")
        if not os.path.exists(img):
            skipped.append(dict(key=k, why="no_image"))
            continue
        fr = _ocr_frame(ocr_dir, r.doc, page) if loc_ocr else None
        if fr is None:
            w = _pdf_width(bun.pdf(r.doc), page)
            fr = (w, 0.0) if w else None
        if fr is None:
            skipped.append(dict(key=k, why="no_frame"))
            continue
        cell_key = (r.doc, page, tuple(round(x) for x in cell))
        if cell_key in seen_cells:
            # two records placed on one cell: one crop, one request; the answer goes to both, and the run checks the
            # printed row label and column heading against each record's
            rows[seen_cells[cell_key]].setdefault("also_keys", []).append(k)
            skipped.append(dict(key=k, why="same_cell_as_" + rows[seen_cells[cell_key]]["req_id"]))
            continue
        rid = f"r{n:05d}"
        cp = os.path.join("crops", f"{rid}.png")
        himg = os.path.join(images, r.doc, f"page-{header_page}.png") if header is not None and header_page != page else None
        if himg and not os.path.exists(himg):
            header, himg, hdr_kind = None, None, "none"
        try:
            size = crop(img, fr[0], fr[1], cell, line, header, os.path.join(out_dir, cp), header_img=himg)
        except Exception as e:
            size = None
            skipped.append(dict(key=k, why=f"crop_error {e}"))
            continue
        if size is None:
            skipped.append(dict(key=k, why="empty_crop"))
            continue
        lab, col = _label(bun, r), r.column
        q = question(re.sub(r"\s*#\d+\s*$", "", lab), col, cell is not None)     # '#n' numbers repeated entities; not printed
        rows.append(dict(req_id=rid, key=k, crop=cp, question=q, row_label=lab, column=col, reason=why, doc=r.doc, page=page,
                         size=list(size), cell_pt=[round(x, 1) for x in cell] if cell else None, row_pt=[round(x, 1) for x in line],
                         loc=loc, cite_pt=alt, header=hdr_kind))
        seen_cells[cell_key] = len(rows) - 1
    with open(os.path.join(out_dir, "requests.jsonl"), "w") as f:
        for x in rows:
            f.write(json.dumps(x) + "\n")
    man = dict(bundle=bundle_dir, mode=mode, ocr_dir=ocr_dir, images=images, identities=identities, schema=schema, numbers=len(bun.records),
               requests=len(rows), selected=len(picks), skipped=skipped, per_100=round(100 * len(rows) / max(1, len(bun.records)), 1),
               by_reason=_count(x["reason"] for x in rows), by_loc=_count(x["loc"] for x in rows),
               by_header=_count(x["header"] for x in rows if x["column"]), skipped_by=_count(x["why"] if not x["why"].startswith("same_cell") else "same_cell (also_keys)" for x in skipped), max_height=max((x["size"][1] for x in rows), default=0))
    json.dump(man, open(os.path.join(out_dir, "manifest.json"), "w"), indent=1)
    return man


def _count(it):
    out: Dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def printed_of(t: dict) -> Optional[str]:
    for f in ("text", "printed_text"):
        if t.get(f):
            return str(t[f])
    v = t.get("value")
    if not _num(v):
        return None
    sc = float(t.get("scale") or 1.0) if B.is_money(str(t.get("units") or "")) else 1.0
    x = v / sc
    s = f"{abs(x):,.0f}" if abs(x - round(x)) < 1e-9 else f"{abs(x):,.2f}"
    return f"({s})" if x < 0 else s


def _spot_answer(q, truth, by_page):
    """What a reader of the outlined cell sees (when the truth has cell boxes): the truth of the cell under the outline,
    another record's number when the outline sits on another cell, None when no cell is outlined."""
    t = truth.get(q["key"])
    cb = q.get("cell_pt")
    if not t or not t.get("bbox"):
        return "own" if not cb else None
    if not cb:
        # no outline: the reader reads the row band in the asked column; the row band may sit on another row
        rb = q.get("row_pt")
        if not rb:
            return None
        inb = lambda u: u["page"] == q["page"] and rb[1] - 3 <= (u["bbox"][1] + u["bbox"][3]) / 2 <= rb[3] + 3
        if inb(t):
            return "own"
        col = q["key"].split("|")[-1]
        same = [u for u in by_page.get((t["doc"], q["page"]), ()) if inb(u) and str(u.get("key", "")).split("|")[-1] == col]
        return same[0] if same else None
    cy, cx = (cb[1] + cb[3]) / 2, (cb[0] + cb[2]) / 2
    best = None
    for u in by_page.get((t["doc"], q["page"]), ()):
        b = u["bbox"]
        d = abs((b[1] + b[3]) / 2 - cy) + 0.3 * abs((b[0] + b[2]) / 2 - cx)
        if abs((b[1] + b[3]) / 2 - cy) < 6 and abs((b[0] + b[2]) / 2 - cx) < 20 and (best is None or d < best[0]):
            best = (d, u)
    return best[1] if best else None


def simulate(req_dir: str, bundle_dir: str, out: Optional[str] = None, digit: float = 0.05, null: float = 0.03, seed: int = 0,
             spot: bool = True, words_dir: Optional[str] = None) -> str:
    """Answers from the truth with read noise: `null` of them null, `digit` of them with one digit replaced. With
    `spot` (and truth cell boxes), the reader reads the outlined cell: another record's number when the outline sits on
    another cell, null when the outline sits on no record's cell or there is no outline. With `words_dir` (OCR json of
    the pages; a stress test), an outline on no record's cell is read as the words printed there (as the OCR has them),
    with the row label left out unless the record's own cell is on that row (so the row check cannot catch it)."""
    ocr_words = {}

    def words_at(doc, page, cb):
        if doc not in ocr_words:
            try:
                ocr_words[doc] = {int(pg["page"]): pg.get("words") or [] for pg in json.load(open(os.path.join(words_dir, doc + ".json")))["pages"]}
            except Exception:
                ocr_words[doc] = {}
        ws = [w for w in ocr_words[doc].get(page, []) if w[3] > cb[0] - 1 and w[1] < cb[2] + 1 and w[4] > cb[1] - 1 and w[2] < cb[3] + 1]
        txt = " ".join(str(w[0]) for w in sorted(ws, key=lambda w: w[1]))
        return txt if re.search(r"\d", txt) else None
    truth = B.truth(bundle_dir)
    bun = B.load(bundle_dir, identities_file=False)
    recs = {r.key: r for r in bun.records}
    for k_, t_ in truth.items():
        t_.setdefault("key", k_)
    by_page: Dict[tuple, list] = {}
    for t in truth.values():
        if t.get("bbox") and t.get("page"):
            by_page.setdefault((t["doc"], t["page"]), []).append(t)
    rng = random.Random(seed)
    out = out or os.path.join(req_dir, "answers.jsonl")
    with open(out, "w") as f:
        for l in open(os.path.join(req_dir, "requests.jsonl")):
            if not l.strip():
                continue
            q = json.loads(l)
            t = truth.get(q["key"])
            raw = None
            if spot and by_page:
                u = _spot_answer(q, truth, by_page)
                if u is None and words_dir and q.get("cell_pt") and t is not None:
                    raw = words_at(t["doc"], q["page"], q["cell_pt"])
                    tb, cb = t.get("bbox"), q["cell_pt"]
                    same_row = bool(tb) and abs((tb[1] + tb[3]) / 2 - (cb[1] + cb[3]) / 2) < 6
                t = t if u == "own" else u
            if raw is not None:
                rl, cl, p = (_label(bun, recs[q["key"]]) if same_row else None), None, raw
            else:
                rl = _label(bun, recs[t["key"]]) if t and t.get("key") in recs else None
                cl = recs[t["key"]].column if t and t.get("key") in recs else None
                p = printed_of(t) if t else None
            u = rng.random()
            if p is None or u < null:
                p = None
            elif u < null + digit:
                ds = [i for i, c in enumerate(p) if c.isdigit()]
                if ds:
                    i = rng.choice(ds)
                    p = p[:i] + rng.choice([c for c in "0123456789" if c != p[i]]) + p[i + 1:]
            f.write(json.dumps(dict(req_id=q["req_id"], printed_text=p, row_label=rl, column_label=cl)) + "\n")
    return out
