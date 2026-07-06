"""Hand-computed locks for the added evaluator metrics (efficacy lane 1a).

Every expected value below is derived BY HAND from the committed docstring
formulas (the arithmetic is written out in comments) — never round-tripped
through the implementation. These tests are the spec's anchor: if a formula
changes, a hand-computed number here must be re-derived, loudly.

Also guards the additive/non-breaking contract: the original call shapes
(positional LabeledQuery/EvalResult construction, evaluate without per_query)
must keep working unchanged.
"""

from __future__ import annotations

import math

import pytest

from rekoll.evaluation import (
    EvalResult,
    LabeledQuery,
    QueryMetrics,
    average_precision,
    evaluate,
    hit_rate_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

# 1/log2(3): the rank-2 discount, used all over the nDCG arithmetic below.
INV_LOG2_3 = 1 / math.log2(3)  # = 0.6309297535714575


# ---------------------------------------------------------------- hit_rate_at_k


def test_hit_rate_at_k_hand_computed():
    # "c" is at rank 3: not in the top-2 -> 0.0; in the top-3 -> 1.0.
    assert hit_rate_at_k(["a", "b", "c"], frozenset({"c"}), 2) == 0.0
    assert hit_rate_at_k(["a", "b", "c"], frozenset({"c"}), 3) == 1.0
    # Any single hit in top-k is enough (both relevant in top-2 -> still 1.0).
    assert hit_rate_at_k(["a", "b"], frozenset({"a", "b"}), 2) == 1.0


def test_hit_rate_at_k_edges():
    assert hit_rate_at_k(["a", "b"], frozenset(), 5) == 0.0  # empty relevant
    assert hit_rate_at_k(["a"], frozenset({"a"}), 0) == 0.0  # k = 0
    # k longer than the ranking: same as scanning the whole (short) list.
    assert hit_rate_at_k(["a", "b", "c"], frozenset({"c"}), 10) == 1.0
    assert hit_rate_at_k([], frozenset({"a"}), 5) == 0.0  # empty ranking


def test_negative_k_yields_zero_for_all_k_taking_metrics():
    # Committed spec: k <= 0 yields 0.0. Without an explicit guard a negative
    # k would slice from the END (ranked[:-1] scans ["a","b"] here) and
    # hit_rate would wrongly return 1.0 for relevant {"a"}.
    assert hit_rate_at_k(["a", "b", "c"], frozenset({"a"}), -1) == 0.0
    assert precision_at_k(["a", "b", "c"], frozenset({"a"}), -1) == 0.0
    assert ndcg_at_k(["a", "b", "c"], frozenset({"a"}), -1) == 0.0
    assert ndcg_at_k(["a", "b", "c"], {"a": 2}, -1) == 0.0  # graded path too


# --------------------------------------------------------------- precision_at_k


def test_precision_at_k_hand_computed():
    # top-4 of [a,b,c,d] ∩ {a,c,z} = {a,c} -> 2/4 = 0.5
    assert precision_at_k(["a", "b", "c", "d"], frozenset({"a", "c", "z"}), 4) == 0.5
    # top-2 = [a,b], ∩ {a,c,z} = {a} -> 1/2 = 0.5
    assert precision_at_k(["a", "b", "c", "d"], frozenset({"a", "c", "z"}), 2) == 0.5
    # top-3 = [a,b,c], ∩ {a,c,z} = {a,c} -> 2/3
    assert precision_at_k(["a", "b", "c", "d"], frozenset({"a", "c", "z"}), 3) == pytest.approx(2 / 3)


def test_precision_at_k_uses_k_as_denominator_not_len_ranked():
    # Ranking has only 4 items but k=10: {a,c} found -> 2/10 = 0.2 (NOT 2/4).
    assert precision_at_k(["a", "b", "c", "d"], frozenset({"a", "c", "z"}), 10) == pytest.approx(0.2)


def test_precision_at_k_edges():
    assert precision_at_k(["a", "b"], frozenset(), 2) == 0.0  # empty relevant
    assert precision_at_k(["a"], frozenset({"a"}), 0) == 0.0  # k = 0
    assert precision_at_k([], frozenset({"a"}), 3) == 0.0  # empty ranking


# ------------------------------------------------------------ average_precision


def test_average_precision_hand_computed():
    # ranking [a,b,c,d,e], relevant {a,c,e}:
    #   a hits at rank 1 -> P@1 = 1/1 = 1
    #   c hits at rank 3 -> P@3 = 2/3
    #   e hits at rank 5 -> P@5 = 3/5
    # AP = (1 + 2/3 + 3/5) / 3 = (15/15 + 10/15 + 9/15) / 3 = (34/15)/3 = 34/45
    assert average_precision(["a", "b", "c", "d", "e"], frozenset({"a", "c", "e"})) == pytest.approx(34 / 45)


def test_average_precision_unretrieved_relevant_counts_in_denominator():
    # ranking [b,a], relevant {a,z}: a hits at rank 2 -> P@2 = 1/2; z never
    # retrieved. AP = (1/2) / |{a,z}| = (1/2)/2 = 0.25
    assert average_precision(["b", "a"], frozenset({"a", "z"})) == pytest.approx(0.25)


def test_average_precision_edges():
    assert average_precision(["a", "b"], frozenset()) == 0.0  # empty relevant
    assert average_precision(["x", "y"], frozenset({"a"})) == 0.0  # no hits
    assert average_precision([], frozenset({"a"})) == 0.0  # empty ranking
    # Perfect ranking: [a,b] vs {a,b}: (1/1 + 2/2)/2 = 1.0
    assert average_precision(["a", "b"], frozenset({"a", "b"})) == pytest.approx(1.0)


# ------------------------------------------------------------------- ndcg_at_k


def test_ndcg_at_k_binary_hand_computed():
    # ranking [a,b,c], relevant {b,c}, k=3. Binary gains: a=0, b=1, c=1.
    #   DCG  = 0/log2(2) + 1/log2(3) + 1/log2(4)
    #        = 1/log2(3) + 1/2                (log2(4) = 2)
    #   IDCG = ideal gains [1,1] -> 1/log2(2) + 1/log2(3) = 1 + 1/log2(3)
    expected = (INV_LOG2_3 + 0.5) / (1 + INV_LOG2_3)
    # Numerically: (0.63093 + 0.5) / 1.63093 = 1.13093 / 1.63093 ≈ 0.69343
    assert ndcg_at_k(["a", "b", "c"], frozenset({"b", "c"}), 3) == pytest.approx(expected)
    assert ndcg_at_k(["a", "b", "c"], frozenset({"b", "c"}), 3) == pytest.approx(0.69343, abs=1e-5)


def test_ndcg_at_k_binary_perfect_is_one():
    # [a,b] vs {a,b}, k=2: DCG = 1 + 1/log2(3) = IDCG -> exactly 1.0
    assert ndcg_at_k(["a", "b"], frozenset({"a", "b"}), 2) == pytest.approx(1.0)


def test_ndcg_at_k_graded_hand_computed():
    # ranking [a,b,c], grades {a:1, b:3, c:0, d:2}, k=3.
    # gains: a = 2^1-1 = 1, b = 2^3-1 = 7, c = 2^0-1 = 0.
    #   DCG  = 1/log2(2) + 7/log2(3) + 0/log2(4) = 1 + 7/log2(3)
    # Ideal grades sorted desc = [3,2,1,0], truncated at k=3 -> [3,2,1]:
    #   IDCG = 7/log2(2) + 3/log2(3) + 1/log2(4) = 7 + 3/log2(3) + 1/2
    # ("d" has grade 2 but was never retrieved — it still shapes the ideal.)
    grades = {"a": 1, "b": 3, "c": 0, "d": 2}
    expected = (1 + 7 * INV_LOG2_3) / (7 + 3 * INV_LOG2_3 + 0.5)
    # Numerically: (1 + 4.41651) / (7 + 1.89279 + 0.5) = 5.41651 / 9.39279 ≈ 0.57667
    assert ndcg_at_k(["a", "b", "c"], grades, 3) == pytest.approx(expected)
    assert ndcg_at_k(["a", "b", "c"], grades, 3) == pytest.approx(0.57667, abs=1e-5)


def test_ndcg_at_k_ideal_truncated_at_k():
    # ranking [b], grades {a:3, b:1}, k=1.
    #   DCG  = (2^1-1)/log2(2) = 1
    #   IDCG = ideal [3,1] truncated at 1 -> [3]: (2^3-1)/log2(2) = 7
    # nDCG = 1/7
    assert ndcg_at_k(["b"], {"a": 3, "b": 1}, 1) == pytest.approx(1 / 7)


def test_ndcg_at_k_grade_zero_and_zero_idcg():
    # All-zero grades -> every gain 0 -> IDCG 0 -> defined as 0.0.
    assert ndcg_at_k(["a", "b"], {"a": 0, "b": 0}, 2) == 0.0
    assert ndcg_at_k(["a", "b"], frozenset(), 2) == 0.0  # binary empty set
    assert ndcg_at_k([], {"a": 2}, 3) == 0.0  # empty ranking: DCG 0, IDCG 7
    assert ndcg_at_k(["a"], {"a": 2}, 0) == 0.0  # k = 0


def test_ndcg_at_k_negative_grade_rejected():
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], {"a": -1}, 1)


