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
- Benchmark harness with a recall-quality regression gate over a sealed split.

### Changed

- CI `audit` job now audits the installed dependency closure of the optional
  extras (`.[dev,mcp,embeddings,bench]`) instead of the empty runtime-dep set of
  the bare project.
- CI `test` matrix now includes `macos-latest` on the core (zero-extra) suite.

[Unreleased]: https://github.com/rekreatedigital/rekoll/commits/main
