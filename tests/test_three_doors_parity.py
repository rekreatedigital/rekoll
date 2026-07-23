"""Three-doors parity — SDK, CLI, and MCP must return IDENTICAL results.

Rekoll ships three doors over one engine:

 - **SDK**  — ``rekoll.Memory`` in-process (src/rekoll/memory.py),
 - **CLI**  — ``python -m rekoll recall`` as a real subprocess (src/rekoll/cli.py),
 - **MCP**  — ``python -m rekoll.mcp_server`` over real stdio, driven by the
   official MCP client (src/rekoll/mcp_server.py).

The parity contract pinned here: for the same (store, query, k, kind, scope,
embedder, reranker), every door returns the SAME ordered top-k id list AND
NAMES the same pipeline that produced it (``RecallResult.mode``, ADR-0024). A
rank flip or a membership diff between doors would mean the doors run
*different* pipelines — the exact bug class this file exists to catch. A mode
that is absent, or disagrees, at one door means an agent behind that door
cannot tell a full hybrid ranking from a degraded lexical-only one (issue #25).

Determinism pins (parity is about PIPELINE identity, not semantic quality):

 - The store is built ONCE via the SDK with the explicit knobs
   ``embedder=StubEmbedder(), reranker=None`` — the stub is pure hashlib, so
   vectors are identical across processes and machines.
 - The CLI and MCP doors expose NO embedder/reranker parameters (deliberately —
   the CLI ships on the zero-dependency path; MCP pins everything server-side).
   Their pin is therefore ENVIRONMENTAL, not parametric: each subprocess gets a
   ``fastembed`` import shim on ``PYTHONPATH`` that raises ImportError, so
   ``memory._auto_embedder`` deterministically falls back to ``StubEmbedder()``
   and ``memory._auto_reranker`` to ``None`` — on every machine, including one
   with the real 'embeddings' extra installed. (Same convention as
   tests/test_cli.py, which monkeypatches the same two seams in-process.)
 - Auto-resolution defaults: ``StubEmbedder()`` is dim=64 / identity
   "stub-hash" in both spellings, so the store's pinned embedder identity
   matches in every door (no ADR-0024 degradation).

Environments:

 - default (no-extra) venv / CI: SDK and CLI legs run; MCP legs skip cleanly
   (``pytest.importorskip("mcp")``).
 - a venv with the 'mcp' extra: all three doors run.

Where each door reports its mode:

 - SDK — ``RecallResult.mode`` (and ``HealthReport.mode``).
 - CLI — ``rekoll recall --json`` -> ``{context, ids, mode, count}``; also
   ``rekoll doctor``'s freshness line, which renders ``Memory.health().mode``.
   The human-facing recall formats (default list, ``--ids``, ``--context``)
   stay byte-for-byte as they were: mode is opt-in, for machines.
 - MCP — the ``recall`` result payload AND the ``status`` tool.

Known, NAMED non-parity surfaces (asserted/documented here, by design):

 - MCP ``recall`` has no ``kind`` filter (a deliberately smaller LLM-facing
   surface), so kind-filtered parity is SDK<->CLI only. This is decided, not
   drifted: it is documented at the tool itself, in ``mcp_server.recall``'s
   docstring, which points a filtering caller at the SDK or CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rekoll import Memory
from rekoll.embedding import StubEmbedder
from rekoll.model import Kind, Status, TrustTier

K = 5
PROJECT = "parity"
_SRC = str(Path(__file__).resolve().parent.parent / "src")

# -- the fixed corpus (committed verbatim: parity must be reproducible) --------

REMEMBERED_FACTS = (
    "We chose Postgres over BigQuery because egress cost dominated the analytics bill.",
    "The Kafka consumer group rebalances whenever the payments pod autoscales past six replicas.",
    "Deploy window is Tuesday 14:00 UTC; Fridays are frozen for the on-call handover.",
    "Rebuilding the vector index takes eleven minutes on the m6i.large staging box.",
    "New agents must read the onboarding checklist before touching the ingestion pipeline.",
    "SQLite WAL checkpoints are tuned to 4096 pages to keep p99 write latency flat.",
    "Redis uses allkeys-lru eviction because the session cache tolerates cold misses.",
    "Terraform state drift is detected nightly by the drift-sentinel job in CI.",
    "Grafana alert thresholds page the on-call when queue depth exceeds twelve thousand.",
    "Swapping the embedding model requires Memory.reindex, never a plain re-ingest.",
    "The billing reconciliation script rounds to four decimal places to match the ledger.",
    "Feature flags live in LaunchDarkly; the search kill switch is named search-master-off.",
)

OBSERVATIONS = (
    "Observed the staging deploy fail twice while the migration lock was held by a zombie pod.",
    "Observed that queue depth spikes correlate with the Tuesday deploy window.",
    "Observed reranker latency doubling once the candidate pool exceeds two hundred hits.",
)

INGESTED_DOCS = {
    "runbook.md": (
        "# Incident runbook\n\n"
        "Page the on-call, then check Grafana queue depth before anything else.\n\n"
        "## Kafka partitions\n\n"
        "A stuck partition usually means the payments pod is mid-rebalance; wait one "
        "cycle before reassigning.\n\n"
        "## Redis cache\n\n"
        "Session cache misses after an eviction storm are expected; do not flush "
        "manually during business hours.\n"
    ),
    "decisions.md": (
        "# Decision log\n\n"
        "Postgres won over BigQuery on egress cost; revisit if the analytics bill "
        "triples.\n\n"
        "## Embedding model swaps\n\n"
        "Always run Memory.reindex after a model swap; re-ingesting identical content "
        "stores no new vectors.\n\n"
        "## Terraform\n\n"
        "The drift-sentinel CI job owns state drift; humans never run apply by hand.\n"
    ),
}

QUERIES = (
    "why postgres over bigquery",
    "kafka consumer group rebalancing",
    "deploy window friday freeze",
    "vector index rebuild time",
    "onboarding checklist ingestion",
    "sqlite wal checkpoint tuning",
    "redis eviction policy",
    "terraform state drift",
    "grafana alert queue depth",
    "embedding model swap reindex",
)

EXPECTED_MODE = "vector+lexical (stub-embedder)"
EXPECTED_EMBEDDER = "stub-hash"


# -- fixtures -------------------------------------------------------------------

@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """ONE store built via the SDK; every door reads THIS file.

    Also prepares the subprocess environment for the CLI/MCP doors: PYTHONPATH
    pins (a) this checkout's ``src`` first — an editable install elsewhere must
    not shadow the code under test (same pin as tests/test_cli.py) — and (b) a
    ``fastembed`` shim that raises on import, so both subprocess doors resolve
    the deterministic StubEmbedder + no reranker on any machine.
    """
    tmp = tmp_path_factory.mktemp("three-doors")
    shim = tmp / "no-fastembed-shim"
    shim.mkdir()
    (shim / "fastembed.py").write_text(
        'raise ImportError(\n'
        '    "three-doors parity harness: fastembed is deliberately unavailable in "\n'
        '    "this subprocess so every door resolves the deterministic StubEmbedder "\n'
        '    "(pipeline parity, not semantic quality)"\n'
        ')\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(shim) + os.pathsep + _SRC + os.pathsep + env.get("PYTHONPATH", "")
    )

    db = tmp / "parity.db"
    mem = Memory(path=str(db), project=PROJECT, embedder=StubEmbedder(), reranker=None)
    for fact in REMEMBERED_FACTS:
        mem.remember(fact)
    for obs in OBSERVATIONS:
        mem.remember(obs, kind=Kind.OBSERVATION)
    for name, text in INGESTED_DOCS.items():
        assert mem.ingest_text(text, name=name) >= 1
    total = mem.count()
    quarantined = mem.adapter.count(scope=mem.scope, status=Status.QUARANTINED.value)
    mode = mem.recall(QUERIES[0], k=K).mode
    mem.close()

    # The honest label for the pinned pipeline (protocol step 1): a stub-backed
    # hybrid read must SAY so, and the corpus must be entirely recallable (a
    # quarantined chunk would make per-door counts legitimately disagree).
    assert mode == EXPECTED_MODE
    assert quarantined == 0
    assert total >= 20

    return SimpleNamespace(db=str(db), root=tmp, env=env, total=total, mode=mode)


@pytest.fixture(scope="module")
def sdk(store):
    """Door 1 under test: a FRESH Memory over the same file (not the builder)."""
    mem = Memory(path=store.db, project=PROJECT, embedder=StubEmbedder(), reranker=None)
    yield mem
    mem.close()


# -- door helpers -----------------------------------------------------------------

def _sdk_ids(sdk: Memory, query: str, *, k: int = K, kind: Kind | None = None) -> list[str]:
    return sdk.recall(query, k=k, kind=kind).ids()


def _cli_ids(store, query: str, *, k: int = K, kind: str | None = None) -> list[str]:
    """Door 2: ``python -m rekoll recall --ids`` (machine-readable, one id/line).

    Exit code 1 with "No memories found" is the CLI's documented empty result
    (grep convention) — mapped to [] so emptiness is comparable across doors.
    """
    cmd = [
        sys.executable, "-m", "rekoll", "recall", query,
        "--ids", "-k", str(k), "--path", store.db, "--project", PROJECT,
    ]
    if kind is not None:
        cmd += ["--kind", kind]
    proc = subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace",
        env=store.env, cwd=str(store.root), timeout=120,
    )
    if proc.returncode == 1 and "No memories found" in proc.stderr:
        return []
    assert proc.returncode == 0, (
        f"CLI recall failed (rc={proc.returncode}) for {query!r}:\n{proc.stderr}"
    )
    # An embedder-identity mismatch (ADR-0024) would be re-emitted here as
    # 'rekoll: warning:' and silently degrade the CLI door to lexical-only —
    # that is a pipeline DIVERGENCE, not a cosmetic message.
    assert "rekoll: warning:" not in proc.stderr, proc.stderr
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _cli_json(
    store, query: str, *, k: int = K, kind: str | None = None,
    min_score: float | None = None,
) -> dict:
    """Door 2, machine format: ``python -m rekoll recall --json`` -> one object.

    Exit code 1 ("No memories found", or an abstain) is the CLI's documented
    empty result and still prints the object — so the payload, its ``mode``, and
    ``abstained`` are readable in exactly the case a caller most needs them.
    """
    cmd = [
        sys.executable, "-m", "rekoll", "recall", query,
        "--json", "-k", str(k), "--path", store.db, "--project", PROJECT,
    ]
    if kind is not None:
        cmd += ["--kind", kind]
    if min_score is not None:
        cmd += ["--min-score", str(min_score)]
    proc = subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace",
        env=store.env, cwd=str(store.root), timeout=120,
    )
    assert proc.returncode in (0, 1), (
        f"CLI recall --json failed (rc={proc.returncode}) for {query!r}:\n{proc.stderr}"
    )
    assert "rekoll: warning:" not in proc.stderr, proc.stderr  # a silent degradation
    return json.loads(proc.stdout)


def _cli_doctor_mode(store) -> str:
    """The CLI's own report of the pipeline it runs, from ``rekoll doctor``'s
    freshness line (``Memory.health().mode`` — the same honest-degradation
    string ``RecallResult.mode`` carries; recall itself does not print it)."""
    proc = subprocess.run(
        [sys.executable, "-m", "rekoll", "doctor", "--path", store.db, "--project", PROJECT],
        capture_output=True, encoding="utf-8", errors="replace",
        env=store.env, cwd=str(store.root), timeout=120,
    )
    assert proc.returncode == 0, f"doctor failed:\n{proc.stdout}\n{proc.stderr}"
    match = re.search(r"index is fresh \(mode=(.+)\)", proc.stdout)
    assert match, f"no fresh-index mode line in doctor output:\n{proc.stdout}"
    return match.group(1)


def _payload(result) -> dict:
    """Tool result -> dict (same harness as tests/test_mcp_server.py:
    structuredContent on mcp>=1.10, JSON text block at the version floor)."""
    assert not result.isError, f"tool errored: {result.content}"
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    text = next(c.text for c in result.content if getattr(c, "type", "") == "text")
    return json.loads(text)


def _mcp_recall_bulk(store, calls: list[tuple[str, int]], *, min_score: float | None = None):
    """Door 3: the REAL server over stdio, driven by the official MCP client.

    Configured via REKOLL_MCP_* env vars (path/project/root) — the documented
    deployment surface — pointing at the same store file and project scope.
    ``min_score``, when given, is applied to every call (the abstain gate).
    Returns ({(query, k): recall_payload}, status_payload).
    """
    pytest.importorskip("mcp")

    async def _inner():
        import inspect

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(store.env)
        env["REKOLL_MCP_PATH"] = store.db
        env["REKOLL_MCP_PROJECT"] = PROJECT
        env["REKOLL_MCP_ROOT"] = str(store.root)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "rekoll.mcp_server"],
            cwd=str(store.root),
            env=env,
        )
        # errlog as a real file: the SDK's default stderr can be a pytest capsys
        # stream without an OS handle (same guard as tests/test_mcp_server.py).
        with (store.root / "mcp-stderr.log").open("w", encoding="utf-8") as errlog:
            kwargs = (
                {"errlog": errlog}
                if "errlog" in inspect.signature(stdio_client).parameters
                else {}
            )
            async with stdio_client(params, **kwargs) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    out = {}
                    for query, k in calls:
                        args = {"query": query, "k": k}
                        if min_score is not None:
                            args["min_score"] = min_score
                        out[(query, k)] = _payload(
                            await session.call_tool("recall", args)
                        )
                    status = _payload(await session.call_tool("status", {}))
                    return out, status

    return asyncio.run(_inner())


def _parity_table(rows: list[tuple[str, dict[str, list[str]]]]) -> str:
    """query x door -> id list, rendered for failure messages and -s runs."""
    lines = []
    for query, per_door in rows:
        doors = sorted(per_door)
        match = "MATCH" if len({tuple(per_door[d]) for d in doors}) == 1 else "DIFF"
        lines.append(f"[{match}] {query!r}")
        for door in doors:
            lines.append(f"    {door:<4} {per_door[door]}")
    return "\n".join(lines)


# -- SDK <-> CLI (runnable in the default, no-extra environment) -------------------

def test_store_is_stub_pinned_and_fully_recallable(store):
    """Protocol step 1: the pinned pipeline announces itself honestly."""
    assert store.mode == EXPECTED_MODE
    assert store.total >= 20


def test_sdk_and_cli_return_identical_ordered_ids(store, sdk):
    rows = []
    for query in QUERIES:
        rows.append((query, {"sdk": _sdk_ids(sdk, query), "cli": _cli_ids(store, query)}))
    table = _parity_table(rows)
    print("\nSDK<->CLI parity (k=%d):\n%s" % (K, table))
    diffs = [q for q, per in rows if per["sdk"] != per["cli"]]
    assert not diffs, f"SDK vs CLI ordered-id divergence on {diffs}:\n{table}"
    # Every query must actually exercise the ranking (no vacuous [] == []).
    assert all(per["sdk"] for _, per in rows)


@pytest.mark.parametrize("k", [1, 3, 10])
def test_sdk_and_cli_agree_across_k_values(store, sdk, k):
    """A different default/truncation of k between doors would corrupt parity at
    exactly one list length — pin several."""
    for query in QUERIES[:3]:
        assert _sdk_ids(sdk, query, k=k) == _cli_ids(store, query, k=k), (
            f"k={k} divergence on {query!r}"
        )


def test_sdk_and_cli_agree_on_kind_filtered_recall(store, sdk):
    """kind= filter parity. SDK<->CLI only: MCP recall exposes NO kind filter
    (a deliberately smaller LLM-facing surface — named non-parity, by design)."""
    for query in ("deploy fail migration lock", "queue depth spikes"):
        got_sdk = _sdk_ids(sdk, query, kind=Kind.OBSERVATION)
        got_cli = _cli_ids(store, query, kind="observation")
        assert got_sdk == got_cli
        assert got_sdk, "kind filter returned nothing — the filter leg went unexercised"


def test_sdk_and_cli_agree_on_empty_result(store, sdk):
    """No stored directives: both doors must report the SAME emptiness — the SDK
    as [], the CLI as its documented exit-1 'No memories found' (mapped to [])."""
    assert _sdk_ids(sdk, QUERIES[0], kind=Kind.DIRECTIVE) == []
    assert _cli_ids(store, QUERIES[0], kind="directive") == []


def test_cli_reports_the_same_pipeline_mode_as_the_sdk(store, sdk):
    """Mode-string parity, SDK<->CLI, at BOTH of the CLI's mode surfaces:
    ``recall --json`` (the read itself) and ``doctor``'s freshness line (which
    renders ``Memory.health().mode``). Pin both to the SDK's
    ``RecallResult.mode`` for the same store, so the doors NAME the same
    pipeline, not just rank alike."""
    sdk_mode = sdk.recall(QUERIES[0], k=1).mode
    assert sdk_mode == EXPECTED_MODE
    assert _cli_json(store, QUERIES[0])["mode"] == sdk_mode
    assert _cli_doctor_mode(store) == sdk_mode


def test_cli_json_is_a_new_view_of_the_same_recall_not_a_new_ranking(store, sdk):
    """``--json`` is additive: identical ids to ``--ids`` and to the SDK, plus
    the mode the other formats never printed."""
    for query in QUERIES[:3]:
        payload = _cli_json(store, query)
        assert payload["ids"] == _cli_ids(store, query) == _sdk_ids(sdk, query)
        assert payload["count"] == len(payload["ids"])
        assert payload["mode"] == EXPECTED_MODE


def test_cli_json_reports_mode_even_when_nothing_matched(store, sdk):
    """The degraded-and-empty case is the one a machine caller most needs to
    read: no directives are stored, so the door returns zero hits, exit 1 — and
    STILL names the pipeline that found nothing."""
    payload = _cli_json(store, QUERIES[0], kind="directive")
    assert payload["ids"] == [] and payload["count"] == 0
    assert payload["mode"] == EXPECTED_MODE
    assert _sdk_ids(sdk, QUERIES[0], kind=Kind.DIRECTIVE) == []


# -- all three doors (requires the 'mcp' extra; skips cleanly without it) ----------

def test_three_doors_return_identical_ordered_ids_over_real_stdio(store, sdk):
    """THE parity pin: one store, ten queries, k=5 — SDK, CLI subprocess, and
    the real MCP stdio server (official client) must produce identical ordered
    id lists, and MCP's count must equal its own id-list length."""
    calls = [(q, K) for q in QUERIES]
    mcp_out, status = _mcp_recall_bulk(store, calls)

    rows = []
    for query in QUERIES:
        rows.append((query, {
            "sdk": _sdk_ids(sdk, query),
            "cli": _cli_ids(store, query),
            "mcp": mcp_out[(query, K)]["ids"],
        }))
    table = _parity_table(rows)
    print("\nthree-doors parity (k=%d):\n%s" % (K, table))

    diffs = [q for q, per in rows if not (per["sdk"] == per["cli"] == per["mcp"])]
    assert not diffs, f"three-door ordered-id divergence on {diffs}:\n{table}"
    assert all(per["sdk"] for _, per in rows)
    for query in QUERIES:
        payload = mcp_out[(query, K)]
        assert payload["count"] == len(payload["ids"])
        # Every MCP recall NAMES its pipeline, not just the ranking it produced.
        assert payload["mode"] == EXPECTED_MODE

    # Pipeline-identity over the MCP wire: the pinned scope, the resolved
    # embedder identity, and the mode string the other two doors report.
    assert status["scope"] == f"default/{PROJECT}/default"
    assert status["embedder"] == EXPECTED_EMBEDDER
    assert status["mode"] == EXPECTED_MODE
    assert status["memories"] == store.total  # nothing quarantined => counts agree


