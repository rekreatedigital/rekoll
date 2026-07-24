"""Doc-vs-code snapshots — the drift tripwires behind the Lane-5 docs fixes.

DESIGN.md twice described enforcement machinery that didn't exist (a BLOCK
defense action; a 4+2 MCP tool surface with different names). These tests pin
the two surfaces that drifted, so the NEXT rename/addition fails CI and forces
the docs edit in the same PR:

- ``DefenseAction`` members (the screen's verdict vocabulary), and the
  DESIGN.md line that enumerates them;
- the shipped MCP tool names, and the docs/MCP.md table + DESIGN.md §8 /
  README lines that list them.

Deliberately NOT here: prose claims a regex can't check (those went through
the human docs pass). Keep this file to exact, enumerable surfaces.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from rekoll import mcp_server
from rekoll.firewall import DefenseAction

REPO = Path(__file__).resolve().parents[1]

# The pinned snapshots. Changing either surface is fine — but it is a
# DOCUMENTED surface, so update DESIGN.md (§3/§6 actions; §0/§8/§12 tools),
# docs/MCP.md, README.md, and these sets together.
PINNED_DEFENSE_ACTIONS = {"ALLOW", "REDACT", "QUARANTINE"}
PINNED_MCP_TOOLS = {"remember", "recall", "ingest_path", "forget", "status", "board"}


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _shipped_tool_names() -> set:
    """The ``@server.tool()``-decorated functions in ``build_server``, scraped
    from source so this runs without the optional ``mcp`` extra installed."""
    src = inspect.getsource(mcp_server)
    return set(re.findall(r"@server\.tool\(\)\s+async def (\w+)\(", src))


# -- DefenseAction: code snapshot + the DESIGN.md lines that enumerate it -------

def test_defense_action_members_are_pinned():
    assert set(DefenseAction.__members__) == PINNED_DEFENSE_ACTIONS


def test_design_defense_decision_enumeration_matches_code():
    design = _read("docs/DESIGN.md")
    matches = re.findall(r"DefenseDecision\(([^)]*)\)", design)
    assert matches, "DESIGN.md no longer enumerates DefenseDecision actions"
    for actions in matches:
        named = {a.strip().strip("`") for a in re.split(r"\\?\|", actions)}
        assert named == {a.value for a in DefenseAction}, (
            f"DESIGN.md says DefenseDecision({actions}) but the code's actions "
            f"are {sorted(a.value for a in DefenseAction)}"
        )
    # The §6 screen line: same vocabulary, upper-case spelling. No BLOCK — the
    # code never had one (external content quarantines, it is never dropped).
    assert "`ALLOW/REDACT/QUARANTINE`" in design
    assert re.search(r"ALLOW/REDACT/BLOCK", design) is None


# -- MCP tools: code snapshot + the docs that list them --------------------------

def test_shipped_mcp_tool_names_are_pinned():
    assert _shipped_tool_names() == PINNED_MCP_TOOLS


def test_mcp_md_tool_table_lists_exactly_the_shipped_tools():
    # Table rows whose FIRST cell is a backticked bare name — the config
    # table's first cells (`--path`, ...) don't match on purpose.
    rows = set(re.findall(r"^\|\s*`(\w+)`\s*\|", _read("docs/MCP.md"), re.MULTILINE))
    assert rows == _shipped_tool_names()


def test_design_and_readme_name_every_shipped_tool():
    design, readme = _read("docs/DESIGN.md"), _read("README.md")
    for tool in _shipped_tool_names():
        assert f"`{tool}`" in design, f"DESIGN.md never names shipped tool `{tool}`"
        assert f"`{tool}`" in readme, f"README.md never names shipped tool `{tool}`"
    # The stale §8 surface this file exists to keep dead: the old 4-core
    # vocabulary and the never-shipped `mem` CLI registration.
    assert "`memory_status`" not in design
    assert "mem mcp" not in design


# -- wrap(): planned, not shipped — DESIGN.md must not advertise it as live ------

def test_design_marks_wrap_as_planned_until_it_ships():
    """§8 sold `wrap(llm_client, scope=...)` in present tense while no wrap()
    exists anywhere in src/ (owner decision 2026-07: fix docs now, build
    later). Pin the honest wording: every DESIGN.md line that mentions the
    `wrap(...)` on-ramp must say "planned" on that same line, and the old
    present-tense sales phrasing stays dead. The hasattr guards trip the two
    obvious landing spots (module-level `rekoll.wrap` and a `Memory.wrap`
    method), forcing the docs flip to present tense in the same PR; a wrap()
    shipped anywhere else still needs this pin retired by hand."""
    import rekoll

    for spot in (rekoll, rekoll.Memory):
        assert not hasattr(spot, "wrap"), (
            "wrap() has shipped — reword DESIGN.md §8 (and its Planned block) "
            "to present tense and retire this pin in the same PR"
        )
    design = _read("docs/DESIGN.md")
    wrap_lines = [line for line in design.splitlines() if "wrap(" in line]
    assert wrap_lines, "DESIGN.md no longer mentions the wrap() on-ramp at all"
    for line in wrap_lines:
        assert "planned" in line.lower(), (
            f"DESIGN.md mentions wrap() without marking it planned: {line!r}"
        )
    assert "is the two-line on-ramp" not in design  # the pre-W5 shipped claim


# -- memory+index (ADR-0037): planned, not shipped — same discipline as wrap() ---

def test_design_marks_memory_plus_index_as_planned_until_it_ships():
    """ADR-0037 designs tracked sources / `remember --to` / provenance
    pointers without building any of it (issue #75). Same honesty pin as the
    wrap() one above: while none of the three obvious landing spots exist —
    a Memory sources/adopt/sync surface, a `sources` CLI subcommand, a
    `remember --to` flag — every DESIGN.md line naming the feature must say
    "planned" on that same line. Whichever lane ships a piece retires or
    reworks this pin in the same PR."""
    import argparse

    import rekoll
    from rekoll import cli

    for name in ("sources", "adopt", "sync"):
        for spot in (rekoll, rekoll.Memory):
            assert not hasattr(spot, name), (
                f"{spot.__name__}.{name} has shipped — flip DESIGN.md's "
                "memory+index wording to present tense and retire this pin "
                "in the same PR"
            )
    sub = next(a for a in cli._build_parser()._actions
               if isinstance(a, argparse._SubParsersAction))
    assert "sources" not in sub.choices, "the sources verb has shipped — retire this pin"
    remember_opts = {s for a in sub.choices["remember"]._actions
                     for s in a.option_strings}
    assert "--to" not in remember_opts, "remember --to has shipped — retire this pin"

    design = _read("docs/DESIGN.md")
    feature_lines = [line for line in design.splitlines()
                     if "tracked source" in line.lower()
                     or "tracked file source" in line.lower()
                     or "remember --to" in line.lower()]
    assert feature_lines, "DESIGN.md no longer mentions the memory+index feature at all"
    for line in feature_lines:
        assert "planned" in line.lower(), (
            f"DESIGN.md names the memory+index feature without marking it planned: {line!r}"
        )


# -- MCP tool RESULTS: the keys an agent reads, and the docs that list them -------
#
# The tool NAMES were pinned above; their RESULT payloads were not, and they
# drifted exactly as this file predicts a documented surface will. docs/MCP.md
# described recall as returning "a context block + record ids" while the code
# returned three keys (`count` was never documented), and issue #25 found that
# `mode` — the honest-degradation contract — crossed no door at all. ADR-0027
# then grew `ingest_path` a `filtered` key that the MCP door silently dropped.
#
# So: snapshot the payload keys against the code, and require docs/MCP.md to
# name each one. Adding a key to a tool result is fine — it is a DOCUMENTED,
# LLM-FACING surface, so the docs edit lands in the same PR.

PINNED_MCP_RECALL_KEYS = {
    "context", "directives", "ids", "mode", "count", "abstained", "top_vector_score",
}
PINNED_MCP_STATUS_KEYS = {
    "memories", "scope", "store", "write_trust", "writable_kinds",
    "embedder", "mode", "firewall", "version",
}
PINNED_MCP_INGEST_KEYS = {
    "files", "chunks", "skipped", "filtered",
    "secrets_skipped", "secrets_stored", "total",
}
# The board tool's payload (ADR-0035): the builder's CONSTANT key set plus the
# constant per-entry key set. Both are LLM-facing, documented surfaces.
PINNED_MCP_BOARD_KEYS = {"rules", "majors", "recent", "pending_open", "latest"}
PINNED_MCP_BOARD_ENTRY_KEYS = {"id", "kind", "trust", "created_at", "board", "text"}


def _live_payload_keys() -> tuple[set, set, set, set]:
    """``recall`` / ``status`` / ``board`` result keys, from the real tool
    bodies.

    The bodies are plain functions (that is why they live outside
    ``build_server``), so this needs no ``mcp`` extra — it runs on the default
    CI matrix, where doc drift would otherwise go unnoticed.
    """
    from rekoll import Memory, TrustTier
    from rekoll.embedding import StubEmbedder

    mem = Memory(path=":memory:", project="docs", embedder=StubEmbedder(),
                 reranker=None, default_trust=TrustTier.UNVERIFIED)
    try:
        mem.remember("we chose Postgres over BigQuery for cost")
        mem.remember("a curated board item", board="major",
                     trust=TrustTier.TRUSTED_SOURCE)
        config = mcp_server.ServerConfig(
            path=":memory:", tenant="default", project="docs", agent="default",
            trust=TrustTier.UNVERIFIED, root=REPO,
        )
        board = mcp_server._board(mem, config)
        assert board["majors"], "seed guarantees a curated entry to snapshot"
        return (
            set(mcp_server._recall(mem, "why postgres", 3)),
            set(mcp_server._status(mem, config)),
            set(board),
            set(board["majors"][0]),
        )
    finally:
        mem.close()


def test_mcp_tool_result_keys_are_pinned():
    recall_keys, status_keys, board_keys, board_entry_keys = _live_payload_keys()
    assert recall_keys == PINNED_MCP_RECALL_KEYS
    assert status_keys == PINNED_MCP_STATUS_KEYS
    assert set(mcp_server._INGEST_RESULT_KEYS) == PINNED_MCP_INGEST_KEYS
    assert board_keys == PINNED_MCP_BOARD_KEYS
    assert board_entry_keys == PINNED_MCP_BOARD_ENTRY_KEYS


def test_mcp_md_documents_every_recall_ingest_and_board_result_key():
    """docs/MCP.md enumerates these payloads explicitly, so every key an
    agent can read must appear there. (``status``'s keys are self-describing
    prose in the same table; its snapshot above is the tripwire.) The board's
    entry keys are included: an agent reads them off every ``majors``/``recent``
    element."""
    mcp_md = _read("docs/MCP.md")
    for key in (
        PINNED_MCP_RECALL_KEYS | PINNED_MCP_INGEST_KEYS
        | PINNED_MCP_BOARD_KEYS | PINNED_MCP_BOARD_ENTRY_KEYS
    ):
        assert f"`{key}`" in mcp_md, (
            f"docs/MCP.md never names the `{key}` key that an MCP tool returns — "
            "an agent reads this payload; document it in the same PR that adds it"
        )


def test_readme_recall_json_keys_match_the_cli_payload():
    """The README once advertised ``{context, ids, mode, count}`` for
    ``rekoll recall --json`` while the payload had grown to seven keys (the
    PR #62 review's drift finding). The CLI's ``_recall_payload`` is
    key-identical to the MCP ``recall`` tool by design (its docstring, plus the
    three-doors parity suite), and the MCP side is snapshotted live above — so
    pinning the README's enumerated set to ``PINNED_MCP_RECALL_KEYS`` closes
    the loop: the next added key fails here until the README names it too."""
    readme = _read("README.md")
    m = re.search(r"`rekoll recall --json` emits\s*\n?`\{([^}]*)\}`", readme)
    assert m, "README no longer enumerates the recall --json keys"
    advertised = {k.strip() for k in m.group(1).split(",")}
    assert advertised == PINNED_MCP_RECALL_KEYS, (
        f"README advertises recall --json keys {sorted(advertised)} but the "
        f"payload's keys are {sorted(PINNED_MCP_RECALL_KEYS)} — update the "
        "README sentence and this pin in the same PR"
    )


def test_mcp_md_explains_the_degraded_mode_an_agent_must_act_on():
    """`mode` is only useful if the doc says what the degraded value MEANS —
    the whole point of issue #25 is that a degraded ranking is otherwise
    indistinguishable from a healthy one."""
    mcp_md = _read("docs/MCP.md")
    assert "lexical-only: embedder mismatch" in mcp_md
    assert "ADR-0024" in mcp_md
