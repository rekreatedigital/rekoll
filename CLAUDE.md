# Rekoll — Project Rules (for AI assistants & contributors)

Rekoll is an injection-hardened, storage-agnostic, **private** memory layer for AI
agents. This repo **dogfoods itself**: we use Rekoll's own memory while building it.

## Before you start work

This project uses Rekoll to remember itself. Recall relevant context first:

```bash
python scripts/dogfood.py recall "<what you're about to work on>"
```

(Re-run `python scripts/dogfood.py ingest` after notable changes. See
[docs/DOGFOOD.md](docs/DOGFOOD.md). Recall is word-overlap quality until P1 ships
real embeddings — treat it as a smart grep for now.)

## Dev workflow

```bash
python -m venv .venv && . .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Zero runtime dependencies (standard library only) — keep it that way unless a
dependency is clearly justified.

## Invariants (do not violate)

- **Provenance + trust are NOT-NULL and never set by an LLM** (ADR-0002).
- **No unbounded JSON in storage** — flat scalars or bounded child tables (ADR-0001).
- **Reads never call an LLM**; local + private is the default path (ADR-0007).
- **Storage adapters** are keyword-only, return typed results, and advertise only
  the capabilities they truly support; they MUST pass `rekoll.conformance.run_all`
  (ADR-0005).
- **Memory-kind vocabulary is frozen** (ADR-0004); content-addressed IDs keep
  ingestion idempotent (ADR-0006).
- Any load-bearing decision gets an **ADR** under `docs/adr/`.
- Respect the [non-goals](NON_GOALS.md); security issues go private ([SECURITY.md](SECURITY.md)).

## Where things are

- Design + roadmap: [docs/DESIGN.md](docs/DESIGN.md) · Decisions: [docs/adr/](docs/adr/)
- Core package: `src/rekoll/` · Contract suite: `src/rekoll/conformance.py` · Tests: `tests/`

## Status

**P0 (foundation) — done & tested.** Next: **P1** — hybrid retrieval + a real
local embedding model + LongMemEval/LoCoMo benchmarks wired into CI.
