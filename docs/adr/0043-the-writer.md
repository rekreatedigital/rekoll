# ADR-0043 — The writer: Rekoll writes the documents it already knows how to write

**Status:** Proposed · **Date:** 2026-07-28 · **Extends:** ADR-0037 (tracked sources + `remember --to` — the approved write primitives), ADR-0035 (board payload: bounded, trust-gated, byte-deterministic), ADR-0034 (standing-directive channel), ADR-0004 (frozen kinds), ADR-0006/0026 (content-addressed ids) · **Interacts with:** ADR-0007 (zero-LLM reads), ADR-0013 (envelope byte-identity), ADR-0015 (BYO-AI provider layer), ADR-0017 (directive vouch), ADR-0025 (tombstones/supersession), ADR-0033 (warn-don't-restrict), ADR-0036 (opt-in wizard) · **Paired with:** ADR-0042 (the writer is the decisions tier's transport) · **Evidence:** issues #75, #82, #87, #101 and the 2026-07-25 audit of the 2RD-Automation memory layer

## Context

Rekoll is a **librarian**: it stores, searches, and protects. The product goal is
a **writer**: plain-English documentation kept current in the repo, so that a
non-coder and an AI both understand the project — and, per ADR-0042, so that
memory travels between teammates in git.

The evidence that this is the missing half is unusually strong, because three
independent parties built it by hand rather than wait:

- **#101** (love-belle-dashboard, 2026-07-28): a real client repo hand-built a
  `CLAUDE.md` router, `docs/MASTER.md` (overwritten, not appended),
  `docs/BOARD.md` (a **150-line one-liner spine** plus ribs/overflow),
  `docs/sessions/`, `docs/adr/`, per-feature readmes — **with every fact written
  twice**, once to a doc and once via `rekoll remember`.
- **#87 / #82**: of 2,734 memories in a 12-hour session, 2,727 were code chunks
  and **7 were hand-written decisions — and all the recalled value was in the 7**.
- **The 2RD audit**: ~1.99M lines of markdown, ~48% of 3,224 commits touching a
  `.md` file, 30+ hooks and 4 generators — a memory *product*, hand-built, whose
  shapes this ADR treats as the reference target.

Users hand-rolling a feature is the strongest adoption signal a design can get.
Two repos converged on nearly the same shape without contact, which means the
shape is close to right.

**But the 2RD audit's real value is how it failed**, because every failure is a
requirement:

| # | what happened | requirement it creates |
|---|---|---|
| 1 | the rollup died **13 days** before anyone noticed; 211 unconsumed patches; the footer still claimed "processed 0 patches at 07-12" | freshness is a **correctness property**, and a footer that can go stale cannot report it |
| 2 | `HOT.md` sat at **246 lines against its own 150-line cap**, carrying a banner reading *"a human must condense"* — injected into every session, unactioned, for weeks | **a cap whose only remedy is a human is not a cap** |
| 3 | 89 of 89 `Status: ACTIVE` injects exceeded the 5-day staleness rule | a warning that is always true is not a warning |
| 4 | **100% of hand-maintained enumerations drifted; 0% of generated ones did** ("8 lanes" vs 37) | generate anything that enumerates |
| 5 | four live docs instructed agents to read **deleted** files | referential integrity over the doc graph |
| 6 | three competing "master" docs, two claiming canonical | exactly one always-current surface |

The audit's verdict: *"the design is excellent and in places ahead of commercial
memory products. What it lacks is a runtime that keeps its own promises without a
human in the loop. That gap is the product."*

This ADR designs that runtime. It is deliberately narrower than the reference
target, and §1 explains the line.

## Decision

**Rekoll writes the documents whose truth it already holds, and nothing else.**

The discriminator that produces every ruling below:

> If the store already holds the truth, Rekoll **generates** the document — and
> hand-editing it is an error the tool detects.
> If a human holds the truth, the document is **hand-authored** and Rekoll writes
> at most a marked block inside it — or does not touch it at all.

That line is 2RD's failure #4 turned into architecture, and it is the reason the
writer can be useful without an LLM anywhere near it.

### 1. Two writers, split by whether an LLM is required — and only one ships first

The owner's goal ("plain-English documentation so a non-coder understands it")
contains two very different jobs, and conflating them would either break
ADR-0007's zero-LLM read contract or ship nothing.

