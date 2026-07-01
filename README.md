# Rekoll

**Injection-hardened, storage-agnostic, private memory for AI agents.**
Give your agent durable memory of a whole codebase + database — that it can't be tricked into trusting, and that never leaves your infrastructure.

> **Status: pre-alpha, but usable.** Working today: the `Memory` facade, local
> semantic + keyword (hybrid) search with cross-encoder reranking, the injection
> firewall, a bring-your-own-database adapter contract, and a benchmark gate.
> Upcoming: the learning loop, more DB backends, and an MCP server — see
> [docs/DESIGN.md](docs/DESIGN.md). Not yet on PyPI.

---

## What makes Rekoll different

It aims to be the first agent-memory layer that is *all five at once*:

- **Storage-agnostic** — one adapter contract; SQLite by default, point it at Postgres / Supabase / your own DB.
- **Private by default** — local store, local embeddings, no telemetry; your data never leaves your machine.
- **Hybrid** — fast local recall now, with an *optional* learning loop later (never on the read path).
- **Injection-hardened** — memory-poisoning defenses on by default (the gap no major memory library fills).
- **Human-legible** — content is verbatim and auditable, never an opaque blob.

## How you'll use it (three doors, one engine)

1. **MCP server** (the vibe-coder default) — one command in Claude Code / Cursor / Windsurf, **no Python and no API key required**. *(Coming — the front door is Node/`npx` so non-technical users never touch Python.)*
2. **Python SDK** — `pip install rekoll` → `from rekoll import Memory`. *(High-level facade lands in a later phase; the foundation pieces are importable today.)*
3. **Self-host service** — one container pointed at your own database.

**Do you need an AI key?** No — saving and searching memory uses a local model, no key, no internet, free. Only the *optional* learning loop calls an LLM, and you can bring any model (OpenAI, Claude, Gemini, local Ollama, …) or run it locally.

## Quickstart

```bash
pip install rekoll                  # core: local, private, no API key
pip install "rekoll[embeddings]"    # + real local semantic search & reranking
```

```python
from rekoll import Memory

mem = Memory(project="myapp")               # local SQLite, firewall on, zero config
mem.remember("we chose Postgres over BigQuery for cost")
mem.remember("the deploy runs on a Hostinger VPS")

print(mem.recall("why postgres?").texts()[0])          # the right memory, by meaning
print(mem.recall("where does it deploy?").context())   # LLM-ready, safe data envelope
```

Reads need **no API key and call no LLM** — everything stays on your machine.

### Bring your own AI (optional)

If you *want* cloud AI, plug in any provider's key — explicitly, with zero new
dependencies. The no-key local default never changes, and cloud is opt-in only:
the default path never reads a key or opens a socket (CI-gated).

```python
mem = Memory(embedder="openai:text-embedding-3-small")     # key from OPENAI_API_KEY

from rekoll.providers import OpenAICompatibleConsolidator  # merge memories with YOUR LLM
mem.consolidate(query="database decisions",
                consolidator=OpenAICompatibleConsolidator("gpt-4o-mini"))
```

OpenAI, DeepSeek, Qwen, Mistral, Gemini, Voyage (the embeddings answer for
Claude users), Ollama / LM Studio, any OpenAI-compatible `base_url`, … — see
[docs/PROVIDERS.md](docs/PROVIDERS.md). Consolidation output stays auditable:
firewall-screened, provenance-linked to its sources, trust capped at the
minimum of what went in — an LLM can never promote its own words.

### Use it in your own project

Until it's on PyPI, install from git or a local clone — **don't copy the source in**:

```bash
pip install "git+https://github.com/ryankyleocampo-github/rekoll"   # once published
pip install -e "/path/to/rekoll[embeddings]"                    # from a local clone
```

Then `from rekoll import Memory` anywhere. Add `.rekoll/` to your `.gitignore` (the
store is a rebuildable index). Index a whole repo with `mem.ingest_path(".")`, or
point it at your own database later via `Memory(backend=...)` (Postgres/Supabase
adapters land in a later phase).

### Develop Rekoll itself

```bash
git clone https://github.com/ryankyleocampo-github/rekoll && cd rekoll
python -m venv .venv && . .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[dev,embeddings]"
pytest
```

## Docs & policies

- [Design document](docs/DESIGN.md) · [Architecture Decision Records](docs/adr/)
- [Security policy](SECURITY.md) · [Non-goals](NON_GOALS.md) · [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE) © Rekreate Digital. You own and are responsible for whatever data you store with Rekoll.
