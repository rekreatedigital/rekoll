# Field report: first external-user onboarding audit (2026-07-24)

> **Who:** a Claude Code conductor session working in a *different* repo (rekreate-codex),
> instructed by RK to act as a cold outside user: land on rekoll.dev, follow only what
> the site says, adopt Rekoll for real in a production project, and report honestly.
> **Environment:** Windows 11 · Python 3.12.6 · pip 24.3.1 · global interpreter (no venv)
> · corporate-style TLS-intercepting network · Claude Code running as the **VS Code
> extension** (no `claude` CLI on PATH — this matters, see finding 2).
> **Outcome:** adopted for real — Rekoll now runs inside rekreate-codex (`.rekoll/`
> store, 5 memories, project `.mcp.json` registering `rekoll-mcp`).

## Verdict

**The onboarding is genuinely smooth.** Cold landing on rekoll.dev → working semantic
recall inside a real production repo in under five minutes, zero troubleshooting.
The single-command quickstart is accurate; nothing on the happy path lied. That is
rarer than it should be.

## What was executed, verbatim

```bash
pip install "rekoll[embeddings] @ git+https://github.com/rekreatedigital/rekoll"   # OK, ~30s
rekoll init                                                                        # OK output, BUT see finding 1
rekoll remember "..." (×5, real project facts)                                     # OK, exit 0
rekoll recall "how do we make sure we don't lose data if the server dies?"         # correct #1 hit
rekoll recall "what's blocking <a specific in-progress feature>?"                  # correct #1 hit
```

Both recall queries were deliberate paraphrases with **zero keyword overlap** with the
stored text ("lose data if the server dies" vs. a stored memory describing the
project's offsite-backup procedure in infrastructure terms — no shared words). Both
ranked the right memory first. Semantic retrieval is real, not keyword luck. Recall
output rendered the trust envelope as promised: `(raw_fact | trust: owner | id: rk_…)`.
*(The verbatim project memories are redacted here — this file is public; the adopting
repo is not.)*

## Findings

### 1. 🐛 BUG — `rekoll init` exits non-zero on Windows despite succeeding
`rekoll init` printed the full success output, created `.rekoll/`, and patched
`.gitignore` — then the process exit code was **-1** (PowerShell `$LASTEXITCODE`).
`remember`/`recall` exit 0 correctly, so it's init-specific. Consequence: the site's
own one-liner `pip install … && rekoll init` *worked*, but anything chained AFTER init
(`rekoll init && rekoll ingest .` in a script or CI) aborts spuriously on Windows.
Suspect: whatever init does last on the Windows path (console handling? a spawned
process's code propagating?). Repro is deterministic on this machine.

### 2. 📄 DOCS — the MCP hint assumes the `claude` CLI exists
The site suggests `claude mcp add rekoll -- rekoll-mcp`. On a machine where Claude Code
runs as the **VS Code extension**, there is no `claude` on PATH — the command dies with
CommandNotFound, and a newcomer hits their first dead end exactly at the "connect it to
your agent" step (the emotional payoff moment). The portable alternative worked
first-try and is arguably better docs-material because it's agent-config-as-code:

```json
// .mcp.json in the project root — Claude Code picks it up automatically
{ "mcpServers": { "rekoll": { "command": "rekoll-mcp", "args": [] } } }
```

Suggest: show both paths on the site, `.mcp.json` first. (`rekoll-mcp.exe` was on PATH
immediately after pip install — the entry point itself is fine.)

### 3. 📄 DOCS — quickstart installs into global Python, with a real side effect
Following the site verbatim (no venv) upgraded a globally shared dependency
(`click` 8.1.8 → 8.4.2) as a side effect. Nothing broke here, but polluting the global
interpreter is the classic way Python tools earn angry issues. One line on the site
("recommend `pipx install` or a venv") prevents it. Alternatively make `pipx` the
headline command — Rekoll is a CLI-first tool, it's the natural fit.

### 4. 🔎 UNTESTED — relevance cutoff behavior at tiny corpus size
With only 3–5 memories stored, every recall returned the entire store (ranked). Fine —
but a new user's very first experience is exactly this corpus size, and "it returned
everything" can read as "search doesn't filter." Worth checking what a *wildly*
irrelevant query returns at n=3 (e.g. "what's the capital of France") — if the answer
is "all three memories, confidently," consider a score floor or a rendered relevance
hint at small n.

### 5. 👏 What's genuinely good (keep these)
- **`[embeddings]` = fastembed/ONNX, not torch.** Install was ~30s and light. The
  competing tools ship multi-GB downloads for the same first-run moment. Major
  differentiator for onboarding — consider bragging about it on the site.
- **`init` auto-gitignores `.rekoll/`.** Right default, silently prevents the
  "committed my private memory" incident every alternative invites.
- **The init copy is excellent trust-writing** — "a download, not an upload; after
  that, fully offline" answers the privacy objection at the exact moment it forms.
- **TLS-intercepting network: zero issues.** git-over-HTTPS install + model download
  both survived a corporate-style MITM proxy that regularly breaks Python tooling.
- **Trust envelope on every recall** (`raw_fact | trust: owner`) — visible, compact,
  and it matches the injection-firewall story the site sells.

## Coexistence note (integration pattern others will copy)

The adopting project already had a curated file-based memory layer (Claude Code
auto-memory: an index + topic files loaded at session start). Rekoll slotted in beside
it with a clean division of labor, no conflicts: **files = small curated state that
every session auto-loads; Rekoll = deep searchable recall queried on demand.** The
index file now cross-references the Rekoll store. If Rekoll documents one "works with
your agent's existing memory" pattern, this is it.

## Suggested priority

1. Fix the Windows init exit code (it's the only thing on the happy path that's wrong).
2. Add `.mcp.json` docs next to the `claude mcp add` hint.
3. One venv/pipx line on the site.
4. Investigate small-corpus relevance floor.