- **Writer-D (deterministic) — this ADR's subject.** Renders stored records into
  markdown. Zero LLM, zero network, zero inference. Every byte it writes is
  either verbatim stored content, a stored id/timestamp, or fixed template text
  shipped inside the package. It cannot hallucinate, because there is nothing in
  its path that could.
- **Writer-S (synthesizing) — named here, deferred.** The MASTER/architecture
  narrative — prose *about* the project rather than a view *of* the store.
  Requires the BYO-AI provider layer (ADR-0015) and the consolidation loop, both
  of which DESIGN §0 lists as planned and unshipped. It is a later lane with its
  own ADR, and when it lands its output is confined to marked blocks (§7) and
  labelled as generated.

**Writer-D alone kills the disease.** Read #101 precisely: *"every **fact**
written twice (doc + `rekoll remember`)"*. What gets duplicated is facts,
decisions, rules, and open items — not prose. Every one of those is already in
the store in a structured, trust-labelled form. Writing them out is a rendering
problem, and rendering is exactly what a zero-LLM product can do perfectly.

### 2. Which documents, where they live, and each one's lifecycle

Four surfaces, three lifecycles. Paths are conventions, all overridable.

| # | surface | lifecycle | source of truth |
|---|---|---|---|
| **A** | `docs/rekoll/CURRENT.md` | **generated, rewritten in full** every sync | the store (board payload + rules) |
| **B** | `docs/rekoll/decisions/YYYY-MM-DD-<slug>.md` | **append-only**; a file is never rewritten | new content, written here first |
| **C** | `docs/rekoll/sessions/YYYY-MM-DD-<session>.md` | **append-only**, one file per close | new content, written here first |
| **D** | a marked block inside any hand-authored file | **generated block only**; bytes outside are never touched | the store |
| **(E)** | `docs/rekoll/rules.md` (ADR-0042 §4.3) | generated, rewritten in full | adopted directives |

**A — `CURRENT.md`, the one always-current surface.** This is 2RD's `HOT.md`
done as a *projection*. Crucially it is not a new bounded thing that needs a new
cap: it is a markdown rendering of `build_board_payload`, which ADR-0035 already
made **bounded** (10 recent + 10 majors + 5 rules by default, hard ceiling 50 per
leg), **trust-gated** (entry `text` is null below `BOARD_FLOOR`), **injection-
neutralized** (delimiter neutralizer, 200-char excerpt cap), and
**byte-deterministic** (pure function of stored rows, fixed key order, no clock).
Every property the audit says this surface needs, the board already has and
proves by test. §3 is therefore about *rendering* a cap, not inventing one.

Exactly one such document exists per scope (failure #6: three competing masters).

**B — the decision log.** One file per decision, dated, never rewritten. Written
in ADR-0037 §6's chunk-stable format so that appending decision N+1 cannot change
the bytes of decisions 1..N, which makes every re-ingest a content-addressed
no-op except for the new one. This is the tier ADR-0042 §4.2 identified as the
one with no competitor, and it is what actually travels to a teammate in git.

**C — the session log.** History, append-only, one file per close. Maps to
`Kind.EPISODE` (§6).

**D — marked blocks in files Rekoll does not own.** `CLAUDE.md`, `AGENTS.md`, a
per-feature README: Rekoll writes only between
`<!-- rekoll:begin <id> -->` and `<!-- rekoll:end <id> -->` and never a byte
outside. This is 2RD's `Arc.md` hybrid (hand-authored narrative + a generated
block in markers), and it is the primitive that makes contributing to a human's
file survivable at all. It is also how the "router" role from #101 gets served
without Rekoll claiming ownership of `CLAUDE.md`.

