"""MCP boundary output hygiene — what the calling model gets to see.

Three properties of the LLM-facing surface (companions to test_mcp_server.py,
kept in their own file):

1. Tool ERROR MESSAGES never leak the server's absolute filesystem layout.
   The configured root and resolved paths are the operator's business; the
   model only needs the path as IT spelled it plus what to do next. An
   absolute path in an error is a small reconnaissance gift (usernames,
   directory layout) to whatever is driving the model (L-mcp-rootleak).

2. Tool RESULTS don't silently under-report: ``ingest_path`` must surface the
   core's ``skipped`` count (symlinks/junctions, oversize files, over-chunk-cap
   documents, undecodable bytes) AND its ``filtered`` count (names excluded
   unread — vendored venvs, lockfiles, credential-shaped names; ADR-0027). The
   core signals both with ``warnings`` — which never cross stdio — so without
   the counts an MCP caller ingesting such a tree sees ``{files: 0, chunks: 0}``
   with no explanation. Counts only, never names.

3. Tool RESULTS say HOW they were produced: ``recall`` and ``status`` carry
   ``mode``, the honest-degradation string (ADR-0024). A degraded read (vector
   leg refused after an embedder swap) returns hits of the SAME SHAPE as a
   healthy one, just ranked worse — and the ``embedder`` name is identical in
   both, because it is the STORED identity that differs. Without ``mode`` the
   calling model has no way at all to tell them apart, and Rekoll's promise not
   to bluff a broken index would stop at the SDK boundary (issue #25).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rekoll import Memory, TrustTier
from rekoll.embedding import StubEmbedder
from rekoll.mcp_server import (
    ServerConfig,
    _assert_no_symlink_escape,
    _contained_path,
    _ingest_path,
    _recall,
    _status,
)

_HAS_MCP = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(
    not _HAS_MCP, reason="optional extra not installed: pip install 'rekoll[mcp]'"
)

_SRC = str(Path(__file__).resolve().parent.parent / "src")

HEALTHY_MODE = "vector+lexical (stub-embedder)"
DEGRADED_MODE = "lexical-only: embedder mismatch"


def _mem(**kwargs) -> Memory:
    """A Memory wired like the server's default: firewall on, UNVERIFIED
    write trust, stub embedder (deterministic, no extras)."""
    kwargs.setdefault("project", "unit")
    kwargs.setdefault("default_trust", TrustTier.UNVERIFIED)
    kwargs.setdefault("path", ":memory:")
    return Memory(embedder=StubEmbedder(), reranker=None, **kwargs)


def _cfg(tmp_path: Path, **kwargs) -> ServerConfig:
    fields = dict(
        path=str(tmp_path / "m.db"), tenant="default", project="unit",
        agent="default", trust=TrustTier.UNVERIFIED, root=tmp_path,
    )
    fields.update(kwargs)
    return ServerConfig(**fields)


def _make_escape_link_or_skip(link: Path, target_file: Path, target_dir: Path) -> None:
    """Create a link at ``link`` that resolves out of the root: a file symlink
    where the host allows one, else (Windows) a directory junction — ``mklink
    /J`` needs no privilege, and ``_assert_no_symlink_escape`` must catch both.
    Skip only when the host can create neither."""
    try:
        os.symlink(str(target_file), str(link))
        return
    except (OSError, NotImplementedError):  # pragma: no cover - host-dependent
        pass
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_dir)],
            capture_output=True,
        )
        if proc.returncode == 0 and link.exists():
            return
    pytest.skip("host can create neither a symlink nor a junction")


# -- 1. error messages: no absolute paths ---------------------------------------

def test_containment_refusal_leaks_no_absolute_root(tmp_path):
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    with pytest.raises(ValueError) as exc:
        _contained_path(root, "../outside.md")
    msg = str(exc.value)
    assert "outside the project root" in msg  # the pinned, e2e-asserted phrase
    assert "--root" in msg  # the actionable guidance stays
    assert str(root) not in msg and str(tmp_path) not in msg


def test_missing_path_error_echoes_callers_spelling_not_resolved(tmp_path):
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    with pytest.raises(ValueError) as exc:
        _contained_path(root, "missing.md")
    msg = str(exc.value)
    assert "does not exist" in msg
    assert "missing.md" in msg  # the caller's own spelling...
    assert str(root) not in msg  # ...never the server-resolved absolute form


def test_symlink_escape_error_names_entry_without_absolute_prefix(tmp_path):
    root = (tmp_path / "proj").resolve()
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "ok.md").write_text("fine", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    _make_escape_link_or_skip(docs / "escape", outside / "secret.txt", outside)

    with pytest.raises(ValueError) as exc:
        _assert_no_symlink_escape(root, docs)
    msg = str(exc.value)
    assert "outside the project root" in msg
    # The operator can still find the offender (named relative to the root)...
    assert "escape" in msg
    # ...but no absolute path — neither the root nor the walked entry.
    assert str(root) not in msg and str(tmp_path) not in msg


# -- 2. results: the skipped AND filtered counts cross the boundary ---------------

def test_ingest_path_surfaces_the_cores_skipped_count(tmp_path):
    mem = _mem()
    root = tmp_path.resolve()
    docs = root / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "bad.md").write_bytes(b"\xff\xfe\xfa not utf-8 \xff")  # undecodable -> skipped

    out = _ingest_path(mem, root, "docs")
    assert set(out) == {
        "files", "chunks", "skipped", "filtered",
        "secrets_skipped", "secrets_stored", "total",
    }
    assert out["files"] == 1 and out["chunks"] >= 1
    assert out["skipped"] == 1  # the model is told, not left to infer from silence


def test_ingest_path_surfaces_the_cores_filtered_count(tmp_path):
    """``filtered`` (ADR-0027: names excluded unread — vendored venvs, lockfiles,
    credential-shaped names) must cross too. The core announces it through
    ``warnings``, which never reach an MCP caller; without the count a
    lockfile-heavy repo ingests "inexplicably small" (ADR-0027 §5) and the model
    cannot tell that from a broken ingest.
    """
    mem = _mem()
    root = tmp_path.resolve()
    docs = root / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    (docs / "credentials.json").write_text('{"api_key": "sk-not-a-real-key"}', encoding="utf-8")

    # The core warns in-process (the SDK's half of the contract); the warning
    # never crosses stdio, which is exactly why the count below has to.
    with pytest.warns(UserWarning, match="look like credentials"):
        out = _ingest_path(mem, root, "docs")
    assert out["files"] == 1 and out["skipped"] == 0
    assert out["filtered"] == 2  # the lockfile and the credentials file, unread
    # Counts, never names: the server's filesystem layout is not the model's
    # business (L-mcp-rootleak), and "which file looked like a credential" is
    # exactly the detail an injected instruction would want back.
    assert "credentials" not in json.dumps(out)
    assert "package-lock" not in json.dumps(out)


def test_ingest_path_filter_keeps_secrets_out_of_recallable_memory(tmp_path):
    """The filtered credential file is not merely uncounted — it is never read,
    so it can never be recalled through any door."""
    mem = _mem()
    root = tmp_path.resolve()
    docs = root / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "credentials.json").write_text('{"api_key": "sk-not-a-real-key"}', encoding="utf-8")

    with pytest.warns(UserWarning, match="look like credentials"):
        _ingest_path(mem, root, "docs")
    hits = _recall(mem, "api_key sk credentials", 5)
    assert "sk-not-a-real-key" not in hits["context"]
    assert "api_key" not in hits["context"]


def test_ingest_path_carries_every_key_the_core_reports(tmp_path):
    """``mcp_server._ingest_path`` copies the core's stats key by key, so a key
    ADDED to ``Memory.ingest_path``'s return dict would be silently dropped at
    the boundary — the same class of gap as the missing ``mode`` (issue #25):
    the door quietly reports less than the engine knows.

    This fails loudly instead, forcing a decision about whether the new key
    belongs on the LLM-facing surface.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")

    core_keys = set(_mem().ingest_path(str(docs), follow_symlinks=False))
    door_keys = set(_ingest_path(_mem(), tmp_path.resolve(), "docs"))
    assert door_keys == core_keys, (
        f"the MCP door reports {sorted(door_keys)} but Memory.ingest_path returns "
        f"{sorted(core_keys)} — carry the new key across the boundary (or decide, "
        "in mcp_server._ingest_path, that the calling model must not see it)"
    )


def test_mcp_folder_ingest_surfaces_secrets_skipped_over_the_wire(tmp_path):
    """Issue #41 (Case A): a folder ingest that EXCLUDED a credential-shaped file
    reports it as a COUNT in the wire payload — the core's warning cannot cross
    stdio, so this count is the only signal. Never the name."""
    root = tmp_path.resolve()
    docs = root / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "credentials.json").write_text('{"api_key": "sk-not-a-real-key"}', encoding="utf-8")

    with pytest.warns(UserWarning, match="look like credentials"):
        payload = _ingest_path(_mem(), root, "docs")

    assert payload["secrets_skipped"] == 1
    assert payload["secrets_stored"] == 0
    assert payload["files"] == 1  # only good.md was read
    # Counts only — the filename never crosses the boundary.
    assert "credentials" not in json.dumps(payload)


