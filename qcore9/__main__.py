"""python -m qcore9 run <bundle> <extraction.json> [--mode text|notext|ocr] [--ocr-dir DIR] [--identities hand|auto|both]
                     [--schema hand|auto] [--reread answers.jsonl] [--out result.json]
python -m qcore9 score <bundle> <extraction.json> [result.json] [same options as run] [--out score.json]
python -m qcore9 ocr <images dir> <ocr dir> --engine tesseract|doctr [--bundle B] [--dpi N] [--docs a,b]
python -m qcore9 reread-requests <bundle> <extraction.json> --mode ocr|notext|text [--ocr-dir D] --images <page image dir>
                     --out <reqdir> [--identities ...] [--schema ...] [--max-share 0.6]
python -m qcore9 reread-simulate <reqdir> <bundle> [--digit 0.05] [--null 0.03] [--seed 0] [--words-dir OCR] [--out answers.jsonl]"""

from __future__ import annotations

import argparse
import json
import sys

from . import run, score


def _load_ext(path):
    d = json.load(open(path))
    if isinstance(d, dict):
        d = d.get("extraction") or d.get("records") or []
    return d


def _common(p):
    p.add_argument("--mode", choices=["text", "notext", "ocr"], default="text")
    p.add_argument("--ocr-dir")
    p.add_argument("--identities", choices=["hand", "auto", "both"], default="hand")
    p.add_argument("--schema", choices=["hand", "auto"], default="hand")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ocr"]:
        from .ocr import main as ocr_main
        return ocr_main(argv[1:])
    ap = argparse.ArgumentParser(prog="qcore9")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="tier 1 over a bundle")
    r.add_argument("bundle")
    r.add_argument("extraction")
    _common(r)
    r.add_argument("--reread")
    r.add_argument("--out")
    s = sub.add_parser("score", help="score a result against truth.jsonl")
    s.add_argument("bundle")
    s.add_argument("extraction")
    s.add_argument("result", nargs="?")
    _common(s)
    s.add_argument("--reread")
    s.add_argument("--out")
    q = sub.add_parser("reread-requests", help="crops and questions for a cheap model to re-read the uncertain cells")
    q.add_argument("bundle")
    q.add_argument("extraction")
    _common(q)
    q.add_argument("--images", required=True)
    q.add_argument("--out", required=True)
    q.add_argument("--max-share", type=float, default=0.6)
    m = sub.add_parser("reread-simulate", help="answers from truth with read noise (for tuning)")
    m.add_argument("reqdir")
    m.add_argument("bundle")
    m.add_argument("--digit", type=float, default=0.05)
    m.add_argument("--null", type=float, default=0.03)
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--words-dir", help="OCR json dir: read outlines on no record's cell as the words printed there (stress test)")
    m.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "reread-simulate":
        from .reread import simulate
        print(simulate(a.reqdir, a.bundle, a.out, a.digit, a.null, a.seed, True, a.words_dir))
        return 0
    ext = _load_ext(a.extraction)
    if a.cmd == "reread-requests":
        from .reread import make_requests
        man = make_requests(a.bundle, ext, a.mode, a.out, a.images, a.ocr_dir, a.identities, a.schema, a.max_share)
        print(json.dumps({k: v for k, v in man.items() if k != "skipped"}))
        return 0
    kw = dict(identities=a.identities, schema=a.schema, reread=a.reread)
    if a.cmd == "run":
        res = run(a.bundle, ext, a.mode, a.ocr_dir, **kw)
        text = json.dumps(res, indent=1, default=str)
        if a.out:
            open(a.out, "w").write(text)
            print(json.dumps(dict(fixes=len(res["fixes"]), tier2=len(res["tier2"]), failing_before=len(res["failing_before"]),
                                  failing_after=len(res["failing_after"]), identities=res["identities"], coverage=res["coverage"])))
        else:
            print(text)
        return 0
    res = json.load(open(a.result)) if a.result else run(a.bundle, ext, a.mode, a.ocr_dir, **kw)
    card = score(a.bundle, ext, res)
    if a.out:
        json.dump(card, open(a.out, "w"), indent=1, default=str)
    print(json.dumps(card["totals"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