**Deliberately NOT written by Writer-D:** the MASTER narrative and per-feature
explainer prose (§1 — that is Writer-S), and anything that would be a *second*
always-current surface (failure #6).

**Where the reference target was overruled.** #101's shape has a `MASTER.md`
overwritten with current truth. Writer-D does not write it, because its content
is synthesis, and a deterministic renderer producing a document called MASTER
would either be a duplicate of `CURRENT.md` (failure #6) or would need an LLM
(ADR-0007). The honest answer is: `CURRENT.md` is the always-current surface, and
MASTER is Writer-S's, later.

### 3. The bounded surface and the automatic degrade — the audit's #1 lesson

> *A cap whose only remedy is "a human must condense" will sit broken for weeks.*
> — 2RD failure #2, observed at 246 lines against a 150-line cap.

**That state must be structurally unreachable.** Under this design it is: the
renderer never emits a document over cap, because it degrades first.

- **The cap is expressed in lines AND bytes.** A 150-line cap with 2,000-character
  lines is not a bound. Defaults: **150 lines / 8 KB**, both configurable. (8 KB
  is chosen to sit just above ADR-0035 §8's arithmetic ceiling of ~8.5 KB at
  default limits, so the ladder is exercised only in the tail — but it *is*
  exercised, and it is tested at cap-1.)
- **The degrade ladder** — deterministic, fixed order, applied step by step until
  the render fits:
  1. Drop `recent activity` entries, **oldest first** (cheapest to lose; fully
     recoverable via `rekoll board`).
  2. Narrow entry excerpts from ADR-0035's 200-char cap to 120.
  3. Drop board `major` entries, **oldest first**.
  4. Drop the open-items list down to a count line.
  5. **Rules are never dropped and never truncated.** ADR-0035 §8 already ruled
     this for the same reason: *a truncated instruction is a different
     instruction.*
- **The one case a human must act on, and why it is safe to leave to a human.**
  If the rules alone exceed the cap, the document exceeds the cap. It then says
  so at the top, loudly, naming the count — this is the *only* "a human must act"
  state in the design, and unlike 2RD's it is bounded by a number the operator
  directly controls (how many standing rules they keep), rather than by unbounded
  incoming traffic. It also cannot be reached by accident: `max_pinned_directives`
  defaults to 5 (ADR-0034 §3).
- **Every drop is disclosed, in the document.** `_12 older entries not shown —
  `rekoll board` for the full feed._` Silent truncation is worse than a small
  document, because a truncated document reads exactly like a complete one. (This
  is the "no silent caps" discipline, applied to prose.)

### 4. Freshness as a surfaced property — and why the file carries no clock

2RD's worst failure was invisible staleness: the rollup died, and the footer that
was supposed to report freshness *was itself part of what stopped updating*, so it
confidently reported a two-week-old world. A footer that goes stale cannot detect
staleness.

**Decision: the generated document contains no generation timestamp, and
freshness is computed on demand instead.**

- The document body is a **pure function of the store's content** — inheriting
  ADR-0035 §4's byte-determinism (stored `created_at` values verbatim; no
  read-time clock anywhere).
- Therefore **staleness is exact, not heuristic**: render the document in memory
  and compare its bytes to the file on disk. Equal ⇒ current. Different ⇒ stale,
  and the diff is the precise list of what changed. This costs one render plus one
  comparison, no LLM, no writes, no network — the same cheap-read discipline
  ADR-0037 §5 required of the staleness check.
- `rekoll status` and `rekoll doctor` report it, in the audit's own vocabulary:

  ```
  WARN  docs    docs/rekoll/CURRENT.md is behind the store
                (+3 majors, +1 rule, 2 items resolved since it was written)
                to update it:  rekoll writer sync
  ```

- **A second, free benefit:** because the body has no clock, `git diff` on a
  regenerated file is **empty when nothing changed**. Regeneration is idempotent
  and produces no commit noise, which means a non-empty diff is real signal —
  and a CI job can assert "docs are current" by regenerating and checking for an
  empty diff, exactly the way generated code is usually gated.

This is the direct answer to failure #1: "nothing alarmed" is unreachable, because
there is no pipeline whose death could go unreported — the freshness answer is
recomputed from scratch every time anyone asks.

Referential integrity (audit failure #5 — four live docs pointing at deleted
files) is the same mechanical check and belongs in the same `doctor` leg: every
repo-relative path a Rekoll-written document names must still exist. Cheap,
deterministic, and nothing in the ecosystem does it.

### 5. Capture without a command — reconciled with no-daemon and with consent

The audit is blunt: *"a command you must remember to type is just another tax."*
#82 said the same from the field: *"memory quality equals how often someone
remembers to call `remember`."* But Rekoll's posture is zero background processes
(ADR-0035 §6, ADR-0037 §5), and its consent rules forbid silent anything.

**The reconciliation: a hook is not a daemon.** The no-daemon posture bans
processes that run *when the operator did nothing* — a listener, a watcher, a
schedule, a thing with a lifetime. A hook has none of those: it runs exactly once,
as a consequence of something the operator already did. Nothing new is running
when nothing is happening. Three legitimate touchpoints follow, none of which is a
daemon:

1. **A standing directive that instructs the AI to run the verb.** Rekoll already
   owns the mechanism: ADR-0034 makes directives ride **every** recall envelope,
   deterministically, regardless of query. A rule like *"before you finish a
   session, run `rekoll writer close`"* is therefore present in every session that
   recalls anything, at zero new machinery. **Recommended first**, because it
   costs nothing to build and is the cheapest possible experiment in whether
   AI-driven capture works at all.
2. **Harness session hooks** (Claude Code `Stop` / `PreCompact` / session-end).
   DESIGN §8 already plans exactly this — *"Stop + PreCompact auto-capture hooks
   through one cross-platform capture entrypoint (Windows-safe; auto-captures land
   at low trust so the firewall can quarantine)"* — so this ADR adopts that plan
   rather than inventing a second one. This is the real answer; it needs that
   entrypoint built.
3. **A `post-commit` git hook recipe** (#82's own suggestion) that syncs changed
   paths. Offered as a **documented recipe**, never installed silently.

**Consent, non-negotiable, for all three:** installing a hook means writing to a
file the user owns (`.git/hooks/`, a harness settings file), which is §7's
territory and gets §7's rules in full. `rekoll writer install-hook` **prints the
exact lines it would add and asks**; declining leaves zero bytes (ADR-0037 §4).
Nothing is ever installed by plain `init` — ADR-0036 pins plain init
silent-and-zero-config, and a hook installed by a zero-config command would be the
single most surprising thing this product could do.

**And per ADR-0037 §7, the MCP door drives none of this.** A model-authored write
into a file the harness loads as instructions lands one level *above* Rekoll's
firewall, where no envelope, floor, or quarantine can reach it. The writer's
file-writing verbs are **CLI/SDK only** in v1. (§8 shows why the generated content
is nevertheless safe even when the *records* came from MCP.)

### 6. decisions ≠ todos ≠ history, on the frozen four kinds

The 2RD spine, stolen verbatim because it is right: *decisions are durable intent
that hold until superseded and never get "done"; todos are time-bound work;
history is the record. To change the arc you record a decision, not a todo. Filing
a work-ticket to change your mind is a category error.*

Mapped onto ADR-0004's frozen vocabulary — **with no new kinds and no new
metadata**, because ADR-0035 already built the mechanism:

| concept | kind | carrier | verb | writer view |
|---|---|---|---|---|
| **decision** | `raw_fact` | `board = "major"` metadata tag, floored at `BOARD_FLOOR` | `remember --board major` | **B**, the decision log |
| **todo** | `raw_fact` | `board = "pending"` tag, same floor | `remember --board pending` / `rekoll resolve` | **A**'s open-items section |
| **history** | **`Kind.EPISODE`** | the kind itself | the writer's close verb | **C**, the session log |
| **standing rule** | `Kind.DIRECTIVE` | the kind itself + the ADR-0017 vouch | `remember --kind directive` | **E**, plus ADR-0042 §4.3 |

Three notes, none of them a fudge:

- **`episode` is a real, frozen, currently unused kind.** The `episodes` table
  exists and `Kind.EPISODE` is MCP-writable, but nothing in the product produces
  one. The writer's session-close becomes episode's **first producer**, which is
  the kind finally doing the job ADR-0004 named it for.
- **Why a tag and not a kind, again.** ADR-0035 §2 settled it: kinds are
  lifecycle-distinct physical tables and *"importance is not a lifecycle."* The
  same argument applies here — decision-vs-todo is a distinction in *intent*, and
  a tag plus a trust floor already carries it. Reaching for a fifth kind would
  break a freeze that exists to prevent exactly this.
- **The one real limitation, named rather than smoothed over.** Today `resolve`
  is a single verb (`set_status`, ACTIVE→SUPERSEDED), so a *resolved todo* ("done")
  and a *superseded decision* ("we changed our mind") are indistinguishable in the
  store. That is a genuine collision of two different lifecycles on one
  transition. The writer must therefore **not conflate them in its views**: the
  decision log (B) renders majors **including superseded ones**, marked
  `superseded` — because a reversed decision is history, not deletion — while
  `CURRENT.md`'s open-items list renders only ACTIVE pendings. Same records, two
  views, per the spine. If the owner wants a *stored* distinction, the honest
  mechanism is a `superseded_by` link in the existing `record_links` table
  (D5 below), not a new kind and not a new status.

### 7. The file-writing safety model — the highest-risk surface this product will have

Everything else in this ADR is recoverable. This is not: **one lost user file
loses the user forever.** The rules below are designed to that standard, and every
one of them fails *closed*.

**7.1 Rekoll writes only where it has been explicitly allowed to write.**
Write permission is per-file, per-operator, and never inferred — an extension of
ADR-0037's `tracked_sources` registry (a `writable` flag on the existing table,
**not** a second registry), so it inherits that ADR's rulings: operator-only input,
never in the working tree, never adopted from repo data, never via MCP. Granting
*write* is a louder, separate ceremony from adopting a source for *reading*.

**7.2 Three write modes, three contracts.**

| mode | Rekoll owns | guard |
|---|---|---|
| **OWNED** (`CURRENT.md`, `rules.md`) | every byte | the file must begin with `<!-- rekoll:owned -->`. **No marker ⇒ refuse.** Rekoll did not create this file, so it will not overwrite it |
| **BLOCK** (`CLAUDE.md`, a README) | only between `<!-- rekoll:begin <id> -->` / `<!-- rekoll:end <id> -->` | markers missing, unbalanced, nested, or out of order ⇒ **refuse**, name the file and line, change nothing |
| **APPEND** (`decisions/`, `sessions/`) | bytes added at EOF | never rewrites; the format is ADR-0037 §6's chunk-stable block |

**7.3 Never invent content — structurally, not as a promise.** Every byte
Writer-D emits is (a) verbatim stored record content, already through the
firewall's neutralizer at read time, (b) a stored id or timestamp, or (c) fixed
template text shipped in the package. **There is no LLM in the path**, so
"it made something up" is not a failure mode that exists. This is the strongest
available answer to the requirement, and it is a second reason Writer-D ships
before Writer-S.

**7.4 Conflict — the file changed since Rekoll last wrote it.** The registry
stores the sha256 of exactly what Rekoll last wrote (ADR-0037's `content_hash`
column, doing double duty).

- **OWNED:** on-disk hash ≠ last-written hash ⇒ someone hand-edited a file Rekoll
  owns. **Refuse. Show what differs. Offer `--force`, which backs up first.**
  Never silently reconcile, and never "merge" — there is no principled merge
  between a human's edit and a regeneration.
- **BLOCK:** hash-check **only the block's bytes**, never the whole file. This
  detail is load-bearing: a human editing prose around the block must not trigger
  a conflict, or the mode is unusable and people will stop using markers.
- **APPEND:** no conflict is possible by construction (appending is monotone);
  the check is only that the file still ends cleanly.

**7.5 Reversible, two ways.**

- **Git is the real undo, and it is why writing into a repo is defensible at
  all.** The writer only ever writes inside a git working tree, so every byte it
  changed is visible in `git diff` and revertible with `git checkout --`. Say this
  in the docs; it is the honest reason a user should be comfortable.
- **A bounded local backup covers the uncommitted window.** Before any OWNED
  rewrite or BLOCK replace, the prior bytes go to
  `.rekoll/writer-backups/<target>/<sha>.md` — inside the gitignored store
  directory, so backups never pollute the repo — keeping the last **N = 10** per
  target, oldest dropped. Bounded, with the bound stated, because an unbounded
  backup directory is just a slower disk-space bug. The restore command is printed
  at write time, not buried in docs.

**7.6 Attributable, and never silent.** Every write prints a ledger — path, mode,
bytes before → after, records rendered, backup location — modelled on the audit's
one enforcement that actually held (*a printed WRITTEN/SKIPPED/DROPPED ledger; a
reasonless drop is a hard gate fail*). `--quiet` suppresses progress chatter and
**never** suppresses the write ledger. The generated markers name Rekoll as the
author in the file itself.

**7.7 Path safety, inherited and non-negotiable.** Immediately before opening any
target, re-verify it with ADR-0037 §5's realpath discipline (no symlinks or
junctions, containment intact), because a naive `open(path, "w")` follows links on
every OS, and an adopted file swapped for a link redirects an operator *write* to
an arbitrary target. Writes go through **temp-file + atomic replace in the same
directory** (`os.replace`), so a crash or a full disk can never leave a truncated
user file.

