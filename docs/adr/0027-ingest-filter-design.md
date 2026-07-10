# ADR-0027 — One ingest-filter system: directory pruning + filename globs

**Status:** Accepted · **Date:** 2026-07-10 · **Resolves:** issues #27, #28, #29

## Context

The efficacy program's dogfood lane (PR #26) found three ways `ingest_path`'s
zero-config walk stores the wrong things, all verified by run:

- **#27 (HIGH):** `DEFAULT_SKIP_DIRS` skips `venv`/`.venv` but not `env`
  (`python -m venv env` is in half the Flask tutorials) or `site-packages`. A
  repo with a committed `env/` virtualenv produced 9,106 records (99.7%
  vendored), 20+ minutes, ~8.9 GB peak RSS, and 4/10 recall misses; the
  filtered rerun took 2.2 s for 47 records and 10/10 hits (~500×).
- **#28:** machine-generated lockfiles were 53–74% of all stored chunks in two
  real JS repos, for zero recall value.
- **#29:** a real Google OAuth `credentials.json` was chunked, embedded, and
  stored as an ordinary retrievable record — and would travel with any export.

Three symptoms, one root cause: ingestion had only **one** filtering axis
(directory names). There was no file-level policy at all, so anything with an
included suffix that survived the walk was ingested, and directory policy was
a name list that a differently-named virtualenv sidesteps.

## Decision

Ingest filtering is **one two-level system**, both levels defaulted, both
overridable per call:

### 1. Directory level — `skip_dirs` (existing) + structural venv detection

- `DEFAULT_SKIP_DIRS` gains `env` and `site-packages`.
- `_walk` prunes any directory containing a **`pyvenv.cfg`** file: that is what
  `python -m venv X` drops at X's root, so it identifies a virtualenv by
  STRUCTURE regardless of its name. This is the durable fix; the name entries
  (`venv`, `.venv`, `env`) remain as a stat-free fast path. Detection lives in
  `_walk`'s dirnames-pruning loop, before the real-path containment and cycle
  guards, which are unchanged. Cost: one `isfile` stat per directory
  considered.
- The pruning applies to directories the walk would *descend into*. Pointing
  `ingest_path` at a virtualenv root itself is explicit intent and is walked
  (consistent with the single-file rule below).

### 2. File level — new `skip_files` parameter (filename globs)

- `skip_files: Optional[Iterable[str]]` on `ingest_path`: case-insensitive
  glob patterns matched against the **bare filename** (`fnmatchcase` on
  lowered strings, so behavior is identical on Windows and POSIX — these are
  naming conventions, not case-exact identifiers).
- `None` (the default) means `DEFAULT_SKIP_FILES`; a provided set **replaces**
  the defaults; an **explicitly empty set disables** filename filtering. The
  implementation checks `is None`, deliberately not falsiness — the existing
  `skip_dirs` falsy-empty wart is documented, not repeated.
- `DEFAULT_SKIP_FILES` = two tiers with different intents:
  - `DEFAULT_SKIP_LOCKFILES` (#28): `package-lock.json`, `yarn.lock`,
    `pnpm-lock.yaml`, `poetry.lock`, `Cargo.lock`, `bun.lockb`,
    `Gemfile.lock`, `composer.lock` — machine-generated, skipped silently
    like any other non-content file.
  - `DEFAULT_SKIP_SECRETS` (#29, owner decision: **skip + warn, never
    silent**): `credentials.json`, `id_rsa`, `id_ed25519`, `*.pem`, `*.key`,
    `.env`, `service-account*.json`, `token.pickle`.

### 3. Secrets are never silent (#29)

- Skipping secret-named files emits **one** plain-English `warnings.warn`
  per `ingest_path` call, naming every skipped file and stating the override
  routes (matching the existing symlink-root warning's style).
- Ingesting a secret-named file anyway — via a direct file path or a
  `skip_files` override — also warns (a "STORED" warning naming the files).
  Recognition for the warning always uses `DEFAULT_SKIP_SECRETS`; the
  override changes what is *filtered*, not what is *announced*.

### 4. The walk filters; a direct file path does not

Pointing `ingest_path` straight at one file is explicit intent: the filename
filter never blocks it (the secrets warning still fires — good manners, and
the "never silent" rule). Defaults exist to protect the *bulk* walk, where
the caller has not looked at every file; a caller naming one exact path has.
This also gives the simplest override story: *the way you ingest one filtered
file is to point at it.*

### 5. Observability — a `filtered` count

The return dict grows a `filtered` key: walk candidates excluded by the
filename filter. `skipped` keeps its established meaning (files we tried but
could not ingest: symlink, oversize, over-chunk-cap, undecodable). Without
this, a lockfile-heavy repo ingest looks inexplicably small.

## Scope honesty

- The filter (and its warning) applies to **walk candidates** — files that
  pass `include_ext`. Under the default extensions, `id_rsa`, `*.pem`,
  `token.pickle`, and `.env` never reach the walk at all (wrong or no
  suffix); the skip-list still covers them as defense for callers who broaden
  `include_ext`. Rekoll is not a secrets *scanner*: it does not warn about
  files it was never going to read, and it does not inspect file *content*
  for secrets (that is `redact_pii` / firewall territory, out of scope here).
- Name-based recognition has false negatives by construction (`my-creds.txt`
  is not on the list). The list targets *well-known* conventional names — the
  footgun class actually observed — not adversarial hiding.

## Consequences

- Zero-config ingest of a repo with a committed virtualenv (any name), a
  lockfile-heavy JS repo, or a stray `credentials.json` now does what a user
  would expect, with the ~500× dogfood pathology gone.
- The zero-dep default path is intact: `fnmatch` is stdlib; no new imports
  elsewhere; no behavior change on the read path.
- `filtered` is core-SDK-visible only for now; the MCP door whitelists its
  response keys and is owned by another lane (noted in the fix PR).
- A directory literally named `env/` that is *not* a virtualenv is now
  skipped by default — the cost of #27's fix. Escape hatch: pass
  `skip_dirs=` without `env`, or point `ingest_path` at it directly.
