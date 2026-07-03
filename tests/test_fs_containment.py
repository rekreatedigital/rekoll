"""Filesystem containment: an ingested tree must never read OUTSIDE its root.

The historical symlink guard trusted ``is_symlink()`` — which is ``False`` for an
NTFS **junction** (a directory reparse point creatable with ``mklink /J`` and no
admin). A junction planted inside an ingested tree was therefore descended by
``os.walk`` and its out-of-tree target read into recallable memory. The fix is
real-path containment (``os.path.realpath`` + ``is_relative_to(root)``) applied to
every directory the walk would descend AND every file it would read, in both the
core ``ingest_path`` and the MCP ``_assert_no_symlink_escape`` guard. Real-path
containment catches junctions, symlinks, and any reparse point on every OS.

Coverage: the junction cases are Windows-only (``mklink /J``); the symlink cases
run everywhere the host can create a symlink (Linux CI always can), so the fix is
exercised on POSIX too. A no-link same-name test proves no false-positive skip.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from rekoll import Memory
from rekoll.embedding import StubEmbedder
from rekoll.retrieval import hybrid_search

SAFE = "safe-marker in-root prose that must remain recallable"
SECRET = "secret-marker out-of-root credential material that must never leak"


def _mem() -> Memory:
    return Memory(path=":memory:", embedder=StubEmbedder(), reranker=None)


def _all_text(mem: Memory) -> str:
    """Every stored record's text, INCLUDING quarantined ones (recall hides
    those) — the strongest 'did the secret enter memory at all?' probe."""
    result = hybrid_search(
        mem.adapter, scope=mem.scope, query=f"{SAFE} {SECRET}",
        embedder=mem.embedder, k=50, include_quarantined=True,
    )
    return " ".join(h.record.content for h in result.hits)


def _make_junction_or_skip(link, target) -> None:
    """``link`` -> ``target`` as an NTFS junction, or skip. Junctions need no
    admin (unlike symlinks) but are Windows/NTFS only."""
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows/NTFS-only reparse point")
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:  # pragma: no cover - host-dependent
        pytest.skip(f"could not create a junction: {res.stderr.strip() or res.stdout.strip()}")


def _make_symlink_or_skip(link, target) -> None:
    """``link`` -> ``target`` as a symlink, or skip where the host forbids it
    (Windows without Developer Mode raises WinError 1314). Linux CI always can."""
    try:
        import os
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host-dependent
        pytest.skip(f"cannot create a symlink on this host: {exc}")


def _planted_escape_tree(tmp_path, make_link):
    """root/ (note.md=SAFE) with a dir-link ``root/link`` -> outside/ (secret.txt
    =SECRET). Returns ``root``. ``make_link`` is the junction/symlink planter."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.md").write_text(f"# Note\n\n{SAFE}\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    make_link(root / "link", outside)
    return root


# -- core ingest_path: real-path containment ----------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_dir_escape_is_not_walked_into_by_core(tmp_path):
    # THE blocking bug: is_symlink() is False for a junction, so os.walk descends
    # it and the out-of-tree secret is read into recallable memory.
    root = _planted_escape_tree(tmp_path, _make_junction_or_skip)
    mem = _mem()
    stats = mem.ingest_path(str(root))
    text = _all_text(mem)
    assert "secret-marker" not in text, "junction escaped the ingestion root"
    assert "safe-marker" in text, "the legitimate in-root file must still ingest"
    assert stats["files"] == 1  # only note.md; the junction subtree is not descended
    mem.close()


def test_symlink_dir_escape_is_not_walked_into_by_core(tmp_path):
    # POSIX-visible sibling of the junction case (runs on Linux CI): a directory
    # symlink pointing out of the tree must never be descended.
    root = _planted_escape_tree(tmp_path, _make_symlink_or_skip)
    mem = _mem()
    stats = mem.ingest_path(str(root))
    text = _all_text(mem)
    assert "secret-marker" not in text, "symlinked dir escaped the ingestion root"
    assert "safe-marker" in text
    assert stats["files"] == 1
    mem.close()


def test_legit_same_name_subdir_is_still_ingested(tmp_path):
    # No false positive: a real in-root subdir (no link) whose leaf shares a name
    # with the root must still be fully walked and ingested.
    root = tmp_path / "root"
    (root / "root").mkdir(parents=True)  # a genuine nested dir literally named "root"
    (root / "top.md").write_text(f"# Top\n\n{SAFE} alpha\n", encoding="utf-8")
    (root / "root" / "deep.md").write_text("# Deep\n\ndeep-marker nested prose\n", encoding="utf-8")
    mem = _mem()
    stats = mem.ingest_path(str(root))
    result = hybrid_search(
        mem.adapter, scope=mem.scope, query="deep-marker nested alpha",
        embedder=mem.embedder, k=20, include_quarantined=True,
    )
    text = " ".join(h.record.content for h in result.hits)
    assert "deep-marker" in text and "safe-marker" in text
    assert stats["files"] == 2
    mem.close()


# -- MCP guard: fail-closed real-path containment -----------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_dir_escape_is_refused_by_mcp_guard(tmp_path):
    from rekoll.mcp_server import _assert_no_symlink_escape, _ingest_path

    root = (tmp_path / "root").resolve()
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "ok.md").write_text("fine in-root", encoding="utf-8")
    _assert_no_symlink_escape(root, docs)  # benign tree: no raise

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    _make_junction_or_skip(docs / "link", outside)  # planted junction escape

    with pytest.raises(ValueError, match="outside the project root"):
        _assert_no_symlink_escape(root, docs)
    # And the full ingest path fails closed — nothing enters memory.
    mem = _mem()
    with pytest.raises(ValueError, match="outside the project root"):
        _ingest_path(mem, root, "docs")
    assert mem.count() == 0
    mem.close()


def test_symlink_dir_escape_is_refused_by_mcp_guard(tmp_path):
    # POSIX-visible sibling (runs on Linux CI): the guard fails closed on a
    # directory symlink resolving out of root, via real-path containment.
    from rekoll.mcp_server import _assert_no_symlink_escape

    root = (tmp_path / "root").resolve()
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "ok.md").write_text("fine in-root", encoding="utf-8")
    _assert_no_symlink_escape(root, docs)  # benign: no raise

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    _make_symlink_or_skip(docs / "link", outside)

    with pytest.raises(ValueError, match="outside the project root"):
        _assert_no_symlink_escape(root, docs)