def test_mode_crosses_every_door(store, sdk):
    """THE issue-#25 pin: the honest-degradation string is readable through ALL
    THREE doors, in every place a caller would look for it — so an agent behind
    any door can tell a full hybrid ranking from a degraded one.

    Five witnesses to one pipeline. Before this, only the first two existed.
    """
    query = QUERIES[0]
    mcp_out, status = _mcp_recall_bulk(store, [(query, K)])

    witnesses = {
        "sdk.recall().mode": sdk.recall(query, k=K).mode,
        "sdk.health().mode": sdk.health().mode,
        "cli recall --json": _cli_json(store, query)["mode"],
        "cli doctor": _cli_doctor_mode(store),
        "mcp recall": mcp_out[(query, K)]["mode"],
        "mcp status": status["mode"],
    }
    print("\nmode across doors:\n" + "\n".join(f"    {d:<20} {m!r}" for d, m in witnesses.items()))
    disagree = {d: m for d, m in witnesses.items() if m != EXPECTED_MODE}
    assert not disagree, f"doors disagree about the pipeline they run: {disagree}"


def test_cli_json_and_mcp_recall_hand_back_one_payload_shape(store):
    """The two machine doors return the SAME four keys, so a caller can move a
    script from the CLI to MCP (or an agent the other way) without reshaping
    what it reads. Divergence here is how the mode gap crept in the first time.
    """
    query = QUERIES[0]
    mcp_out, _ = _mcp_recall_bulk(store, [(query, K)])
    mcp_payload = mcp_out[(query, K)]
    cli_payload = _cli_json(store, query)

    assert set(cli_payload) == set(mcp_payload) == {
        "context", "directives", "ids", "mode", "count", "abstained", "top_vector_score",
    }
    assert cli_payload["ids"] == mcp_payload["ids"]
    assert cli_payload["mode"] == mcp_payload["mode"]
    assert cli_payload["count"] == mcp_payload["count"]
    assert cli_payload["context"] == mcp_payload["context"]  # one envelope, one renderer
    assert cli_payload["abstained"] == mcp_payload["abstained"]  # abstain gate (ADR-0028/0031)
    assert cli_payload["top_vector_score"] == mcp_payload["top_vector_score"]
    assert cli_payload["directives"] == mcp_payload["directives"]  # standing channel (ADR-0034)