**7.8 Never write outside the project root.** A write target outside the repo is
refused. The asymmetry with ADR-0037 §4 — which *does* permit adopting the
user-home auto-memory directory for **reading**, behind a loud prompt — is
deliberate and worth stating: reading outside the project is a privacy question
the operator can answer for themselves; writing outside it is a damage question,
and the blast radius is the whole filesystem.

**7.9 Dry-run first, always.** `rekoll writer sync --dry-run` prints the complete
diff and writes nothing. The **first** write to any target requires an interactive
confirmation or an explicit `--yes` (the `_stdin_is_interactive` oracle the vouch
gate and wizard already use).

**7.10 A refusal is never a silent no-op.** Every refusal exits non-zero, names
the path, states the reason, and prints the one command that fixes it.

### 8. Composition with ADR-0037 — extend, don't duplicate

- **`remember --to` is the append primitive.** The decision log (B) is
  `remember --to <dated file>` plus the `board = "major"` tag. The writer adds no
  second append path and no second on-disk format — ADR-0037 §6's chunk-stable
  block is the format for all of B, C, and E.
- **The registry is ADR-0037's**, plus a `writable` flag (§7.1).
- **Append-mode outputs are tracked sources.** B and C carry genuinely new
  content, so write → auto-register → index → recallable closes the loop in one
  command, exactly as the brief intends.
