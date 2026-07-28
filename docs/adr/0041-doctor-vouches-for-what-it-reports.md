# ADR-0041 — `doctor` must not report health it cannot vouch for

**Status:** Accepted · **Date:** 2026-07-28 · **Implements:** issues #104 (install identity), #84 (MCP registration) · **Follows:** ADR-0040 (loud scope split) · **Interacts with:** ADR-0018 (bounded reads), ADR-0035 §6 (no store discovery)

## Context

Three field reports, three different users, one repeating failure: **`rekoll
doctor` printed a clean bill of health for a machine it had not actually
checked.** ADR-0040 fixed one instance of this (a store split across scopes
read as "All checks passed"). Two more remained, and both cost real time:

1. **A stale install answered instead of the fresh one (#104).** A tester
   installed 0.1.3 via pipx, but a 0.1.1 sitting earlier on PATH shadowed it.
   They then filed two bug reports against 0.1.3 — *both already fixed in
   0.1.3, neither reproducible*. `doctor` had printed the true version the
   whole time, as `ok`, which nobody reads as a warning, and said nothing
   about the other copies. A diagnostic that cannot vouch for **its own
   identity** turns a careful tester into a source of phantom bugs.

2. **A configured MCP server had never loaded (#84).** A `.mcp.json` was
   present and correct; the client needed a restart nobody knew about. An
   agent ran a 12-hour, ~20-PR session believing it had rekoll tools. It had
   none. `doctor` printed nine green checks and never mentioned MCP. The same
   config later broke twice more after a folder rename, because a venv
   console-script shim embeds the absolute path of the environment that
   created it — again silently.

The common failure is not a missing feature; it is **misplaced confidence**.
Every one of these machines was, by `doctor`'s own account, healthy.

## Decision

`doctor` gains two checks, and both are governed by one rule: *report only
what was verified, name what was not.*

### 1. `rekoll` — install identity (#104)

The existing version line becomes a real check:

- Enumerate every `rekoll` / `rekoll-mcp` executable on PATH (in PATH order),
  not just `shutil.which`'s winner — the question is how many *different*
  answers exist.
- Determine each one's version **by reading files, never by executing them**.
  Running a stranger's binary that merely happens to be named `rekoll`, just
  to ask its version, would turn a diagnostic into an arbitrary-code-execution
  path. A `_version.py` read is sufficient and cannot be escalated. A test
  pins this: `subprocess` is monkeypatched to raise, and the check must still
  work.
- **WARN** when a readable version disagrees with the running one, naming both
  paths, both versions, and the fix. Offenders are listed bounded (3 + "and N
  more"), per ADR-0040's rendering rule.
- **WARN** when the `rekoll` PATH would pick is not this installation.
- Editable installs (`pip install -e`) are identified and **excluded from
  disagreement**: their recorded version is the install-time one and goes
  stale the moment the source is bumped (a real checkout reads `0.0.0` against
  a `0.1.3` source). Alarming on that would cry wolf at every developer on
  every run — a false alarm is the same disease as a false all-clear.
- When other copies exist but could not be read, the line says so
  (`N could not be read`) rather than implying agreement it never verified.

### 2. `mcp` — registration reality (#84)

Reported **only when a project-local registration exists** (`.mcp.json`,
`.cursor/mcp.json`, `.vscode/mcp.json`) — CLI-only users must never be nagged
about a door they never opened, and an entry that registers somebody else's
server is ignored.

- **WARN** when a config exists but is not valid JSON: no client can read it,
  so no server starts — the same silence with an earlier cause.
- **WARN** when the registered `command` does not resolve, or a pinned
  `--path` does not exist. This is the rename incident, twice.
- **WARN** when the registration is sound but **nothing in this scope has ever
  been written through the MCP door**. This is the only signal that separates
  "configured and working" from "configured and never loaded" — config checks
  cannot see it, because in the reported incident the config was correct. The
  message gives the actionable step ("ask it to list its tools") and says
  plainly that it is harmless for CLI-only use.

MCP-origin writes are identifiable because `mcp_server._remember` passes
`source="mcp"`, which lands in `provenance.source_uri`.

### 3. A targeted provenance count, not a recency window

The first implementation of the check above inferred "never loaded" from
whether any of the 50 most recent memories came from MCP. Attacking this
ADR's own branch showed that is **measurably wrong**: a scope with one MCP
write followed by 60 CLI writes reported *"may never have loaded"* about a
door that demonstrably had loaded. A check whose entire premise is "do not
claim what you have not verified" cannot ship with that at its centre.

So the storage contract gains one optional read,
`StorageAdapter.count_by_source(scope, source_uri) -> int` — concrete with a
raising default, effective-status gated, exactly the ADR-0040
`scope_counts()` precedent. It answers the question exactly. The recency
window survives only as a **degraded fallback** for adapters that cannot serve
it, and in that case the sentence weakens itself to "none of the 50 most
recent" instead of "ever". Both wordings are pinned by tests.

### 4. What the check asks about must be what the user actually runs

Two blockers found by adversarially reviewing this ADR's own branch, both of
which made doctor cry wolf on a correct setup — the mirror image of the
disease, and just as corrosive, because a warning users learn to ignore is
worse than no warning:

- **The running environment was derived by guessing directory layouts** and
  landed one level short on both Windows and POSIX, so *every* ordinary
  `pip`/`pipx` install compared an environment against itself and warned that
  "a different install answers". It now asks the interpreter (`sys.prefix`)
  instead of pattern-matching paths.
- **The MCP check queried the CLI's scope**, not the server's. Those defaults
  differ — that is issue #83's entire subject — so with the *documented*
  `.mcp.json` (which pins no `--project`), doctor announced that nothing had
  ever come through the MCP door seconds after a successful MCP write, and
  sent the user off to restart a working client. It now derives the store path
  and scope the registered server would actually use, reusing
  `mcp_server._derived_project` so there is one definition of that rule, and
  it names the scope in the message.

Two more false alarms went with them: a pinned `--path` that does not exist
yet is **not** a failure (the server creates its store on first write, like
`rekoll init`, so every fresh clone would have been told its server "cannot
start"), and client-side variable syntax such as VS Code's
`${workspaceFolder}` cannot be resolved by us, so it is reported as
unverified rather than broken.

## Alternatives rejected

- **Executing other `rekoll` binaries to read their version.** Precise, and an
  RCE footgun in a tool people run when something is already wrong.
- **A `source_counts()` census** mirroring ADR-0040's `scope_counts()`:
  dropped because `source_uri` is a file path for ingested content, so the map
  is unbounded on a real repo — it would have violated the bounded-read
  discipline it was modelled on. The targeted single-source count in §3 is
  bounded by construction and answers the only question actually being asked.
- **Scanning the user's home directory for editor configs.** Unbounded,
  unreliable, and outside the project the operator is standing in.
- **FAIL instead of WARN.** Nothing here is broken or lost; the operator is
  misinformed. Warn-don't-restrict: say it clearly, never block.

## Consequences

- Both incidents are now impossible to hit silently: the shadowed install and
  the never-loaded server each produce a WARN naming the fix.
- `doctor`'s exit code is unchanged (WARN keeps exit 0), so scripts gating on
  it do not change behavior.
- Developers working from a checkout see no new noise (editable installs are
  recognized), which was an explicit design constraint.
- The install check does filesystem work proportional to PATH length; it is
  fail-soft at every step, and a PATH inspection error degrades to the plain
  identity line rather than failing `doctor`.
