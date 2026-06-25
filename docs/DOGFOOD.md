# Dogfooding — Rekoll on Rekoll

We use Rekoll as the memory layer for Rekoll's own development, starting at P0.
Eating our own dog food is how we find the rough edges before users do.

## How

```bash
# (re)index the repo into a local Rekoll store (.rekoll/rekoll.db — gitignored)
python scripts/dogfood.py ingest

# recall project context before working on something
python scripts/dogfood.py recall "how does the storage adapter enforce scope isolation?"

python scripts/dogfood.py status
```

The store lives at `.rekoll/rekoll.db`. It is a **rebuildable index**, not a
source of truth — it is gitignored, and `ingest` is idempotent (content-addressed,
ADR-0006), so re-running never duplicates. Source of truth stays in the files +
git, exactly as the design promises.

## Honest limits at P0

- Recall uses the **non-semantic `StubEmbedder`** (word overlap, not meaning).
  So today it behaves like a smarter grep. **P1** swaps in a real local embedding
  model and hybrid (vector + keyword) ranking — then re-run `ingest` and recall
  becomes semantic, with zero changes to the store contract.
- There is no auto-capture yet. Once the `rekoll-mcp` Node server lands
  (ADR-0008), a coding agent recalls automatically and captures session decisions
  without this script.

## Why this matters now

Dogfooding from P0 means every later phase (real embeddings, the firewall, the
learning loop, the importer) gets validated against a real, growing corpus — this
repo — instead of a toy. When recall is weak, we feel it; when it's good, we know.
