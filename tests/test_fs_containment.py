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


# -- no false positives: containment must not silently drop legitimate files ---

def test_non_redirecting_reparse_point_is_not_flagged(tmp_path):
    # A OneDrive Files-On-Demand placeholder / Windows Dedup stub IS a reparse
    # point but does NOT redirect — its real path equals its own location. The
    # fix keys off "does this resolve ELSEWHERE", never the reparse attribute, so
    # such an in-tree file is read, not skipped. (Guards the regression where an
    # attribute-based check silently dropped real source files in a cloud-synced
    # repo.) We assert the load-bearing predicate directly: a normal in-place
    # file — the same shape a non-redirecting reparse point presents — is not
    # flagged as a redirect.
    from rekoll.memory import _redirects_out

    f = tmp_path / "placeholder.md"
    f.write_text("# Cloud\n\ncloud-marker in-place content\n", encoding="utf-8")
    assert _redirects_out(f) is False
    assert _redirects_out(tmp_path) is False  # a plain directory is not a redirect


def test_ingest_through_symlinked_ancestor_is_not_falsely_skipped(tmp_path):
    # A symlinked ANCESTOR (e.g. macOS /tmp -> /private/tmp, or a symlinked home)
    # must not make the real leaf look like a redirect: containment resolves the
    # leaf against its resolved PARENT, not the literal abspath. Reached through
    # the symlink, the in-tree repo still ingests. (Runs on Linux CI; skips where
    # symlinks can't be created.)
    real_parent = tmp_path / "real"
    (real_parent / "repo").mkdir(parents=True)
    (real_parent / "repo" / "note.md").write_text(f"# R\n\n{SAFE} beta\n", encoding="utf-8")
    link_parent = tmp_path / "linkparent"
    _make_symlink_or_skip(link_parent, real_parent)

    mem = _mem()
    stats = mem.ingest_path(str(link_parent / "repo"))  # reached via a symlinked ancestor
    text = _all_text(mem)
    assert stats["files"] == 1, "a symlinked ancestor must not false-positive the leaf"
    assert "safe-marker" in text
    mem.close()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_directly_pointed_junction_root_warns_and_skips(tmp_path):
    # Pointing ingest_path STRAIGHT at a junction (not its parent) resolves out of
    # the tree it names, so it warns+skips like a directly-pointed symlink — the
    # out-of-tree target is never read. (Old code walked a directly-pointed
    # directory link silently.)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    link = tmp_path / "link"
    _make_junction_or_skip(link, outside)

    mem = _mem()
    with pytest.warns(UserWarning, match="junction"):
        stats = mem.ingest_path(str(link))
    assert stats["files"] == 0 and stats["skipped"] == 1
    assert "secret-marker" not in _all_text(mem)
    mem.close()
