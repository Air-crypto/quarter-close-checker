"""OCR page text: words read from the page images by an OCR engine, as a second, independent noisy reader.

    python -m qcore5 ocr <images dir> <out dir> --engine tesseract|doctr [--bundle <bundle>] [--dpi 110] [--docs a,b]

<images dir>/<doc>/page-N.png are the page images (the same images the extractor read). For each doc this writes
<out dir>/<doc>.json: {"engine", "doc", "pages": [{"page", "width", "height", "skew", "words": [[text, x0, top, x1,
bottom, conf], ...]}]} with coordinates in points (top-left origin, like pdfplumber) and conf in [0, 1].

The page scale (points per pixel) comes from the bundle's PDF page width when --bundle is given and the PDF exists,
else from --dpi (72 / dpi points per pixel). Each image is deskewed first: the angle (within +-3 degrees, 0.05 degree
steps) that maximises the variance of the row ink profile.

Engines (see README "OCR mode"):
- tesseract: Tesseract 5 through pytesseract, image upscaled 3x, --psm 6, word confidences / 100.
- doctr: docTR's db_resnet50 detector with the PARSeq recogniser, through OnnxTR (ONNX runtime, CPU), image upscaled
  2x. Weights are read from ONNXTR_CACHE_DIR (downloaded there on first use).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from typing import List, Optional

ENGINES = ("tesseract", "doctr")


def skew_angle(img) -> float:
    """Rotation (degrees, PIL convention) that makes the text rows horizontal."""
    import numpy as np
    from PIL import Image
    g = img.convert("L")
    small = g.resize((max(1, g.width // 2), max(1, g.height // 2)))

    def score(a):
        r = np.asarray(small.rotate(a, resample=Image.BILINEAR, fillcolor=255), dtype=float)
        return float(np.var((255.0 - r).sum(1)))
    coarse = max((score(a / 10.0), a / 10.0) for a in range(-30, 31))[1]
    fine = max((score(coarse + d / 100.0), coarse + d / 100.0) for d in range(-10, 11, 5))[1]
    return round(fine, 2)


_DOCTR = {}


def _doctr_model():
    if "m" not in _DOCTR:
        os.environ.setdefault("ONNXTR_CACHE_DIR", os.path.join(os.path.expanduser("~"), ".cache", "onnxtr"))
        from onnxtr.models import ocr_predictor
        _DOCTR["m"] = ocr_predictor(det_arch="db_resnet50", reco_arch="parseq", assume_straight_pages=True)
    return _DOCTR["m"]


def read_image(path: str, engine: str) -> dict:
    """Words of one page image, in the deskewed image's pixel coordinates: {"width", "height", "skew", "words"}."""
    from PIL import Image
    img = Image.open(path).convert("L")
    ang = skew_angle(img)
    if ang:
        img = img.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
    words = []
    if engine == "tesseract":
        import pytesseract
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")   # tesseract's OpenMP threads stall on a busy machine
        up = 3
        big = img.resize((img.width * up, img.height * up), Image.LANCZOS)
        d = pytesseract.image_to_data(big, config="--psm 6", output_type=pytesseract.Output.DICT)
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            if not t:
                continue
            c = float(d["conf"][i])
            words.append([t, d["left"][i] / up, d["top"][i] / up, (d["left"][i] + d["width"][i]) / up,
                          (d["top"][i] + d["height"][i]) / up, max(0.0, c) / 100.0])
    elif engine == "doctr":
        import numpy as np
        up = 2
        big = img.resize((img.width * up, img.height * up), Image.LANCZOS).convert("RGB")
        res = _doctr_model()([np.asarray(big)])
        for b in res.pages[0].blocks:
            for ln in b.lines:
                for w in ln.words:
                    (x0, y0), (x1, y1) = w.geometry
                    words.append([w.value, x0 * img.width, y0 * img.height, x1 * img.width, y1 * img.height, float(w.confidence)])
    else:
        raise ValueError(f"engine {engine!r} not in {ENGINES}")
    return dict(width=img.width, height=img.height, skew=ang, words=words)


def ocr_doc(doc_dir: str, engine: str, pt_per_px: float) -> List[dict]:
    pages = []
    for p in sorted(glob.glob(os.path.join(doc_dir, "page-*.png")), key=lambda s: int(re.findall(r"\d+", os.path.basename(s))[0])):
        n = int(re.findall(r"\d+", os.path.basename(p))[0])
        r = read_image(p, engine)
        s = pt_per_px
        r["words"] = [[t, round(x0 * s, 2), round(y0 * s, 2), round(x1 * s, 2), round(y1 * s, 2), round(c, 3)]
                      for t, x0, y0, x1, y1, c in r["words"]]
        pages.append(dict(page=n, width=round(r["width"] * s, 2), height=round(r["height"] * s, 2), skew=r["skew"], words=r["words"]))
    return pages


def _pt_per_px(doc_dir: str, pdf: Optional[str], dpi: Optional[float]) -> float:
    if pdf and os.path.exists(pdf):
        try:
            import pdfplumber
            from PIL import Image
            with pdfplumber.open(pdf) as d:
                w = float(d.pages[0].width)
            first = sorted(glob.glob(os.path.join(doc_dir, "page-*.png")))
            if first:
                return w / Image.open(first[0]).width
        except Exception:
            pass
    return 72.0 / float(dpi or 150.0)


def build(images: str, out: str, engine: str, bundle: Optional[str] = None, dpi: Optional[float] = None,
          docs: Optional[List[str]] = None) -> List[str]:
    os.makedirs(out, exist_ok=True)
    done = []
    names = docs or sorted(d for d in os.listdir(images) if os.path.isdir(os.path.join(images, d)))
    for doc in names:
        dd = os.path.join(images, doc)
        dest = os.path.join(out, f"{doc}.json")
        if os.path.exists(dest):
            done.append(dest)
            continue
        pdf = os.path.join(bundle, "docs", f"{doc}.pdf") if bundle else None
        pages = ocr_doc(dd, engine, _pt_per_px(dd, pdf, dpi))
        tmp = dest + ".tmp"
        json.dump(dict(engine=engine, doc=doc, pages=pages), open(tmp, "w"))
        os.replace(tmp, dest)
        done.append(dest)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qcore5 ocr")
    ap.add_argument("images")
    ap.add_argument("out")
    ap.add_argument("--engine", choices=ENGINES, required=True)
    ap.add_argument("--bundle")
    ap.add_argument("--dpi", type=float)
    ap.add_argument("--docs")
    a = ap.parse_args(argv)
    got = build(a.images, a.out, a.engine, a.bundle, a.dpi, a.docs.split(",") if a.docs else None)
    print(f"{len(got)} docs in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