def test_mcp_direct_path_ingest_surfaces_secrets_stored_over_the_wire(tmp_path):
    """Issue #41 (Case B): pointing ingest_path straight at a credential file
    bypasses the filter (explicit intent) and STORES it — exactly the path an
    injected 'index ./credentials.json' instruction takes. Pre-fix, the wire
    payload was byte-identical to a normal file's; now secrets_stored says so.
    Still a count, never the name."""
    root = tmp_path.resolve()
    docs = root / "docs"
    docs.mkdir()
    (docs / "credentials.json").write_text('{"api_key": "sk-not-a-real-key"}', encoding="utf-8")

    with pytest.warns(UserWarning, match="STORED"):
        payload = _ingest_path(_mem(), root, "docs/credentials.json")

    assert payload["secrets_stored"] == 1
    assert payload["files"] == 1
    # A normal file's payload has secrets_stored == 0 — the two are now distinct.
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    normal = _ingest_path(_mem(), root, "docs/good.md")
    assert normal["secrets_stored"] == 0
    # Counts only — the filename never crosses the boundary.
    assert "credentials" not in json.dumps(payload)


# -- 3. results: the retrieval mode crosses the boundary -------------------------

def _degraded_mem(tmp_path: Path) -> Memory:
    """A Memory whose scope was embedded by a DIFFERENT embedder identity.

    Written with ``StubEmbedder(dim=32)``, reopened with the default
    ``StubEmbedder()`` (dim=64): same model *name*, different config — exactly
    the silent swap ADR-0024 refuses the vector leg for. Reads degrade to
    lexical-only; nothing raises. The in-process warning is asserted here (it is
    the SDK's own half of the contract) — but a warning never crosses stdio,
    which is exactly why ``mode`` has to.
    """
    db = str(tmp_path / "degraded.db")
    seed = Memory(path=db, project="unit", embedder=StubEmbedder(dim=32), reranker=None,
                  default_trust=TrustTier.UNVERIFIED)
    seed.remember("we chose Postgres over BigQuery for cost")
    seed.close()
    with pytest.warns(UserWarning, match="vector leg is REFUSED"):
        return _mem(path=db)  # dim=64 now: identity mismatch