# The abstain gate is a COSINE floor; on the stub corpus every top-1 cosine is
# well under 0.99, so this threshold refuses every query deterministically —
# across processes and machines — without depending on a specific score.
ABSTAIN_MIN_SCORE = 0.99


def test_abstain_reads_as_abstained_through_sdk_and_cli(store, sdk):
    """Issue #47 (SDK<->CLI leg, runs on the no-extra path): the abstain gate
    (ADR-0028) is reachable and HONEST through the CLI, not only the SDK. An
    abstain is zero hits that says WHY — abstained=true + a mode that names the
    gate — and is never confusable with an empty store (the contrast below)."""
    query = QUERIES[0]

    sdk_res = sdk.recall(query, k=K, min_score=ABSTAIN_MIN_SCORE)
    assert sdk_res.abstained is True and len(sdk_res) == 0
    assert "abstained" in sdk_res.mode

    cli = _cli_json(store, query, min_score=ABSTAIN_MIN_SCORE)
    assert cli["abstained"] is True
    assert cli["ids"] == [] and cli["count"] == 0
    assert "abstained" in cli["mode"]
    assert cli["mode"] == sdk_res.mode  # both doors name the same gated pipeline
    # top_vector_score is the cosine the gate compared against — populated, and
    # equal across doors (deterministic stub), and below the threshold.
    assert cli["top_vector_score"] == sdk_res.top_vector_score
    assert cli["top_vector_score"] < ABSTAIN_MIN_SCORE

    # The discriminating contrast: WITHOUT the gate the same query is a normal,
    # non-empty recall that is NOT abstained. Abstain != empty store.
    plain = _cli_json(store, query)
    assert plain["abstained"] is False and plain["count"] > 0