def test_ndcg_at_k_non_int_grade_rejected_bool_allowed():
    # The pre-registered spec says INTEGER grades >= 0: floats are rejected.
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], {"a": 1.5}, 1)
    # bool is an int subclass (True == 1) and is deliberately allowed:
    # [a] vs {a: True}: DCG = (2^1-1)/log2(2) = 1 = IDCG -> 1.0
    assert ndcg_at_k(["a"], {"a": True}, 1) == pytest.approx(1.0)


# ------------------------------------------------- LabeledQuery (graded shape)


def test_labeled_query_positional_construction_unchanged():
    q = LabeledQuery("q1", frozenset({"a"}))
    assert q.query == "q1"
    assert q.relevant_ids == frozenset({"a"})
    assert q.relevant_grades is None


def test_labeled_query_agreeing_grades_accepted():
    # binarized grades (grade >= 1) = {a} == relevant_ids -> OK; grade-0 "b"
    # is NOT binary-relevant.
    q = LabeledQuery("q1", frozenset({"a"}), {"a": 2, "b": 0})
    assert q.relevant_grades == {"a": 2, "b": 0}


def test_labeled_query_disagreeing_grades_raise():
    with pytest.raises(ValueError):
        LabeledQuery("q1", frozenset({"a"}), {"b": 1})  # binarized {b} != {a}
    with pytest.raises(ValueError):
        # grade-0 id binarizes to NOT-relevant: binarized {} != {a}
        LabeledQuery("q1", frozenset({"a"}), {"a": 0})


