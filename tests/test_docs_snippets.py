"""The docs' Python snippets must actually run — extracted verbatim, executed.

This guards the onboarding promise: any ```python fence in the README or
QUICKSTART works against the current engine, unmodified. The auto embedder and
reranker are pinned to their no-extra fallbacks so the test is deterministic
and offline everywhere (snippets use plain ``Memory()``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rekoll import Memory
from rekoll.embedding import StubEmbedder

ROOT = Path(__file__).resolve().parent.parent
_PYTHON_FENCE = re.compile(r"```python\r?\n(.*?)```", re.DOTALL)

DOCS_WITH_SNIPPETS = ["README.md", "docs/QUICKSTART.md"]


def _python_blocks(doc: Path) -> list[str]:
    return _PYTHON_FENCE.findall(doc.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _snippet_world(monkeypatch, tmp_path):
    """An empty project cwd shaped like the docs assume: a docs/ folder exists
    (QUICKSTART calls ``mem.ingest_path("docs/")``), stub embedder, no reranker."""
    monkeypatch.setattr("rekoll.memory._auto_embedder", lambda: StubEmbedder())
    monkeypatch.setattr("rekoll.memory._auto_reranker", lambda: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text(
        "# Notes\n\nThe deploy runs nightly on the VPS.", encoding="utf-8"
    )


# Snippets that cannot run offline (BYO-AI examples needing a provider key)
# opt out with a first line starting `# docs: no-run` — the marker doubles as
# reader-facing documentation that the example needs a key.
_NO_RUN = "# docs: no-run"


@pytest.mark.parametrize("doc", DOCS_WITH_SNIPPETS)
def test_every_python_snippet_runs_as_written(doc, capsys):
    blocks = _python_blocks(ROOT / doc)
    assert blocks, f"no ```python fences found in {doc} - did the fence style change?"
    for i, block in enumerate(blocks):
        if block.lstrip().startswith(_NO_RUN):
            continue
        namespace: dict = {"__name__": "docs_snippet"}
        exec(compile(block, f"<{doc} python snippet {i + 1}>", "exec"), namespace)
        for value in namespace.values():  # don't leave SQLite handles open on Windows
            if isinstance(value, Memory):
                value.close()
    out = capsys.readouterr().out
    assert "Postgres" in out, f"{doc} snippets printed nothing recognizable"