def test_abstain_crosses_every_door(store, sdk):
    """Issue #47 (all three doors; skips cleanly without the mcp extra): an
    abstained recall reads as abstained through SDK, CLI, AND the real MCP stdio
    server — never as an empty store — and every door names the same gated
    pipeline. This is the three-doors twin of the mode pin (issue #25)."""
    query = QUERIES[0]
    mcp_out, _ = _mcp_recall_bulk(store, [(query, K)], min_score=ABSTAIN_MIN_SCORE)
    mcp = mcp_out[(query, K)]

    sdk_res = sdk.recall(query, k=K, min_score=ABSTAIN_MIN_SCORE)
    cli = _cli_json(store, query, min_score=ABSTAIN_MIN_SCORE)

    for name, payload in (("cli", cli), ("mcp", mcp)):
        assert payload["abstained"] is True, f"{name} did not abstain"
        assert payload["ids"] == [] and payload["count"] == 0, f"{name} returned hits"
        assert "abstained" in payload["mode"], f"{name} mode does not name the gate"

    # One gated pipeline, named identically at every door.
    assert sdk_res.mode == cli["mode"] == mcp["mode"]
    assert sdk_res.abstained is True and len(sdk_res) == 0
    assert cli["top_vector_score"] == mcp["top_vector_score"] == sdk_res.top_vector_score