def test_labeled_query_negative_grade_raises():
    with pytest.raises(ValueError):
        LabeledQuery("q1", frozenset({"a"}), {"a": -2})


def test_labeled_query_non_int_grade_raises_bool_allowed():
    with pytest.raises(ValueError):
        LabeledQuery("q1", frozenset({"a"}), {"a": 1.5})
    # bool is an int subclass (True == 1 binarizes to relevant) — allowed.
    q = LabeledQuery("q1", frozenset({"a"}), {"a": True, "b": False})
    assert q.relevant_grades == {"a": True, "b": False}


# --------------------------------------------------- EvalResult (old + new shape)


def test_eval_result_positional_construction_unchanged():
    r = EvalResult(3, 5, 0.5, 0.4)
    assert (r.n_queries, r.k, r.recall_at_k, r.mrr) == (3, 5, 0.5, 0.4)
    # New fields default so old construction/equality is unaffected.
    assert r.hit_rate_at_k == 0.0
    assert r.precision_at_k == 0.0
    assert r.average_precision == 0.0
    assert r.ndcg_at_k == 0.0
    assert r.per_query is None
    assert r == EvalResult(3, 5, 0.5, 0.4)
    assert str(r) == "queries=3  recall@5=0.500  MRR=0.400"


# -------------------------------------------------------------------- evaluate


RANKINGS = {
    "one": ["a", "b", "c"],
    "two": ["b", "c", "x"],
    "three": ["b", "a"],
}


def fake_search(query: str):
    return RANKINGS[query]


def test_evaluate_means_hand_computed():
    # k = 2.
    # q1 "one": ranked [a,b,c], relevant {a}
    #   recall@2 = 1/1 = 1.0        rr = 1/1 = 1.0     hit@2 = 1.0
    #   prec@2 = |{a}|/2 = 0.5      AP = (1/1)/1 = 1.0
    #   nDCG@2 (binary): DCG = 1/log2(2) = 1; IDCG = 1 -> 1.0
    # q2 "two": ranked [b,c,x], relevant {c,d}
    #   recall@2 = |{c}|/2 = 0.5    rr = 1/2 = 0.5     hit@2 = 1.0
    #   prec@2 = |{c}|/2 = 0.5      AP = (P@2)/2 = (1/2)/2 = 0.25
    #   nDCG@2 (binary): DCG = 1/log2(3); IDCG = 1 + 1/log2(3)
    # Means over 2 queries:
    #   recall = (1.0+0.5)/2 = 0.75      mrr = (1.0+0.5)/2 = 0.75
    #   hit    = (1.0+1.0)/2 = 1.0       prec = (0.5+0.5)/2 = 0.5
    #   MAP    = (1.0+0.25)/2 = 0.625
    #   nDCG   = (1.0 + (1/log2(3))/(1+1/log2(3))) / 2
    queries = [
        LabeledQuery("one", frozenset({"a"})),
        LabeledQuery("two", frozenset({"c", "d"})),
    ]
    r = evaluate(fake_search, queries, k=2)
    assert r.n_queries == 2 and r.k == 2
    assert r.recall_at_k == pytest.approx(0.75)
    assert r.mrr == pytest.approx(0.75)
    assert r.hit_rate_at_k == pytest.approx(1.0)
    assert r.precision_at_k == pytest.approx(0.5)
    assert r.average_precision == pytest.approx(0.625)
    ndcg_q2 = INV_LOG2_3 / (1 + INV_LOG2_3)  # ≈ 0.38685
    assert r.ndcg_at_k == pytest.approx((1.0 + ndcg_q2) / 2)
    # Default: no per-query rows (original shape preserved).
    assert r.per_query is None