- **⚠ Generated projections are NOT tracked sources — this overrules the brief.**
  `CURRENT.md`, `rules.md`, and BLOCK regions contain **no information the store
  does not already hold**. Indexing them would store every rule and every major a
  *second* time under a file `source_uri`, and content addressing would **not**
  collapse the duplicate: the render wraps each fact in a heading, so the chunk's
  bytes differ from the original record's bytes, yielding a different
  `content_hash` and therefore a different id — the precise trap ADR-0037 §6
  already analysed for `remember --to` and solved by making the bytes identical.
  Here they cannot be made identical, because a projection is a *reformatting* by
  definition. The consequences of getting this wrong are concrete: every recall
  returns each decision twice, the store inflates without new information, and a
  correction made in the store leaves a stale copy indexed from the projection.
  **Rule: render what is derived; index only what is new.** It is also a token
  win — you never pay to store, embed, or recall the same fact twice.
- **Trust and injection safety come for free, and this is the part worth
  noticing.** `build_board_payload` already nulls an entry's `text` below
  `BOARD_FLOOR` and neutralizes envelope delimiters in what survives (ADR-0035
  §4). So an UNVERIFIED memory written by an MCP worker agent **cannot place its
  text into `CURRENT.md`** — the gate the board needed is exactly the gate the
  writer needs, already built and already tested. A file that a harness loads as
  instructions therefore cannot be reached by untrusted content, even though
  untrusted content can freely enter the store.