def test_three_doors_agree_at_nondefault_k(store, sdk):
    """k travels intact through the MCP schema (capped at 25, so 3 is inert)."""
    query = QUERIES[0]
    mcp_out, _ = _mcp_recall_bulk(store, [(query, 3)])
    ids_mcp = mcp_out[(query, 3)]["ids"]
    assert ids_mcp == _sdk_ids(sdk, query, k=3) == _cli_ids(store, query, k=3)
    assert len(ids_mcp) == 3


# -- the standing-directive channel across all three doors (ADR-0034) ----------

# A rule that shares NO salient words with any QUERY, so it surfaces ONLY because
# it is a standing directive (it never ranks into an unrelated query's top-k).
STANDING_RULE = "Always explain changes in plain language before writing any code."


@pytest.fixture(scope="module")
def directive_store(store):
    """A twin of ``store`` that ALSO holds one OWNER directive, so the
    standing-directive channel (ADR-0034) is exercised across doors. A SEPARATE db
    file reusing ``store``'s shim env/root, so the shared ``store`` fixture — and
    every existing parity assertion built on it — is untouched."""
    db = store.root / "parity-directives.db"
    mem = Memory(path=str(db), project=PROJECT, embedder=StubEmbedder(), reranker=None)
    mem.remember(STANDING_RULE, kind=Kind.DIRECTIVE, trust=TrustTier.OWNER)
    for fact in REMEMBERED_FACTS:
        mem.remember(fact)
    quarantined = mem.adapter.count(scope=mem.scope, status=Status.QUARANTINED.value)
    mem.close()
    assert quarantined == 0
    return SimpleNamespace(db=str(db), root=store.root, env=store.env)


