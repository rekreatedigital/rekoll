# AGENTS.md — guidance for any AI assistant or contributor

> Tool-neutral contributor guide. Claude Code reads `CLAUDE.md` (a pointer to this
> file); Cursor/Windsurf/Copilot/others read their own — but **this file is the
> source of truth**. Using Rekoll as a *product* needs none of these files; this
> is only for *building* Rekoll.

Rekoll is an injection-hardened, storage-agnostic, **private** memory layer for AI
agents. This repo **dogfoods itself** — we use Rekoll's own memory while building it.

## Before you start work

```bash
python scripts/dogfood.py recall "<what you're about to work on>"
```

Re-run `python scripts/dogfood.py ingest` after notable changes. See
[docs/DOGFOOD.md](docs/DOGFOOD.md).

## Dev workflow

```bash
python -m venv .venv && . .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The **core has zero required runtime dependencies** (standard library only). Real
embeddings are an **optional extra** (`pip install -e ".[embeddings]"`) — keep it
that way; the library must import and the stub path must work with no extras.

## Invariants (do not violate)

- **Provenance + trust are NOT-NULL and never set by an LLM** (ADR-0002).
- **No unbounded JSON in storage** — flat scalars or bounded child tables (ADR-0001).
- **Reads never call an LLM**; local + private is the default path (ADR-0007).
- **Storage adapters** are keyword-only, return typed results, advertise only the
  capabilities they truly support, and MUST pass `rekoll.conformance.run_all` (ADR-0005).
- **Memory-kind vocabulary is frozen** (ADR-0004); content-addressed IDs keep
  ingestion idempotent (ADR-0006).
- Default embedder is **local** (no key, no data egress); the optional learning
  loop is the only LLM caller and is off by default (ADR-0008, ADR-0009).
- Any load-bearing decision gets an **ADR** under `docs/adr/`.
- Respect the [non-goals](NON_GOALS.md); security issues go private ([SECURITY.md](SECURITY.md)).

## Where things are

- Design + roadmap: [docs/DESIGN.md](docs/DESIGN.md) · Decisions: [docs/adr/](docs/adr/)
- Core package: `src/rekoll/` · Contract suite: `src/rekoll/conformance.py` · Tests: `tests/`

## Status

- **P0 (foundation)** — done & tested.
- **P1 (retrieval)** — in progress: real local embeddings (fastembed), structure-aware
  chunking, hybrid vector+BM25 retrieval (RRF). Benchmarks (LongMemEval/LoCoMo) next.
