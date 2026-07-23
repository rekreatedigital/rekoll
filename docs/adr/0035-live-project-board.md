# ADR-0035 — The live project board: shared current-state for concurrent sessions

**Status:** Accepted · **Date:** 2026-07-23 · **Extends:** ADR-0005 (storage adapter contract), ADR-0034 (standing-directive channel), ADR-0013 (data-vs-instructions envelope), ADR-0019 (read-path tamper verification), ADR-0018 (input resource limits), ADR-0004 (frozen kind vocabulary), ADR-0002 (provenance + trust foundational) · **Interacts with:** ADR-0025 (forgetting/tombstones — see §7), ADR-0031 (door parity), ADR-0007 (zero-LLM reads), ADR-0003 (scope isolation), ADR-0023 (trust-aware upsert)

## Context

A project increasingly has SEVERAL AI sessions running on it at once — a
conductor, build workers in worktrees, a chat session answering questions. Each
session's context is private, so none of them knows what the others just did,
decided, or left open. The store already holds that state (every write lands in
one scope), but no read surfaces "what is current" — `recall` answers a QUERY,
`newest` feeds `health()`'s freshness probe, and neither is a safe, bounded,
trust-labeled feed a session can poll.

Three reads almost fit, and each fails for a reason this ADR turns into
contract:

* **`newest()` has no status or trust gate — deliberately.** `health()` and
  `reindex()` depend on it seeing every row, quarantined and forged included. A
  forged row (raw `status='active'` at trust 0 — the divergent state
  `MemoryRecord` makes unrepresentable but a store can still hold) COMES BACK
  from `newest()`. That is proven by test
  (`test_newest_returns_the_forged_row_and_recent_records_gates_it`), and it is
  disqualifying for a feed that every concurrent session replays: the board is
  the highest-fanout read in the product, so anything that surfaces on it is
  amplified to every session at once.
* **`recall()` is query-shaped and pays ranking.** "What's going on" is not a
  query, and a board poll must not cost a vector scan + fusion.
* **The ADR-0034 channel is rules-only.** It solves always-surfacing for
  directives; the board needs the same discipline for *activity* and *curated
  state*.

## Decision

A **two-tier live project board plus the rules leg**, built as four OPTIONAL
storage-adapter methods (ADR-0005 style: base raises
`UnsupportedCapabilityError`), one shared payload builder
(`rekoll.board.build_board_payload`) that every door will render, and — v1 —
**explicit-path sharing only**.

### 1. Tier 1 — the activity feed (`recent_records`)

Every EFFECTIVE-active write in the scope, newest first (`created_at` DESC,
`id` DESC tiebreak), trust-labeled, default floor `min_trust = UNVERIFIED`
(owner decision: show all activity; the *text* gate lives in §4). The
effective-status rule (`_effective_status`) gates exactly as every other
surfaced read leg — the forged row `newest()` returns can never appear here,
which is why this is a NEW method and `newest()` is untouched.

### 2. Tier 2 — the curated legs (`board_entries`, `pending_open`)

Membership is a **metadata tag** — `board = "major" | "pending"` — AND the
trust floor `BOARD_FLOOR` (= `TRUSTED_SOURCE`, defined once in `firewall.py`
beside `DIRECTIVE_FLOOR`). Both halves are load-bearing:

* **Why a tag and not a new kind:** the kind vocabulary is FROZEN (ADR-0004) —
  kinds are lifecycle-distinct physical tables, and "importance" is not a
  lifecycle. A tag costs one bounded child-table row and can be added to any
  existing record.
* **Why a floor and not trust-as-importance:** importance and provenance-trust
  must stay ORTHOGONAL (ADR-0002 — trust is set at the ingestion boundary and
  immutable to LLMs). Encoding "major" as a trust tier would let importance
  launder trust. Instead: a tag is data any writer can attach, so the tag alone
  curates NOTHING below the floor.
