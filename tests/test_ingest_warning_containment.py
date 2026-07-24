"""Warnings provoked by PARSING INGESTED CONTENT must never reach rekoll's
output (issue #89, field report #82 finding 7).

Chunking parses ingested Python with the stdlib ``ast``; a target file whose
SOURCE provokes a compile-time warning (e.g. ``x = "foo\\_bar"`` — an invalid
escape sequence: DeprecationWarning on 3.10/3.11, SyntaxWarning on 3.12+) used
to leak ``<unknown>:1: SyntaxWarning: ...`` into the CLI's stderr, polluting
tool/agent output with the target file's lint noise. The containment lives
around exactly that parse (``chunking.chunk_python``); rekoll's OWN warnings
are load-bearing product surface and must keep flowing — both directions are
pinned here, through the same real-subprocess path.

Why subprocesses: Python caches warnings per location (``__warningregistry__``)
and pytest manages the process-wide warning filters around tests, so an
in-process RED/GREEN on this behavior is unreliable. A fresh subprocess running
the real CLI reproduces exactly what a user/agent sees and is immune to that
state. ``PYTHONWARNINGS=always`` makes the leak deterministic across
3.10-3.13: under DEFAULT filters the 3.10/3.11 spelling (DeprecationWarning,
attributed to module ``<unknown>``) is hidden, so only 3.12+ users saw the
leak — but the containment must hold for any category under any user warning
config, which is the property asserted here.

Deliberately extras-free (stdlib + pytest only): CI's core cell runs with
``[dev]`` alone, and the fastembed shim below pins the subprocess to the
deterministic StubEmbedder on machines that DO have the embeddings extra
(same convention as tests/test_three_doors_parity.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _env_pinned_to_this_checkout() -> dict:
    """Subprocesses must import THIS checkout's rekoll, not whatever happens to
    be pip-installed (an editable install can point at a different worktree).
    Same helper as tests/test_cli.py."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _ingest_env(tmp_path: Path) -> dict:
    """The pinned env plus (a) a ``fastembed`` shim so the subprocess resolves
    the offline, deterministic StubEmbedder even where the 'embeddings' extra
    is installed, and (b) ``PYTHONWARNINGS=always`` so every warning category
    the parse could provoke is VISIBLE if it escapes — on 3.10/3.11 the invalid
    escape is a DeprecationWarning that default filters would hide, and a test
    that passes because the leak was merely invisible proves nothing."""
    shim = tmp_path / "no-fastembed-shim"
    shim.mkdir(exist_ok=True)
    (shim / "fastembed.py").write_text(
        'raise ImportError(\n'
        '    "warning-containment harness: fastembed is deliberately unavailable "\n'
        '    "in this subprocess so ingest resolves the deterministic StubEmbedder"\n'
        ')\n',
        encoding="utf-8",
    )
    env = _env_pinned_to_this_checkout()
    env["PYTHONPATH"] = str(shim) + os.pathsep + env["PYTHONPATH"]
    env["PYTHONWARNINGS"] = "always"
    return env


def _cli_ingest(target: Path, db: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rekoll", "ingest", str(target), "--path", str(db)],
        capture_output=True, text=True, env=env,
    )


def test_ingest_does_not_leak_target_file_parse_warnings(tmp_path):
    """Issue #89 repro: a target .py with an invalid escape sequence must index
    cleanly — the file's compile-time lint noise never reaches our output."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "bad_escape.py").write_text('x = "foo\\_bar"\ny = 1\n', encoding="utf-8")

    result = _cli_ingest(corpus, tmp_path / "mem.db", _ingest_env(tmp_path))

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    # The file itself still ingests fine — containment must not eat the chunk.
    assert "Indexed 1 file" in result.stdout, combined
    # Neither the concrete leak from #89 nor either version's spelling of it.
    assert "invalid escape sequence" not in combined, combined
    assert "SyntaxWarning" not in combined, combined
    assert "DeprecationWarning" not in combined, combined
    assert "<unknown>" not in combined, combined


def test_rekolls_own_ingest_warning_still_surfaces(tmp_path):
    """The containment is scoped to the parse of ingested bytes ONLY: rekoll's
    own load-bearing warnings must keep riding the very same CLI path. The
    walk skipping a credential-shaped file warns via ``warnings.warn`` in
    ``ingest_path`` — that must still reach stderr."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "notes.py").write_text("answer = 42\n", encoding="utf-8")
    (corpus / "credentials.json").write_text('{"token": "not-a-real-token"}', encoding="utf-8")

    result = _cli_ingest(corpus, tmp_path / "mem.db", _ingest_env(tmp_path))

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Indexed 1 file" in result.stdout, combined
    assert "[rekoll] ingest_path skipped" in result.stderr, combined
    assert "credentials or private keys" in result.stderr, combined
