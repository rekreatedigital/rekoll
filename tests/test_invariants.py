"""CI gates for Rekoll's non-negotiable invariants (DESIGN §1, ADR-0007).

These lock in claims the docs make but nothing previously enforced:
 - reads/writes on the default path make NO outbound network call;
 - the default path pulls in NO network/LLM/heavy-ML library (zero required deps);
 - provenance + trust are required (NOT-NULL) at construction.
"""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from rekoll import Kind, MemoryRecord, Memory, Provenance, Scope, TrustTier
from rekoll.embedding import StubEmbedder

_LOOPBACK = {"127.0.0.1", "::1", "localhost", None, ""}


def _exercise(mem: Memory) -> None:
    mem.remember("we chose Postgres over BigQuery for cost")
    mem.ingest_text("# Deploy\n\nThe service deploys nightly to a VPS.", name="d.md")
    mem.recall("why postgres", k=3).texts()
    mem.context("deploy schedule", k=3)


def test_default_path_makes_no_outbound_network_call(monkeypatch):
    """ADR-0007: the privacy guarantee. A full write+read cycle on the default
    (stub, local SQLite) path must not open any non-loopback socket."""
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

    mem = Memory(path=":memory:", embedder=StubEmbedder(), reranker=None)
    _exercise(mem)
    mem.close()
    assert not offenders, f"network activity on the default path: {offenders}"


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
        "leaked += sorted(m for m in sys.modules if m.startswith('rekoll.providers'))\n"
        "assert not leaked, 'default path imported: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, (result.stdout + result.stderr)


def test_no_args_memory_never_touches_the_providers_package(tmp_path):
    """BYO-AI is OPT-IN (ADR-0015): a plain ``Memory()`` — auto embedder and
    all — must never import ``rekoll.providers``, so it can never construct a
    cloud provider, read a provider API-key env var, or open a socket toward
    one. (Run in a clean subprocess; cwd=tmp so ./.rekoll lands in a sandbox.)"""
    code = (
        "import sys\n"
        "from rekoll import Memory\n"
        "m = Memory()\n"
        "m.remember('hello world fact')\n"
        "m.recall('hello', k=2).texts()\n"
        "m.close()\n"
        "leaked = sorted(m for m in sys.modules if m.startswith('rekoll.providers'))\n"
        "assert not leaked, 'no-args Memory() imported: ' + repr(leaked)\n"
        # The HTTP-lib check is only meaningful without the [embeddings] extra:
        # fastembed is the sanctioned LOCAL embedder and legitimately imports
        # requests/httpx for its one-time model download. rekoll.providers
        # staying un-imported is the invariant; these libs are a proxy for it.
        "if 'fastembed' not in sys.modules:\n"
        "    banned = {'anthropic', 'openai', 'httpx', 'requests', 'urllib3'}\n"
        "    leaked = sorted(banned & set(sys.modules))\n"
        "    assert not leaked, 'no-args Memory() imported: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path
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