* **Scope isolation trap, named:** `record_metadata` has NO scope column. Every
  gate (scope/status/trust) sits on the kind-table side of the JOIN; a
  metadata-first query would leak another scope's tags. Conformance pins this.

Curated order is **oldest-first** (`created_at` ASC, `id` ASC) — ADR-0034 §4's
exact rationale: under the cap the foundational items survive, and appending
never disturbs the rendered prefix, so prompt caches stay warm. `pending_open`
is the FULL count of open `board=pending` rows passing the Tier-2 gates.

### 3. One snapshot, never torn (`board_snapshot`)

Both tiers AND `pending_open` are read inside ONE read transaction, so a
concurrent writer can never produce tiers that contradict each other (a count
that includes an entry the legs never saw). Proven with two real connections
and a foreign commit driven deterministically into the mid-snapshot window
(`test_board_snapshot_is_untorn_when_a_foreign_commit_lands_mid_read`). Each
call opens a FRESH snapshot, so a foreign session's committed write is always
visible to the *next* poll — board reads never touch the per-connection vector
scan cache. `min_trust` gates the Tier-1 leg only; the curated leg and the
count always apply the `BOARD_FLOOR` policy (owner-locked, not a per-read
preference).

### 4. The payload (`build_board_payload`) — constant shape, trust-gated text

The CONSTANT key set `{"rules", "majors", "recent", "pending_open", "latest"}`,
all keys always present; entries are
`{"id", "kind", "trust", "created_at", "board", "text"}`:

* `rules` reuses `active_directives` with the SAME floor, cap (default 5 ==
  `DEFAULT_MAX_PINNED_DIRECTIVES`, deliberately), order, tamper-verification,
  and fail-soft discipline as `Memory._pinned_directives` — the board's rules
  and recall's pinned directives are the same records by construction (pinned
  by test against `RecallResult.directives()`).
* `trust` is the `trust_tier.name.lower()` spelling the MCP `remember` response
  and the CLI human recall line already use; `created_at` is the STORED
  ISO-8601 value verbatim — never a computed age, never a read-time clock.
* **`text` is trust-gated:** at `trust_tier >= BOARD_FLOOR` it is the first
  line of the content through the firewall's delimiter neutralizer (a stored
  string cannot forge envelope headers/role tags into every session), capped at
  200 chars. BELOW the floor `text` is null — the entry still appears
  (awareness), but the board never amplifies untrusted text (no amplification).
  The key set does not vary with trust.
* Every surfaced record (both tiers AND rules) is content-hash verified
  (ADR-0019); mismatches are withheld with ONE `UserWarning` naming the ids.
* **Byte-determinism:** the payload is a pure function of stored rows — no
  clock, no randomness, fixed key order; two calls with no intervening commit
  `json.dumps` byte-identically. `latest` is the max stored `created_at` among
  the returned `majors`/`recent` entries (else null) — a freshness hint a
  consumer can re-derive from the entries (see §9 for what it does NOT
  promise).
* Board content NEVER enters `ContextEnvelope.render()` or any recall payload:
  the recall envelope's byte-identity contract is untouched (its tests run
  unmodified).

### 5. Resolve (`set_status`) — atomic, gated, marking-only

The board's "done" verb is `set_status(scope, record_id, status)`: a targeted
UPDATE whose effective-status gate lives IN the statement (the
`bump_proof_count` concurrency pattern — no read-modify-write window). Only an
effective-active row transitions; a second call, a cross-scope call, a forged
row, a proposed row all report `False`; nothing can be *resurrected* through
it. Two racing sessions on two real connections yield exactly one transition
(proven by test). The adapter takes the target status as data (validated
against `Status`); the facade (Lane B) owns the product policy of restricting
the verb to ACTIVE→SUPERSEDED.

### 6. Sharing v1 — one file, explicit path, explicitly matching scope

Sharing IS the SQLite WAL file. Concurrent sessions share a board by opening
the SAME store path with the SAME `tenant/project/agent` scope at every door —
docs-only, nothing discovered, nothing resolved:

