"""Load a bundle: docs/<doc_id>.pdf, schema.json, truth.jsonl, identities.json, meta.json."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .pdftext import NUMBER_FORMATS

KINDS = ("within_doc", "cross_doc", "cross_period")
LABEL_READS = ("replace", "backed")


@dataclass
class Field:
    name: str
    labels: List[str]
    units: str = "number"
    sign: str = "any"          # pos | neg | any
    scale: float = 1.0         # stored values are printed in units of `scale` (e.g. 1000: "in thousands", stored as printed)
    no_label: bool = False     # zero-config: the field's label is ambiguous in this document; its cells are not read by label
    weak: bool = False         # qcore7 zero-config: a label the page fits on one line per document but no extracted number
                               # confirms; its reads change a value only by an identity or as a digit misread


@dataclass
class Rec:
    """One printed number the extraction was asked for (no value: run() never sees the truth)."""
    key: str
    doc: str
    field: str
    column: Optional[str] = None
    period: Optional[str] = None
    entity: Optional[str] = None


@dataclass
class Identity:
    id: str
    terms: List[tuple]          # [(key, coef)]
    const: float = 0.0
    tol: float = 0.5
    kind: str = "within_doc"
    products: List[tuple] = field(default_factory=list)   # [(key1, key2, coef)]: coef * value[key1] * value[key2]


@dataclass
class Bundle:
    root: str
    domain: str
    fields: Dict[str, Field]
    records: List[Rec]
    identities: List[Identity]
    meta: dict = field(default_factory=dict)
    number_format: str = "us"     # us: 1,234.56 | dot_thousands: 1.234,56 and 60.000 | grouped: either separator
    label_read: str = "replace"   # replace: a confirmed own-line read replaces the value | backed: only with identity support
    doc_fields: Dict[tuple, Field] = field(default_factory=dict)   # {(doc, field): Field}: per-document labels (zero-config)

    def pdf(self, doc: str) -> str:
        return os.path.join(self.root, "docs", f"{doc}.pdf")

    def field_of(self, r: Rec) -> Field:
        return self.doc_fields.get((r.doc, r.field)) or self.fields.get(r.field) or Field(r.field, [], "number", "any")


def parse_identities(rows: list, known_keys=None) -> List[Identity]:
    """Validate identities.json rows. Raises ValueError on a malformed identity or an unknown key."""
    out, seen = [], set()
    for n, d in enumerate(rows):
        if not isinstance(d, dict) or "terms" not in d:
            raise ValueError(f"identity {n}: needs 'terms'")
        iid = str(d.get("id", f"id{n}"))
        if iid in seen:          # qcore5: a duplicate id is renamed with a suffix (the bundle is not edited)
            base, j = iid, 2
            while f"{base}#{j}" in seen:
                j += 1
            iid = f"{base}#{j}"
        seen.add(iid)
        terms = []
        for t in d["terms"]:
            if not (isinstance(t, (list, tuple)) and len(t) == 2):
                raise ValueError(f"identity {iid}: a term is [key, coef]")
            k, c = str(t[0]), float(t[1])
            if known_keys is not None and k not in known_keys:
                raise ValueError(f"identity {iid}: unknown key {k}")
            terms.append((k, c))
        prods = []
        for t in d.get("products") or []:
            if not (isinstance(t, (list, tuple)) and len(t) == 3):
                raise ValueError(f"identity {iid}: a product term is [key1, key2, coef]")
            k1, k2, c = str(t[0]), str(t[1]), float(t[2])
            for k in (k1, k2):
                if known_keys is not None and k not in known_keys:
                    raise ValueError(f"identity {iid}: unknown key {k}")
            prods.append((k1, k2, c))
        if not terms and not prods:
            raise ValueError(f"identity {iid}: no terms")
        kind = d.get("kind", "within_doc")
        if kind not in KINDS:
            raise ValueError(f"identity {iid}: kind {kind!r} not in {KINDS}")
        out.append(Identity(iid, terms, float(d.get("const", 0.0) or 0.0), float(d.get("tol", 0.5)), kind, prods))
    return out


def load(root: str, schema_file: bool = True, identities_file: bool = True) -> Bundle:
    """qcore6: schema_file / identities_file False (zero-config mode) loads a bundle without schema.json /
    identities.json (fields and identities are then discovered, see auto.py)."""
    sp = os.path.join(root, "schema.json")
    schema = json.load(open(sp)) if schema_file or os.path.exists(sp) else {}
    if not schema_file:
        schema = {k: v for k, v in schema.items() if k == "domain"}
    fields = {}
    for name, f in (schema.get("fields") or {}).items():
        labels = f.get("label", [])
        if isinstance(labels, str):
            labels = [labels]
        try:
            fscale = float(f.get("scale") or 1.0)
        except (TypeError, ValueError):
            raise ValueError(f"schema.json: field {name}: scale must be a number")
        if fscale <= 0:
            raise ValueError(f"schema.json: field {name}: scale must be positive")
        fields[name] = Field(name, list(labels or []), str(f.get("units") or "number"), str(f.get("sign") or "any"), fscale)
    recs = []
    with open(os.path.join(root, "truth.jsonl")) as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            recs.append(Rec(str(d["key"]), str(d["doc"]), str(d["field"]), d.get("column"), d.get("period"), d.get("entity")))
    keys = [r.key for r in recs]
    if len(set(keys)) != len(keys):
        raise ValueError("truth.jsonl: keys are not unique")
    ids = parse_identities(json.load(open(os.path.join(root, "identities.json"))), set(keys)) if identities_file else []
    mp = os.path.join(root, "meta.json")
    meta = json.load(open(mp)) if os.path.exists(mp) else {}
    fmt = str(schema.get("number_format") or "us")
    if fmt not in NUMBER_FORMATS:
        raise ValueError(f"schema.json: number_format {fmt!r} not in {NUMBER_FORMATS}")
    lr = str(schema.get("label_read") or "replace")
    if lr not in LABEL_READS:
        raise ValueError(f"schema.json: label_read {lr!r} not in {LABEL_READS}")
    return Bundle(root, str(schema.get("domain", "")), fields, recs, ids, meta, fmt, lr)


def truth(root: str) -> Dict[str, dict]:
    """{key: truth row} (for scoring only)."""
    out = {}
    with open(os.path.join(root, "truth.jsonl")) as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                out[str(d["key"])] = d
    return out


# ---- units

_MONEY_WORDS = {"currency", "money", "usd", "eur", "gbp", "jpy", "chf", "cad", "aud", "cny", "inr"}


def unit_base(units: str) -> str:
    return re.split(r"[_\s]", str(units or "").strip())[0]


def is_money(units: str) -> bool:
    """A currency amount: an ISO-4217-style code (USD, EUR, ...) or 'currency' / 'money', optionally
    with a suffix ('USD_rounded_0.1M'). Percentages and multiples are never read for these."""
    b = unit_base(units)
    return b.lower() in _MONEY_WORDS or (len(b) == 3 and b.isalpha() and b.isupper())


def is_rounded(units: str) -> bool:
    """A figure printed rounded ('USD_rounded_0.1M', '...approx'): the page can't give it exactly."""
    u = str(units or "").lower()
    return "round" in u or "approx" in u