def test_standing_directive_key_is_identical_across_doors(directive_store):
    """THE ADR-0034 cross-door pin: the ``directives`` payload key is NON-EMPTY and
    identical at SDK, CLI ``--json`` and the real MCP stdio server, for EVERY query
    — including UNRELATED ones, where the rule surfaces only because it is a
    standing directive, not because it ranked in. Skips MCP cleanly without the
    extra (``_mcp_recall_bulk`` importorskips 'mcp')."""
    mcp_out, _ = _mcp_recall_bulk(directive_store, [(q, K) for q in QUERIES])
    sdk = Memory(path=directive_store.db, project=PROJECT, embedder=StubEmbedder(), reranker=None)
    try:
        for query in QUERIES:
            cli = _cli_json(directive_store, query)["directives"]
            mcp = mcp_out[(query, K)]["directives"]
            sdk_dirs = sdk.recall(query, k=K).directives()
            assert cli == mcp == sdk_dirs, f"directives diverged across doors on {query!r}"
            assert cli == [STANDING_RULE], (
                f"the standing rule did not surface (or changed) on {query!r}: {cli}"
            )
    finally:
        sdk.close()


# -- the live project board across all three doors (ADR-0035) -------------------
#
# The board's parity contract is stricter than recall's ordered-id one: the
# PAYLOAD ITSELF is byte-deterministic (a pure function of stored rows, fixed
# key order), so the three doors are pinned BYTE-IDENTICAL — json.dumps of the
# SDK's BoardResult.to_dict(), the CLI's `board --json` stdout, and the MCP
# `board` tool result must be the same string. A one-byte drift means a door
# re-shaped the payload, which is the exact bug class the shared builder
# (rekoll.board.build_board_payload) exists to make impossible.

