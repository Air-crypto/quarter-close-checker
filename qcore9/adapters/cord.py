"""CORD receipt bundles (cord_eval/bundles/cord_<split>) -> the bundle qcore5 runs on.

    python -m qcore9.adapters.cord <cord bundle> <out dir>

The source bundle is not modified. The derived bundle copies the source PDFs and copies truth.jsonl and meta.json
unchanged. identities.json differs only in the line identities (`line_products`, qcore5), and schema.json as below:

- number_format "grouped" instead of "dot_thousands". On CORD TRAIN (truth printed_text) 3,948 three-digit groups
  are separated by a comma and 1,379 by a dot (507 receipts use only commas, 188 only dots, 78 both), so a
  dot-only reading turns "50,000" into 50.0. "grouped" reads a separator followed by exactly three digits as a
  thousands separator, whichever character it is (the convention build_cord.parse_number uses for the truth).

- Labels that several receipt-level fields list (TOTAL is a label of total, subtotal, menuqty_cnt and menutype_cnt;
  ITEMS, TOTAL ITEM, SUB TOTAL, PAID, NET TOTAL) are kept only for the field that carries that label (same
  label words) most often on TRAIN, and dropped from all of them on a tie. qcore5 skips a line whose label
  another field of the document also claims, so a shared label would otherwise read nothing (or, before
  qcore5, the wrong row).

Every choice here was made on TRAIN data only.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import sys

NUMBER_FORMAT = "grouped"
LABEL_READ = "backed"


def _words(label):
    from ..page import label_words, label_ids
    return tuple(sorted(set(label_words(label) + label_ids(label))))


def prune_labels(fields: dict, train_truth: str) -> dict:
    """Each label word set stays with the one field that carries it most often on TRAIN (dropped on a tie)."""
    cnt = collections.Counter()
    for line in open(train_truth):
        if line.strip():
            t = json.loads(line)
            if t.get("label"):
                cnt[(_words(t["label"]), t["field"])] += 1
    claims = collections.defaultdict(set)
    for f, d in fields.items():
        for lab in d.get("label") or []:
            claims[_words(lab)].add(f)
    keep = {}
    for w, fs in claims.items():
        if len(fs) > 1:
            ranked = sorted(((cnt[(w, f)], f) for f in fs), reverse=True)
            keep[w] = ranked[0][1] if ranked[0][0] > ranked[1][0] else None
    out = {}
    for f, d in fields.items():
        d = dict(d)
        d["label"] = [lab for lab in d.get("label") or [] if keep.get(_words(lab), f) == f]
        out[f] = d
    return out


COUNT_OF = {"item_price": ("item_cnt", "item_unitprice"), "sub_price": ("sub_cnt", "sub_unitprice")}


def line_products(identities: list, truth_rows: list) -> list:
    """qcore5: each line identity `price - c x unitprice = 0` of the source bundle carries the line's true count c as a
    coefficient. Where the line's count is itself a record, the identity becomes `price - cnt x unitprice = 0` with the
    count as a value (a product term), so the count is checked and can be repaired like any other number, and the
    identity no longer depends on the truth. Lines without a count record keep the source identity."""
    keys = {t["key"]: t for t in truth_rows}
    by_line = {(t["doc"], t["field"], t["entity"]): t["key"] for t in truth_rows}
    out = []
    for d in identities:
        terms = d.get("terms") or []
        if ".line." in str(d.get("id")) and len(terms) == 2:
            price = next((k for k, c in terms if c == 1 and keys.get(k, {}).get("field") in COUNT_OF), None)
            unit = next((k for k, c in terms if k != price), None)
            if price and unit:
                t = keys[price]
                cnt_field, unit_field = COUNT_OF[t["field"]]
                cnt = by_line.get((t["doc"], cnt_field, t["entity"]))
                if cnt and keys[unit]["field"] == unit_field:
                    d = dict(d, terms=[[price, 1]], products=[[cnt, unit, -1]], note="price = cnt x unitprice (qcore5 product term)")
        out.append(d)
    return out


def convert(src: str, out: str, train: str = None) -> str:
    train = train or os.path.join(os.path.dirname(os.path.abspath(src)), "cord_train")
    os.makedirs(out, exist_ok=True)
    docs = os.path.join(out, "docs")
    if not os.path.isdir(docs):           # a copy, not a link: some filesystems have no symlinks
        shutil.copytree(os.path.join(src, "docs"), docs, dirs_exist_ok=True)
    for f in ("truth.jsonl", "meta.json"):
        shutil.copy(os.path.join(src, f), os.path.join(out, f))
    rows = [json.loads(l) for l in open(os.path.join(src, "truth.jsonl")) if l.strip()]
    ids = line_products(json.load(open(os.path.join(src, "identities.json"))), rows)
    json.dump(ids, open(os.path.join(out, "identities.json"), "w"))
    schema = json.load(open(os.path.join(src, "schema.json")))
    schema["number_format"] = NUMBER_FORMAT
    schema["label_read"] = LABEL_READ
    schema["fields"] = prune_labels(schema["fields"], os.path.join(train, "truth.jsonl"))
    schema.setdefault("adapter", "qcore5.adapters.cord")
    json.dump(schema, open(os.path.join(out, "schema.json"), "w"), indent=1)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(convert(sys.argv[1], sys.argv[2]))
