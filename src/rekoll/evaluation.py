"""Retrieval evaluation: Recall@k, MRR, hit-rate@k, precision@k, AP/MAP, nDCG@k.

Decoupled from storage/embedding via ``search_fn(query) -> list[str]`` (ranked
record ids), so the same harness scores the CI stub gate, a fastembed run, or a
LongMemEval subset (ADR-0011). Keep this dependency-free.

Metric policy (ADR-0011 addendum): on binary/low-positive fixtures recall@k,
MRR, and hit-rate@k are the primary numbers; nDCG@k is a labeled diagnostic
(on binary gold it is roughly "rank of first hit") and is headlined only on a
genuinely graded fixture.
"""

from __future__ import annotations

import math
from collections.abc import Mapping as _MappingABC
from dataclasses import dataclass
from typing import (
    AbstractSet,
    Callable,
    FrozenSet,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

__all__ = [
    "LabeledQuery",
    "EvalResult",
    "QueryMetrics",
    "recall_at_k",
    "reciprocal_rank",
    "hit_rate_at_k",
    "precision_at_k",
    "average_precision",
    "ndcg_at_k",
    "evaluate",
]

#: Relevance for :func:`ndcg_at_k` — a binary set of relevant ids, or a graded
#: mapping ``id -> grade`` with integer grades >= 0.
Relevance = Union[AbstractSet[str], Mapping[str, int]]


@dataclass(frozen=True)
class LabeledQuery:
    """A query with its gold relevance labels.

    ``relevant_ids`` is the binary gold set (the original shape — positional
    construction ``LabeledQuery(query, relevant_ids)`` is unchanged).

    ``relevant_grades`` optionally adds graded relevance (``id -> grade``,
    integer grade >= 0) used by :func:`ndcg_at_k`; ids absent from the mapping
    have grade 0.

    PRE-REGISTERED BINARIZATION RULE: for binary metrics
    (recall/MRR/hit-rate/precision/AP), an id is relevant iff grade >= 1.
    If BOTH relevant_ids and relevant_grades are provided they must agree
    ({id : grade >= 1} == relevant_ids) — ValueError otherwise. (Since
    ``relevant_ids`` is always present, this agreement check runs whenever
    ``relevant_grades`` is given, so ``relevant_ids`` is always the correct
    binarized gold set.)
    """

    query: str
    relevant_ids: FrozenSet[str]
    relevant_grades: Optional[Mapping[str, int]] = None

    def __post_init__(self) -> None:
        if self.relevant_grades is None:
            return
        if any(
            not isinstance(g, int) or g < 0 for g in self.relevant_grades.values()
        ):
            # bool is an int subclass and is deliberately allowed (True == 1).
            raise ValueError("relevant_grades must map ids to integer grades >= 0")
        binarized = frozenset(
            rid for rid, g in self.relevant_grades.items() if g >= 1
        )
        if binarized != frozenset(self.relevant_ids):
            raise ValueError(
                "relevant_ids and relevant_grades disagree: binarized grades "
                f"(grade >= 1) give {sorted(binarized)!r} but relevant_ids is "
                f"{sorted(self.relevant_ids)!r}"
            )


@dataclass(frozen=True)
class QueryMetrics:
    """Per-query metric row from ``evaluate(..., per_query=True)``.

    (Named ``QueryMetrics`` — not ``QueryResult`` — because
    ``rekoll.adapters.base.QueryResult`` is an existing, unrelated retrieval
    type; two public ``QueryResult`` classes in one package would confuse.)

    One row per labeled query, in input order. Rows carry every metric for
    that single query, so a downstream harness can compute bootstrap CIs and
    paired significance tests from the rows alone — no re-running search.
    """

    query: str
    recall_at_k: float
    reciprocal_rank: float
    hit_rate_at_k: float
    precision_at_k: float
    average_precision: float
    ndcg_at_k: float


@dataclass(frozen=True)
class EvalResult:
    """Aggregate (mean-over-queries) metrics; all metric fields are means.

    The first four fields are the original surface — positional construction
    ``EvalResult(n_queries, k, recall_at_k, mrr)`` is unchanged. The added
    metric means default to 0.0 and ``per_query`` defaults to None (populated
    only by ``evaluate(..., per_query=True)``).
    """

    n_queries: int
    k: int
    recall_at_k: float
    mrr: float
    hit_rate_at_k: float = 0.0
    precision_at_k: float = 0.0
    average_precision: float = 0.0
    ndcg_at_k: float = 0.0
    per_query: Optional[Tuple[QueryMetrics, ...]] = None

    def __str__(self) -> str:
        return (
            f"queries={self.n_queries}  recall@{self.k}={self.recall_at_k:.3f}  "
            f"MRR={self.mrr:.3f}"
        )


def recall_at_k(ranked_ids: Sequence[str], relevant: FrozenSet[str], k: int) -> float:
    """Fraction of relevant ids found in the top-k results."""
    if not relevant:
        return 0.0
    topk = set(ranked_ids[:k])
    return len(topk & relevant) / len(relevant)


def reciprocal_rank(ranked_ids: Sequence[str], relevant: FrozenSet[str]) -> float:
    """1/(rank of the first relevant id), or 0 if none retrieved."""
    for i, rid in enumerate(ranked_ids):
        if rid in relevant:
            return 1.0 / (i + 1)
    return 0.0


def hit_rate_at_k(ranked_ids: Sequence[str], relevant: AbstractSet[str], k: int) -> float:
    """1.0 if any relevant id appears in the top-k results, else 0.0.

    Empty ``relevant`` (or ``k <= 0``) yields 0.0.
    """
    if k <= 0:
        return 0.0
    return 1.0 if any(rid in relevant for rid in ranked_ids[:k]) else 0.0


def precision_at_k(ranked_ids: Sequence[str], relevant: AbstractSet[str], k: int) -> float:
    """|top-k ∩ relevant| / k — **k** is the denominator, not ``len(ranked_ids)``.

    A ranking shorter than k is NOT forgiven: missing positions count against
    precision. ``k <= 0`` yields 0.0.
    """
    if k <= 0:
        return 0.0
    topk = set(ranked_ids[:k])
    return len(topk & set(relevant)) / k


def average_precision(ranked_ids: Sequence[str], relevant: AbstractSet[str]) -> float:
    """Standard Average Precision over the full ranking.

    Formula (this committed formula is the spec)::

        AP = (1 / |relevant|) * sum over 1-based ranks r where ranked_ids[r-1]
             is a not-yet-seen relevant id of  P@r = (# distinct relevant ids
             in the top r) / r

    The denominator is ``|relevant|`` (unretrieved relevant ids count against
    AP). Returns 0.0 if ``relevant`` is empty. Duplicate ids in the ranking
    count once, at their first occurrence.
    """
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    seen: set = set()
    for r, rid in enumerate(ranked_ids, start=1):
        if rid in relevant and rid not in seen:
            seen.add(rid)
            hits += 1
            total += hits / r
    return total / len(relevant)


def ndcg_at_k(ranked_ids: Sequence[str], relevant: Relevance, k: int) -> float:
    """Normalized Discounted Cumulative Gain at k (binary or graded).

    ``relevant`` is either a set of ids (binary: grade 1 if present, else 0)
    or a ``Mapping[id, grade]`` with integer grades >= 0 (ids absent from the
    mapping have grade 0; negative grades raise ValueError).

    Formula (this committed formula is the spec)::

        gain(id) = 2**grade(id) - 1          # binary reduces to 1/0
        DCG@k    = sum over 1-based ranks r = 1..k of
                   gain(ranked_ids[r-1]) / log2(r + 1)
        IDCG@k   = DCG of the ideal ranking: all gold grades sorted
                   descending, truncated at k
        nDCG@k   = DCG@k / IDCG@k,  or 0.0 when IDCG@k == 0
    """
    if isinstance(relevant, _MappingABC):
        if any(not isinstance(g, int) or g < 0 for g in relevant.values()):
            # bool is an int subclass and is deliberately allowed (True == 1).
            raise ValueError("graded relevance requires integer grades >= 0")
        gold_grades = list(relevant.values())

        def grade(rid: str) -> int:
            return relevant[rid] if rid in relevant else 0

    else:
        gold_grades = [1] * len(relevant)

        def grade(rid: str) -> int:
            return 1 if rid in relevant else 0

    if k <= 0:
        return 0.0
    dcg = 0.0
    for r, rid in enumerate(ranked_ids[:k], start=1):
        g = grade(rid)
        if g:
            dcg += (2**g - 1) / math.log2(r + 1)
    ideal = sorted(gold_grades, reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(r + 1) for r, g in enumerate(ideal, start=1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def evaluate(
    search_fn: Callable[[str], Sequence[str]],
    queries: Sequence[LabeledQuery],
    *,
    k: int = 5,
    per_query: bool = False,
) -> EvalResult:
    """Mean metrics of ``search_fn`` over the labeled queries.

    Binary metrics (recall@k, MRR, hit-rate@k, precision@k, AP) always score
    against ``LabeledQuery.relevant_ids``; nDCG@k scores against
    ``relevant_grades`` when present, else the binary set. Each query's search
    runs exactly once, feeding all metrics.

    With ``per_query=True`` the result additionally carries one
    :class:`QueryMetrics` row per query (input order) in ``EvalResult.per_query``
    so downstream harnesses can bootstrap CIs / run paired tests without
    re-running search; the default (``per_query=False``) leaves that field
    None, preserving the original result shape.
    """
    if not queries:
        return EvalResult(
            n_queries=0,
            k=k,
            recall_at_k=0.0,
            mrr=0.0,
            per_query=() if per_query else None,
        )
    rows = []
    for q in queries:
        ranked = list(search_fn(q.query))
        graded: Relevance = (
            q.relevant_grades if q.relevant_grades is not None else q.relevant_ids
        )
        rows.append(
            QueryMetrics(
                query=q.query,
                recall_at_k=recall_at_k(ranked, q.relevant_ids, k),
                reciprocal_rank=reciprocal_rank(ranked, q.relevant_ids),
                hit_rate_at_k=hit_rate_at_k(ranked, q.relevant_ids, k),
                precision_at_k=precision_at_k(ranked, q.relevant_ids, k),
                average_precision=average_precision(ranked, q.relevant_ids),
                ndcg_at_k=ndcg_at_k(ranked, graded, k),
            )
        )
    n = len(rows)
    return EvalResult(
        n_queries=n,
        k=k,
        recall_at_k=sum(r.recall_at_k for r in rows) / n,
        mrr=sum(r.reciprocal_rank for r in rows) / n,
        hit_rate_at_k=sum(r.hit_rate_at_k for r in rows) / n,
        precision_at_k=sum(r.precision_at_k for r in rows) / n,
        average_precision=sum(r.average_precision for r in rows) / n,
        ndcg_at_k=sum(r.ndcg_at_k for r in rows) / n,
        per_query=tuple(rows) if per_query else None,
    )