- **The envelope is untouched.** No writer output enters `ContextEnvelope.render()`;
  ADR-0013's and ADR-0034 §4's byte-identity contracts stand, and their tests run
  unmodified — the same ruling ADR-0037 §8 made for provenance pointers.

### 9. Token economy — where the writer's output sits in the budget

The owner's standing goal is that Rekoll **saves** tokens. The audit gives the
reference policy and the number behind it: **exactly one bounded snapshot plus one
scoped document at startup**, justified by the measured cost of the old habit —
**~660K tokens of pure orientation across 52 sessions**, ≈ **12.7K tokens per
session** spent re-learning the project before doing any work.

- **`CURRENT.md` is that one bounded snapshot.** Its budget is ADR-0035 §8's
  arithmetic, not a new promise: ~5 KB typical, ~8.5 KB at the default ceiling —
  call it **~1.3–2.2K tokens**. Against 12.7K, the snapshot costs roughly a sixth
  of the orientation it replaces, and unlike ad-hoc exploration its cost is
  **bounded and known in advance** rather than proportional to how lost the agent
  gets.
- **The second slot** is the one scoped document the session actually needs — a
  decision file, a session log — fetched **on demand** via recall, using the
  provenance pointers that already ship (ADR-0037 §8). The audit's corollary is
  worth keeping: *"a session that needs more orients by asking a few clarifying
  turns, not by pre-loading — pre-loading pays for everything unconditionally. We
  accept turns."*
- **The double-charge, disclosed.** A session that loads `CURRENT.md` at startup
  *and* performs N recalls pays for the standing rules N+1 times, because
  ADR-0034 puts them in every envelope by design. This is real and it is not a
  bug — they are different channels for different consumers — but it should be
  said rather than discovered. Hosts that load `CURRENT.md` at session start can
  set `max_pinned_directives=0` to turn the envelope channel off; the knob already
  exists (ADR-0034 §3). Documented configuration, not a default change.
