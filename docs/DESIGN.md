# Rekoll — Design Document

> **Status:** Draft for review · **License (planned):** MIT · **Core language:** Python (MCP + REST as the language-neutral boundary; TS/JS client to follow)
> **Tagline:** The injection-hardened, storage-agnostic memory layer that lets your agent understand your whole codebase and database — and never forget — without your data ever leaving your infrastructure.

**Naming & namespace (decided 2026-06-23):** Brand + package = **Rekoll** — a stylized "recall" (the core function) that fits the Rekreate family. Verified available: **PyPI `rekoll` ✅**, **npm `rekoll` ✅**, **GitHub org `rekoll` ✅** (only ★0 abandoned repos exist). Ships as the bare name: `pip install rekoll`. **Domain:** `rekoll.com` was registered 2026-06-17 (not available); `rekoll.dev` / `.io` / `.ai` are open — leading candidate **`rekoll.dev`** (fits a dev tool), with `getrekoll.com` / `rekollmemory.com` as `.com` fallbacks. Not purchased yet (project is private/pre-publication). Chosen over earlier picks **Engram** (domain gone) and **Kodex** (bare name taken on PyPI/npm by KIProtect's privacy tool + an internal collision with the existing Kodex team-workspace). "for now" — revisitable; renaming is a one-pass find/replace.

---

## 0. Implementation status (as of 2026-06-26)

This document describes the **target architecture**; several sections below use the
present tense for capabilities that are planned, not yet shipped. Current reality:

**Shipped & tested (P0–P2 + the `Memory` facade):**
- Record model with NOT-NULL provenance/trust; three physical tables; flat-scalar
  metadata in bounded child tables (ADR-0001/0002/0004).
- `StorageAdapter` contract + reference SQLite adapter + importable conformance
  suite; per-scope embedder-identity record; content-addressed ids (ADR-0005/0006).
- Local embeddings (fastembed, optional extra) with a stub fallback; structure-aware
  chunking (markdown headings, Python AST); hybrid vector+BM25 retrieval with RRF
  (k=60) and optional cross-encoder reranking (ADR-0009/0010/0012).
- Injection firewall: ingest screen (secret redaction; NFKC + category-based
  invisible-char stripping; homoglyph-folded marker detection) and the read-time
  data-vs-instructions envelope; quarantine-by-trust (ADR-0013).
- CI gates: storage conformance; a stub-embedder recall **smoke** fixture; and
  zero-network / zero-LLM / zero-dependency invariant tests.

**Planned — NOT yet implemented (described in present tense below):**
- The learned consolidation loop (L3) and legible graduation gate (L4). No LLM
  writer exists yet; reads are zero-LLM today simply because no LLM is wired in.
- The deterministic **trust × recency × proof multiplicative re-rank** (§2/§6.4/§7).
  Read-time ranking is currently RRF → optional rerank → quarantine exclusion, with
  **no trust/recency/proof weighting**. The structural defense (directives only from
  the trusted tier; quarantine never surfaces) IS shipped and does the load-bearing
  work. The earlier §6.4-vs-§7 contradiction (hard 1.0/0.7/0.4 weights vs a
  "~±20% cap") is **resolved** to a single story — a bounded multiplicative trust
  factor capped near ±20%, QUARANTINED excluded (ADR-0020) — still future work to
  implement.
- `sqlite-vec` acceleration (§5): vector search is currently an exact pure-Python
  cosine scan. The adapter contract is unchanged; only the index backend is pending.
- The RRF **interleave** alternative (§7); real **LongMemEval/LoCoMo** gates (only a
  keyword smoke fixture exists); the MCP server, REST API, DB-schema/row ingestion,
  and the TS client.

**Behavioral note:** the hard-fail embedder guard `guard_identity()` exists, but the
`Memory` facade currently **warns** on a model swap rather than hard-failing
(ADR-0014) — diverging from §10 P1's "hard-fails" wording. The authoritative
behavior is an open decision.

---

## 1. Overview

Rekoll is a pure-MIT, Python-core AI-agent memory layer for the build-your-own-agent / Agentic-as-a-Service market. Its job-to-be-done is concrete: **drop it into someone else's project — often by a vibe coder — and let their agent understand a huge codebase plus its database and never forget, without sacrificing retrieval quality and without any data leaving the user's infrastructure.**

Rekoll is the first memory layer to own all **five differentiator axes** at once:

- **(a) Storage-agnostic / bring-your-own-DB** — one `StorageAdapter` ABC.
- **(b) Privacy-first / local-capable** — local ONNX embeddings, embedded SQLite store, loopback-only serving as the *default, not a flag*.
- **(c) Hybrid recall + a batched off-read learning loop.**
- **(d) Injection-hardened by default** — the field-wide blind spot (OWASP ASI06 memory poisoning; MINJA >95% and AgentPoison >80% attack-success rates against undefended stores) that **no major OSS memory library defends.**
- **(e) Human-legible / git-auditable** — DB-as-truth → generated read-only markdown view → git transport.

Every decision is grounded in two **code-verified** reference systems (cloned and read on disk): **MemPalace** (verbatim-chunk + advisory-index store, clean backend ABC, conformance suite, local-default embeddings) and **Hindsight** (Postgres ETL with a reasons-carrying consolidation op-stream, zero-LLM RRF reads, deterministic 5-state trend, open auth/metering extension seams, ingest-time injection screen). Rekoll adapts their proven patterns and **engineers against their documented failures** (coarse chunking, auth default-allow, unbounded in-row JSONB, terminology/license drift, prose-only enforcement, retrieval-by-grep).

**Three non-negotiable invariants thread through the whole system:**
1. Provenance and trust are stamped at the ingestion boundary **from line one** and are **immutable to LLM output**.
2. **Reads never call an LLM.**
3. **Local / private / safe is the default path** with zero flags to flip.

---

## 2. Architecture

Rekoll is a **unidirectional pipeline** with two cross-cutting spines and a single language-neutral integration boundary. Data flows one direction only; every module depends strictly downward.

```
                ┌─────────────── INGESTION SOURCES (IngestionSource ABC) ───────────────┐
                │   code · docs · conversation · db-schema  (db-rows later, hardened)    │
                └───────────────────────────────┬───────────────────────────────────────┘
                                                 ▼
  INGEST PIPELINE:  chunk (structural) → [L5] firewall.screen() → content-address (sha256)
                    → provenance + trust_tier stamp → embed (local default)
                                                 ▼
                 ┌──────────── StorageAdapter ABC  (BYO-DB seam) ────────────┐
   WRITE FAN-OUT │  verbatim_records   │   observations   │   directives      │
                 └──────────┬──────────┴────────┬─────────┴──────────┬────────┘
                            │                    │                    │
        ┌───────────────────┘                    │                    │
        │  ASYNC LEARNING LOOP (the only LLM writer, off the read path) │
        │  read verbatim → [L3] consolidate → {creates,updates,deletes}+reason
        │  → PROPOSALS → [L4] legible graduation gate → active observations/directives
        ▼
  READ PATH (ZERO-LLM):  vector_query ∥ lexical_query → RRF (k=60) → local cross-encoder rerank
                         → trust × recency × proof boosts → [L5] data-vs-instructions envelope
                                                 ▼
                       AGENT-FACING SURFACE:  MCP server  +  REST  (+ TS client later)
```

**Cross-cutting spines** (own no storage; decorate every record and result):
- **Provenance / Trust spine** — set at ingest, immutable by LLMs, read by ranking, consolidation-eligibility, and graduation.
- **Injection Firewall** — two choke points (ingest screen + read screen); its `screen()` **output *is* the trust signal** the rest of the system reasons over.

The load-bearing security idea: because the firewall sets trust at the boundary and consolidation + graduation gate on trust, **a poisoned low-trust chunk physically cannot launder itself into a high-trust directive.** The agent never touches the stores — only the API boundary. The MCP/REST/SDK doors are thin (<300 LOC) delegates to a single `Engine`, so behavior is byte-identical whichever door you enter (the explicit fix for MemPalace's 3,952-line MCP server that re-implemented backend logic).

---

## 3. The Five Layers

| Layer | Purpose | Key design |
|---|---|---|
| **L1 — Verbatim Store** (bedrock) | Store the exact bytes a source produced, never paraphrased; the provenance root. No LLM ever writes here, so it can't be poisoned by model output. | Two-tier like MemPalace (verbatim docs + advisory, **non-gating** pointer index). `content_hash` (sha256) **is** the addressing key. Structural chunking per source kind (AST for code, headings for docs, turns for conversation, table/column for schema) — never coarse char-windows. |
| **L2 — Hybrid Retrieval** (zero-LLM, locked) | High-recall reads with no LLM call; deterministic & offline-capable. Verbatim store is the always-on **floor**; advisory indexes only re-rank. | Parallel vector + BM25 (k1=1.5, b=0.75) → **RRF k=60** → local ONNX cross-encoder rerank → multiplicative trust × recency × proof boosts (each capped ~±20%, **no clock-decay**). Query sanitized by L5 *before* embedding. |
| **L3 — Learned Consolidation** (only LLM writer) | Dedup/consolidate verbatim facts into observations (proof_count + trend), off the read path, producing only **proposals**. | LLM emits `{creates, updates, deletes}` with a **required `reason` + `source_fact_ids`** per op (≤1 update per id). Deterministic SQL semantic-dedup backstop runs *after* the LLM. Only trusted-tier facts are consolidation-eligible. |
| **L4 — Legible Proposal / Graduation Gate** | Make every learned write human-legible & git-auditable; make trust elevation an explicit reviewable step. | Proposals render as content-hash-idempotent, generated **read-only markdown diff** over git (pull-rebase, never force-push, never auto-commit to main). Enforcement is **schema-level** (status enum + FK + triggers), not prose. Tiered auto-graduation; QUARANTINED can never auto-graduate. |
| **L5 — Injection Firewall** (co-designed with provenance) | Defend memory poisoning (OWASP ASI06) by being the *source* of the trust signal, not a bolt-on filter. | Two deterministic, no-LLM choke points → `DefenseDecision(allow\|redact\|block\|quarantine)`. Ingest: secret/PII redaction (fingerprinted, never leaks raw) + NFKC-normalized injection-marker detector → sets/lowers trust. Read: query sanitization + data-vs-instructions envelope. Pluggable (trufflehog/detect-secrets/LLM-judge) with zero fork. |

---

## 4. Core Data Model

**One logical record shape, physically split across THREE tables** — because raw facts, observations, and directives have genuinely different lifecycles, cardinalities, and trust dynamics. (Hindsight's hardest-won lesson: it shipped one `memory_units` table + a `fact_type` discriminator and paid with 20+ migrations of churn, a literal `observations↔mental_models` rename migration, and a **256 MB in-row JSONB brick / SQLSTATE 54000**.)

**Logical record fields:**
- `id` — content-addressed: `sha256(scope ‖ source_uri ‖ content_hash)`, truncated → free idempotent re-ingestion.
- `human_id` — stable legible `MEM-NNNN` (the 2RD `AG-NNN` pattern), for the git-auditable view.
- `scope` — composite tenant/project/agent isolation key, on **every row and every query**.
- `kind` — `raw_fact | observation | directive | episode` (**logical discriminator only**).
- `content`, `content_hash`.
- `source_id` → FK to `ingestion_sources`.
- **Provenance block** — `source_uri`, `adapter_name + adapter_version`, `ingest_run_id`, `source_file`, `chunk_index`, `derived_from`.
- `trust_tier` — ordered IntEnum **OWNER=4 / CURATED=3 / TRUSTED_SOURCE=2 / UNVERIFIED=1 / QUARANTINED=0**, set at ingest by the firewall, **immutable by LLMs**.
- `embedding_ref` — vector key + embedder identity.
- `created_at / seen_at / valid_from / valid_until` — deterministic temporal validity, **no clock-decay**.
- `proof_count` (observations), `declared_transformations` (empty == byte-exact), `privacy_class`, `status` (`proposed | active | quarantined | superseded | invalidated`).

**Physical layout:** `verbatim_records` (append-mostly, immutable, `UNIQUE(scope, content_hash)`) · `observations` (mutable + proof_count + trend) with history in a **bounded child table** `observation_history` (write-time DELETE-oldest cap, never an in-row array) · `directives` (tiny cardinality, reviewed) · `record_links` (bounded, CHECK-constrained typed edges) · `proposals` (op, payload, `reason NOT NULL`, source_fact_ids, status).

**Hard rules:** no unbounded JSONB anywhere; provenance/trust columns `NOT NULL` and round-tripped losslessly (conformance-tested); the four-`kind` vocabulary is **frozen in an ADR + schema enums day one** (so Hindsight's terminology churn cannot recur).

---

## 5. Storage-Agnostic & BYO-LLM (the "plug into any stack" answer)

One **`StorageAdapter` ABC** modeled on MemPalace's `BaseBackend/BaseCollection`: kwargs-only `add/upsert/vector_query/lexical_query/get/delete/count` returning **typed result dataclasses** (never raw dicts), a documented **per-scope isolation contract** (normative, not convention), a **three-state embedder-identity guard** (`unknown / known_match / known_mismatch` + a `config_hash` over normalization/pooling/truncation — closing the same-name/different-config corruption gap MemPalace's name-only check leaves), and a `capabilities` frozenset.

Key refinement over MemPalace: a **required vector+metadata core** plus **optional lexical and relational capability mixins**, so `sqlite-vec` (vector + FTS5) and Postgres (all three) both conform **honestly** — core raises `UnsupportedCapabilityError` and retrieval takes a documented fallback rather than a backend silently lying about an unsupported op.

- **Discovery:** entry-point group `rekoll.adapters`; explicit `register()` wins; resolution `explicit-arg > config > env > default`.
- **Default:** local **SQLite + sqlite-vec + FTS5** single file (WAL, no daemon, no key, git-portable).
- **Shipped tiers:** SQLite (default) → Postgres/pgvector → Supabase → Qdrant/Chroma → BYO via entry-points + conformance suite.
- **BYO-LLM/embeddings:** standardize on the **OpenAI wire format** (`{base_url, api_key, model, dimensions?}`) so Ollama/vLLM/LM Studio/OpenRouter/Azure work with zero new code; LiteLLM optional, not the contract. Default embedder is **local ONNX/fastembed**, default LLM is **none** (reads never call an LLM).
- **Config shape:** the familiar Mem0 three-block `{store, embedder, llm}`, each `{provider, config}`.
- **Migrations:** versioned, **additive-by-default**, bounded child tables, never-reuse-a-dropped-name, idempotent, single-version/single-license — all enforced by **CI lints** (the antidote to Hindsight's 81-migration churn + license drift).

---

## 6. Security Model (the headline wedge)

Threat model targets **OWASP ASI06 memory poisoning** (MINJA >95%, AgentPoison >80% against undefended stores) with defense-in-depth whose spine is provenance + `trust_tier`. **Trust is assigned by the ingestion source** (a mandatory `default_trust_tier` on the source ABC), not inferred from content — so a db-row/email/web adapter is *structurally incapable* of minting a TRUSTED memory.

Eight cooperating layers:
1. **Data-vs-instructions envelope** — retrieved memories are never concatenated into the instruction region; returned in a typed envelope with a `directives` field populated **only from the TRUSTED tier** and an `evidence` field for everything else, with delimiter-spoofing neutralized (the explicit fix for Hindsight injecting recalled memory straight into a system message).
2. **Monotonic, never-auto-elevating trust** — a derived memory inherits `trust = MIN(parents)`; a poisoned low-trust chunk can't launder itself by being summarized.
3. **Ingest-time screen** — `ALLOW/REDACT/BLOCK` with fingerprint-not-leak previews + an NFKC-normalized injection-phrase detector (homoglyph/zero-width/bidi-aware); **default action on external content is QUARANTINE-not-drop** (auditable, no denial-of-ingest vector).
4. **Deterministic zero-LLM trust-aware re-rank** — a **bounded multiplicative trust factor capped near ±20%** (OWNER/CURATED at the top of the band, UNVERIFIED lightly penalized, QUARANTINED excluded outright), applied alongside recency/proof (ADR-0020). The cap is deliberate: the *structural* separation (directives only from the trusted tier; quarantine never surfaces) is the load-bearing defense, so the ranking factor only nudges an embedding-optimized AgentPoison trigger down toward evidence without crushing legitimate low-trust recall.
5. **SHA-256 content-addressing** for tamper-evidence + dedup; **optional signing of the TRUSTED tier** (an unsigned mutation auto-demotes to QUARANTINED).
6. **Optional local PII detection** (regex floor + opt-in local NER, never a remote API).
7. **Auth DEFAULT-ON** — the server **refuses to start bound to a non-loopback interface without an auth provider** (fatal startup error; the explicit inversion of Hindsight's `0.0.0.0` + allow-all default).
8. **Supply-chain integrity** — signed releases, hash-pinned deps, single canonical namespace, **named maintainers** (anti-MemPalace-malware-domain posture).

**The regex screen is a tripwire, not the wall:** the primary defense is structural — even an undetected injection lands as LOW-trust *evidence*, never reaching the instruction channel, and trust elevation to a directive only ever happens through a human-approved, git-auditable proposal.

---

## 7. Retrieval & Learning

**Retrieval — 100% zero-LLM (CI-enforced; a mocked-client test asserts `call_count==0`).** Fixed-arm parallel fetch (semantic cosine + BM25 Okapi k1=1.5/b=0.75, optional graph/temporal arms, each capped) → **RRF k=60** (interleave alternative shipped for the documented RRF average-down case) → local `ms-marco-MiniLM` cross-encoder rerank (CPU, no network, default-on with a rank-seeded passthrough fallback) → multiplicative `CE_norm × recency × temporal × proof × trust`, each a small factor capped near ±20% (trust included — the single resolved story, ADR-0020; QUARANTINED is excluded before ranking, not merely down-weighted). Verbatim per-rule store is the always-on floor; advisory indexes can never inject or hide a record. Chunking is one-record-per-rule, structure-aware (tree-sitter for code, headings for markdown), content-hash idempotent with parent/sibling FK links.

**Learning — batched, background, the only place an LLM writes.** Consolidation reads new raw facts + K nearest observations → strict `{creates, updates, deletes}` op-stream with a **required reason** per op (the audit trail that feeds the gate), transactional with adaptive batch-halving, tag-scoped, backstopped by a deterministic create-time **and** update-time SQL semantic-dedup. Three physical tiers: raw verbatim facts → consolidated observations (proof_count + algorithmic trend) → on-demand reflect answers.

**Freshness — deterministic density-ratio 5-state trend** (NEW / STRENGTHENING / STABLE / WEAKENING / STALE) from evidence timestamps only — no decay constant, no wall-clock decay, no LLM eyeballing. Feeds both ranking and the graduation gate (STALE auto-flags for review).

**Benchmarks — LongMemEval + LoCoMo as continuous CI gates** from the moment retrieval exists: a hard blocking deterministic R@k gate + a tracked end-to-end QA gate (LLM-judge, abstention-as-correct), always measured after consolidation drains, with a **sealed train/test split**, pinned judge + dataset hash + seeds, and a published per-category table — so comparison vs Mem0 (49%) / Zep (63.8%) is credible and reproducible.

---

## 8. Developer Experience — "one engine, three doors"

Progressive disclosure is the enforced law: zero-config local-private-LLM-free defaults; every powerful capability behind exactly **three uniform knobs** — `storage={provider, config}`, `model={base_url,...}` (only for optional learning), `learning={consolidate, reflect, freshness}` (off by default). A CI test asserts the zero-arg path needs no key and calls no LLM.

- **Door 1 — MCP server** (vibe-coder default in Claude Code/Cursor/Windsurf): a deliberately **small** surface — 4 core tools (`recall`, `remember`, `forget`, `memory_status`) + 2 conditional (`recall_schema`, `memory_review`) — rejecting MemPalace's 33-tool ontology. `mem init` (idempotent) detects the host, registers via the host's own CLI (`claude mcp add memory -- mem mcp`, never hand-editing JSON), mines the repo, introspects+ingests DB schema (prompted, read-only, metadata-only), and installs Stop + PreCompact auto-capture hooks through one cross-platform `mem capture` entrypoint (Windows-safe; auto-captures tagged `trust='observed'` so the firewall can quarantine).
- **Door 2 — one-line Python SDK:** `pip install rekoll` → `from rekoll import Memory; mem = Memory()` — zero-config, local, private, no key, no LLM on reads. `remember/recall/forget` mirror the MCP verbs 1:1 (async twins available); `scope` is the one tenancy primitive; results are plain dataclasses (`.text/.score/.scope/.provenance/.trust`); `wrap(llm_client, scope=...)` is the two-line on-ramp (recall-before, remember-after).
- **Door 3 — self-host service:** one container, BYO key, point at your Supabase/Postgres, REST + MCP-over-HTTP, per-tenant, local/private still the in-container default, auth **deny-by-default**.

A stable enumerated public contract (SDK verbs, MCP tool schemas, REST `/v1` routes, CLI, config precedence) is frozen under SemVer and snapshot-tested so accidental breaks fail CI.

---

## 9. Governance & Quality (contributor-grade, zero red flags)

A single **tight monorepo** (not Hindsight's 16-separately-licensed sub-packages that produced MIT/ISC drift) because the data model, `StorageAdapter` ABC, MCP tool schemas, REST OpenAPI, and TS client are **one coupled contract** that must move atomically — one version train, one root LICENSE.

- **Accountable real identity** — every maintainer in `MAINTAINERS.md` is attributable, mirrored by `CODEOWNERS`, with a named CoC contact (anti-MemPalace-pseudonym rule).
- **License hygiene** — exactly one root MIT `LICENSE` every manifest references, enforced by a **license-guard CI job** (Hindsight's control-plane `package.json` says ISC while root says MIT — verified on disk).
- **Public/internal boundary** — public = `__all__` + MCP/REST schemas + the ABCs; everything under `_internal/` has no stability promise; import-linter + public-surface snapshot test fail the build on leaks; SemVer 2.0 with default-bodied new ABC methods (additions are MINOR), 90-day deprecation.
- **Four CI-gated test layers:** (1) unit, no network/no key, 85% coverage; (2) an **importable storage-adapter conformance suite** (run identically by first- and third-party adapters); (3) a versioned **injection-attack corpus** with an **attack-success-rate regression gate (ASR may only go DOWN)**; (4) a benchmark-recall regression gate with a sealed split.
- **Supply chain** — releases only via GitHub Actions OIDC Trusted Publishing (no stored tokens) behind an approval gate, cosign keyless signing, SLSA provenance, fully hash-pinned deps, weekly Dependabot, pip-audit/osv-scanner gates.
- ADRs for every load-bearing decision; CONTRIBUTING uses DCO sign-off (not a CLA); Keep-a-Changelog with a Security heading. **"Zero red flags" is an explicit release-checklist item.**

---

## 10. Quality-First Phased Roadmap

*Not MVP-rushed. No phase "ships" without its definition-of-done (tests + benchmarks + docs) passing.*

| Phase | Goal | Definition of Done | Unblocks |
|---|---|---|---|
| **P0 — Foundation** | Stable substrate with provable isolation, identity, trust | Two backends (SQLite + one vector) pass the conformance suite; trust columns proven non-nullable & lossless; trust enum + promotion rules fixed in an ADR | Everything |
| **P1 — Hybrid Retrieval + Verbatim Store + Chunking** | High-recall zero-LLM retrieval; benchmarks in CI | Zero-LLM-on-read assertion (call_count==0); LongMemEval + LoCoMo R@k as a **hard** CI gate w/ sealed split; embedder-identity hard-fails on silent swap | P2, P3, P5/P7 |
| **P2 — Injection Firewall + Threat Tests** | First OSS memory layer with poisoning defenses ON by default | Measured **ASR drop** on the published corpus + ASR regression gate (only down); REDACT-default w/ fingerprinted audit; server refuses non-loopback without auth | Trustworthy P3/P4; tested privacy claims |
| **P3 — Learning/Consolidation + Freshness** | Memory learns without nondeterministic decay, off the read path | Verbatim tier always survives (consolidation additive); freshness reproducible w/ no wall-clock decay; SQL dedup catches hallucinated dup-create | P4 |
| **P4 — Legible Proposal/Graduation Gate** | Human-legible, reviewable memory; DB truth, markdown view, git transport | Markdown view regenerates **byte-identically** in CI; enforcement is DB constraints + generated-file checks; tiered auto-graduation works; QUARANTINED never auto-graduates | Auditable-memory trust; legibility differentiator operational |
| **P5 — DB-Schema Ingestion Source** | Ingest a DB's schema as the first non-filesystem source | Schema text screened by P2; re-ingest idempotent on schema hash; reuses the same source ABC unchanged | Core JTBD ("understand my codebase + DATABASE") |
| **P6 — TS/JS Client** | First-class TS client over the language-neutral boundary | Client types generated from the single schema source, CI-checked for drift; verb-for-verb parity; trust/provenance identical across languages | JS/TS agent adoption |
| **P7 — DB Rows + Live Sync** (hardened) | Row-level ingestion + sync — the hard, privacy-sensitive surface, deliberately late | Every row passes P2 screen before storage; re-sync idempotent (proven); P2+P0+P4 prerequisites present & tested | Full "understand my data" |
| **P8 — Optional Hosted Seam** ⏸ **PENDING** (deferred — not publishing yet) | A future hosted tier WITHOUT a fork; nothing gated now | Core passes all tests with zero extensions loaded (CI matrix); default tenant extension safe (loopback + single-tenant); non-loopback + allow-all still fires a startup error | Sustainable project; privacy-first OSS intact |

---

## 11. Key Decisions

1. **Three separate physical tables** (verbatim/observations/directives), `kind` as a logical discriminator — different lifecycles/cardinalities/trust; avoids Hindsight's single-table churn + JSONB brick.
2. **Provenance + trust stamped at ingest, immutable to LLMs, foundational** — can't be retrofitted; closes the ASI06/MINJA/AgentPoison gap no OSS lib defends.
3. **Reads never call an LLM** (CI invariant); the LLM only writes proposals, gated by a legible graduation step.
4. **Three ABCs** (StorageAdapter, IngestionSource, Extension) are the only core seams; local ONNX + SQLite + sqlite-vec default; required vector core + optional lexical/relational mixins.
5. **Content-addressed ids** + human `MEM-NNNN` + `UNIQUE(scope, content_hash)` — DB-enforced idempotence/uniqueness, not prose convention.
6. **Auth ON by default** — refuse non-loopback without an auth provider; bind 127.0.0.1 (anti-Hindsight).
7. **Single tight monorepo, one version train, one root MIT LICENSE**, license-guard + version-guard CI, real-identity maintainers.
8. **Injection ASR + benchmark recall are hard CI regression gates** with committed baselines and a sealed split.

---

## 12. Anti-Patterns We Engineer Against (with sources)

- Single discriminator table + unbounded in-row JSONB (Hindsight 256 MB brick / SQLSTATE 54000) → three tables + bounded child tables.
- Coarse char-window chunking that buries records (MemPalace) → structural one-record-per-rule chunking.
- Auth default allow-all on a public interface (Hindsight `0.0.0.0` + allow-all) → loopback default + fatal refusal without auth.
- Injecting recalled memory straight into a system message (Hindsight) → data-vs-instructions envelope, directives only from TRUSTED tier.
- License/terminology/version drift (Hindsight MIT-vs-ISC + rename migration) → one LICENSE + one version train + frozen vocabulary, CI-enforced.
- Prose-only enforcement + auto-commit to main (2RD) → DB constraints + triggers + reviewable git diff.
- Retrieval-by-grep / substring search (2RD Eli) → zero-LLM hybrid recall.
- 33-tool MCP ontology + 3,952-line server re-implementing backend logic (MemPalace) → 4+2 plain-English tools, <300-LOC delegate doors.
- Pseudonymous maintainer + look-alike malware domains (MemPalace) → real-identity maintainers, canonical channels, signed releases.
- LLM-assessed staleness / clock-decay (2RD) → deterministic density-ratio 5-state trend.
- Proof-count-based auto-promotion to trusted (the signal poisoning inflates) → trust only elevates via human-approved proposal.

---

## 13. Open Questions (need resolution before/within the phase noted)

1. **Index ownership** — does `StorageAdapter` own both vector and lexical indexes (unified-per-adapter) or can they split? Leaning unified-per-adapter w/ a capability flag. *(before P1 closes)*
2. **Default fusion** — RRF k=60 vs MemPalace's 0.6/0.4 weighted blend, pending a LongMemEval+LoCoMo head-to-head; may be profile-dependent.
3. **Exact trust-factor values** — the *shape* is settled (a bounded multiplicative factor capped near ±20%, QUARANTINED excluded, ADR-0020); the exact per-tier values within that band are still to be tuned empirically against the MINJA/AgentPoison reproduction so down-ranking defeats optimized triggers without crushing legitimate low-trust recall.
4. **Trust-score formula inputs/weighting** (source reputation, graduated-vs-raw, screen verdict, proof_count, trend) — settle jointly with the firewall; ADR before P0 closes.
5. **`episode` kind lifecycle** — thin table vs view over verbatim_records grouped by session.
6. **Scope granularity** — per-project / per-source / per-trust-tier? Affects isolation + DB-schema partitioning.
7. **Chunking ownership** — per-source (each adapter owns chunking) vs a core default; tree-sitter vs language-native parser stack for the day-1 language set.
8. **Graduation auto-approve policy** — which observation classes may bypass review below a blast-radius threshold, and how that interacts with the injection guarantee.
9. **Data-envelope delimiter-neutralization algorithm** — specify + prove against an envelope-escape fuzz corpus; pin the wire contract across MCP + REST (breaking-change-sensitive).
10. **TRUSTED-tier signing scheme** — HMAC vs asymmetric/minisign vs Sigstore-style; at-rest key management.
11. **Default local embedder** — general (bge-small) vs code-tuned; affects dimension, ONNX asset size, codebase recall.
12. **BYO-DB threat boundary** — when the user's DB is the backend, an attacker with DB write access bypasses ingest screening; document content_hash verification + signed-TRUSTED demotion as the detection layer; state in/out of scope explicitly.
13. **Name — RESOLVED to Rekoll** (bare package `rekoll`, verified free on PyPI + npm + GitHub org). Remaining: pick the domain (`rekoll.dev` recommended — `rekoll.com` is taken as of 2026-06-17); reserve typo variants; pick type-checker (mypy strict vs ty); confirm the LICENSE copyright holder string. "for now" — name is revisitable.

---

## 14. Pre-build considerations & locked additions (2026-06-23)

A pre-build review (migration, non-technical UX, BYO-AI reality, pre-mortem) added these. All three headline promises survived; each gained a precise rule.

**Adopting Rekoll when you already have memory — three doors, never a forced delete:**
- **Import once** via the pluggable `IngestionSource` readers (folder-of-markdown/CLAUDE.md, git-notes, a competitor export e.g. Mem0 JSON, a generic DB-table). Imports are **read-only on the originals**, **idempotent** (content-addressed, ADR-0006), keep the **original timestamps**, and carry a visible "imported from X" tag.
- **Coexist** by pointing Rekoll at the user's **existing** Postgres/Supabase (own namespaced schema beside their data).
- **Wrap, don't replace** an existing agent during a trial, with a one-flag cutover to "Rekoll is the single source of truth."
- Honest limit: content + dates + tags transfer faithfully; another tool's internal scores/links are **rebuilt** on import, not copied. Bulk imports run through the **same firewall** as live writes (a prime poisoning vector).

**Non-technical UX — the differentiator (ADR-0008):** a default-Python tool fails non-techies in 5 minutes (where both competitors stumble). The **front door is Node/`npx` MCP** (hides Python); errors are plain-English with one next action; QA'd on a clean Windows box.

**BYO-AI reality (ADR-0008):** save/search need **no AI and no key** (local, free, private). Only the *optional* learning loop calls an LLM (any provider via OpenAI-wire/LiteLLM). The **embedding slot is separate** from the chat model and defaults local — Anthropic/Groq sell no embeddings, so "use Claude for everything" auto-falls-back to free local, documented. Tool-calling is not assumed for weak/local models. One docs page: "What AI do I need and what does it cost?"

**Trust & survival (handle now):** **zero telemetry** as an architectural guarantee (ADR-0007); a private **security-disclosure** channel + threat model (SECURITY.md); single named **LICENSE** holder + a "you own/secure what you store" disclaimer; a **non-goals** list to decline scope creep in one link; **support via GitHub Discussions** (not a 24/7 Discord); **data-must-still-open-after-upgrade** (SemVer + git-auditable format); an **export** path (anti-lock-in); and a v1 **secret/PII write-path scrub** decision (basic scrub in v1).

**New design additions folded into the plan:** `IngestionSource` reader interface + 4-5 first-party readers; an export command; two-slot AI config; the `rekoll-mcp` Node wrapper; zero-config first run (no questions); a bundled small local embedder (~30MB); a learning-loop model pre-flight check; a `rekoll doctor` self-check; cost guardrails (daily cap) for cloud learning; Windows-first QA + code-signing.

---

*Grounded in code-verified reads of MemPalace (v3.4.1) and Hindsight, plus the 2RD-Automation memory layer. This document guides a long-term, quality-first build. **P0–P2 and the `Memory` facade are implemented and tested** (see §0 for the precise shipped-vs-planned split) — see the repo root, `docs/adr/`, and `tests/`.*