def test_evaluate_per_query_rows():
    queries = [
        LabeledQuery("one", frozenset({"a"})),
        LabeledQuery("two", frozenset({"c", "d"})),
    ]
    r = evaluate(fake_search, queries, k=2, per_query=True)
    assert r.per_query is not None and len(r.per_query) == 2
    q1, q2 = r.per_query
    assert isinstance(q1, QueryMetrics)
    # Rows are in input order and carry the query string + every metric
    # (hand-derived in test_evaluate_means_hand_computed above).
    assert q1.query == "one"
    assert (q1.recall_at_k, q1.reciprocal_rank, q1.hit_rate_at_k) == (1.0, 1.0, 1.0)
    assert q1.precision_at_k == pytest.approx(0.5)
    assert q1.average_precision == pytest.approx(1.0)
    assert q1.ndcg_at_k == pytest.approx(1.0)
    assert q2.query == "two"
    assert q2.recall_at_k == pytest.approx(0.5)
    assert q2.reciprocal_rank == pytest.approx(0.5)
    assert q2.hit_rate_at_k == 1.0
    assert q2.precision_at_k == pytest.approx(0.5)
    assert q2.average_precision == pytest.approx(0.25)
    assert q2.ndcg_at_k == pytest.approx(INV_LOG2_3 / (1 + INV_LOG2_3))
    # The means must equal the mean of the rows (harness can recompute /
    # bootstrap from rows without re-running search).
    assert r.recall_at_k == pytest.approx((q1.recall_at_k + q2.recall_at_k) / 2)
    assert r.ndcg_at_k == pytest.approx((q1.ndcg_at_k + q2.ndcg_at_k) / 2)


def test_evaluate_graded_query_uses_grades_for_ndcg_only():
    # q3 "three": ranked [b,a], relevant_ids {a,b}, grades {a:2, b:1}, k=2.
    # Binary metrics (from relevant_ids): recall@2 = 2/2 = 1.0; rr = 1.0 (b at
    # rank 1); hit = 1.0; prec@2 = 2/2 = 1.0; AP = (1/1 + 2/2)/2 = 1.0.
    # Graded nDCG@2: gains b = 2^1-1 = 1 (rank 1), a = 2^2-1 = 3 (rank 2)
    #   DCG  = 1/log2(2) + 3/log2(3) = 1 + 3/log2(3)
    #   IDCG = ideal [2,1] -> 3/log2(2) + 1/log2(3) = 3 + 1/log2(3)
    # ≈ (1 + 1.89279) / (3 + 0.63093) = 2.89279 / 3.63093 ≈ 0.79671 — strictly
    # < 1.0, whereas BINARY nDCG here would be exactly 1.0 (both relevant,
    # equal gains). Proves grades drive nDCG while binary metrics ignore them.
    q = LabeledQuery("three", frozenset({"a", "b"}), {"a": 2, "b": 1})
    r = evaluate(fake_search, [q], k=2, per_query=True)
    row = r.per_query[0]
    assert row.recall_at_k == pytest.approx(1.0)
    assert row.reciprocal_rank == pytest.approx(1.0)
    assert row.hit_rate_at_k == 1.0
    assert row.precision_at_k == pytest.approx(1.0)
    assert row.average_precision == pytest.approx(1.0)
    expected_ndcg = (1 + 3 * INV_LOG2_3) / (3 + INV_LOG2_3)
    assert row.ndcg_at_k == pytest.approx(expected_ndcg)
    assert row.ndcg_at_k == pytest.approx(0.79671, abs=1e-5)
    assert row.ndcg_at_k < 1.0


def test_evaluate_empty_queries():
    r = evaluate(fake_search, [])
    assert r.n_queries == 0 and r.recall_at_k == 0.0 and r.mrr == 0.0
    assert r.per_query is None
    r2 = evaluate(fake_search, [], per_query=True)
    assert r2.per_query == ()


def test_old_call_shapes_still_work_end_to_end():
    # The exact pre-existing usage pattern (positional LabeledQuery, evaluate
    # with only k=) must be untouched by the additive changes.
    labeled = [LabeledQuery(query="one", relevant_ids=frozenset({"a"}))]
    result = evaluate(fake_search, labeled, k=5)
    assert result.recall_at_k == 1.0 and result.mrr == 1.0
    assert recall_at_k(["a", "b", "c"], frozenset({"a", "z"}), 5) == 0.5
    assert reciprocal_rank(["a", "b", "c"], frozenset({"b"})) == 0.5