* **Every discovery mechanism is REJECTED for v1** — no config file in the
  repo, no upward directory search, no environment auto-detection, no second
  database, no daemon. The security reason is the same for all of them: a
  repo-controlled path redirect. If a file in a cloned repository could tell
  Rekoll where the store lives, a hostile clone would silently retarget every
  session's memory — reads served from an attacker's store (planted "rules"
  and history), writes exfiltrated into a file the attacker later collects.
  The store path stays **operator-only input** (a flag/env the operator sets,
  never data found in the working tree), exactly the posture `rekoll-mcp`
  already takes for trust and redaction settings.
* **Local filesystem only.** SQLite's WAL locking is not reliable on network
  filesystems (NFS/SMB); a shared board lives on one machine. Multi-machine
  sharing is a future adapter (e.g. Postgres), not a v1 stretch.
* A missing `--path` simply means each session has its own private store —
  nothing breaks; they just don't see each other.

### 7. Relation to ADR-0025 — the first implemented lifecycle slice

ADR-0025 binds "any interim EVICTION feature" to its tombstone/drop-order
contract. Resolve **evicts nothing**: `set_status` marks a row SUPERSEDED and
every byte stays in the store (pinned by conformance — the resolved record is
still `get`-able). It is therefore ADR-0025's *supersession* path
(`directive… leaves only by explicit host action or supersession`), implemented
early and compatibly: when compaction lands, superseded rows are candidates
*with* tombstones, per that ADR. ADR-0025 carries a one-line annotation
pointing here.

### 8. The bounded-read promise, numerically

Defaults: 10 recent + 10 majors + 5 rules, `pending_open`, `latest`. An entry
is at most ~400 bytes of JSON (27-char id, ≤200-char excerpt, ≤32-char
timestamp, fixed labels), typically well under half that. **Default worst case
≈ 5 KB** (20 entries at typical width + five one-line rules + syntax); the
arithmetic ceiling at defaults is ~8.5 KB with every excerpt at its 200-char
cap. Hard cap (`BOARD_LIMIT_CEILING = 50` per leg, validated loudly — negative
or over-ceiling raises, never clamps; 0 disables a leg): 100 entries ≈ 40 KB
ceiling. The one full-width leg is `rules` (rules are instructions; truncating
an instruction changes its meaning) — same width recall's directive channel
already pays, bounded in practice by rules being one-liners and hard-bounded by
the store's `max_content_chars`. Zero LLM, zero embedding, zero ranking
(ADR-0007): a board poll is a handful of indexed SELECTs (`(scope_key,
created_at)` per kind table + `(key, record_id)` on `record_metadata`, both
added idempotently — an existing store gains them on next open, no migration
machinery).

### 9. Honesty caveats — what the board does NOT promise

* **Same-id re-remember REOPENS a resolved item.** The content-addressed upsert
  rewrites `status` unconditionally (sqlite.py `_write_one`), so re-asserting
  the same content from the same source flips SUPERSEDED back to ACTIVE. This
  is INTENDED reopen semantics — "saying it again means it's current again" —
  and it is pinned by test so any future change is a conscious one.
* **Same-id re-ingest rewrites `created_at`** and can therefore reorder the
  board (an old major "renews" to the top of the feed and the back of nothing —
  its curated position moves because curated order keys on `created_at`).
* **`latest` is a hint, not a change token.** It does not move on resolves or
  forgets (nothing new was created; resolving the newest entry can even step it
  backward, since it is computed over the rows the payload returned). The
  authoritative change check is byte-comparing the payload — which
  byte-determinism makes cheap and exact.
* **`PRAGMA data_version` is deliberately NOT exposed** as a change token: it
  is connection-relative (it does not bump for your own writes), so two
  sessions can never compare values — it cannot serve as a shared cross-session
  cursor. Byte-comparison is the contract instead.