def test_recall_and_status_report_the_same_healthy_mode(tmp_path):
    mem = _mem()
    mem.remember("we chose Postgres over BigQuery for cost")
    recall = _recall(mem, "why postgres", 3)
    status = _status(mem, _cfg(tmp_path))
    assert recall["mode"] == status["mode"] == HEALTHY_MODE
    # One pipeline, one label: a status check taken at session start must
    # describe the same read a later recall performs.
    assert "mode" in recall and "mode" in status


def test_degraded_recall_is_labelled_lexical_only_not_bluffed(tmp_path):
    """THE property this issue exists for. A mismatched scope still answers —
    with plausible, keyword-ranked hits. Only ``mode`` distinguishes them."""
    mem = _degraded_mem(tmp_path)
    out = _recall(mem, "why postgres", 3)

    assert out["count"] >= 1  # hits still arrive, same shape as a healthy recall...
    assert "Postgres" in out["context"]
    assert out["mode"] == DEGRADED_MODE  # ...but the model is told they are degraded
    mem.close()


def test_status_exposes_the_degradation_that_the_embedder_name_hides(tmp_path):
    """``embedder`` alone cannot reveal a mismatch: it reports the embedder the
    server is HOLDING, which is 'stub-hash' in both the healthy and the degraded
    scope — only the STORED identity differs. So ``mode`` is not redundant with
    it; it is the only signal a calling agent has."""
    healthy = _mem()
    degraded = _degraded_mem(tmp_path)

    healthy_status = _status(healthy, _cfg(tmp_path))
    degraded_status = _status(degraded, _cfg(tmp_path))

    assert healthy_status["embedder"] == degraded_status["embedder"] == "stub-hash"
    assert healthy_status["mode"] == HEALTHY_MODE
    assert degraded_status["mode"] == DEGRADED_MODE
    degraded.close()


