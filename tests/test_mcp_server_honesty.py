"""MCP boundary output hygiene — what the calling model gets to see.

Two properties of the LLM-facing surface (companions to test_mcp_server.py,
kept in their own file):

1. Tool ERROR MESSAGES never leak the server's absolute filesystem layout.
   The configured root and resolved paths are the operator's business; the
   model only needs the path as IT spelled it plus what to do next. An
   absolute path in an error is a small reconnaissance gift (usernames,
   directory layout) to whatever is driving the model (L-mcp-rootleak).

2. Tool RESULTS don't silently under-report: ``ingest_path`` must surface the
   core's ``skipped`` count (symlinks/junctions, oversize files, over-chunk-cap
   documents, undecodable bytes). The core signals skips with ``warnings`` —
   which never cross stdio — so without the count an MCP caller ingesting a
   tree of skipped files sees ``{files: 0, chunks: 0}`` with no explanation.
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
    _assert_no_symlink_escape,
    _contained_path,
    _ingest_path,
)

_HAS_MCP = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(
    not _HAS_MCP, reason="optional extra not installed: pip install 'rekoll[mcp]'"
)


def _mem(**kwargs) -> Memory:
    """A Memory wired like the server's default: firewall on, UNVERIFIED
    write trust, stub embedder (deterministic, no extras)."""
    kwargs.setdefault("project", "unit")
    kwargs.setdefault("default_trust", TrustTier.UNVERIFIED)
    return Memory(path=":memory:", embedder=StubEmbedder(), reranker=None, **kwargs)


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


# -- 2. results: the skipped count crosses the boundary --------------------------

def test_ingest_path_surfaces_the_cores_skipped_count(tmp_path):
    mem = _mem()
    root = tmp_path.resolve()
    docs = root / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "bad.md").write_bytes(b"\xff\xfe\xfa not utf-8 \xff")  # undecodable -> skipped

    out = _ingest_path(mem, root, "docs")
    assert set(out) == {"files", "chunks", "skipped", "total"}
    assert out["files"] == 1 and out["chunks"] >= 1
    assert out["skipped"] == 1  # the model is told, not left to infer from silence


# -- 3. the same two properties over the REAL stdio wire -------------------------
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


def _run_server_session(tmp: Path, fn):
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
                "--path", str(tmp / "mem.db"),
                "--project", "e2e",
                "--root", str(tmp),
            ],
            cwd=str(tmp),
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
def test_e2e_ingest_path_reports_skipped_over_the_wire(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Note\n\nThe deploy runs nightly.", encoding="utf-8")
    (docs / "bad.md").write_bytes(b"\xff\xfe\xfa not utf-8 \xff")  # undecodable -> skipped

    async def fn(session):
        return _payload(await session.call_tool("ingest_path", {"path": "docs"}))

    out = _run_server_session(tmp_path, fn)
    assert out["files"] == 1 and out["skipped"] == 1


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