* **The cross-door scope trap is real today:** `rekoll-mcp` derives `project`
  from the launch directory's NAME (`_derived_project`, mcp_server.py) while
  the SDK and CLI default `project="default"`. Same folder, same `--path`, and
  the MCP session still boards a DIFFERENT scope than a default-scope CLI
  session — with no error, because scope isolation is working as designed
  (ADR-0003). Until a later lane aligns the doors, sharing docs must say:
  match `--path` AND pass the scope triple explicitly at every door.

### 10. No author/actor field (owner decision)

v1 records carry no "who wrote this" column; attribution is an in-text
convention ("conductor: merged lane A"). An author field would be a new
unverifiable channel (any writer could claim any name) and a schema commitment
for something sessions can already express in text. Revisit only with a real
identity mechanism to back it.

## Consequences

* Concurrent sessions on one store finally share "what is current" — bounded,
  deterministic, trust-labeled, injection-neutralized, and identical no matter
  which door serves it (the builder is the single rendering path the SDK
  facade and the CLI/MCP doors all call).
* The storage contract grows four OPTIONAL methods + one typed result
  (`BoardSnapshot`); a third-party adapter that skips them keeps working, and
  one that implements them is held by three new conformance checks in
  `ALL_CHECKS` to the trust/status **gates**, the **ordering** and **bounds**
  (including limit validation), scope isolation, and `set_status`'s **gating,
  honest return value, and marks-never-evicts** behavior. Two properties are
  deliberately NOT in conformance because a single-writer sequential check
  cannot observe them: `set_status`'s **atomicity** and the **untorn**
  `board_snapshot` are proven by reference-adapter (SQLite) tests using two
  real connections. A third-party adapter must therefore establish those two
  itself; conformance passing is not evidence of them.
* Two additive indexes speed the board and `newest()`-style recency scans; no
  migration, idempotent bootstrap.
* `firewall.py` gains exactly one constant (`BOARD_FLOOR`), mirrored once as
  `adapters.base.BOARD_TRUST_FLOOR` (an int, because `firewall` imports
  `adapters.base` and the reverse would cycle). Every storage-side Tier-2 floor
  reads that mirror rather than restating the tier, and a test pins the two
  equal — including a behavioral check, since a signature-only pin cannot see a
  floor restated inside a method body. The envelope, the
  screen, and every existing read path are untouched (full suite byte-green).
* The forged-row hazard is now CONTRACT everywhere it can surface: `newest()`
  keeps its ungated semantics for health, and everything board-shaped is gated
  + conformance-pinned.

## Alternatives rejected

* **Reuse `newest()` for the feed.** Returns forged/quarantined rows (proven by
  test); adding gates to it would silently change `health()`/`reindex()`
  semantics. A new gated method is honest on both sides.
* **A new `Kind.BOARD_ITEM`.** ADR-0004 froze the vocabulary for exactly this
  temptation; membership is not a lifecycle. A tag + floor does it without
  schema surgery.
* **Importance encoded as trust.** Breaks ADR-0002's orthogonality — trust is
  provenance, set at the boundary; a "promote to major" that raised trust would
  be trust laundering.
* **Discovery (config file / upward search / auto-detect).** A repo-controlled
  path redirect: a hostile clone silently retargets every session's store —
  reads poisoned, writes exfiltrated. Operator-only path, docs-only sharing.
* **A second "board database" or a daemon.** Two stores can disagree (which is
  the truth?); a daemon is a server in a local-first, zero-daemon product. The
  WAL file already IS the shared medium.
* **`PRAGMA data_version` as a shared change cursor.** Connection-relative by
  definition; unusable across sessions. Byte-comparison of a deterministic
  payload replaces it.
* **Truncating `rules` like entry text.** A truncated instruction is a
  different instruction; rules stay full-width (recall already pays exactly
  this) and bounded by count instead.
* **An `author` column.** Unverifiable self-asserted identity — a new spoofing
  channel dressed as provenance (see §10).
