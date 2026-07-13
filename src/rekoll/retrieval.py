"""Hybrid retrieval: fuse vector + lexical results with Reciprocal Rank Fusion.

Zero LLM on this path (ADR-0007). The verbatim store is the floor; lexical is an
additive arm when the adapter advertises it. RRF (k=60, the widely-used constant)
combines the ranked lists without needing comparable raw scores.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Iterable, Optional

from .adapters.base import CAP_LEXICAL, QueryHit, QueryResult, StorageAdapter
from .embedding import Embedder
from .firewall import sanitize_unicode
from .model import RECALLABLE_STATUSES, Kind, Scope, Status, TrustTier
from .reranking import Reranker

__all__ = [
    "rrf_fuse",
    "hybrid_search",
    "FusedResult",
    "DEFAULT_RRF_K",
    "MAX_QUERY_CHARS",
    "GATE_OFF",
    "GATE_PASS",
    "GATE_ABSTAIN",
    "GATE_NO_VECTOR_LEG",
    "GATE_NON_COSINE",
    "GATE_NO_VECTOR_CANDIDATES",
]

DEFAULT_RRF_K = 60

# Read path must degrade, never DoS: queries are truncated (not rejected) to a
# bound far past any real question before embedding/lexical search (ADR-0018).
MAX_QUERY_CHARS = 8_192

# -- the abstain gate's verdict (ADR-0028) ------------------------------------
# Exactly one of these lands on FusedResult.gate. The "unavailable: " prefix is
# load-bearing: Memory turns it into the ``min_score not applied (...)`` clause
# of the honest-degradation mode string, so a caller who asked for a gate and
# did not get one is never left guessing.
GATE_OFF = "off"  # min_score was not requested
GATE_PASS = "pass"  # gate ran; the best surfacable cosine cleared min_score
GATE_ABSTAIN = "abstain"  # gate ran; nothing was close enough — hits withheld
GATE_NO_VECTOR_LEG = "unavailable: no vector leg"
GATE_NON_COSINE = "unavailable: non-cosine metric"
GATE_NO_VECTOR_CANDIDATES = "unavailable: no vector candidates"


@dataclass(frozen=True)
class FusedResult(QueryResult):
    """``hybrid_search``'s return: a ``QueryResult`` plus the abstain gate's verdict.

    A plain ``QueryResult`` (``.hits``) to every existing caller; the extra
    fields exist so an ABSTAINED result can never be mistaken for an empty
    store (ADR-0028).

    ``top_vector_score`` is the top-1 **cosine similarity** from the vector leg,
    measured BEFORE fusion, over the hits that would actually be allowed to
    surface. It is populated exactly when a cosine-metric vector leg ran and
    returned at least one surfacable candidate — that is, precisely when the
    gate is evaluable. It is emphatically NOT ``hits[0].score``: after RRF
    fusion a hit's score is the fused rank score (~0.01–0.03), and after
    reranking it is the cross-encoder's score. Neither is comparable to a
    cosine, and neither is what ``min_score`` compares against.
    """

    abstained: bool = False
    top_vector_score: Optional[float] = None
    gate: str = GATE_OFF


def rrf_fuse(
    result_lists: Iterable[Iterable[QueryHit]],
    *,
    k: int = DEFAULT_RRF_K,
    top: int = 10,
) -> list[QueryHit]:
    """Reciprocal Rank Fusion over several ranked hit lists, keyed by record id."""
    scores: dict[str, float] = {}
    records = {}
    for hits in result_lists:
        for rank, hit in enumerate(hits):
            rid = hit.record.id
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            records[rid] = hit.record
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [QueryHit(record=records[rid], score=score) for rid, score in ranked[:top]]


def _surfacable(hit: QueryHit) -> bool:
    """Would this hit be allowed out of ``hybrid_search`` (quarantine excluded)?

    The ONE definition of the surfacing filter, shared by the post-fusion filter
    and the abstain gate — so the gate can never be evaluated against a cosine
    belonging to a record the search would go on to withhold.

    It must AGREE with ``build_envelope`` (which drops status==QUARANTINED AND
    trust<=QUARANTINED): ``RecallResult``'s raw accessors (.texts()/.ids()/
    .records()) expose exactly these hits. Construction already forces
    status=QUARANTINED at quarantine-level trust (model.MemoryRecord); the trust
    clause keeps the agreement explicit for any adapter/legacy row that slips one
    through. "Recallable" itself (status==ACTIVE) is defined ONCE in
    model.RECALLABLE_STATUSES — the same predicate the MCP status count uses —
    so a future supersede/propose loop can't surface here either.
    """
    return (
        hit.record.status in RECALLABLE_STATUSES
        and hit.record.trust_tier > TrustTier.QUARANTINED
    )


def _top_vector_cosine(
    hits: list[QueryHit], *, include_quarantined: bool
) -> Optional[float]:
    """Top-1 cosine over the vector leg's SURFACABLE hits; None if there are none.

    ``max`` rather than ``hits[0]`` — the adapter contract promises a ranked
    list, but this must not silently depend on it once quarantined rows are
    filtered out of the middle of one.
    """
    pool = hits if include_quarantined else [h for h in hits if _surfacable(h)]
    return max((h.score for h in pool), default=None)


def _verify_hits(hits: list[QueryHit]) -> list[QueryHit]:
    """Tamper check (ADR-0019): demote any hit whose content fails its hash.

    An attacker with direct write access to the backing store bypasses ingest
    screening entirely; the content-address is the detection layer SECURITY.md
    promises. Mismatched records are demoted to QUARANTINED **in memory** (the
    store is never written on the read path) so the normal quarantine filtering
    below decides surfacing, and one warning names the withheld ids.
    """
    bad: list[str] = []
    for hit in hits:
        if not hit.record.verify():
            # Safe to mutate: adapters reconstruct a FRESH MemoryRecord per query
            # (no shared/cached instance), so this demotion is local to this
            # result set and never persisted (reads stay side-effect-free).
            hit.record.status = Status.QUARANTINED
            bad.append(hit.record.id)
    if bad:
        warnings.warn(
            f"[rekoll] {len(bad)} recalled record(s) failed content-hash "
            f"verification and were withheld (possible direct-DB tampering; "
            f"re-ingest or delete them): {', '.join(sorted(bad))}",
            stacklevel=3,
        )
    return hits


def hybrid_search(
    adapter: StorageAdapter,
    *,
    scope: Scope,
    query: str,
    embedder: Embedder,
    k: int = 10,
    kind: Optional[Kind] = None,
    where: Optional[dict] = None,
    rrf_k: int = DEFAULT_RRF_K,
    reranker: Optional[Reranker] = None,
    candidates: Optional[int] = None,
    include_quarantined: bool = False,
    use_vector: bool = True,
    use_lexical: bool = True,
    min_score: Optional[float] = None,
) -> FusedResult:
    """Vector + (optional) lexical search fused by RRF, then optionally reranked.

    Reads call no LLM — the cross-encoder reranker is a small local model, not a
    generative LLM (ADR-0010). Without a reranker, RRF order is returned.
    Quarantined memory (firewall, ADR-0013) is excluded by default.

    The query is sanitized by the firewall (NFKC + invisible-char strip — the
    same normalization stored content got, so hidden characters can't split
    terms or skew matching) and truncated to ``MAX_QUERY_CHARS`` before any
    embedding or lexical work (DESIGN §7 "query sanitized before embedding").

    Every candidate is content-hash verified before surfacing; a mismatch
    (direct-DB tampering) is demoted to QUARANTINED in memory and warned about
    (ADR-0019).

    ``candidates`` sizes the pool each leg retrieves and RRF fuses (default
    ``6*k``). It is a **reranker-feeding knob, NOT a recall knob**: a reranker
    rescores the whole pool, so a deeper pool gives it more to find. Without a
    reranker there is nothing to rescore, and fusing two *longer* ranked lists
    lets a document ranked poorly in BOTH out-score one ranked high in only one
    — the noisy tail reaches the top-k. Raising ``candidates`` with no reranker
    therefore *lowers* quality (measured paraphrase recall@5 on a 1,000-doc
    corpus: 0.901 at pool 30, 0.870 at 50, 0.823 at 100, 0.760 at 200), and
    warns. A reranker rescues depth but does not beat a shallow pool: pool 200
    + reranker recovers only to 0.849.

    That degradation is a **fusion** effect and needs two legs. RRF's score,
    ``1/(rrf_k + rank + 1)``, is strictly decreasing in rank, so fusing a single
    ranked list reproduces its order exactly: on a one-leg search (``use_vector``
    or ``use_lexical`` refused, or an adapter with no lexical capability) a
    deeper pool changes only how much is read, never the top-k. No warning is
    raised there, because there is nothing to warn about.

    ``use_vector=False`` refuses the vector leg entirely — the query is never
    embedded and ``vector_query`` is never called. Callers use this for honest
    lexical-only degradation when the scope's stored vectors are not comparable
    to the current embedder (embedder-identity mismatch, ADR-0024).

    ``use_lexical=False`` is its mirror: the lexical leg is refused even on an
    adapter that advertises ``CAP_LEXICAL``, and ``lexical_query`` is never
    called. Callers use it for a vector-only run — an ablation arm, or a scope
    whose FTS index is corrupt/misbehaving — without dropping to
    ``adapter.vector_query`` directly, which would bypass this function's query
    sanitization, tamper verification and quarantine filtering (#33).

    With BOTH legs refused (or ``use_vector=False`` on an adapter with no
    lexical capability) the result is honestly empty rather than a garbage
    ranking.

    ``min_score`` (opt-in, default off) is the **abstain gate** of ADR-0028: a
    floor on the vector leg's top-1 cosine similarity, evaluated BEFORE fusion.
    If no surfacable memory is at least this close to the query, the search
    abstains — it returns no hits and says so (``abstained=True``,
    ``gate="abstain"``) rather than handing back ``k`` confident-looking
    best-effort hits for a question the store cannot answer. The gate is a
    query-level decision ("is anything in here about this at all?"), not a
    per-hit filter, because that is exactly what the evidence supports: over a
    frozen 1,000-doc fixture, top-1 cosine separates answerable from
    unanswerable queries with AUC 0.931 (means 0.777 vs 0.646). No calibration
    is claimed for the 2nd..kth hit, so none is applied.

    ``min_score`` is a COSINE, not a fused score. Do not pass a number you read
    off ``hits[0].score`` after a search: that is an RRF rank score (~0.01–0.03)
    or a reranker score. Read ``FusedResult.top_vector_score`` instead — it is
    populated on every cosine-metric vector search, gate or no gate, precisely
    so a threshold can be chosen from observed data.

    When the gate cannot be evaluated it is NOT silently skipped and NOT
    guessed: ``gate`` carries an ``"unavailable: ..."`` reason and (for the two
    caller-fixable cases) a warning is raised. No vector leg ran → no cosine
    exists; a non-cosine ``adapter.distance_metric`` → the number is not the
    quantity ``min_score`` is calibrated against; no surfacable vector candidate
    → nothing to score. In all three the hits are returned ungated, because
    fabricating an abstain from a quantity that was never measured is the same
    bluff this gate exists to prevent.
    """
    if min_score is not None and not -1.0 <= min_score <= 1.0:
        raise ValueError(
            f"min_score={min_score} is out of range: it is a cosine similarity "
            f"in [-1.0, 1.0], not a fused/RRF score. See FusedResult."
        )
    # Truncate BEFORE sanitizing (not after): sanitize_unicode does a full-string
    # NFKC normalize + invisible-strip, so running it on the raw query first made
    # read work unbounded in query size (a 20M-char query = ~2s) despite the cap —
    # contradicting ADR-0018 ("truncated before search, never a DoS"). Bounding the
    # sanitize input to MAX_QUERY_CHARS keeps it O(cap); re-slice as NFKC can grow a
    # boundary char by a few codepoints.
    query = sanitize_unicode(query[:MAX_QUERY_CHARS])[:MAX_QUERY_CHARS]
    default_pool = max(k * 6, k)
    pool = candidates or default_pool
    # The depth footgun is a FUSION effect, and only fires when two legs fuse.
    # RRF's score, 1/(rrf_k + rank + 1), is strictly decreasing in rank, so
    # fusing ONE ranked list reproduces that list's order exactly: a deeper pool
    # cannot change a single-leg search's top-k, only how much it reads. What
    # degrades quality is a document sitting deep in BOTH lists out-scoring one
    # ranked high in only one — impossible with a single leg. Warning there
    # would be a false alarm, and ``use_lexical=False`` (#33) makes single-leg
    # searches a first-class thing to do.
    fuses_two_legs = use_vector and use_lexical and adapter.supports(CAP_LEXICAL)
    if (
        candidates is not None
        and candidates > default_pool
        and reranker is None
        and fuses_two_legs
    ):
        warnings.warn(
            f"[rekoll] candidates={candidates} exceeds the default pool of "
            f"{default_pool} (6*k, k={k}) and no reranker is attached. "
            f"`candidates` FEEDS A RERANKER; it is not a recall knob. Fusing "
            f"two deeper ranked lists lets a document ranked poorly in BOTH "
            f"out-score one ranked high in only one, so the noisy tail reaches "
            f"the top-k and quality MEASURABLY DEGRADES (measured paraphrase "
            f"recall@5: 0.901 at pool 30 -> 0.760 at pool 200). Attach a "
            f"reranker, or leave `candidates` unset (issue #36).",
            stacklevel=2,
        )
    # An adapter whose vector leg does not rank by cosine cannot be gated: the
    # score it returns is a different quantity than min_score is calibrated on.
    metric = getattr(adapter, "distance_metric", "cosine")
    top_vector_score: Optional[float] = None
    lists: list[Iterable[QueryHit]] = []
    if use_vector:
        query_vec = embedder.embed([query])[0]
        vector_hits = list(
            adapter.vector_query(scope=scope, embedding=query_vec, k=pool, kind=kind, where=where).hits
        )
        if metric == "cosine":
            # Captured HERE, pre-fusion, because this is the only point at which
            # a hit's score is still a cosine (ADR-0028).
            top_vector_score = _top_vector_cosine(
                vector_hits, include_quarantined=include_quarantined
            )
        lists.append(vector_hits)

    gate = _evaluate_gate(
        min_score, use_vector=use_vector, metric=metric, top_vector_score=top_vector_score
    )
    if gate == GATE_ABSTAIN:
        # Refuse before the lexical leg, fusion and reranking: nothing after the
        # gate ran, so nothing after the gate may be reported as having run.
        return FusedResult(
            hits=(), abstained=True, top_vector_score=top_vector_score, gate=gate
        )

    if use_lexical and adapter.supports(CAP_LEXICAL):
        lists.append(
            adapter.lexical_query(scope=scope, text=query, k=pool, kind=kind, where=where).hits
        )
    if not lists:
        # No vector leg (refused) AND no lexical leg (refused, or the adapter
        # has no lexical capability): honestly empty rather than a garbage
        # ranking (ADR-0024).
        return FusedResult(hits=(), top_vector_score=top_vector_score, gate=gate)
    fused = _verify_hits(rrf_fuse(lists, k=rrf_k, top=pool))
    if not include_quarantined:
        fused = [h for h in fused if _surfacable(h)]
    if reranker is not None:
        fused = reranker.rerank(query, fused, top=k)
    else:
        fused = fused[:k]
    return FusedResult(
        hits=tuple(fused), top_vector_score=top_vector_score, gate=gate
    )


def _evaluate_gate(
    min_score: Optional[float],
    *,
    use_vector: bool,
    metric: str,
    top_vector_score: Optional[float],
) -> str:
    """Decide the abstain gate's verdict (ADR-0028).

    Warns (it is not pure) when the caller asked for a gate that cannot run: the
    two ``unavailable`` cases a caller can actually fix. Python's default filter
    shows such a warning once per call site, so the PER-CALL contract is the
    returned verdict — which ``Memory`` folds into ``RecallResult.mode`` on every
    single recall, warning or not.
    """
    if min_score is None:
        return GATE_OFF
    if not use_vector:
        warnings.warn(
            "[rekoll] min_score was requested but the vector leg did not run "
            "(use_vector=False, or an embedder-identity mismatch, ADR-0024), so "
            "no cosine exists to threshold. Results are returned UNGATED; "
            "RecallResult.mode says so. Reindex the scope, or drop min_score.",
            stacklevel=3,
        )
        return GATE_NO_VECTOR_LEG
    if metric != "cosine":
        warnings.warn(
            f"[rekoll] min_score was requested but this adapter ranks vectors by "
            f"'{metric}', not cosine. A cosine threshold has no meaning against "
            f"that score, so the gate did NOT run and results are returned "
            f"UNGATED; RecallResult.mode says so.",
            stacklevel=3,
        )
        return GATE_NON_COSINE
    if top_vector_score is None:
        # A data condition, not caller error (e.g. records written without
        # vectors during a mismatch, ADR-0024 §2, still reachable lexically):
        # there is nothing to score, so there is nothing to abstain FROM.
        return GATE_NO_VECTOR_CANDIDATES
    return GATE_ABSTAIN if top_vector_score < min_score else GATE_PASS
