# ADR-0045 — the `mcp` SDK major boundary: pin now, port deliberately

**Status:** Accepted · **Date:** 2026-07-28 · **Implements:** issue #114 · **Follows:** ADR-0008 (MCP as door 1), ADR-0041 (report only what you verified) · **Interacts with:** ADR-0013 (envelope byte-identity), ADR-0044 (stored content cannot drive the terminal)

## Context

`mcp` 2.0.0 was published on 2026-07-28. `pyproject.toml` declared the extra as
`mcp = ["mcp>=1.3"]` — **no upper bound** — so every fresh install resolved to
2.0.0 within the hour. 2.0.0 removed `mcp.server.fastmcp`, the exact module
`build_server` lazy-imports.

Two things then failed at once, and only one of them was the version break.

**The door closed.** Verified against the shipped 0.1.4 wheel in a clean venv:

```
$ pip install "rekoll[mcp]"        # rekoll 0.1.4, mcp 2.0.0
$ rekoll-mcp
The Rekoll MCP server needs the optional 'mcp' extra.
Install it with:  pip install "rekoll[mcp]"
```

`pipx install "rekoll[mcp]"` is the first command in the Quickstart and MCP is
the door the README calls the vibe-coder default. The CLI and SDK were
unaffected. In the repo, 23 tests went red — the three-doors parity suite and
both MCP suites — all with `MCPError: Connection closed`, because the server
subprocess died at startup.

**And the error blamed the user.** The guard caught *any* `ImportError` out of
`mcp.server.fastmcp` and re-raised it as the install hint, so a user who had
just installed the extra was told to install the extra. This is the same
failure ADR-0041 was written about — a component reporting a state it had not
verified — shipped in the server rather than in `doctor`.

The matrix did not catch the drift because `test-mcp` has a floor cell
(`mcp==1.3.0`, which kept passing) and **no ceiling cell**. The pin drifted
silently upward until the day it broke.

## Decision

### 1. Bound the extra at the break: `mcp>=1.3,<2`

The ceiling follows the **actual break boundary**, not a tested maximum. A
pinned known-good ceiling (`<=1.29`) would freeze users out of 1.x point
releases that carry security fixes, and would need a PR every time upstream
ships a patch — a maintenance tax paid to prevent a break that has not
happened. `<2` is the honest statement of what is known: every 1.x we have
tested works, 2.x does not.

The floor is untouched. `mcp==1.3.0` remains a supported, CI-pinned
configuration: `FastMCP(instructions=...)` and `InitializeResult.instructions`
first exist there, and a real user on an old client depends on it.

### 2. The failure message distinguishes the two cases

`build_server` now asks whether an `mcp` is installed at all before choosing
its message:

- **No `mcp` installed** — the install hint, unchanged.
- **An `mcp` IS installed** — name its version, name the constraint it
  violates, quote the real `ImportError`, and give a command that fixes it for
  both pip and pipx.

The probe reads packaging metadata and falls back to `find_spec`. **Neither
imports `mcp`**, so it cannot run third-party module-level code, and the
`import rekoll` invariant is untouched.

Version parsing is a nine-line numeric-prefix comparison rather than a
dependency: rekoll has zero runtime dependencies, and the only question asked
is which side of 1.3 and 2.0 a release sits on. It returns **three** states —
supported, unsupported, and "could not tell" — because an unparseable version
must not be guessed at in either direction.

### 3. `doctor` reports the SDK, and says only what it checked

`doctor` reported MCP *registration* (ADR-0041 §2) while saying nothing about
whether the server could *start*, so it printed a clean bill of health for
precisely the machine state that broke every new install. It now carries an
`mcp sdk` line.

Two constraints shape it:

- **It never imports `mcp`.** ADR-0041 §1 refuses to execute a stranger's
  `rekoll` binary to ask its version, because that turns a diagnostic into a
  code path. Importing a package is a weaker version of the same act — it runs
  module-level code — and the distinction matters enough to write down: an
  import inside doctor's own process is *not* the same thing as executing a
  configured command, but it is on the same spectrum, and a version read
  answers the question without stepping onto it at all.
- **It claims only the range.** A metadata read establishes that the installed
  release is inside or outside `mcp>=1.3,<2`. It does **not** establish that
  the server will start, so the `ok` text does not say so. Only starting the
  server could establish that, and doctor does not start servers.

The line is **suppressed entirely when no `mcp` is installed** — that user
never opened this door, and ADR-0041's rule against nagging CLI-only users
applies here for the same reason it applies to the registration check.

### 4. 2.x is a real port, but a small and mechanical one — and it is not this PR

This was measured, not estimated. A throwaway spike on this branch, run
against `mcp==2.0.0`:

| Change | Size |
| --- | --- |
| `from mcp.server.fastmcp import FastMCP` -> `from mcp.server.mcpserver import MCPServer` | 1 import line |
| `MCPServer(name, instructions=...)`, `@server.tool()`, `server.run(transport="stdio")` | **unchanged** — all three survive |
| Test harness: `result.isError`, `result.structuredContent`, `tool.inputSchema` | 3 accessors, camelCase -> snake_case |

With those, **all three MCP suites pass on 2.0.0** — including the three-doors
byte-parity suite and the board byte-identity test. The frozen surfaces hold:
payloads are byte-identical across the major boundary.

Two findings shape the recommendation:

- **The structured-output change is not a regression for rekoll.** 2.x refuses
  a bare `-> dict` tool as structured output (`InvalidSignature`) and returns
  `structured_content=None` by default. But rekoll's six tools already emit no
  `structuredContent` on 1.29 either — verified over the real stdio wire; they
  ride the JSON text block. There is nothing to lose here, only something not
  yet gained.
- **The dependency footprint is the real cost.** 2.x pulls `httpx2`,
  `mcp-types`, `opentelemetry-api`, `pyjwt[crypto]`, `starlette`, `uvicorn`,
  `sse-starlette`, and `python-multipart`. For a package whose stated posture
  is zero runtime dependencies with everything heavy behind an extra, that is
  a supply-chain decision on its own merits — not a side effect of a rename.

So the port is small, but supporting **both** majors in one codebase means
dual-import shims and camelCase/snake_case tolerance in the harness, and it
needs a CI cell proving the floor and the ceiling simultaneously before anyone
can believe it. That is a lane, not a hotfix, and the pin must not wait behind
it: v0.1.5 is blocked on the door, not on the port.

**Verdict: 2.x support gets its own issue, carrying the table above.**

### 5. The matrix grows a ceiling

A floor-only matrix can only catch regressions *downward*. The `test-mcp`
matrix gains an explicit **latest** cell that installs an unpinned `mcp` and is
allowed to fail loudly, so the next major announces itself in CI instead of in
a user's first five minutes. The workflow is conductor-owned; the diff is in
the PR body.

## Consequences

- Installs work again. The pin is the whole fix for the outage.
- The server can no longer tell a user to install what they already have.
- `doctor` explains a broken MCP door instead of printing nine green checks
  next to one.
- Rekoll is pinned to 1.x until the port lane runs. `mcp>=1.3,<2` and
  `_MCP_REQUIREMENT` in `src/rekoll/mcp_server.py` must stay byte-identical —
  the server quotes the constraint to users as fact, so a drift between them
  would be a new instance of the lie this ADR closes. `tests/test_mcp_compat.py`
  pins the pair against `pyproject.toml`.
- Both failure branches are pinned by **simulated** SDK state, not by whichever
  `mcp` the runner resolves, so they hold identically in the no-mcp job, at the
  1.3.0 floor cell, and on a box with 2.x installed.
