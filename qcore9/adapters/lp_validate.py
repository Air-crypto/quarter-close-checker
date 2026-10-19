"""Validate qcore against the frozen LP checker (v2) on the held-out 2 model runs.

    python -m qcore9.adapters.lp_validate <runs dir> [--out out/qcore/lp/runs] [--v2-ref <dir>] [--run-v2]

<runs dir> holds synth_fundNN_<model>/docMM/{answer.json,fields.json}. For each run and page-text mode
(text, notext) it writes <out>/<run>/{extraction.json, <mode>_result.json, <mode>_score.json}.
With --run-v2 it also scores the frozen v2 on the same answers (v2's own synth_eval.score_one, unchanged,
in a subprocess) into <v2-ref>/<run>/<mode>/score.json. Then it compares outcomes error by error and
writes <out>/comparison.json.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
MODES = ("text", "notext")
V2_SCRIPT = r"""
import os, sys, json
mode, run_dir, out = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "notext":
    os.environ["QCLOSE_NO_PAGE_TEXT"] = "1"
sys.path.insert(0, os.path.join(sys.argv[4], "v2"))
from qclose import synth_eval
synth_eval.score_one(run_dir, mode, out_dir=out)
"""


def _one(args):
    runs_dir, run, mode, out = args
    sys.path.insert(0, ROOT)
    import qcore9 as qcore
    from qcore9.adapters import lp
    fund = run.split("_")[1]
    bundle = os.path.join(ROOT, "out", "qcore", "lp", fund)
    os.makedirs(os.path.join(out, run), exist_ok=True)
    ext_path = os.path.join(out, run, "extraction.json")
    ext = lp.extraction(os.path.join(runs_dir, run), os.path.join(ROOT, "out", "synth", fund), bundle)
    json.dump(ext, open(ext_path, "w"), indent=0)
    res = qcore.run(bundle, ext, mode)
    card = qcore.score(bundle, ext, res)
    json.dump(res, open(os.path.join(out, run, f"{mode}_result.json"), "w"), indent=0, default=str)
    json.dump(card, open(os.path.join(out, run, f"{mode}_score.json"), "w"), indent=0, default=str)
    return run, mode


def _v2(args):
    runs_dir, run, mode, ref = args
    dest = os.path.join(ref, run, mode)
    os.makedirs(dest, exist_ok=True)
    subprocess.run([sys.executable, "-c", V2_SCRIPT, mode, os.path.join(runs_dir, run), dest, ROOT], check=True,
                   cwd=dest, env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    return run, mode


def compare(runs, out, ref):
    rows, diffs = [], []
    tot = {m: collections.Counter() for m in MODES}
    for run in runs:
        for m in MODES:
            q = json.load(open(os.path.join(out, run, f"{m}_score.json")))
            vp = os.path.join(ref, run, m, "score.json")
            v = json.load(open(vp)) if os.path.exists(vp) else None
            qt = q["totals"]
            row = dict(run=run, mode=m, errors=qt["tier0_errors"],
                       qcore=dict(fixed=qt["fixed"], closer=qt["closer"], wrong=qt["wrong"], tier2=qt["tier2"], missed=qt["missed"],
                                  broke=qt["broke"], tier2_items=qt["tier2_items"]))
            if v:
                row["v2"] = dict(fixed=v["fixed"], closer=v["closer"], wrong=v["wrong"], tier2=v["tier2"], missed=v["missed"],
                                 broke=v["wrong_changes_to_correct_values"], tier2_items=v["tier2_items"])
                vo = {e["key"]: e["outcome"] for e in v["errors"]}
                qo = {e["key"]: e["outcome"] for e in q["errors"]}
                for k in sorted(set(vo) | set(qo)):
                    if vo.get(k) != qo.get(k):
                        diffs.append(dict(run=run, mode=m, key=k, v2=vo.get(k), qcore=qo.get(k)))
            for b in q["broke"]:
                diffs.append(dict(run=run, mode=m, key=b["key"], v2="correct", qcore="broke"))
            for who in ("qcore", "v2"):
                for k, x in (row.get(who) or {}).items():
                    tot[m][f"{who}_{k}"] += x
            tot[m]["errors"] += qt["tier0_errors"]
            rows.append(row)
    res = dict(rows=rows, totals={m: dict(c) for m, c in tot.items()}, differences=diffs)
    json.dump(res, open(os.path.join(out, "comparison.json"), "w"), indent=1)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qcore9.adapters.lp_validate")
    ap.add_argument("runs_dir")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "qcore9_work", "lp_runs"))
    ap.add_argument("--v2-ref", default=os.path.join(ROOT, "out", "qcore", "lp", "v2_reference"))
    ap.add_argument("--run-v2", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    runs = sorted(d for d in os.listdir(a.runs_dir) if d.startswith("synth_fund"))
    with ProcessPoolExecutor(a.jobs) as ex:
        list(ex.map(_one, [(a.runs_dir, r, m, a.out) for r in runs for m in MODES]))
        if a.run_v2:
            list(ex.map(_v2, [(a.runs_dir, r, m, a.v2_ref) for r in runs for m in MODES]))
    res = compare(runs, a.out, a.v2_ref)
    for m, t in res["totals"].items():
        print(m, json.dumps(t))
    print(len(res["differences"]), "per-error differences")


if __name__ == "__main__":
    main()
