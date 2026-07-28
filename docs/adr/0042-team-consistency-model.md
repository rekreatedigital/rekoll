# ADR-0042 — The team consistency model: you don't sync the memory, you sync the source

**Status:** Proposed · **Date:** 2026-07-28 · **Extends:** ADR-0006 (content-addressed ids, amended by ADR-0026), ADR-0037 (memory + index: files are truth), ADR-0035 (board — §6's no-discovery posture and one-file sharing), ADR-0003 (scope granularity) · **Interacts with:** ADR-0005 (storage adapter contract), ADR-0023 (trust-aware upsert), ADR-0024 (embedder-identity mismatch), ADR-0017/0034 (directive vouch + standing channel), ADR-0033 (warn-don't-restrict), ADR-0041 (`doctor` reports only what it verified — governs the three `doctor` checks proposed in §4.3/D1/D3), ADR-0043 (the writer) · **Blocked-on for full effect:** issue #83 (doors default to different scopes — its own lane, ADR number not yet assigned; ADR-0041 went to the `doctor` lane) · **Evidence:** issues #75, #82, #87, #101

## Context

Three independent field reports now describe the same team-shaped failure.

- **#82** (a 12-hour, ~20-PR multi-agent session): rekoll worked as a *curated
  project ledger*, and the reporter's honest verdict was that its real wins are
  "long gaps between sessions, knowledge spanning repos, codebases too large for
  context, and **handoffs between agents that never share a window**." Three of
  those four are team problems. None of them is answered today.
- **#87 / #101**: real teams hand-build the same shape — a `CLAUDE.md` router, a
  MASTER doc overwritten rather than appended, a bounded ~150-line board file,
  `docs/sessions/`, `docs/adr/`, per-feature readmes — and write **every fact
  twice**, once into a doc and once via `rekoll remember`. Two independent repos,
  the same disease.
- **#75**: the same double-write, named as the thing Rekoll exists to cure.

ADR-0037 answered the single-operator half: files are truth, Rekoll is the index
over them. It did not answer the question a second person on the project asks
immediately: **"I cloned the repo. Where is the memory?"**

Today the honest answer is *nowhere*. `.rekoll/` is gitignored by `init`, so a
teammate's clone arrives with the files and an empty store. That is the correct
behavior — this ADR argues it is also the *complete* answer, provided one
convention is pinned and one gap is named out loud.

## Decision

**Git is the sync layer. `.rekoll/` is a derived local index.**

You do not ship the memory to your teammate; you ship the *source*, and their
Rekoll rebuilds an index that is bit-for-bit equivalent to yours. This is the
same relationship a database has with its search index, and it is why "just
commit the store" is not merely inconvenient but wrong.

The principle has one precondition (§2), four reasons behind it (§3), and it
covers three of the four things a team needs to share but not the fourth (§4).
Everything below is measured on `main` @ `73c434c` (v0.1.3) unless marked
otherwise.

### 1. The derived-index principle, proven

Two "clones" of the same project, at **different absolute paths and different
folder names**, each running `rekoll init` and then `rekoll ingest .` from the
repo root, over a byte-identical `docs/notes.md`:

```
alice/myrepo                                   -> rk_6d899d81be2a64ce983dddb5
bob/deeply/nested/checkout-of-the-same-repo    -> rk_6d899d81be2a64ce983dddb5
```

Identical. This is not luck; it falls out of ADR-0006 as amended by ADR-0026.
A record's id is `sha256(scope_key | source_uri | kind | content_hash)`
(`model.py`, `MemoryRecord.create`), and for an ingested file the `source_uri` is
`file://{rel}` where `rel` is the path **relative to the ingest root**, not the
absolute path (`memory.py`, `ingest_path`:

```python
rel = fp.name if single_file else fp.relative_to(root).as_posix()
```

). Nothing machine-specific enters the id. Two people on the same commit derive
the same memory, and neither had to send the other anything.

That is the whole model. Everything else in this ADR is the fine print.

### 2. The precondition this ADR exists to pin: one ingest root, named

Convergence holds **only when teammates ingest from the same root**, because the
root is what `rel` is relative to. Measured, three commands over the same file in
three fresh stores:

| command (run from the repo root) | stored `source_uri` | record id |
|---|---|---|
| `rekoll ingest .` | `file://docs/notes.md` | `rk_6d899d81be2a64ce983dddb5` |
| `rekoll ingest docs` | `file://notes.md` | `rk_8b35b13f1d48b269da1ce967` |
| `rekoll ingest docs/notes.md` | `file://notes.md` | `rk_8b35b13f1d48b269da1ce967` |

