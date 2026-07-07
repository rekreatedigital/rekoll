#!/usr/bin/env python
"""Verify (and define the derivation rules for) the frozen semantic_v1 fixture.

This file is BOTH the library and the auditor for every derived field in
``semantic_v1.json``:

- the lexical-overlap tokenizer + stopword handling (pre-registered in
  PREREGISTRATION_semantic_v1.md section 5),
- the overlap measure and bucket assignment,
- the hard-negative miner: a self-contained Okapi BM25 (k1=1.5, b=0.75) that
  is deliberately NOT the embedding model under test and NOT the product's
  SQLite FTS5 code path,
- the canonical content hash.

Running it re-derives everything from the raw committed content and asserts
it matches what is frozen in the JSON, so a conductor can prove the derived
fields were not hand-tuned:

    python benchmarks/fixtures/verify_semantic_v1.py

Pure stdlib. No model, no network, no product imports.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_PATH = HERE / "semantic_v1.json"

SEED = 20260707

# Frozen stopword list (also embedded in the fixture metadata and covered by
# the content hash — this constant and the fixture copy must stay identical).
STOPWORDS = frozenset(
    """a about after all an and any are as at be been before being but by can
    could did do does doing for from had has have how i if in into is it its
    of on or our out over should so some that the their them then there these
    they this to under up was we were what when where which who why will with
    would you your""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Lowercase [a-z0-9]+ runs of length >= 2 (pre-registered tokenizer)."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2]


def content_tokens(text: str) -> set[str]:
    """Token set minus stopwords — the overlap vocabulary."""
    return {t for t in tokens(text) if t not in STOPWORDS}


def overlap(query: str, doc_texts: list[str]) -> float:
    """max over gold docs of |T(q) & T(g)| / |T(q)| (pre-registered measure)."""
    q = content_tokens(query)
    if not q:
        return 0.0
    return max((len(q & content_tokens(d)) / len(q)) for d in doc_texts)


def bucket(ov: float) -> str:
    """LOW [0,0.25), MED [0.25,0.5), HIGH [0.5,1.0] (pre-registered edges)."""
    if ov < 0.25:
        return "low"
    if ov < 0.5:
        return "med"
    return "high"


class OkapiBM25:
    """Self-contained Okapi BM25 (k1=1.5, b=0.75) used ONLY to mine hard
    negatives. Term frequencies come from the stopword-filtered token list
    (same tokenizer as the overlap measure)."""

    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tokens = {
            k: [t for t in tokens(v) if t not in STOPWORDS] for k, v in docs.items()
        }
        self.doc_len = {k: len(v) for k, v in self.doc_tokens.items()}
        self.avgdl = (sum(self.doc_len.values()) / len(self.doc_len)) if docs else 0.0
        self.n = len(docs)
        self.df: dict[str, int] = {}
        for toks in self.doc_tokens.values():
            for t in set(toks):
                self.df[t] = self.df.get(t, 0) + 1

    def score(self, query: str, key: str) -> float:
        q_terms = [t for t in tokens(query) if t not in STOPWORDS]
        toks = self.doc_tokens[key]
        if not toks:
            return 0.0
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in q_terms:
            if t not in tf:
                continue
            idf = math.log((self.n - self.df[t] + 0.5) / (self.df[t] + 0.5) + 1.0)
            denom = tf[t] + self.k1 * (
                1 - self.b + self.b * self.doc_len[key] / self.avgdl
            )
            s += idf * tf[t] * (self.k1 + 1) / denom
        return s


def mine_hard_negatives(
    query: str, gold: set[str], bm25: OkapiBM25, all_keys: list[str], n: int = 20
) -> list[str]:
    """Top-n non-gold committed docs by BM25; zero/tie broken by a seeded
    shuffle so every query gets exactly n (pre-registered, seed 20260707)."""
    rng = random.Random(SEED)
    shuffled = list(all_keys)
    rng.shuffle(shuffled)
    tiebreak = {k: i for i, k in enumerate(shuffled)}
    cands = [k for k in all_keys if k not in gold]
    cands.sort(key=lambda k: (-bm25.score(query, k), tiebreak[k]))
    return cands[:n]


def canonical_content_hash(fixture: dict) -> str:
    """sha256 over canonical JSON (sorted keys, compact separators, UTF-8) of
    the fixture minus metadata.content_sha256 (pre-registered, section 11)."""
    clone = json.loads(json.dumps(fixture))
    clone.get("metadata", {}).pop("content_sha256", None)
    blob = json.dumps(clone, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify(fixture: dict) -> list[str]:
    """Re-derive every derived field from raw content; return mismatches."""
    errors: list[str] = []
    docs = {d["key"]: d["text"] for d in fixture["documents"]}
    all_keys = [d["key"] for d in fixture["documents"]]

    if fixture["metadata"]["overlap"]["stopwords"] != sorted(STOPWORDS):
        errors.append("stopword list in fixture metadata != verifier constant")

    bm25 = OkapiBM25(docs)
    for q in fixture["queries"]:
        gold_texts = [docs[k] for k in q["relevant"]]
        ov = overlap(q["query"], gold_texts)
        if abs(ov - q["overlap"]) > 1e-9:
            errors.append(f"{q['qid']}: overlap {q['overlap']} != recomputed {ov}")
        if bucket(ov) != q["bucket"]:
            errors.append(f"{q['qid']}: bucket {q['bucket']} != recomputed {bucket(ov)}")
        mined = mine_hard_negatives(q["query"], set(q["relevant"]), bm25, all_keys)
        if mined != q["hard_negatives"]:
            errors.append(f"{q['qid']}: hard negatives differ from re-mined set")

    corpus_tokens: set[str] = set()
    for t in docs.values():
        corpus_tokens |= content_tokens(t)
    for c in fixture["controls"]:
        present = [t for t in c["control_terms"] if t in corpus_tokens]
        if present:
            errors.append(f"{c['qid']}: control terms present in corpus: {present}")

    recomputed = canonical_content_hash(fixture)
    frozen = fixture["metadata"].get("content_sha256")
    if recomputed != frozen:
        errors.append(f"content_sha256 {frozen} != recomputed {recomputed}")
    return errors


def main() -> int:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    errors = verify(fixture)
    if errors:
        print("FIXTURE VERIFICATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(
        f"semantic_v1 OK: {len(fixture['queries'])} scored queries, "
        f"{len(fixture['controls'])} controls, {len(fixture['documents'])} docs, "
        f"sha256={fixture['metadata']['content_sha256'][:16]}..."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
