# Changelog

All notable changes to Rekoll are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it starts tagging releases.

Rekoll is **pre-alpha and not yet published to PyPI**, so there are no tagged
releases yet — everything below lives under **Unreleased** until the first `0.x`
tag. A dedicated **Security** heading is kept per the governance commitment in
[docs/DESIGN.md](docs/DESIGN.md) §9.

## [Unreleased]

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
- MCP server exposing five tools (`remember`, `recall`, `ingest_path`,
  `forget`, `status`) over a project's private store (the `[mcp]` extra).
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
  are exported from the package root. The CLI and MCP board surfaces land in a
  follow-up lane.
- Benchmark harness with a recall-quality regression gate over a sealed split.

### Changed

- CI `audit` job now audits the installed dependency closure of the optional
  extras (`.[dev,mcp,embeddings,bench]`) instead of the empty runtime-dep set of
  the bare project.
- CI `test` matrix now includes `macos-latest` on the core (zero-extra) suite.

### Fixed

- `SQLiteAdapter.set_status` now rolls back a failed multi-table sweep. A
  failure partway through could previously leave a matching `UPDATE` in an open
  transaction that the next unrelated write silently committed — a resolve that
  reported failure taking effect later, with the scan-cache patch never applied.
- The board payload's tamper warning counted a record once per leg while naming
  its id once, so a tampered curated major (which also rides the activity feed)
  was reported as "2 board record(s)" followed by a single id.

[Unreleased]: https://github.com/rekreatedigital/rekoll/commits/main