Same bytes, same repo layout, same scope — two different ids. (The single-file
form collapses onto the sub-directory form because `single_file` uses `fp.name`,
dropping the directory entirely.)

**So the convention is normative, not advisory:**

> **Ingest a project with `rekoll ingest .` from the repository root.**
> Sub-paths (`rekoll ingest docs`) are fine for a one-off private index, but a
> team that mixes roots has silently stopped converging.

**The trap that makes this worth an ADR: within one store, the divergence is
invisible.** Running all three commands against a *single* store leaves exactly
one record. The id collision is absorbed by `UNIQUE(scope_key, content_hash)`
plus the trust-aware upsert's equal-trust rule (`sqlite.py`, `_write_one`):

```python
if int(r.trust_tier) == prior["trust_tier"] and not same_id:
    return  # equal-trust, different source: dedup, keep incumbent
```

The first ingest's `source_uri` wins and later ingests from other roots are
no-ops. There is no duplicate, no warning, and no local symptom of any kind. The
divergence exists *only* across machines — which is precisely where nobody is
looking, and precisely where it breaks things.

Nothing keys on cross-machine record ids **today**, so this costs nothing right
now. It stops being free the moment anything does: a shared-backend board (§4), a
store export/merge, a "has your teammate seen this decision" cross-reference, or
any dedup across two operators' stores. Pinning the convention before that is
cheap; discovering it afterwards means a migration.

### 3. The store is never committed — all four reasons, honestly

`rekoll init` adds `.rekoll/` to `.gitignore` (`cli.py`, `_ensure_gitignore`), and
that stays. **One honest nuance, measured:** the gitignore step runs only inside a
git repository — outside one, `init` prints *"not a git repository - skipped
.gitignore"* and writes no ignore rule. A directory that gets `git init`'d *after*
`rekoll init` therefore has an unignored store. Naming this is cheaper than a
support thread; §6's docs plan carries it, and `doctor` growing a check is a
follow-up.

Why the store must not be committed, in the order the reasons actually bite:

1. **Merge conflicts, unresolvable.** A SQLite database plus its WAL is a binary
   blob. Git cannot three-way-merge it, so *every* teammate's *every* write
   conflicts with every other, and the only resolutions are "take mine" and
   "take theirs" — each of which silently discards someone's memory.
2. **Size.** #82's real store reached **29.6 MB at 2,734 memories**, most of it
   float32 vectors. Committing it adds that much to the repo *per sync*, forever,
   in a format that does not delta-compress.
3. **Embedder identity.** Vectors are only comparable to vectors from the same
   embedder (ADR-0024). A teammate who installed without the `embeddings` extra
   opens a fastembed-written store, and the facade correctly refuses the vector
   leg and degrades to lexical-only. It is honest — but it is a worse index than
   the one they would have built themselves in ten seconds from the same files.