BOARD_RULE = "Resolve pending items before starting new work."


@pytest.fixture(scope="module")
def board_store(store):
    """A twin of ``store`` with a seeded board: one rule, curated majors and a
    pending item, plain activity, and one below-floor row (text withheld). A
    SEPARATE db reusing ``store``'s shim env/root, so the shared fixtures — and
    every recall-parity assertion on them — stay untouched."""
    db = store.root / "parity-board.db"
    mem = Memory(path=str(db), project=PROJECT, embedder=StubEmbedder(), reranker=None)
    mem.remember(BOARD_RULE, kind=Kind.DIRECTIVE, trust=TrustTier.OWNER)
    mem.remember("storage lane shipped", board="major")
    mem.remember("facade lane shipped", board="major")
    pending = mem.remember("docs pass still open", board="pending")
    for fact in REMEMBERED_FACTS[:3]:
        mem.remember(fact)
    mem.remember("an unverified drive-by note", trust=TrustTier.UNVERIFIED)
    quarantined = mem.adapter.count(scope=mem.scope, status=Status.QUARANTINED.value)
    mem.close()
    assert quarantined == 0
    return SimpleNamespace(db=str(db), root=store.root, env=store.env,
                           pending_id=pending.id)


def _sdk_board_line(board_store) -> str:
    mem = Memory(path=board_store.db, project=PROJECT,
                 embedder=StubEmbedder(), reranker=None)
    try:
        return json.dumps(mem.board().to_dict())
    finally:
        mem.close()


def _cli_board_line(board_store) -> str:
    """Door 2: ``python -m rekoll board --json`` — the RAW stdout line, so the
    comparison is bytes-on-the-wire, not a parsed-and-redumped approximation."""
    proc = subprocess.run(
        [sys.executable, "-m", "rekoll", "board", "--json",
         "--path", board_store.db, "--project", PROJECT],
        capture_output=True, encoding="utf-8", errors="replace",
        env=board_store.env, cwd=str(board_store.root), timeout=120,
    )
    assert proc.returncode == 0, f"CLI board failed:\n{proc.stderr}"
    assert "rekoll: warning:" not in proc.stderr, proc.stderr  # nothing withheld
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"board --json must print exactly one line:\n{proc.stdout}"
    return lines[0]


def _cli_resolve(board_store, record_id: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "rekoll", "resolve", record_id,
         "--path", board_store.db, "--project", PROJECT],
        capture_output=True, encoding="utf-8", errors="replace",
        env=board_store.env, cwd=str(board_store.root), timeout=120,
    )
    assert proc.returncode == 0, f"CLI resolve failed:\n{proc.stderr}"
    return proc.stdout.strip()