- **The writer must never make reads more expensive.** Nothing here adds a byte
  to the recall envelope, and no read path gains a file access — freshness is
  computed only by `status`/`doctor`/`writer`, never by `recall` or `board`
  (ADR-0037 §5's rule, extended).

### 10. Implementation lanes (file surfaces, so the conductor can schedule)

Ordered by dependency; **nothing in this ADR is built in this PR.**

- **W1 — the render core.** New `src/rekoll/writer.py`: pure functions from a
  board payload + directives to markdown, the §3 degrade ladder, the cap
  arithmetic, the marker grammar. **Zero I/O**, so it is fully unit-testable and
  collides with nothing. Tests: `tests/test_writer_render.py` — determinism (two
  renders byte-identical), the ladder at each rung, cap-1 / cap / cap+1, rules
  never truncated, every drop disclosed.
- **W2 — the safe-write engine.** New `src/rekoll/writer_io.py`: the three modes,
  marker parsing, hash-conflict detection, realpath re-verification, temp-file +
  atomic replace, bounded backups, the ledger. Independent of W1; **can run in
  parallel.** Tests: `tests/test_writer_io.py` — refusal on a missing `owned`
  marker, unbalanced/nested markers, a BLOCK write leaving surrounding bytes
  byte-identical, conflict detection, a symlinked target refused, an interrupted
  write leaving the original intact, backup rotation at N.
- **W3 — registry extension.** `src/rekoll/adapters/base.py` + `sqlite.py` +
  `conformance.py`: the `writable` flag on `tracked_sources`. **Blocked on
  ADR-0037 lane (a)** — the table must exist first. Do not schedule before it.
- **W4 — doors.** `src/rekoll/memory.py`, `src/rekoll/cli.py` (`writer` verb),
  `docs/`. Depends on W1+W2+W3. ⚠ `cli.py` is the most contended file in the repo
  — schedule last, alone.
- **W5 — freshness + integrity checks.** The `status`/`doctor` legs (§4) and the
  referential-integrity check. Touches `cli.py`; sequence with or after W4.
- **W6 — capture touchpoints.** The §5 directive route (wizard-offered, opt-in),
  the hook entrypoint, the documented recipes. Depends on W4.
- **W7 — LATER: Writer-S.** Gated on ADR-0015's provider layer and the
  consolidation loop. Its own ADR, its own threat model.
- **Tripwire (ships WITH this ADR, matching the wrap() and ADR-0037 precedents):**
  `test_design_marks_the_writer_as_planned_until_it_ships` in
  `tests/test_docs_consistency.py` — while no `rekoll.writer` module, no
  `Memory.write_docs`, and no `writer` CLI subcommand exist, every DESIGN.md line
  naming the feature must carry "planned". It retires in whichever PR ships the
  first slice.

## 11. Owner decisions (plain English, one recommendation each)

- **D1 — Ship the deterministic writer first and defer the narrative one?**
  Options: **yes — Writer-D now, Writer-S later behind the AI layer
  (recommended)** / build both together / wait and do it all at once. *Why:* the
  deterministic half needs no AI, cannot invent anything, and fixes the actual
  complaint in every field report (facts written twice). The narrative half needs
  an LLM Rekoll does not yet wire in, and shipping it first would mean a memory
  tool that can hallucinate into your repo. Do the safe, useful half now.
- **D2 — Where do the generated docs live?** Options: **`docs/rekoll/`
  (recommended)** / the repo root / ask during `init --wizard`. *Why:* one obvious
  folder keeps Rekoll's files together and unmistakable, so a reader always knows
  which documents are generated and which a person wrote. The path stays
  configurable for repos that do it differently.
- **D3 — What happens when someone hand-edits a file Rekoll generates?**
  Options: **refuse, show what changed, offer `--force` which backs up first
  (recommended)** / overwrite it / merge the edits. *Why:* overwriting silently
  destroys someone's work, and there is no honest way to merge a person's edit
  into a regenerated document. Stopping and asking is the only option that cannot
  lose anything. Merging is the tempting one and it is the one that eventually
  eats a file.
- **D4 — How does capture happen without a command to remember?** Options:
  **start with a standing rule that tells the AI to run the verb, then add real
  session hooks (recommended)** / hooks only / keep it a manual command. *Why:*
  the rule costs nothing to build and rides machinery that already exists — every
  session already sees standing rules — so it is the cheapest way to find out
  whether AI-driven capture actually works before investing in the hook
  entrypoint. Neither is a background process, and neither is ever installed
  without asking.
- **D5 — Should a superseded *decision* be distinguishable from a completed
  *todo* in the store?** Options: **not yet — the writer shows them differently
  and we revisit if it bites (recommended)** / add a `superseded_by` link now /
  add a new status. *Why:* today one verb does both, and the writer can present
  them correctly anyway (a reversed decision shows as history; a finished todo
  just leaves the open list). A link in the existing links table is the honest fix
  if it turns out to matter — but adding schema for a distinction nobody has
  complained about yet is how vocabularies rot. A new status is the wrong tool
  entirely.
- **D6 — May the writer ever write files when driven from an AI tool call
  (MCP)?** Options: **no, CLI and SDK only (recommended)** / yes, for generated
  files only. *Why:* Rekoll's generated documents are meant to be read by the AI
  at session start, so letting an AI write them lets a model write its own future
  instructions — the one loop the entire trust model exists to prevent. A person
  runs the command; the AI can ask them to.
- **D7 — Should CI be able to check that the generated docs are current?**
  Options: **yes — publish the recipe, since regenerating produces an empty diff
  when nothing changed (recommended)** / no. *Why:* §4's no-clock design makes
  this free, and it is the difference between docs that are current and docs that
  were current once.

## Consequences

- The double-write disease has a cure: facts, decisions, and rules are written
  **once** — to the store — and the writer renders them into the repo, where git
  carries them to teammates (ADR-0042) and where the harness already loads them.
- 2RD's six failures are answered structurally, not by discipline: the cap
  degrades itself (#2), freshness is recomputed rather than stored (#1, #3),
  everything that enumerates is generated (#4), integrity is a `doctor` check
  (#5), and there is exactly one always-current surface (#6).
- Rekoll gains its highest-risk surface — writing to a user's files — behind a
  model whose every rule fails closed: explicit per-file permission, three bounded
  ownership modes, refuse-on-conflict, atomic replacement, bounded backups, a
  printed ledger, and git underneath all of it.
- `Kind.EPISODE` finally has a producer, and the decision/todo/history spine is
  carried with **no new kinds and no new metadata** — ADR-0004's freeze holds.
- The store stops growing a second copy of everything it already knows, because
  projections are rendered and not indexed (§8).
- Writer-D can never hallucinate into a repository, because no LLM is in its path.
  When Writer-S changes that, it will be a conscious decision with its own ADR.

## Alternatives rejected

- **One writer that does prose and rendering together.** Either it needs an LLM on
  a path ADR-0007 promises is LLM-free, or it ships nothing. The split is what
  makes the safe half deliverable now.
- **Indexing the generated projections as tracked sources** (the brief's
  suggestion). Duplicates every rule and decision under a second id — content
  addressing cannot collapse them because the render reformats the bytes (§8) —
  inflating the store, doubling recall hits, and stranding stale copies after a
  correction.
- **A `MASTER.md` written by Writer-D.** Either a second always-current surface
  (the audit's failure #6, three competing masters) or synthesis without an LLM.
- **A generation timestamp in the file.** Makes every regeneration a diff, so
  "nothing changed" becomes indistinguishable from "everything changed", and the
  footer itself goes stale when the pipeline dies — 2RD's failure #1 exactly.
- **A file watcher, or `rekoll watch`.** A daemon in a zero-daemon product.
  Hooks give the same ergonomics with no process running when nothing is
  happening.
- **Auto-installing hooks during `init`.** ADR-0036 pins plain init
  silent-and-zero-config, and writing to `.git/hooks` or a harness settings file
  uncommanded is the most surprising thing this product could do.
- **A fifth kind (`decision`, `todo`, or `document`).** ADR-0004 froze the
  vocabulary against exactly this pressure, and ADR-0035 §2 already showed the
  tag-plus-floor mechanism carries the distinction.
- **Merging a human's hand-edit into a regenerated document.** There is no
  principled merge; the tempting version is the one that eventually destroys work.
- **Writing outside the project root.** Reading outside is a privacy decision the
  operator can make (ADR-0037 §4); writing outside is a damage decision with the
  filesystem as its blast radius.
- **MCP-driven file writes.** ADR-0037 §7's ruling, and it binds harder here: the
  files Rekoll writes are meant to be read by an AI at session start, so a
  model-written one is a model writing its own instructions.
