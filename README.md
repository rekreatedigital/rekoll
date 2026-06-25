# Rekoll

**Injection-hardened, storage-agnostic, private memory for AI agents.**
Give your agent durable memory of a whole codebase + database — that it can't be tricked into trusting, and that never leaves your infrastructure.

> **Status: pre-alpha (P0 — foundation).** The storage spine, the memory-record
> model, and the bring-your-own-database adapter contract are implemented and
> tested. Retrieval, the injection firewall, and the learning loop are upcoming
> phases — see [docs/DESIGN.md](docs/DESIGN.md) for the full plan. Not yet on PyPI.

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

## Try the foundation today

```bash
git clone https://github.com/rekreatedigital/rekoll
cd rekoll
python -m venv .venv && . .venv/Scripts/activate   # (Windows) or: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

```python
from rekoll import MemoryRecord, Scope, Provenance, Kind, TrustTier, StubEmbedder
from rekoll.adapters.sqlite import SQLiteAdapter

emb = StubEmbedder()
db = SQLiteAdapter("memory.db")            # or get_adapter("sqlite", path="memory.db")
scope = Scope(tenant="me", project="app", agent="assistant")

rec = MemoryRecord.create(
    scope=scope, kind=Kind.RAW_FACT, content="We chose Postgres over BigQuery for cost.",
    provenance=Provenance(source_uri="decision://2026-06-23"), trust_tier=TrustTier.OWNER,
).with_embedding(emb.embed(["We chose Postgres over BigQuery for cost."])[0], name="stub-hash", dim=emb.dim)

db.add(records=[rec])
# NOTE: StubEmbedder is a non-semantic placeholder for the foundation — it matches
# on shared words, not meaning. Real local (semantic) embeddings arrive in P1.
hits = db.vector_query(scope=scope, embedding=emb.embed(["Postgres BigQuery cost"])[0], k=3)
print(hits.hits[0].record.content)
```

## Docs & policies

- [Design document](docs/DESIGN.md) · [Architecture Decision Records](docs/adr/)
- [Security policy](SECURITY.md) · [Non-goals](NON_GOALS.md) · [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE) © Rekreate Digital. You own and are responsible for whatever data you store with Rekoll.