def _mcp_board(board_store) -> dict:
    """Door 3: the real stdio server, `board` called with ZERO arguments."""
    pytest.importorskip("mcp")

    async def _inner():
        import inspect

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(board_store.env)
        env["REKOLL_MCP_PATH"] = board_store.db
        env["REKOLL_MCP_PROJECT"] = PROJECT
        env["REKOLL_MCP_ROOT"] = str(board_store.root)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "rekoll.mcp_server"],
            cwd=str(board_store.root),
            env=env,
        )
        with (board_store.root / "mcp-board-stderr.log").open("w", encoding="utf-8") as errlog:
            kwargs = (
                {"errlog": errlog}
                if "errlog" in inspect.signature(stdio_client).parameters
                else {}
            )
            async with stdio_client(params, **kwargs) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return _payload(await session.call_tool("board", {}))

    return asyncio.run(_inner())


def test_board_payload_is_byte_identical_across_sdk_and_cli(board_store):
    """The no-extra leg of THE board pin: the CLI's one stdout line IS the
    SDK's json.dumps — byte-for-byte, key order included — and it is a real
    board (every leg populated, the below-floor entry present with text null)."""
    sdk_line = _sdk_board_line(board_store)
    cli_line = _cli_board_line(board_store)
    assert cli_line == sdk_line

    payload = json.loads(sdk_line)
    assert payload["rules"] == [BOARD_RULE]
    assert [e["board"] for e in payload["majors"]] == ["major", "major", "pending"]
    assert payload["pending_open"] == 1
    withheld = [e for e in payload["recent"] if e["text"] is None]
    assert withheld and all(e["trust"] == "unverified" for e in withheld)


def test_board_payload_is_byte_identical_across_all_three_doors(board_store):
    """THE board parity pin (needs the mcp extra; skips cleanly without): one
    seeded store, three doors, ONE byte string — and after a CLI resolve, all
    doors byte-agree on the NEW board too (the resolved item gone everywhere,
    pending_open down). Fresh MCP session per read: board_snapshot reads a
    fresh snapshot, so a foreign commit is visible to the next poll."""
    sdk_line = _sdk_board_line(board_store)
    cli_line = _cli_board_line(board_store)
    mcp_line = json.dumps(_mcp_board(board_store))
    assert sdk_line == cli_line == mcp_line

    before = json.loads(sdk_line)
    assert board_store.pending_id in [e["id"] for e in before["majors"]]

    assert _cli_resolve(board_store, board_store.pending_id) == "Resolved 1 of 1."

    sdk_after = _sdk_board_line(board_store)
    cli_after = _cli_board_line(board_store)
    mcp_after = json.dumps(_mcp_board(board_store))
    assert sdk_after == cli_after == mcp_after
    assert sdk_after != sdk_line  # the byte-compare change check really moved
    after = json.loads(sdk_after)
    surfaced = [e["id"] for e in after["majors"] + after["recent"]]
    assert board_store.pending_id not in surfaced
    assert after["pending_open"] == 0


def test_standing_directive_crosses_doors_even_under_abstain(directive_store):
    """The channel is abstain-proof through every door: with min_score forcing an
    abstain (zero ranked hits) the standing rule STILL rides ``directives``,
    identically at SDK/CLI/MCP, while ``ids``/``count`` are empty. A standing rule
    is never silenced by an abstain — on any door."""
    query = QUERIES[0]
    mcp_out, _ = _mcp_recall_bulk(directive_store, [(query, K)], min_score=ABSTAIN_MIN_SCORE)
    mcp = mcp_out[(query, K)]
    cli = _cli_json(directive_store, query, min_score=ABSTAIN_MIN_SCORE)
    sdk = Memory(path=directive_store.db, project=PROJECT, embedder=StubEmbedder(), reranker=None)
    try:
        sdk_res = sdk.recall(query, k=K, min_score=ABSTAIN_MIN_SCORE)
        for name, payload in (("cli", cli), ("mcp", mcp)):
            assert payload["abstained"] is True and payload["ids"] == [], f"{name} not abstained"
            assert payload["directives"] == [STANDING_RULE], f"{name} dropped the standing rule"
        assert sdk_res.directives() == [STANDING_RULE]
        assert cli["directives"] == mcp["directives"] == sdk_res.directives()
    finally:
        sdk.close()