def test_mode_string_leaks_no_paths_model_names_or_config_hashes(tmp_path):
    """``mode`` is a new field on the LLM-facing surface, so it inherits property
    #1: it must describe the PIPELINE, never the deployment. The mismatch
    *warning* names both embedders and their config hashes — none of that may
    ride out on the wire (L-mcp-rootleak)."""
    for mem in (_mem(), _degraded_mem(tmp_path)):
        for value in (_recall(mem, "anything", 3)["mode"], _status(mem, _cfg(tmp_path))["mode"]):
            assert not any(ch in value for ch in "/\\"), value  # no paths
            assert "dim=" not in value and "config=" not in value  # no identity internals
            assert str(tmp_path) not in value
            # A closed vocabulary: legs, the stub marker, and the mismatch reason.
            leftover = (
                value.replace("vector", "").replace("lexical-only", "")
                .replace("lexical", "").replace("rerank", "").replace("none", "")
                .replace("(stub-embedder)", "").replace(": embedder mismatch", "")
                .replace("+", "").replace(" ", "")
            )
            assert leftover == "", f"unexpected content in mode string: {value!r} -> {leftover!r}"
        mem.close()


def test_mode_never_contaminates_the_context_envelope(tmp_path):
    """``mode`` rides beside the envelope, never inside it: ``context()`` stays a
    pure function of the hits so an agent's prompt cache isn't busted by a
    degradation notice appearing mid-conversation (RecallResult.context)."""
    for mem in (_mem(), _degraded_mem(tmp_path)):
        mem.remember("the deploy window is Tuesday")
        out = _recall(mem, "deploy window", 3)
        assert out["mode"] not in out["context"]
        assert "mismatch" not in out["context"] and "stub" not in out["context"]
        mem.close()


# -- 4. the same three properties over the REAL stdio wire -----------------------
#
# The unit tests above exercise the tool bodies; these spawn the actual server
# subprocess and drive it with the official client, so a FastMCP serialization
# change (dropping dict keys, rewrapping error text) can't silently void the
# boundary properties. Mirrors test_mcp_server.py's e2e harness, kept local so
# the two files stay independently runnable.

def _payload(result) -> dict:
    assert not result.isError, f"tool errored: {result.content}"
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    text = next(c.text for c in result.content if getattr(c, "type", "") == "text")
    return json.loads(text)


def _error_text(result) -> str:
    assert result.isError, "expected a tool error"
    return " ".join(getattr(c, "text", "") for c in result.content)


def _stub_pinned_env(tmp: Path) -> dict:
    """Environment for a server subprocess that must resolve a KNOWN embedder.

    Pins (a) this checkout's ``src`` ahead of any editable install and (b) a
    ``fastembed`` module that raises on import, so ``memory._auto_embedder``
    deterministically falls back to ``StubEmbedder()`` (dim=64) and
    ``_auto_reranker`` to ``None`` — on any machine, including one with the real
    'embeddings' extra installed. Same convention as
    tests/test_three_doors_parity.py.
    """
    shim = tmp / "no-fastembed-shim"
    shim.mkdir(exist_ok=True)
    (shim / "fastembed.py").write_text(
        'raise ImportError("pinned unavailable: this test needs a deterministic embedder")\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(shim) + os.pathsep + _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_server_session(tmp: Path, fn, *, db: str = "mem.db", env: dict | None = None):
    """Spawn ``python -m rekoll.mcp_server`` rooted at ``tmp`` and drive it
    (same errlog handling as test_mcp_server.py — the SDK's default stderr can
    be a capsys stream without an OS handle)."""

    async def _inner():
        import inspect

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "rekoll.mcp_server",
                "--path", str(tmp / db),
                "--project", "e2e",
                "--root", str(tmp),
            ],
            cwd=str(tmp),
            env=env,  # None => the SDK's default environment (existing callers)
        )
        with (tmp / "server-stderr.log").open("w", encoding="utf-8") as errlog:
            kwargs = (
                {"errlog": errlog}
                if "errlog" in inspect.signature(stdio_client).parameters
                else {}
            )
            async with stdio_client(params, **kwargs) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)

    return asyncio.run(_inner())


