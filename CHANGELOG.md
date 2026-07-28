# Changelog

All notable changes to Rekoll are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A dedicated **Security** heading is kept per the governance commitment in
[docs/DESIGN.md](docs/DESIGN.md) §9.

## [Unreleased]

### Added

- **`rekoll doctor` now vouches for what it reports (ADR-0041).** Three field
  reports showed it printing a clean bill of health for machines it had not
  actually checked, so it gains two checks:
  - **Install identity (#104).** It names the exact copy it is speaking for and
    **WARNs when another rekoll on your PATH would answer instead** — a stale
    0.1.1 shadowing a fresh 0.1.3 made a careful tester file two bug reports
    against code they were not running, both already fixed in the version they
    thought they had. Versions of other copies are read from files, never by
    executing them (a diagnostic must not run arbitrary programs that happen to
    be named `rekoll`; a test pins this). Editable checkouts are recognized and
    never raise a false alarm, and copies whose version could not be read are
    reported as unread rather than assumed to agree.
  - **MCP registration (#84).** When a project-local `.mcp.json` (or
    `.cursor/mcp.json` / `.vscode/mcp.json`) registers rekoll, doctor WARNs if
    the config is invalid JSON, if its `command` or pinned `--path` no longer
    exists (this broke silently twice after folder renames), or if **nothing in
    this scope has ever been written through the MCP door** — the only signal
    that separates "configured and working" from "configured and never
    loaded", which once cost a 12-hour agent session. No line at all when no
    registration exists, so CLI-only users are never nagged.
- **`StorageAdapter.count_by_source()`** — a new optional, bounded storage read
  (concrete with a raising default, so out-of-tree adapters keep working)
  counting effective-active records in a scope by `provenance.source_uri`. It
  is what lets the MCP check answer "ever" exactly instead of inferring it from
  a recency window; an adapter that cannot serve it makes doctor weaken its own
  wording to "none of the 50 most recent" rather than over-claim.

### Changed

- **Docs: the first five minutes on a fresh machine (#102, #103, #85).** The
  Quickstart and README now say how to *get* `pipx` (it ships with no Python)
  and warn that you must open a **new terminal** afterwards — a freshly
  installed command isn't on the PATH of a shell that was already running, and
  a tester read that as a failed install. `docs/MCP.md` now makes
  `python -m rekoll.mcp_server` with a relative interpreter the documented
  default for a project virtualenv, because the `rekoll-mcp` console-script
  shim embeds an absolute path and breaks on rename.

## [0.1.3] - 2026-07-25

### Added

- **A silently split store is now loud (#83, ADR-0040).** The CLI's default
  scope (`--project default`) and the MCP server's (project derived from the
  launch folder's name) differ, so an AI writing through MCP and a human
  typing bare CLI commands in the same repo read different scopes of the same
  store — and every diagnostic used to reassure: `recall` said "No memories
  found", `status` said "Memories: 0", `doctor` said "All checks passed".
  Now, when the scope a command reads is empty but the store holds memories
  under other scopes, `status` and bare `recall` print a note naming those
  scopes (largest first) with the exact command that reads them, and `doctor`
  gains a `scopes` check that WARNs with the same command. Nothing moves,
  nothing auto-switches, machine payloads (`recall --json`, MCP) are
  byte-unchanged, and the note counts rows the same way `status` does
  (effective-active) — a scope holding only quarantined rows is never
  advertised. Scope names come from the store, so they are printed
  conservatively (restricted alphabet, length-capped) and a name that could
  not be typeset safely is never turned into a copy-paste command.
  Backed by a new optional adapter census, `StorageAdapter.scope_counts()`
  (concrete, raising-by-default: out-of-tree adapters keep working and the
  note simply stays absent).

### Fixed

- `rekoll doctor` no longer prints mojibake on Windows consoles: every
  `Memory.health()` note it can surface (empty scope, dead ingest, tamper,
  embedder mismatch, and the store/enumeration failure notes) used an em dash,
  violating the CLI's ASCII-messages rule. A tripwire test now pins the whole
  set to ASCII.
- `rekoll status` and `rekoll doctor` no longer echo a stored embedder identity
  verbatim. A store is a file a repo can ship, and its rows never passed the
  ingest-time firewall, so a crafted identity could fire raw terminal escape
  sequences at the console — which matters more now that the new scope note can
  point you at another scope. (Recall's rendering of stored *content* has the
  same exposure and is tracked separately in #98.)

## [0.1.2] - 2026-07-25

### Added

- **Provenance pointers on recall** (ADR-0037 §8): every recalled hit that came
  from a file now says which file. The CLI's human line gains
  `| from: CLAUDE.md#4`, and the SDK (`RecallResult.sources()`), `recall --json`
  and the MCP `recall` tool gain a nullable `sources` list parallel to `ids` —
  `{"file", "chunk"}` per hit, `null` for a `remember`ed fact with no file.
  Correct a wrong memory where the truth lives instead of patching the index.
  `ContextEnvelope.render()` is byte-for-byte unchanged (ADR-0013). (#75, read half)
- **Honest relevance on `rekoll recall` (human door).** A recall now ends with one
  advisory line — how much of the scope came back and how close the closest memory
  was (`showing all 3 memories in scope | top similarity 0.46 - weak match; …`) —
  so a small store returning everything can't read as a confident answer. It
  informs, never filters: no hits are hidden, no default `--min-score` ships, and
  exit codes and the `--json`/MCP payloads are unchanged. The Quickstart gains a
  `--min-score` calibration recipe (ADR-0039, #73).

### Performance

- `recall()` no longer decodes the ~88 stored vectors per query that RRF fusion
  ranks away and discards; embeddings now materialize on first read. Recall p50
  is ~2x faster on a 1,000-record corpus (30.1 ms → 14.1 ms, bge-small) with
  bit-identical ranking (ADR-0038, #43).

### Fixed

- `ingest` no longer leaks compile-time warnings provoked by the *ingested*
  file's source (e.g. `SyntaxWarning: invalid escape sequence` on Python
  3.12+) into CLI/MCP output; rekoll's own warnings still surface (#89).
- `Memory.health()` stays fail-soft when a stored vector cannot be decoded: the
  record counts as not-embedded and earns a note naming possible tampering,
  instead of propagating a `ValueError` (found gating #43; ADR-0038).

## [0.1.1] - 2026-07-24

### Fixed

- `rekoll init` now creates the memory store file itself, so `rekoll status`
  (and `board`/`recall`) work immediately after setup instead of reporting
  "no memory store — run 'rekoll init'" (#71). `init` also now refuses a
  `--path` pointing at a non-rekoll SQLite database, and the no-store hints
  echo a custom `--path` so they can be followed verbatim.

### Changed

- Onboarding docs (#74): a project-root `.mcp.json` is documented beside the
  `claude` CLI hint (Claude Code's VS Code extension ships no CLI), install
  guidance is pipx-first with an honest SDK caveat, and docs/MCP.md is
  PyPI-first now that `pip install rekoll` is real.
- ADR-0037 designs the "memory + index" integration (#75) — tracked file
  sources, write-through `remember --to`, provenance pointers on recall.
  Design only: nothing is implemented, and a tripwire test pins DESIGN.md's
  wording to say so until it ships.

## [0.1.0] - 2026-07-24

The first public release — Rekoll is on PyPI: `pip install rekoll`.
Still pre-alpha in spirit: young, honest about its gaps, and built in the open
(1,250 tests across Linux/macOS/Windows on Python 3.10–3.13).

### Security

- **Injection firewall on by default.** Ingest-time screening redacts secrets,
  quarantines memory-poisoning / prompt-injection markers at low trust, and
  recall returns stored content inside a data envelope — handed to a model as
  DATA, never as instructions (ADR-0013).
- **Versioned attack corpus with a regression gate.** `benchmarks/attack_corpus.json`
  backs an attack-success-rate gate that may only go **down**, never up
  (ADR-0020).
- **Opt-in PII redaction.** Secrets (API keys, tokens, private-key blocks,
  database DSNs) are always redacted before storage; emails / US SSNs / phone
  numbers are redacted only when you enable `--redact-pii` (CLI) or
  `rekoll-mcp --redact-pii` / `REKOLL_MCP_REDACT_PII` (MCP). Redaction records a
  non-reversible audit tag, never the raw value (ADR-0022, ADR-0033).
- **Trust is monotonic.** An untrusted re-ingest can never downgrade a record's
  provenance or trust tier (ADR-0023); recall content-hash-verifies every
  candidate and withholds mismatches with a warning (ADR-0019).
- **Supply-chain posture.** All GitHub Actions are pinned to full commit SHAs,
  Dependabot proposes weekly bumps, and a `pip-audit` gate scans the full
  transitive closure of the optional extras people actually install
  (`[mcp]`, `[embeddings]`).

### Added

- `rekoll` CLI (`init`, `remember`, `recall`, `ingest`, `status`, `forget`,
  `doctor`) and the `Memory` Python facade — zero-config, local, private, no
  key and no LLM on the read path.
- Hybrid recall: local semantic + keyword search with optional cross-encoder
  reranking (the `[embeddings]` extra; falls back to a keyword stub when absent).
- MCP server exposing six tools (`remember`, `recall`, `ingest_path`,
  `forget`, `status`, `board`) over a project's private store (the `[mcp]` extra).
- Bring-your-own-database adapter contract with a SQLite adapter shipped by
  default, and bring-your-own-embedder / consolidator provider hooks.
- **Standing-directive channel** — a saved directive (e.g. "always explain
  simply") now **always** rides the recall envelope's instruction channel, on
  every recall, independent of the query and of the abstain gate — instead of
  surfacing only when it happened to rank into the top-k. A bounded
  (`max_pinned_directives`, default 5, `0` disables), deterministically ordered
  (oldest-first), deduplicated, tamper-verified, zero-LLM scoped read. Exposed as
  a new `directives` key on the SDK (`RecallResult.directives()`), CLI
  `recall --json`, and MCP `recall` — identical across all three doors — and as a
  new optional `StorageAdapter.active_directives` adapter method with its own
  conformance check (ADR-0034).
- **Live-project-board storage layer** (ADR-0035) — the shared current-state
  read for multiple concurrent AI sessions on one store: a trust-labeled
  activity feed (`recent_records`, effective-status gated — unlike `newest()`,
  which deliberately isn't), a curated majors/pending leg (`board_entries`,
  membership = a `board` metadata tag + the `TRUSTED_SOURCE` floor), an untorn
  one-transaction `board_snapshot`, and an atomic `set_status` resolve verb
  (marks SUPERSEDED, never deletes) — four new optional `StorageAdapter`
  methods with three conformance checks, plus `rekoll.board.build_board_payload`,
  the deterministic, tamper-verified, injection-neutralized payload every door
  will render.
- **Live-project-board SDK surface** (ADR-0035) — the `Memory` facade door onto
  that board: `mem.board()` returns a frozen `BoardResult` whose `to_dict()` is
  byte-identical to `build_board_payload`'s dict (so the SDK, CLI and MCP boards
  cannot drift), `mem.resolve(*ids)` marks board items done — ACTIVE →
  SUPERSEDED only, returning how many actually transitioned, never deleting —
  and `mem.remember(..., board="major"|"pending")` tags a curated item without
  changing its record id. The board is a free read: it builds no embedder and
  credits nothing to the was-it-used ledger. `BoardResult` and `BoardSnapshot`
  are exported from the package root.
- **Live-project-board doors** (ADR-0035) — the board now serves at every door,
  byte-identical (pinned by the three-doors parity suite). CLI: `rekoll board`
  (`--json` for scripts; `--recent/--majors/--rules` caps; an empty board exits
  0 — a status view, not a search), `rekoll resolve <id>...` (active →
  superseded only, never deletes; prints `Resolved N of M.` and exits 0 — a
  status verb), and `rekoll remember --board major|pending` (orthogonal to the
  standing-rule confirmation; below-floor and dual-leg cases get honest stderr
  notes). MCP: a sixth tool, `board`, with **zero arguments** — its leg caps
  are operator-only server config (`--board-recent/--board-majors/--board-rules`
  or `REKOLL_MCP_BOARD_*`; flags win, 0 disables a leg), so a calling model can
  never widen the board. Deliberately absent in v1: an MCP resolve tool and any
  board input on MCP `remember` — nothing model-transited can reach the curated
  tier. Sharing stays explicit: same `--path` AND the same scope triple at
  every door (docs/QUICKSTART.md documents the cross-door scope trap).
- Benchmark harness with a recall-quality regression gate over a sealed split.
- **Release workflow** (`.github/workflows/release.yml`) — publishes to PyPI via
  OIDC Trusted Publishing (no long-lived token to steal) on a published GitHub
  Release, with a manual TestPyPI dry-run path; SHA-pinned actions; the build
  job holds no publish credential and a guard refuses to publish when the
  release tag disagrees with `src/rekoll/_version.py`.

### Changed

- Version `0.0.0` → `0.1.0.dev0` — an honest in-development marker for the
  run-up to the first tagged release (`0.1.0` lands with the tag itself).

- CI `audit` job now audits the installed dependency closure of the optional
  extras (`.[dev,mcp,embeddings,bench]`) instead of the empty runtime-dep set of
  the bare project.
- CI `test` matrix now includes `macos-latest` on the core (zero-extra) suite.

### Fixed

- `SQLiteAdapter.set_status` now rolls back a failed multi-table sweep. A
  failure partway through could previously leave a matching `UPDATE` in an open
  transaction that the next unrelated write silently committed — a resolve that
  reported failure taking effect later, with the scan-cache patch never applied.
- The `[mcp]` extra declared `mcp>=1.2`, but the server's agent-guidance
  `instructions` string (the board polling-rhythm teaching handed over on
  initialize) needs `FastMCP(instructions=...)` and `InitializeResult.instructions`,
  which first exist in **mcp 1.3.0** — 1.2.x silently drops them. The floor is
  now `mcp>=1.3` and the CI floor cell installs `mcp==1.3.0`, so the declared
  minimum genuinely runs every shipped MCP feature. (Surfaced by the first
  public CI run; local dev already used a newer mcp, which is why it passed
  there.)
- The board payload's tamper warning counted a record once per leg while naming
  its id once, so a tampered curated major (which also rides the activity feed)
  was reported as "2 board record(s)" followed by a single id.
- Every repo link (README, QUICKSTART, pyproject `[project.urls]`, issue
  templates, MAINTAINERS) still pointed at the pre-transfer personal-account
  URL `ryankyleocampo-github/rekoll`; they now point at
  `rekreatedigital/rekoll` — the pyproject ones would have shipped as the PyPI
  project links.
- The README described `rekoll recall --json` as emitting 4 keys; the payload
  has 7. The sentence now enumerates them exactly, and a docs-consistency
  tripwire pins the README's list to the code so the next added key fails CI
  until the README names it.
- The battle-harness absolute ReDoS budgets are now true hang-backstops
  (10s / 45s, ≥10x the worst recorded trip) — the old 3.0s budget sat only ~2x over
  the marker-dense test's genuine ~1.5s runtime and tripped twice on loaded
  shared runners (2026-07-15, Windows and macOS) with every other cell green.
  Super-linear *scaling* stays caught by the runner-independent ratio gates.

[Unreleased]: https://github.com/rekreatedigital/rekoll/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/rekreatedigital/rekoll/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/rekreatedigital/rekoll/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/rekreatedigital/rekoll/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/rekreatedigital/rekoll/releases/tag/v0.1.0
