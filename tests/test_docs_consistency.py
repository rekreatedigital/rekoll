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
PINNED_MCP_TOOLS = {"remember", "recall", "ingest_path", "forget", "status"}


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
    "context", "ids", "mode", "count", "abstained", "top_vector_score",
}
PINNED_MCP_STATUS_KEYS = {
    "memories", "scope", "store", "write_trust", "writable_kinds",
    "embedder", "mode", "firewall", "version",
}
PINNED_MCP_INGEST_KEYS = {
    "files", "chunks", "skipped", "filtered",
    "secrets_skipped", "secrets_stored", "total",
}


def _live_payload_keys() -> tuple[set, set]:
    """``recall`` / ``status`` result keys, from the real tool bodies.

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
        config = mcp_server.ServerConfig(
            path=":memory:", tenant="default", project="docs", agent="default",
            trust=TrustTier.UNVERIFIED, root=REPO,
        )
        return set(mcp_server._recall(mem, "why postgres", 3)), set(mcp_server._status(mem, config))
    finally:
        mem.close()


def test_mcp_tool_result_keys_are_pinned():
    recall_keys, status_keys = _live_payload_keys()
    assert recall_keys == PINNED_MCP_RECALL_KEYS
    assert status_keys == PINNED_MCP_STATUS_KEYS
    assert set(mcp_server._INGEST_RESULT_KEYS) == PINNED_MCP_INGEST_KEYS


def test_mcp_md_documents_every_recall_and_ingest_result_key():
    """docs/MCP.md enumerates these two payloads explicitly, so every key an
    agent can read must appear there. (``status``'s keys are self-describing
    prose in the same table; its snapshot above is the tripwire.)"""
    mcp_md = _read("docs/MCP.md")
    for key in PINNED_MCP_RECALL_KEYS | PINNED_MCP_INGEST_KEYS:
        assert f"`{key}`" in mcp_md, (
            f"docs/MCP.md never names the `{key}` key that an MCP tool returns — "
            "an agent reads this payload; document it in the same PR that adds it"
        )


def test_mcp_md_explains_the_degraded_mode_an_agent_must_act_on():
    """`mode` is only useful if the doc says what the degraded value MEANS —
    the whole point of issue #25 is that a degraded ranking is otherwise
    indistinguishable from a healthy one."""
    mcp_md = _read("docs/MCP.md")
    assert "lexical-only: embedder mismatch" in mcp_md
    assert "ADR-0024" in mcp_md