@requires_mcp
def test_e2e_ingest_path_reports_skipped_and_filtered_over_the_wire(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "bad.md").write_bytes(b"\xff\xfe\xfa not utf-8 \xff")  # undecodable -> skipped
    (docs / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")  # filtered

    async def fn(session):
        return _payload(await session.call_tool("ingest_path", {"path": "docs"}))

    out = _run_server_session(tmp_path, fn, env=_stub_pinned_env(tmp_path))
    assert out["files"] == 1 and out["skipped"] == 1
    # The core announces filtering with a warning; warnings never cross stdio.
    # This count is the ONLY way the calling model learns the folder held more.
    assert out["filtered"] == 1
    assert "package-lock" not in json.dumps(out)  # counts, never names


@requires_mcp
def test_e2e_degraded_mode_reaches_the_calling_model_over_the_wire(tmp_path):
    """The honest-degradation contract, end to end: a scope embedded by another
    identity is served by a real ``rekoll-mcp`` subprocess, and BOTH tools tell
    the calling model that this ranking is lexical-only.

    Everything else about the response is indistinguishable from a healthy one —
    hits arrive, the envelope renders, and ``embedder`` reads 'stub-hash' either
    way. ``mode`` is the only wire signal that separates them.
    """
    db = tmp_path / "mem.db"
    seed = Memory(path=str(db), project="e2e", embedder=StubEmbedder(dim=32), reranker=None,
                  default_trust=TrustTier.UNVERIFIED)
    seed.remember("we chose Postgres over BigQuery for cost")
    seed.close()

    async def fn(session):
        recall = _payload(await session.call_tool("recall", {"query": "why postgres", "k": 3}))
        status = _payload(await session.call_tool("status", {}))
        return recall, status

    # The server auto-resolves StubEmbedder() (dim=64) under the shim => mismatch.
    recall, status = _run_server_session(tmp_path, fn, env=_stub_pinned_env(tmp_path))

    assert set(recall) == {
        "context", "ids", "mode", "count", "abstained", "top_vector_score",
    }
    assert recall["count"] >= 1 and "Postgres" in recall["context"]  # looks healthy...
    assert recall["mode"] == DEGRADED_MODE  # ...and says otherwise
    assert recall["abstained"] is False  # no gate was requested
    assert status["mode"] == DEGRADED_MODE
    assert status["embedder"] == "stub-hash"  # the name alone would have hidden it


@requires_mcp
def test_e2e_healthy_mode_reaches_the_calling_model_over_the_wire(tmp_path):
    """The same wire, an intact index: ``mode`` must name the full pipeline (and
    admit the stub), never omit itself when nothing is wrong."""

    async def fn(session):
        await session.call_tool("remember", {"content": "the deploy window is Tuesday 14:00 UTC"})
        recall = _payload(await session.call_tool("recall", {"query": "deploy window", "k": 3}))
        status = _payload(await session.call_tool("status", {}))
        return recall, status

    recall, status = _run_server_session(tmp_path, fn, env=_stub_pinned_env(tmp_path))
    assert recall["count"] >= 1
    assert recall["mode"] == status["mode"] == HEALTHY_MODE


@requires_mcp
def test_e2e_tool_error_text_carries_no_absolute_paths(tmp_path):
    async def fn(session):
        escape = await session.call_tool("ingest_path", {"path": "../"})
        missing = await session.call_tool("ingest_path", {"path": "no-such-dir"})
        return _error_text(escape), _error_text(missing)

    escape_text, missing_text = _run_server_session(tmp_path, fn)
    assert "outside the project root" in escape_text
    assert "does not exist" in missing_text and "no-such-dir" in missing_text
    for text in (escape_text, missing_text):
        # Neither spelling of the server's root may reach the model.
        assert str(tmp_path) not in text
        assert str(tmp_path.resolve()) not in text
