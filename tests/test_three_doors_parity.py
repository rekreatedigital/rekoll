"""Three-doors parity — SDK, CLI, and MCP must return IDENTICAL results.

Rekoll ships three doors over one engine:

 - **SDK**  — ``rekoll.Memory`` in-process (src/rekoll/memory.py),
 - **CLI**  — ``python -m rekoll recall`` as a real subprocess (src/rekoll/cli.py),
 - **MCP**  — ``python -m rekoll.mcp_server`` over real stdio, driven by the
   official MCP client (src/rekoll/mcp_server.py).

The parity contract pinned here: for the same (store, query, k, kind, scope,
embedder, reranker), every door returns the SAME ordered top-k id list. A rank
flip or a membership diff between doors would mean the doors run *different*
pipelines — the exact bug class this file exists to catch.

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

Known, NAMED non-parity surfaces (asserted/documented here, by design):

 - MCP ``recall`` has no ``kind`` filter (smaller LLM-facing surface), so
   kind-filtered parity is SDK<->CLI only.
 - MCP does not expose ``RecallResult.mode`` (the honest-degradation string);
   its ``status`` tool reports the embedder identity name only. Pipeline-mode
   parity for the MCP door is therefore asserted via the embedder identity plus
   id-list identity, not a mode string.
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
from rekoll.model import Kind, Status

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


def _mcp_recall_bulk(store, calls: list[tuple[str, int]]):
    """Door 3: the REAL server over stdio, driven by the official MCP client.

    Configured via REKOLL_MCP_* env vars (path/project/root) — the documented
    deployment surface — pointing at the same store file and project scope.
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
                        out[(query, k)] = _payload(
                            await session.call_tool("recall", {"query": query, "k": k})
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
    """Mode-string parity, SDK<->CLI: the CLI's doctor freshness line renders
    ``Memory.health().mode`` — pin it to the SDK's ``RecallResult.mode`` for the
    same store, so the two doors NAME the same pipeline, not just rank alike."""
    sdk_mode = sdk.recall(QUERIES[0], k=1).mode
    assert sdk_mode == EXPECTED_MODE
    assert _cli_doctor_mode(store) == sdk_mode


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

    # Pipeline-identity over the MCP wire: mode is not exposed (named gap), so
    # pin what IS: the pinned scope and the resolved embedder identity — the
    # same "stub-hash" the SDK door runs (an identity mismatch would have
    # degraded ranking and broken the tables above anyway).
    assert status["scope"] == f"default/{PROJECT}/default"
    assert status["embedder"] == EXPECTED_EMBEDDER
    assert status["memories"] == store.total  # nothing quarantined => counts agree


def test_three_doors_agree_at_nondefault_k(store, sdk):
    """k travels intact through the MCP schema (capped at 25, so 3 is inert)."""
    query = QUERIES[0]
    mcp_out, _ = _mcp_recall_bulk(store, [(query, 3)])
    ids_mcp = mcp_out[(query, 3)]["ids"]
    assert ids_mcp == _sdk_ids(sdk, query, k=3) == _cli_ids(store, query, k=3)
    assert len(ids_mcp) == 3
