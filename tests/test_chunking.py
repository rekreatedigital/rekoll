"""P1: structure-aware chunking behaves sensibly."""

from __future__ import annotations

from rekoll.chunking import chunk_file, chunk_markdown, chunk_python, chunk_text


def test_short_text_is_one_chunk():
    assert chunk_text("hello world") == ["hello world"]


def test_empty_inputs():
    assert chunk_text("") == []
    assert chunk_markdown("") == []


def test_long_text_splits_within_size():
    text = ("paragraph body line.\n\n" * 200).strip()
    chunks = chunk_text(text, size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_markdown_splits_on_headings():
    md = "# Alpha\nalpha body\n\n# Beta\nbeta body\n\n## Gamma\ngamma body"
    chunks = chunk_markdown(md)
    assert any(c.startswith("# Alpha") for c in chunks)
    assert any(c.startswith("# Beta") for c in chunks)
    assert any(c.startswith("## Gamma") for c in chunks)


def test_markdown_without_headings_falls_back_to_text():
    assert chunk_markdown("just text, no headings here") == ["just text, no headings here"]


def test_chunk_file_dispatches_by_extension():
    assert chunk_file("notes.md", "# H\nbody") == chunk_markdown("# H\nbody")
    assert chunk_file("code.py", "x = 1") == chunk_python("x = 1")


def test_chunk_python_splits_by_def_and_class():
    code = (
        "import os\n\n"
        "X = 1\n\n"
        "def foo():\n    return 1\n\n"
        "@deco\nclass Bar:\n    def m(self):\n        return 2\n"
    )
    chunks = chunk_python(code)
    assert any(c.startswith("def foo") for c in chunks)
    assert any(c.startswith("@deco") or c.startswith("class Bar") for c in chunks)
    assert any("import os" in c for c in chunks)  # module-level preamble is its own chunk


def test_chunk_python_syntax_error_falls_back_to_text():
    bad = "def (:\n  oops"
    assert chunk_python(bad) == chunk_text(bad)
