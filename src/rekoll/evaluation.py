"""Retrieval evaluation: Recall@k and MRR over labeled queries.

Decoupled from storage/embedding via ``search_fn(query) -> list[str]`` (ranked
record ids), so the same harness scores the CI stub gate, a fastembed run, or a
LongMemEval subset (ADR-0011). Keep this dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, FrozenSet, Sequence

__all__ = ["LabeledQuery", "EvalResult", "recall_at_k", "reciprocal_rank", "evaluate"]


@dataclass(frozen=True)
class LabeledQuery:
    query: str
    relevant_ids: FrozenSet[str]


@dataclass(frozen=True)
class EvalResult:
    n_queries: int
    k: int
    recall_at_k: float
    mrr: float

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


def evaluate(
    search_fn: Callable[[str], Sequence[str]],
    queries: Sequence[LabeledQuery],
    *,
    k: int = 5,
) -> EvalResult:
    """Mean Recall@k and MRR of ``search_fn`` over the labeled queries."""
    if not queries:
        return EvalResult(n_queries=0, k=k, recall_at_k=0.0, mrr=0.0)
    total_recall = 0.0
    total_rr = 0.0
    for q in queries:
        ranked = list(search_fn(q.query))
        total_recall += recall_at_k(ranked, q.relevant_ids, k)
        total_rr += reciprocal_rank(ranked, q.relevant_ids)
    n = len(queries)
    return EvalResult(n_queries=n, k=k, recall_at_k=total_recall / n, mrr=total_rr / n)
