# ADR-0037 — Memory + index: legible files stay the truth; Rekoll is the tracked retrieval + safety layer over them

**Status:** Proposed (design accepted for planning; owner decisions listed in §10 — NOTHING in this ADR is implemented) · **Date:** 2026-07-24 · **Extends:** ADR-0006 (content-addressed ids), ADR-0016 (ingest trust default), ADR-0023 (trust-aware upsert), ADR-0025 (tombstones/supersession), ADR-0035 (board — §5 `set_status`, §6 no-discovery posture), ADR-0036 (opt-in wizard) · **Interacts with:** ADR-0002 (provenance foundational), ADR-0004 (frozen kinds), ADR-0013 (envelope byte-identity), ADR-0017/0034 (directive vouch + standing channel), ADR-0031 (door parity), ADR-0033 (warn-loudly posture)

## Context

The first external adopter (field report,
`docs/research/field-report-2026-07-24-first-external-onboarding.md`; issue #75)
hit a workflow failure no test could: their repo already had a harness-owned
memory layer — Claude Code auto-memory files, loaded into context at every
session start by machinery Rekoll does not control. To get both guaranteed
visibility (the files) and searchability (Rekoll), they wrote every fact
**twice** — once to the files, once via `rekoll remember`. Two stores of truth
drift, and drift is the disease Rekoll exists to cure. Their hand-rolled fix —
write facts to the files once, `rekoll ingest` the directory, stop parallel
remembers — is this feature done manually, which is the strongest adoption
signal a design can get.

"Convert everything into Rekoll" is the wrong answer: the existing files are
loaded by the harness at session start, so conversion blinds the agent exactly
where memory matters most, and reads as lock-in in precisely the repos most
likely to adopt.

The model this ADR commits to: **truth lives in the existing legible files;
Rekoll is the retrieval + safety layer over that truth.** A database and its
search index are not two databases. This is DESIGN §L1's verbatim/human-legible
pillar taken seriously in a repo that already has files.

What already exists makes this cheap:

- Provenance persists per record **today**: `Provenance.source_file` /
  `chunk_index` (`model.py`) round-trip through the SQLite adapter
  (`prov_source_file` / `prov_chunk_index`). Recall just never renders them —
  the human line shows only `(kind | trust: tier | id: rk_…)` (`cli.py`,
  `_cmd_recall`). Surfacing a file pointer is rendering, not storage.
- Ingest is content-addressed and idempotent (ADR-0006, amended by ADR-0026):
  re-ingesting unchanged content is a no-op by construction. "Tracked re-ingest
  on change" is the natural extension, not new machinery.
- Trust is monotonic on identical content (ADR-0023) and bulk ingest defaults
  to UNVERIFIED with vouching as an explicit per-call act (ADR-0016) — the
  exact ceremony a tracked source needs, minus persistence of the vouch.
- The board wave already built the supersession verb (`set_status`,
  ADR-0035 §5) and rejected every discovery mechanism for security reasons
  (ADR-0035 §6) — both rulings are load-bearing here.

## Decision

Three capabilities, one model, **zero daemons**: (1) **tracked sources** — an
operator adopts a legible file/directory once, with an explicit trust vouch,
and explicit commands re-ingest it when its content hash changes; (2)
**write-through remember** — `remember --to <file>` appends legible markdown to
the tracked file and indexes it through the same ingest path, minting ONE
record, not two; (3) **provenance pointers on recall** — every hit that came
from a file says so, so corrections are made where truth lives.

### 1. The native/file line, drawn once

**Files are truth for knowledge; ceremonies stay native.** Tracked-source
ingestion writes `Kind.RAW_FACT` only — exactly what `ingest_path` writes
today. It can NEVER mint a `directive`, set a board tag, or resolve anything,
at any vouched tier, for the same reason MCP's `WRITABLE_KINDS` excludes
directives (ADR-0008): content that arrives through an unceremonied channel
must not be able to promote itself into instructions. Directives keep the
ADR-0017 vouch gate and the ADR-0034 standing channel; board membership and
resolve keep their floors and lifecycle (ADR-0035); observations are derived
(consolidator-owned); episodes are session events. The docs sentence: *if a
human could read it in a file, the file is the truth and Rekoll indexes it; if
it needs a ceremony a file can't carry — a vouch, a floor, a resolve, a proof
count — it lives in Rekoll.* Where no legible layer exists, `.rekoll` IS the
layer (today's behavior, unchanged) — dual-store is the fallback, not the
pitch.

### 2. The tracked-source registry — in the store, never in the repo

A new bounded table `tracked_sources` in the operator's store (`.rekoll/` —
gitignored by `init` since day one), behind OPTIONAL storage-adapter methods
(ADR-0005 style: base raises `UnsupportedCapabilityError`; conformance checks
gate whoever implements them). One row per adopted source, per scope, flat
scalars only (ADR-0001):

| column | meaning |
|---|---|
| `scope_key` | same tenancy primitive as everything else (ADR-0003) |
| `path` | operator-resolved at adopt time: relative to the store's parent for in-project sources (survives a repo move), absolute for outside-project sources (e.g. the Claude auto-memory dir) |
| `trust_tier` | the vouched tier (§3) |
| `content_hash` | sha256 of the source content at last sync — the staleness oracle |
| `last_run_id` | the `ingest_run_id` of the last sync, so stale records are findable (§5) |
| `adopted_at` / `last_synced_at` | stored ISO-8601, verbatim (board discipline) |

**The registry is operator-only data and must never live in the working
tree.** A checked-in registry would let a hostile clone pre-vouch its own paths
at high trust — the same repo-controlled redirect ADR-0035 §6 rejected for
store paths, applied to trust. Adoption input comes from the operator (a prompt
answer or a CLI argument), never from data found in the repo. Consequence:
teammates each adopt on their own machine — consent is per-operator, which is
the correct posture, and the docs say so rather than hiding it.

Doors: the SDK (`Memory.sources()` / `.adopt()` / `.sync()` naming is a lane
detail) and the CLI (`rekoll sources add | list | rm | sync` — the verb is
free; current CLI has nine subcommands, none named `sources`) expose the
registry. **MCP exposes none of it in v1** (§7). The three-doors parity suite
pins only surfaces all three doors share, so this is additive, not a parity
break.

### 3. Trust mapping — vouch the source once, loudly

- Adopt-time: the operator picks the tier, **default `TRUSTED_SOURCE`**.
  `OWNER` is offered but takes a second explicit confirmation; `UNVERIFIED` is
  offered for vendored/third-party files (a vendored AGENTS.md you didn't
  write). This is ADR-0016's "vouching for a tree is a per-call, explicit act"
  made persistent — with the persistence itself an explicit act.
- Every later sync stamps that source's NEW content at the SAME vouched tier.
  **Vouching a source is vouching its future edits** — that is the point of
  adoption, and it is why the default is `TRUSTED_SOURCE`, not `OWNER`, and
  why the ADR says it in bold instead of hoping nobody notices. `sources sync`
  prints, per changed file, the path, the tier being stamped, and the chunk
  counts before ingesting — warn loudly, never block (ADR-0033 posture).
- ADR-0023 monotonicity is untouched: changed bytes are new records (new
  content hash → new id); identical bytes already stored at a HIGHER tier keep
  the incumbent (a re-ingest can never downgrade); identical bytes at a lower
  tier are the dedup no-op they always were. A tracked source at
  `TRUSTED_SOURCE` meets no floor that matters: it cannot mint directives or
  board tags at any tier (§1), so the highest-blast-radius surfaces are
  structurally out of reach of a file edit.

### 4. Consent-prompted detection — never silent, two distinct levels

Recognition is limited to a short, shipped list of conventions: in-project
`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `docs/adr/`; outside-project the
Claude auto-memory directory `~/.claude/projects/<slug>/memory/`. Detection
never ingests, never registers, never even reads file contents before a "yes"
— existence checks only. Automatic silent path discovery is a standing
security no-go (ADR-0035 §6), and this ADR keeps the posture: **nothing is
adopted that the operator did not name or confirm.**

- **Plain `rekoll init` is untouched, byte-identical** — no prompt, no
  advisory line. ADR-0036 pins plain init silent-and-zero-config by test, and
  it runs in CI/scripts where a prompt hangs a pipeline. Detection therefore
  lives in `rekoll init --wizard` (one adopt step after the three interview
  questions — an amendment to ADR-0036's "at most 3 questions" bound, §10 D5)
  and in the always-available explicit verb `rekoll sources add <path>`.
- **In-project prompt** (per detected convention, Enter = no):
  > Found `CLAUDE.md` in this project. Index it so recall can search it?
  > Rekoll will re-read it only when you run `rekoll sources sync` — never in
  > the background. \[y/N]
- **Outside-project prompt** names the exact absolute path and the boundary
  being crossed:
  > Found a Claude Code auto-memory folder for this project at
  > `C:\Users\you\.claude\projects\<slug>\memory\` — this is OUTSIDE your
  > project folder, in your user profile. Index it too? \[y/N]
- Tier prompt follows any "yes" (§3 wording: "Treat this file as a trusted
  source? Its content — including future edits by anyone — will be stored at
  this trust."), with `OWNER` behind its own confirmation.
- **"No" leaves zero bytes**: no registry entry, no recorded refusal, no
  store side effects. The wizard already guarantees declining saves nothing
  (ADR-0036); the adopt step inherits that. Re-offering happens only on the
  next explicit wizard or `sources add` run — a remembered refusal would
  itself be un-consented state.

### 5. Stale records — supersede, never delete; no daemons, explicit touchpoints

- **Touchpoints (all explicit):** initial ingest at adopt; `rekoll sources
  sync` re-hashes every registered source and re-ingests ONLY the changed ones
  (unchanged files cost one file read + one sha256 each; unchanged chunks
  inside a changed file are content-addressed no-ops); `rekoll status` /
  `doctor` report staleness (hash compare only — they never ingest);
  `recall` / `board` / MCP reads never sync anything. Zero background
  processes, ever.
- **Edited file:** after re-ingest, the records minted from the source's
  PREVIOUS sync (found via `prov_source_file` + the registry's `last_run_id`)
  whose content is absent from the new chunk set are marked
  `ACTIVE → SUPERSEDED` via `set_status` — the marking-only verb ADR-0035 §5
  built and ADR-0025 §7 classifies as the supersession path. Every byte stays
  in the store, auditable and `get`-able. Content that RETURNS to the file
  re-activates automatically: the content-addressed upsert rewrites status on
  the same id — the exact reopen semantics ADR-0035 §9 pinned, doing the right
  thing here for free.
- **Deleted (or unreadable) file:** sync supersedes all of that source's
  records, prints a loud warning, and KEEPS the registry entry flagged missing
  until the operator runs `sources rm` — a path that vanishes silently would
  make memories vanish silently. Never an error that blocks the rest of the
  sync.
- Trust is never touched by staleness — supersession is status, not tier;
  ADR-0023 stays intact.

### 6. Write-through remember — a scribe, not a second store

`rekoll remember --to <file>` (SDK: `remember(text, to=...)`; a configured
default target is a follow-up, not v1) appends legible markdown to a TRACKED
file and indexes it **through the tracked-source ingest path**. It mints NO
direct record.

**Why ADR-0006 alone does not collapse the two-record risk (analyzed, not
assumed):** the markdown chunker is heading-delimited (`chunk_markdown`) — a
bullet appended under an existing heading changes that whole section's bytes,
so the re-ingested chunk's content hash matches NEITHER the old chunk NOR the
remembered string. A naive "remember normally, also append, let content
addressing dedup it" ships three overlapping records. Content addressing only
collapses byte-identical content; the design must make the bytes identical.

So the appended format is **chunk-stable by construction**: each fact is its
own heading-bounded block —

```markdown
## 2026-07-24 — postgres-over-bigquery
We chose Postgres over BigQuery for cost.
```

— so the chunker emits it as one standalone section, and appending fact N+1
never rewrites the bytes of facts 1..N: on re-ingest, every earlier block is a
content-addressed no-op (here ADR-0006 earns its keep), and exactly one new
record appears, carrying file provenance (`source_file`, `chunk_index`) at the
file's vouched tier.

- The record's trust is the **file's vouched tier, not `remember`'s OWNER
  default** — truth lives in the file, so the file's ceremony governs. The
  command says so when the tiers differ ("stored at trusted_source — the tier
  you vouched for CLAUDE.md — not owner").
- `--to` targeting an UNTRACKED file offers adoption first (interactive TTY,
  the same `_stdin_is_interactive` oracle as the vouch gate and wizard) or
  fails with the one-line fix (`rekoll sources add <file>`) when
  non-interactive. It never silently adopts (§4).
- `--to` is `raw_fact`-only in v1. `--to` with `kind=directive` is refused
  with a pointer to the native flow (`remember --kind directive` +
  the vouch gate): a directive written to a file would be inert as a rule
  (§1 — files cannot mint directives), and storing rules two ways rebuilds
  the dual-store disease inside the cure. This refusal is API-surface
  honesty, not a user-data restriction — the warn-don't-block posture
  (ADR-0033) governs choices about the user's own data safety, not
  unsupported flag combinations that would silently do less than they say.

### 7. MCP — read-side only in v1

The MCP door gains provenance pointers in its recall payload (§8) and
**nothing else**: no `remember --to`, no adopt/vouch, no registry listing.

- **File-writing via MCP is an injection escalation, not a convenience.** MCP
  content transits a model (ADR-0008), which is why `WRITABLE_KINDS` excludes
  directives and write trust is capped below CURATED. But `remember --to
  CLAUDE.md` writes to a file the HARNESS loads as instructions at session
  start — a model-authored append there becomes instructions one level ABOVE
  Rekoll's own firewall, where no envelope, floor, or quarantine can reach it.
  That is a new blast radius pointed at the exact channel the whole trust
  model exists to protect. Not in v1; if ever, it is its own ADR with its own
  threat analysis.
- **Adoption via MCP would let a model mint trust** — the vouch is the
  operator's conscious act (ADR-0016/0017); a tool call cannot carry it, for
  the same reason `rekoll-mcp` refuses CURATED/OWNER write trust outright.

### 8. Provenance pointers on recall — rendering + payloads, envelope untouched

Every door's recall surface cites the source file when a hit has one
(`source_file` is nullable — `remember`ed records legitimately lack it):

- CLI human line: `(raw_fact | trust: trusted_source | id: rk_… | from:
  CLAUDE.md#4)` — file plus chunk index, omitted entirely when absent.
- Machine payloads (SDK result, CLI `--json`, MCP `recall`): a nullable
  `source` field per hit (`{"file": …, "chunk": …}` or null), byte-identical
  across the three doors, pinned by the parity suite and documented in
  `docs/MCP.md` (whose key set is docs-consistency-pinned — the
  implementation lane updates both together).
- **`ContextEnvelope.render()` is UNCHANGED in v1.** The envelope is a pure,
  byte-stable function of its inputs by contract (ADR-0013; ADR-0034 §4
  re-affirmed it for cache stability), and every byte added to the envelope is
  a per-read token cost the pointer does not earn — agents and humans correct
  files via the payload/CLI pointer; the envelope is for the model's evidence,
  not its errata workflow. Putting pointers IN the envelope is a conscious
  future change with its byte-identity tests re-pinned, or never.

The point of the pointer: a wrong memory is corrected **where truth lives** —
edit the file, `sources sync`, the old chunk supersedes, the new one lands.
Without the pointer the user "fixes" the index and the file re-poisons it on
the next sync.

### 9. Docs presentation — integrated first, dual-store fallback, importer opt-in

The docs lead with integrated mode ("already have CLAUDE.md / auto-memory?
Rekoll indexes it — your files stay the truth"), present today's `.rekoll`-only
behavior as the fallback where no legible layer exists, and keep `import` a
strictly opt-in migration verb (the planned mem0/Zep importer is proposal 5's
generalization; DESIGN §14's import block already promises read-only-on-
originals + idempotent + visibly tagged, which tracked adoption satisfies by
construction). This is the adopter's own priority order, and it is also the
anti-lock-in story: the conversion pitch is harmful in exactly the repos most
likely to adopt (issue #75). Accepted as proposed — no overrule.

## 10. Owner decisions (plain language, one recommendation each)

- **D1 — default trust tier at adopt time.** Options: `TRUSTED_SOURCE`
  (recommended) / `UNVERIFIED` / `OWNER`. Why: trusted-source makes the file
  searchable and useful immediately, but keeps the top "owner" badge as a
  separate deliberate step — because adopting a file means trusting whatever
  anyone writes into it later, the top tier should never be the silent
  default.
- **D2 — where the adopt offer appears.** Options: only `init --wizard` + the
  explicit `rekoll sources add` command (recommended) / also during plain
  `rekoll init`. Why: plain init is promised to ask nothing and print the
  same thing everywhere — scripts and CI depend on that promise, and a test
  pins it (ADR-0036).
- **D3 — consent wording for the outside-project (user-home) prompt.** Approve
  or edit the §4 wording. Why it matters: that folder is outside the project,
  so the prompt must say exactly where it is and that Rekoll will only ever
  re-read it on an explicit command — this is the line between "helpful" and
  "creepy".
- **D4 — MCP exposure in v1.** Options: read-only source pointers on recall
  (recommended) / also let MCP list tracked sources / also let MCP write
  files via `remember --to`. Why: anything an AI writes into CLAUDE.md gets
  read back as instructions by the NEXT session automatically — letting the
  MCP door write files hands a model a path around the firewall, so v1 keeps
  MCP read-only here (§7).
- **D5 — amending the wizard's question budget.** Options: wizard grows one
  adopt step after its 3 interview questions, each detected source one y/N
  (recommended) / adoption only via `rekoll sources add`, wizard untouched.
  Why: the wizard is the one interactive moment a new user already has;
  adding Enter-skippable consents there meets users where they are without
  breaking the "at most 3 interview questions" spirit (the rules interview
  stays 3).
- **D6 — `remember --to` an untracked file.** Options: offer adoption on the
  spot when a human is at the terminal, plain one-line error otherwise
  (recommended) / always error. Why: the interactive offer keeps the flow
  moving without ever silently adopting; scripts get a deterministic error
  with the fix in it.

## 11. Implementation lanes (cheapest and least-risky first) — NONE built in this PR

Order: **(c) → (a) → (b)**. (c) is rendering over already-persisted fields;
(a) is the schema + verbs; (b) depends on (a)'s registry and sync.

- **Lane (c) — provenance pointers on recall.** Surface:
  `src/rekoll/cli.py` (human line + `--json`), `src/rekoll/mcp_server.py`
  (payload key), `src/rekoll/memory.py` (payload builder), `docs/MCP.md`
  (key-pinned), `tests/test_three_doors_parity*` + docs-consistency
  updates. Tests: parity pins the new nullable `source` field identical at
  all three doors; a rendering test covers a `remember`ed record (null) vs an
  ingested one (file + chunk); envelope byte-identity tests run UNMODIFIED
  (the envelope must not change). ⚠ Scheduling: `cli.py` and `docs/MCP.md`
  are owned by in-flight parallel lanes — schedule (c) only after those
  merge.
- **Lane (a) — registry + detect-and-adopt + sync.** Surface:
  `src/rekoll/adapters/base.py` + `sqlite.py` (`tracked_sources` table,
  optional methods: register/list/remove/update-sync + a
  records-for-source read), `src/rekoll/conformance.py` (gates: scope
  isolation, tier persistence, supersede-not-delete via the diff),
  `src/rekoll/memory.py` (adopt/sync/sources API), `src/rekoll/cli.py`
  (`sources` verb + wizard adopt step), docs. Tests: conformance for the new
  methods; edited/deleted/restored-file lifecycle (supersede → reopen);
  consent tests (wizard "no" leaves zero bytes; plain init byte-identical
  pin stays green); a monotonicity re-check (tracked re-ingest can never
  downgrade a higher-trust incumbent).
- **Lane (b) — write-through `remember --to`.** Surface:
  `src/rekoll/memory.py`, `src/rekoll/cli.py`, docs. Tests: the
  chunk-stability property (append N facts → sync → facts 1..N-1 are
  content-addressed no-ops, exactly one new record); the no-two-records
  guarantee (no record with the remember-verb's provenance shape exists
  after a `--to` write); tier stamping (file's vouched tier, not OWNER);
  the directive refusal; untracked-target behavior both interactive and not.
- **Tripwire (proposed here, WRITTEN by the implementation wave or conductor
  — tests/ is out of this docs lane):** mirror the wrap() pin
  (`test_design_marks_wrap_as_planned_until_it_ships`,
  `tests/test_docs_consistency.py`): assert `rekoll.Memory` has no
  `sources`/`adopt`/`sync` attribute and the CLI registers no `sources`
  subcommand and `remember` takes no `--to`; while true, every DESIGN.md line
  mentioning tracked sources or `remember --to` must contain "planned";
  when it trips, the docs flip to present tense and the pin retires in the
  same PR.

## Consequences

- The adopter's hand-rolled workflow becomes the product: write facts once, in
  files the harness already loads; Rekoll indexes, searches, trust-labels, and
  points back at the file. No second store of truth, no drift.
- The store schema grows one bounded table behind optional adapter methods;
  third-party adapters that skip them keep working (ADR-0005 discipline).
- A vouched source is a standing trust grant over a file others can edit —
  stated in bold, defaulted below OWNER, printed at every sync. The
  highest-blast-radius surfaces (directives, board, resolve) are structurally
  unreachable from any file at any tier.
- Everything is explicit-command-driven; the zero-daemon posture and the
  cheap-read promise (recall never syncs, never hashes files) are untouched.
- The envelope's byte-identity contract is untouched in v1; provenance
  pointers ride the human line and machine payloads only.
- Until the lanes ship, DESIGN.md carries this as PLANNED (wrap() precedent,
  PR #61), with the §11 tripwire keeping the wording honest.

## Alternatives rejected

- **Convert-to-Rekoll as the default (importer-first docs).** Blinds the
  harness at session start and reads as lock-in — actively harmful in the
  repos most likely to adopt (issue #75's key constraint).
- **Remember-then-dedup for write-through** ("append AND store; content
  addressing will collapse them"). False in general: heading-delimited
  chunking means the file's bytes differ from the remembered string — verified
  against `chunk_markdown`. Scribe-then-ingest with a chunk-stable format is
  the design that makes ADR-0006 actually do the collapsing.
- **File watchers / background sync.** A daemon in a zero-daemon product;
  ADR-0035 §6's posture extended: nothing runs that the operator didn't just
  type.
- **Registry as a file in the repo** (`.rekoll.toml` of tracked paths). A
  hostile clone pre-vouches its own content at high trust — the ADR-0035 §6
  repo-controlled-redirect attack, aimed at trust instead of the store path.
- **Per-edit re-vouch ("Rekoll refuses to sync until you confirm each
  change").** A refusal gate on the user's own chosen source — the posture is
  warn loudly, never block (ADR-0033); loud per-file sync output is the
  honest middle.
- **Delete stale records on sync.** Destroys audit history and fights
  ADR-0025's tombstone/drop-order contract; supersession is reversible,
  auditable, and already has reopen semantics that do the right thing when
  content returns to a file (ADR-0035 §9).
- **Tracked sources may mint directives at OWNER vouch.** A file edit becoming
  a standing instruction with no ceremony is the ADR-0017 vulnerability
  reintroduced through the side door; rules keep their vouch, full stop.
