"""CI gates for Rekoll's non-negotiable invariants (DESIGN §1, ADR-0007).

These lock in claims the docs make but nothing previously enforced:
 - reads/writes on the default path make NO outbound network call;
 - the default path pulls in NO network/LLM/heavy-ML library (zero required deps);
 - both hold for the ``rekoll`` CLI entry path too, not just the SDK;
 - provenance + trust are required (NOT-NULL) at construction.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from rekoll import Kind, MemoryRecord, Memory, Provenance, Scope, TrustTier
from rekoll.embedding import StubEmbedder

_LOOPBACK = {"127.0.0.1", "::1", "localhost", None, ""}
_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _env_pinned_to_this_checkout() -> dict:
    """Subprocess gates must exercise THIS checkout's rekoll, not whatever is
    pip-installed (an editable install can point at a different worktree)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _block_fastembed(monkeypatch) -> None:
    """Force the no-extra default path even on machines WITH the extra.

    A bare ``sys.modules['fastembed'] = None`` is not enough: submodules such as
    ``fastembed.rerank.cross_encoder`` already cached by an earlier test would
    still import fine. Blank out every cached fastembed name."""
    for name in [m for m in list(sys.modules) if m == "fastembed" or m.startswith("fastembed.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "fastembed", None)


def _exercise(mem: Memory) -> None:
    mem.remember("we chose Postgres over BigQuery for cost")
    mem.ingest_text("# Deploy\n\nThe service deploys nightly to a VPS.", name="d.md")
    mem.recall("why postgres", k=3).texts()
    mem.context("deploy schedule", k=3)


@pytest.fixture()
def network_guard(monkeypatch) -> list[str]:
    """Patch socket so any non-loopback connect/DNS both fails AND is recorded."""
    offenders: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _host(address: object) -> object:
        return address[0] if isinstance(address, tuple) else address

    def guard_connect(self, address):
        host = _host(address)
        if host not in _LOOPBACK:
            offenders.append(f"connect:{host}")
            raise AssertionError(f"outbound connect to {host!r} on the default path")
        return real_connect(self, address)

    def guard_connect_ex(self, address):
        host = _host(address)
        if host not in _LOOPBACK:
            offenders.append(f"connect_ex:{host}")
            raise AssertionError(f"outbound connect_ex to {host!r} on the default path")
        return real_connect_ex(self, address)

    def guard_getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            offenders.append(f"dns:{host}")
            raise AssertionError(f"DNS lookup for {host!r} on the default path")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guard_getaddrinfo)
    return offenders


def test_default_path_makes_no_outbound_network_call(network_guard):
    """ADR-0007: the privacy guarantee. A full write+read cycle on the default
    (stub, local SQLite) path must not open any non-loopback socket."""
    mem = Memory(path=":memory:", embedder=StubEmbedder(), reranker=None)
    _exercise(mem)
    mem.close()
    assert not network_guard, f"network activity on the default path: {network_guard}"


def test_cli_default_path_makes_no_outbound_network_call(
    network_guard, monkeypatch, tmp_path, capsys
):
    """The CLI is the onboarding front door; it must hold the same privacy bar.
    ``fastembed`` is blocked so the auto embedder AND reranker take their no-extra
    fallbacks even on machines that have the extra installed."""
    from rekoll.cli import main

    _block_fastembed(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    assert main(["init"]) == 0
    assert main(["remember", "we chose Postgres over BigQuery for cost"]) == 0
    assert main(["recall", "why postgres", "-k", "3"]) == 0
    assert main(["recall", "why postgres", "--context"]) == 0
    assert main(["status"]) == 0
    assert not network_guard, f"network activity on the CLI default path: {network_guard}"


def test_default_path_imports_no_network_or_llm_library():
    """Reads never call an LLM, and the core needs zero required deps. Run a clean
    subprocess so sys.modules reflects only what the default path actually loads."""
    code = (
        "import sys\n"
        "from rekoll import Memory\n"
        "from rekoll.embedding import StubEmbedder\n"
        "m = Memory(path=':memory:', embedder=StubEmbedder(), reranker=None)\n"
        "m.remember('hello world fact')\n"
        "m.recall('hello', k=2).texts()\n"
        "m.context('hello', k=2)\n"
        "banned = {'anthropic','openai','httpx','requests','urllib3','fastembed','torch','numpy','onnxruntime'}\n"
        "leaked = sorted(banned & set(sys.modules))\n"
        "assert not leaked, 'default path imported: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=_env_pinned_to_this_checkout(),
    )
    assert result.returncode == 0, (result.stdout + result.stderr)


def test_cli_default_path_imports_no_network_or_llm_library(tmp_path):
    """Same gate for the CLI: a remember/recall/status cycle through
    ``rekoll.cli.main`` must not pull in any network/LLM/heavy-ML module.
    ``fastembed`` is import-blocked to pin the no-extra default path."""
    db = tmp_path / "cli.db"
    code = (
        "import sys\n"
        "sys.modules['fastembed'] = None\n"
        "from rekoll.cli import main\n"
        f"db = {str(db)!r}\n"
        "assert main(['remember', 'hello world fact', '--path', db]) == 0\n"
        "assert main(['recall', 'hello', '-k', '2', '--path', db]) == 0\n"
        "assert main(['status', '--path', db]) == 0\n"
        "banned = {'anthropic','openai','httpx','requests','urllib3','fastembed','torch','numpy','onnxruntime'}\n"
        "leaked = sorted(m for m in banned if sys.modules.get(m) is not None)\n"
        "assert not leaked, 'CLI default path imported: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=_env_pinned_to_this_checkout(),
    )
    assert result.returncode == 0, (result.stdout + result.stderr)


def test_provenance_requires_source_uri():
    with pytest.raises(ValueError):
        Provenance(source_uri="")


def test_record_requires_trust_tier_and_nonempty_content():
    scope = Scope()
    # trust_tier is keyword-only and required — omitting it must fail loudly.
    with pytest.raises(TypeError):
        MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content="x",
            provenance=Provenance(source_uri="s://x"),
        )
    with pytest.raises(ValueError):
        MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content="",
            provenance=Provenance(source_uri="s://x"), trust_tier=TrustTier.OWNER,
        )
