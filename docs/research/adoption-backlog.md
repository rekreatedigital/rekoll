# Adoption backlog — ranked ideas from the hands-on competitor analysis

Companion to [competitor-analysis.md](competitor-analysis.md) (same date, same
hands-on evidence). Ranked by (value to Rekoll's wedge) × (fit with our invariants).
**Ideas only — zero code was or may be copied** (memvid and mem0 are Apache-2.0,
incompatible with naive copy-paste into MIT; mempalace/hindsight are MIT but the
rule stands).

Effort guesses: **S** ≈ a day, **M** ≈ a week, **L** ≈ multi-week.
"Threatens zero-key/zero-LLM default?" — if *yes*, the feature must be opt-in and
must never touch the default read/write path (CI invariants in
`tests/test_invariants.py` stay green without it).

---

## Ranked list

### 1. Low-token `wake_up()` / session-start context block — from mempalace
Their `wake-up` emits a ~300-token structured block (identity + essential story +
per-memory source attribution) meant to be pasted into an agent's context at session
start. It was the single most "I want this" UX moment of the whole exercise — and
also their biggest security hole, because it pipes raw memory (including our
injection string) into the instruction stream. **Rekoll's version is the same
feature emitted through the read envelope** — trusted-tier directives separated from
DATA-wrapped evidence — which turns their vulnerability into our demo.
**Effort:** S–M (retrieval + budgeting exist; this is selection + formatting).
**Threat to zero-key/zero-LLM:** none — deterministic selection, no LLM.

### 2. Editor/agent auto-save hooks — from mempalace
Claude Code / MCP hooks that capture session content automatically ("sessions expire
in 30 days without auto-save hooks wired" is their whole retention pitch). Memory
layers live or die on *capture friction*; Rekoll currently assumes explicit
`remember()`/`ingest_path()`. A `rekoll hook` (stdin JSON → stored, screened memory)
that plugs into Claude Code hooks + the planned MCP server would close the loop.
Every hooked write goes through the ingest screen — auto-capture is exactly where
poisoned content arrives from, so this pairs with, not against, the firewall.
**Effort:** M (hook CLI + docs; MCP server is already planned separately).
**Threat:** none — capture + screen, no LLM, no network.

### 3. Single portable-file export/import — from memvid
`.mv2`'s best property: the whole memory (content + indexes + metadata) is one file
you can copy, ship, or back up. Rekoll's default store is already one SQLite file,
so we are 80% there — the missing 20% is making it a *product feature*:
`mem.export("myapp.rekoll")` / `Memory.from_file(...)`, documented as "your memory
is a file you own", embeddings included so import needs no re-indexing (and no
model, i.e. works on a machine without the embeddings extra). Avoid their mistakes:
no tiers, no capacity gates, no telemetry in the file layer.
**Effort:** S–M (mostly contract + docs; vacuum/copy + integrity check).
**Threat:** none.

### 4. Openly published benchmark harness with per-question results — from all four
mempalace commits per-question result files and reproduce commands (credible even
while their headline number was debunked — reproducibility is what let reviewers
catch it); mem0 open-sourced its eval framework; memvid/hindsight publish claims of
varying verifiability. Rekoll already has `benchmarks/` and a CI benchmark gate:
extend it to at least one public dataset (LongMemEval-style retrieval recall), commit
per-question outputs, and document the honest methodology (held-out split, no
tuning on the test set, k disclosed — the exact sins the mempalace audit found).
Never publish a side-by-side table using competitors' self-reported numbers.
**Effort:** M–L (dataset harness + CI wiring + docs).
**Threat:** none (benchmark uses the local default path; any LLM-judged eval is
clearly separated and opt-in).

### 5. Three-verb API surface with an opt-in `reflect` — from hindsight
`retain / recall / reflect` is the cleanest API pitch of the four. Rekoll already has
the first two as `remember`/`recall`. A `reflect()` (synthesized answer over
retrieved memories) is the one high-value feature that inherently needs an LLM —
ship it later as the first consumer of the learning loop: bring-your-own-model,
never on the read path, output clearly marked as derived (never stored as fact
without provenance saying so). hindsight's 9 s reflect latency also says: keep it
explicitly async/optional, never in the recall hot path.
**Effort:** M (once the learning-loop plumbing exists).
**Threat:** **yes — LLM required.** Opt-in only, separate extra, invariants stay
green without it.

### 6. Deterministic temporal extraction ("when did this happen") — from hindsight/memvid
hindsight's standout trick: `occurred_start/occurred_end` extracted from content
("approved on 2026-06-15" → queryable date; "in 1843" → `When: 1843`). memvid has a
time index with as-of queries. Rekoll can get 80% of this with zero LLM: a
deterministic date-pattern extractor at ingest (ISO dates, month-name dates, years)
stored as indexed metadata, plus `recall(..., as_of=...)`/date-range filtering.
Keep it stdlib-only to protect the zero-dependency invariant (no `dateutil`).
**Effort:** M (extractor S; query-path filtering + adapter contract changes M).
**Threat:** none if regex/stdlib-based. An LLM-grade temporal normalizer ("last
Tuesday") is learning-loop material — opt-in later.

### 7. User/session/agent memory scoping ergonomics — from mem0
mem0's `user_id`/`agent_id`/`run_id` scoping is its stickiest ergonomic win —
multi-tenant memory in one argument. Rekoll's model already has `Scope`; the gap is
surfacing it as first-class, documented `Memory(...)`/`remember(...)`/`recall(...)`
parameters with filtering guarantees (and tests that scope isolation holds).
**Effort:** S–M (mostly API surface + tests over existing model fields).
**Threat:** none.

### 8. Timeline / as-of view — from memvid
Append-only "what did memory look like at time T" is cheap for us (records carry
provenance timestamps; SQLite makes `as_of` a WHERE clause) and it strengthens the
auditability story: "show me exactly what the agent knew when it made that call."
Pairs with #6. Full immutable-frame versioning (edits preserved as history) is L and
can wait.
**Effort:** S for query-level as-of; L for full versioned history.
**Threat:** none.

### 9. Agent-first onboarding — from mem0
mem0 lets an *agent* mint a working key and store its first memory in four commands,
no human dashboard involved. The insight to steal: our quickstart personas include
the agent itself. For Rekoll (no service, no key) this is even simpler — a
copy-pasteable "for AI agents setting up Rekoll" block in the README/MCP docs
(deterministic commands, no interactive prompts, machine-checkable success), so a
coding agent can self-serve the integration.
**Effort:** S (docs + one non-interactive init path).
**Threat:** none.

### 10. Atomic-fact splitting at write time — from mem0
Their extraction turned one preference sentence into three independently-retrievable
facts — genuinely better recall granularity. But it is inherently LLM-rewriting, the
exact thing our verbatim invariant exists to prevent. Adoptable form: an *opt-in*
learning-loop enrichment that ADDS derived atomic facts LINKED to the verbatim
original (provenance: `derived_from=<id>`), never replacing it, never on the write
path by default. The original stays the source of truth; derived facts are clearly
second-class.
**Effort:** M–L (learning-loop feature).
**Threat:** **yes — LLM.** Opt-in only, derived-not-destructive by contract.

---

## Anti-adoption list — weaknesses we must never import

These are observed behaviors (see the analysis doc for the evidence), each mapped to
the Rekoll rule that guards against it:

- **LLM-junk writes / silent data destruction.** hindsight returned
  `success=True, items_count=1` while discarding the fibonacci function, the SQL
  query, and the injection string; mem0 silently dropped the injection and stored a
  prose *description* of code instead of the code. → Rekoll rule: verbatim storage;
  no LLM on the write path; every screen decision (redact/quarantine) is recorded on
  the record — success means *stored and retrievable*, and anything else is an
  explicit, auditable status, never a silent no-op.
- **Benchmark over-claiming.** mempalace's 96.6% was a ChromaDB score with the
  palace adding nothing; its "100%" came from patching the failing test questions;
  memvid headlines "+35% SOTA / 1,372× throughput" that nothing shipped can
  reproduce. → Rekoll rule: publish only numbers reproducible from a committed
  harness with per-question outputs; disclose k/splits; never compare against
  others' self-reported numbers (also: never buy stars — adoption theater is now a
  documented, auditable failure mode).
- **Heavy infra for a "library".** hindsight: 215 packages, embedded Postgres,
  20–37 s boot, HF Hub on every start; mempalace: chromadb's 81-package train. →
  Rekoll rule: zero required dependencies is CI-gated; extras are opt-in and the
  core never imports them.
- **Silent failure modes in the default path.** mem0's shipped quickstart 400s on
  every add (provider changed a model; library broke with no release), while
  `search()` over the empty store returns calmly empty results — quickstart agents
  get amnesia with no error. → Rekoll rule: the default path depends on no external
  service that can drift; failures raise, and recall over an empty store is
  distinguishable from a store that swallowed writes.
- **Telemetry-by-default / phone-home in OSS.** memvid POSTs hashed machine IDs to
  memvid.com unless `MEMVID_TELEMETRY=0`; mem0 ships PostHog on by default. →
  Rekoll rule: zero-network default is CI-gated (`test_invariants.py`); telemetry, if
  ever, is opt-in.
- **Freemium gates inside the local SDK.** memvid's OSS file has `tier: free`, a
  50 MB cap, and monthly query quotas with upgrade URLs in the error table. →
  Rekoll rule: the open core has no tiers, no caps, no upsell paths in error
  messages.
- **Security theater by omission.** None of the four mentions prompt injection in
  its security docs while all four mishandle it. → Rekoll rule: SECURITY.md states
  the threat model explicitly, and the firewall has CI-gated tests proving the
  claimed behavior.
