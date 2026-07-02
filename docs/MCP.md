# Use Rekoll from any agent (MCP)

Rekoll ships an MCP server, so **any MCP-capable agent** — Claude Code, Cursor,
Windsurf, OpenClaw, your own — can use it as project memory in **any repo**,
with no Python code to write. The agent gets five plain tools; everything runs
locally; nothing needs an API key.

## 1. Install

```bash
pip install "rekoll[mcp]"                 # once Rekoll is on PyPI
pip install -e "/path/to/rekoll[mcp]"     # today, from a clone of this repo
```

That gives you a `rekoll-mcp` command (a stdio MCP server).
Add `.rekoll/` to your project's `.gitignore` — the memory store lives there.

> A `npx rekoll-mcp` wrapper (no Python needed at all) is planned — see
> ADR-0008. Today the server needs a Python 3.10+ environment.

## 2. Connect your agent

**Claude Code** — run this inside your project:

```bash
claude mcp add rekoll -- rekoll-mcp
```

**Cursor** — add to `.cursor/mcp.json` in your project (or the global one):

```json
{
  "mcpServers": {
    "rekoll": { "command": "rekoll-mcp" }
  }
}
```

**Any other MCP client** — configure a stdio server whose command is
`rekoll-mcp` (no arguments needed). Launch it with your project directory as
the working directory; that's how it knows which project's memory to open.

> If the client can't find `rekoll-mcp`, use the full path to it (e.g.
> `.venv/bin/rekoll-mcp` or `.venv\Scripts\rekoll-mcp.exe`), or
> `python -m rekoll.mcp_server`.

## 3. What the agent can do

| Tool | What it does |
| --- | --- |
| `remember` | Save one memory (a fact, decision, or event). Screened by the injection firewall first. |
| `recall` | Search memory (semantic + keyword, local, no LLM). Returns a safe context block + record ids. |
| `ingest_path` | Index a file or folder (code + docs) — only inside the project root. |
| `forget` | Delete memories by id (up to 256 per call). |
| `status` | Show the store location, scope, memory count, and write-trust policy. |

## 4. The trust model, in one paragraph

Everything an MCP tool receives comes from a model — and that model may itself
be reading attacker-controlled content (a poisoned README, a malicious issue).
So the server decides the security-critical values itself, at launch, and the
model can never change them: **scope** (which project's memory) is pinned from
server config, **every write is stamped `unverified` trust** (never
owner/curated — those are reserved for humans), **directives** — the one memory
kind that carries instruction weight — **cannot be written over MCP at all**,
and `ingest_path` refuses anything outside the project root. At the default
`unverified` trust, content that looks like prompt injection is quarantined on
write and never comes back out; what `recall` returns is wrapped in a data
envelope that the calling agent is told to treat as reference, not instructions.

If you knowingly want MCP-written memories to rank as team-vetted input, you can
raise the stamp to `trusted_source` in server config (`--trust trusted_source`)
— that's the ceiling, and it's your call as the human operator, never the
model's. **Be aware of the trade-off:** injection quarantine only fires at trust
`unverified` or below, so raising the write tier to `trusted_source`
**disables quarantine** for MCP writes — flagged content is then stored and
recallable. That's the point of vouching for a source, but only do it for a
model whose inputs you trust. The `recall` data envelope still applies at every
tier, so recalled content is never fed back to the agent as instructions.

## 5. Configuration (all optional)

Set by flag or environment variable — flags win. The calling model can't touch
any of these; that's the point.

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--path` | `REKOLL_MCP_PATH` | `./.rekoll/memory.db` | Where the store lives |
| `--project` | `REKOLL_MCP_PROJECT` | launch folder's name | Scope: which project's memory |
| `--tenant` | `REKOLL_MCP_TENANT` | `default` | Scope: tenant |
| `--agent` | `REKOLL_MCP_AGENT` | `default` | Scope: agent |
| `--trust` | `REKOLL_MCP_TRUST` | `unverified` | Trust stamped on MCP writes (`unverified` or `trusted_source`) |
| `--root` | `REKOLL_MCP_ROOT` | launch directory | The only directory `ingest_path` may read |

Example — pin the project name and allow ingesting a sibling docs folder:

```bash
claude mcp add rekoll -- rekoll-mcp --project myapp --root ..
```

## 6. Troubleshooting

- **"The Rekoll MCP server needs the optional 'mcp' extra"** — install it:
  `pip install "rekoll[mcp]"`.
- **`rekoll-mcp` not found** — it's installed into the Python environment you
  ran pip in; activate that environment, or point your client at the full path.
- **Recall quality feels keyword-only** — install real local embeddings too:
  `pip install "rekoll[mcp,embeddings]"` (first run downloads a small model,
  then it's fully offline).
- **Two agents, one repo** — they share memory by default (same store, same
  scope). Give each its own `--agent` name to separate them.
