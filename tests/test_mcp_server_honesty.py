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

import os
import subprocess
from pathlib import Path

import pytest

from rekoll import Memory, TrustTier
from rekoll.embedding import StubEmbedder
from rekoll.mcp_server import (
    _assert_no_symlink_escape,
    _contained_path,
    _ingest_path,
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