4. **Hostile-store security.** A committed store is repo-controlled data that a
   clone would load as *memory*: planted "rules", fabricated history, and content
   that never passed the ingest-time firewall — including raw terminal escapes on
   the human CLI render path (open issue #98). This is ADR-0035 §6's
   repo-controlled-redirect attack with the payload delivered directly instead of
   by reference. ADR-0037 §2 rejected a committed *registry* for the same reason;
   a committed *store* is the stronger form of the same mistake.

Reason 4 is the one that makes this a security boundary and not a preference:
the other three are costs, and a user is entitled to accept costs
(ADR-0033 — warn loudly, never block). Reason 4 is not the committer's risk to
accept on behalf of everyone who clones.

### 4. What a team needs to share — four tiers, three answers

| tier | what it is | carried by | status |
|---|---|---|---|
| **Knowledge** | facts, docs, code, ADRs | **files in git** | ✅ works today |
| **Decisions** | why we chose this | **files in git**, written by the writer | ADR-0043 |
| **Standing rules** | directives every session obeys | **a git-carried proposal + a local vouch** | §4.3 — resolved here |
| **Live board** | who is doing what right now | **a shared backend, never git** | §4.4 — interface only |

#### 4.1 Knowledge — already solved

Files in git, `rekoll ingest .` on the other end, §1's determinism. The only work
is documentation (§6).

#### 4.2 Decisions — the writer's job (ADR-0043)

Decisions are the tier with no competitor (#87: of 2,734 memories, the 7
hand-written decisions held all the recalled value). They are *born* in a session,
not in a file, which is why every field report shows people writing them twice.
Getting them into git in a legible, chunk-stable form is exactly what ADR-0043's
writer does, and it is the reason these two ADRs are one design.

**Consequence worth stating plainly:** a decision that reaches git is a decision
that survives a teammate's rebuild. A decision that only reached `rekoll remember`
does not. Measured — a store with one ingested file, one `remember`ed fact, one
`directive`, and one board `major`, then `rm -rf .rekoll && rekoll init && rekoll
ingest .` (what a fresh clone does):

```
before:  4 memories  (raw_fact 3, directive 1) + 1 board major + 1 rule
after:   1 memory    (raw_fact 1)              + 0 board majors + 0 rules
```

The `remember`ed fact, the standing rule, and the board entry are **gone**. Only
what lived in a file came back. That single measurement is the argument for the
writer, and it is why "we'll just remember things" is not a team strategy.

#### 4.3 Standing rules — the open question, resolved

A rule cannot ride a file. ADR-0037 §1 is categorical: tracked-source ingestion
writes `Kind.RAW_FACT` only and can never mint a `directive` at any vouched tier,
because content arriving through an unceremonied channel must not promote itself
into instructions. A rule needs the ADR-0017 vouch — a human, at a terminal,
confirming — and a file in a repo cannot carry a human's confirmation.

That is the correct rule and this ADR does not weaken it. But it leaves teams with
no story at all, so:

**Decision: git carries the rule *proposal*; each operator's machine carries the
*vouch*.**

- The repo holds a plain, legible rules file (`docs/rekoll/rules.md` by
  convention — the writer's `RULES` target, ADR-0043). It is reviewed and merged
  like any other file. Indexed as an ordinary tracked source, it is **searchable
  by every teammate immediately** — it just does not yet *steer* them.
- A new verb (`rekoll rules adopt`, name is a lane detail) reads that file and
  walks the operator through vouching each proposed rule locally, one at a time,
  through the existing ADR-0017 gate. What lands is a real `directive`, minted by
  a real human confirmation, riding the ADR-0034 standing channel exactly as a
  hand-typed one does. **Zero new trust surface.**
- Adoption is per-operator by design, which is the same posture ADR-0037 §2 took
  for the tracked-source registry and for the same reason: consent does not
  travel in a repository.

This mirrors how teams already work — a rule is proposed in a PR, reviewed,
merged, and *then* each person's environment picks it up — and it is 2RD's own
"Propose → approve → THEN write; never assume silence = approval" with the
approval placed where the ceremony can actually happen.

**The gap this leaves, stated loudly rather than papered over:** a merged rule
does **not** apply on a teammate's machine until they run the adopt step. A rule
everyone agreed to can sit un-adopted and unnoticed. That is a real hole, and the
2RD audit is unambiguous about what happens to a system whose only remedy is a
human remembering something.

So the gap must be **surfaced, not trusted to discipline**: `rekoll status` and
`rekoll doctor` compare the rules file's proposed set against the operator's
adopted directives and report the difference —

```
WARN  rules   this repo proposes 3 standing rules you have not adopted
              (2 new, 1 changed since you adopted it)
              to review and adopt them:  rekoll rules adopt
```

— by hash compare only, never ingesting, never adopting, never blocking
(ADR-0037 §5's cheap-read discipline and ADR-0033's posture). Freshness as a
surfaced property is the 2RD audit's Tier-1 #2, and this is its first application.

*Rejected:* letting a file mint directives at a high vouch (ADR-0037's stated
vulnerability, reintroduced through a side door); auto-adopting on `ingest` (a
merged PR would silently rewrite every teammate's standing instructions — a
supply-chain attack on the instruction channel, which is the highest-blast-radius
surface in the product).

#### 4.4 The live board — not git, ever; a shared backend, eventually

The board is *ephemeral coordination* — who is awake, what is in flight, what is
open right now. It does not belong in git, for a reason that has nothing to do
with security: commit → push → pull is a latency of minutes on a signal whose
useful life is seconds, and every poll would be a commit. A board in git is a
board that is always wrong.

The 2RD system reached this verdict independently and from the other direction:
its `lane_sync.py` bus is **deliberately not a repo file**, living in a
machine-local `~/.rio-crosstalk/` precisely because lanes run from different
worktrees and a repo file would need a commit round-trip to cross folders. Two
designs, no contact, same conclusion.

The cross-machine answer is therefore a **shared storage backend** — the planned
Postgres/Supabase adapter (DESIGN §5) — and not a new product surface. ADR-0035
already built the board as four *optional* adapter methods plus one shared payload
builder, so an adapter that implements them gets a cross-machine board with no
board-side changes at all. This ADR designs only the **expectations such a backend
must meet**:

1. **Two properties conformance cannot prove, which the backend must establish
   itself.** ADR-0035's consequences already name them: `set_status` **atomicity**
   and the **untorn** `board_snapshot`. A single-writer sequential conformance run
   cannot observe either. SQLite proves them with two real connections; a network
   backend must prove them with two real sessions, against real concurrency and
   real transaction isolation. Passing conformance is *not* evidence of these.
2. **Byte-determinism of the payload survives the swap.** `build_board_payload`
   is a pure function of stored rows with fixed key order and no clock
   (ADR-0035 §4), and byte-comparison of that payload is the *only* sanctioned
   change-detection contract (§9 — `latest` is a hint, `PRAGMA data_version` is
   unusable across sessions). A backend that reorders rows, rounds timestamps, or
   re-serializes differently breaks change detection everywhere at once.
   Timestamps are **stored ISO-8601 verbatim**, never re-rendered by the backend's
   clock or timezone.
3. **Never silently fall back.** If the shared backend is configured and
   unreachable, the door **errors and exits non-zero** — it does not quietly serve
   a local board. A session that believes it is reading the team's state while
   reading its own will act on fiction, confidently. (Stolen verbatim from
   `lane_sync.py`, which gets this right and states the reason: corrupting shared
   state is worse than stopping.)
4. **Scope isolation is enforced on the kind-table side of every join.**
   ADR-0035 §2 flagged this trap for SQLite — `record_metadata` has no scope
   column, so a metadata-first query leaks another scope's tags. A multi-tenant
   network backend makes that leak a cross-*customer* leak. Conformance pins it;
   a shared backend must not route around it.
5. **The connection string is operator-only input.** ADR-0035 §6's no-discovery
   ruling applies with more force, not less: a repo-committed backend URL is a
   hostile clone pointing every teammate's memory at an attacker's database.
   Flag or environment variable, set by the operator, never read from the working
   tree.

Everything else — auth, pooling, migrations, latency budgets — is that adapter's
ADR, not this one.

### 5. What "consistent" means, and what it does not

**It means:** two teammates on the **same commit**, having both run
`rekoll ingest .` from the repo root at the **same scope triple**, hold
byte-identical records with identical ids for every file-derived memory.
Convergence is *eventual* and *per-commit*: it happens at the granularity of git,
because git is the transport.

**It does not mean:**

- **Live sync.** Nothing pushes. A teammate's `remember` never reaches you, and
  no amount of waiting changes that — only a commit does.
- **Anything for uncommitted work.** Your working-tree edits are yours until you
  push. That is correct, not a limitation.
- **Convergence of non-file memories.** `remember`ed facts, adopted directives,
  board state, proof counts, and `seen_at` are per-operator local state and stay
  divergent by design. §4.2's measurement is the proof.
- **Convergence across scopes.** The id includes `scope_key`, so two teammates
  agree only if their scope triples agree. This is where issue **#83** stops being
  a UX footgun and becomes a *correctness* precondition for this ADR. Measured —
  the same file, in the same layout, with the project name derived from the clone
  folder (the MCP door's default, `_derived_project`):

  ```
  clone folder "myrepo"       ->  default/myrepo/default       ->  rk_93a2b9b7b8898a9887a76a79
  clone folder "myrepo-fork"  ->  default/myrepo-fork/default  ->  rk_23d5ff556b1d9f6b37ac40a4
  ```

  Byte-identical content, byte-identical layout, **different ids** — because one
  teammate named their clone folder differently. §1's convergence held only
  because the CLI's `project` default is the path-independent `"default"`.

  **This ADR does not solve #83** (it has its own lane) but it sharpens
  the requirement handed to that lane: a folder-derived scope default makes record
  ids *machine-dependent*, which defeats the derived-index model outright. Whatever
  #83 picks must give teammates a scope triple that is a function of the
  **repository**, not of where or under what name it happens to sit on disk. The
  reporter's `rekoll init`-generates-`.mcp.json` idea (#101) is a good fit — it
  writes the MCP *client's* config with a pinned scope rather than adding any
  discovery mechanism to Rekoll, so ADR-0035 §6 is untouched.
- **A merge.** Two divergent stores are not reconciled by this design, and no
  verb here merges them. The reconciliation is: re-ingest from the files.

### 6. Docs plan (plan only — no docs edited in this PR)

Two surfaces, both owned by other lanes today (`docs/QUICKSTART.md`, `README.md`),
so this is a handoff, not an edit:

- **QUICKSTART — a new "Working with a team" section**, placed after the existing
  sharing section it corrects. Contents: the one-line thesis; the normative
  `rekoll ingest .` convention with §2's table as the reason; the teammate
  onboarding recipe (`clone → rekoll init → rekoll ingest . → rekoll rules adopt`);
  §5's honest "what does not travel" list; and the pointer that the *existing*
  "Sharing a board between sessions" block is **one machine only** (SQLite over
  NFS/SMB is unreliable — already documented at `QUICKSTART.md:241`), with §4.4 as
  the cross-machine future.
- **README** — one line in the positioning, no more: memory travels with the repo,
  because the source does.
- The `init`-outside-a-git-repo nuance from §3 goes wherever `init` is documented.

## 7. Owner decisions (plain English, one recommendation each)

- **D1 — Make `rekoll ingest .` from the repo root the documented team
  convention?** Options: **document it as normative and have `doctor` warn when a
  store's records came from mixed roots (recommended)** / document it only /
  do nothing. *Why:* it costs one docs paragraph and one cheap check today, and
  fixing it later means a migration. The `doctor` check is a plain look at the
  stored source paths — no new machinery, and it is the only way a user ever finds
  out, since §2 proved there is no local symptom.
- **D2 — The team story for standing rules.** Options: **a git-carried rules file
  that each teammate adopts locally, with `status`/`doctor` reporting un-adopted
  rules (recommended, §4.3)** / rules stay purely per-operator with no team story /
  wait for the shared backend and let rules live there. *Why:* it keeps the vouch
  ceremony exactly as it is (the alternative is letting a merged file rewrite
  everyone's standing instructions, which is the worst security idea in this
  document), it works today with no new backend, and the un-adopted warning stops
  it from failing silently. The shared-backend option is better *later*, for teams
  that have one — it is not a reason to ship nothing now.
- **D3 — Should `rekoll doctor` warn when `.rekoll/` is not gitignored?**
  Options: **yes, warn with the one-line fix (recommended)** / no. *Why:* §3's
  measurement shows a real path to an unignored store (`git init` after
  `rekoll init`), the consequence is committing a 30 MB binary that a clone would
  load as memory, and a warning is the whole fix. Warn, never block (ADR-0033).
- **D4 — Is the live board's cross-machine answer the shared backend?** Options:
  **yes — a board in git is never proposed again, and §4.4's five expectations
  become that adapter's acceptance criteria (recommended)** / leave it open. *Why:*
  it closes the door on the one bad idea in this space (a committed board file)
  and it costs nothing now, because ADR-0035 already built the board as optional
  adapter methods.

## Consequences

- The team story becomes documentable today, with no new storage, no daemon, and
  no network: clone, `init`, `ingest .`. The determinism that makes it work is
  measured, not assumed.
- One convention (`ingest .` from the root) becomes normative, ahead of anything
  that depends on it. Cheap now; a migration later.
- The gitignored store, previously a default nobody had justified in one place, is
  now a decision with four stated reasons — one of which (hostile stores) is a
  security boundary that a user may not opt out of on their clones' behalf.
- Standing rules get a team story that adds **zero** new trust surface, at the
  cost of an explicit per-operator adopt step — a cost this ADR pays visibly, with
  a warning, rather than hiding.
- Issue #83 is re-scoped from "annoying default" to "precondition for cross-machine
  correctness", with a concrete requirement for whichever lane takes it.
- ADR-0043's writer stops being a documentation nicety and becomes the transport
  for the decisions tier. Neither ADR stands alone.

## Alternatives rejected

- **Commit the store.** §3's four reasons; the fourth is not the committer's risk
  to accept.
- **A sync/push/pull verb over stores.** Rebuilds git badly, inside a product that
  already requires git to be present. Two divergent stores have no principled
  merge — records are content-addressed, but *status*, trust, and proof counts are
  local judgments, and there is no rule for whose judgment wins.
- **A repo-committed config pinning path and scope.** The exact hostile-clone
  redirect ADR-0035 §6 rejected and ADR-0037 §2 re-rejected. #83's lane must find
  a shape that is operator-input or client-config, not repo data.
- **A committed board file.** Latency measured in commits on a signal measured in
  seconds; 2RD reached the same verdict independently and moved its bus off the
  repo for exactly this reason.
- **Auto-adopting rules from a git-carried file on `ingest`.** A merged PR would
  silently rewrite every teammate's standing instructions. Directives are the
  highest-blast-radius surface in the product; a supply-chain path into them is
  not a convenience feature.
- **Absolute paths in `source_uri`** (which would make ids machine-specific and
  kill convergence outright). Not proposed by anyone — recorded because §1's
  determinism depends on the current relative-path behavior, and this ADR is the
  reason it must not change without a migration.
