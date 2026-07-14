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
| `recall` | Search memory (semantic + keyword, local, no LLM). Returns `context` (a safe block to read as data), `ids` (record ids in rank order), `count`, `mode` (see below), plus the abstain envelope `abstained` and `top_vector_score` (see below). Takes an optional `min_score`. |
| `ingest_path` | Index a file or folder (code + docs) — only inside the project root. Returns `files`, `chunks`, `total`, plus `skipped` (tried and passed over) and `filtered` (names excluded unread: vendored venvs, lockfiles, credential-shaped names), plus `secrets_skipped` (credential-shaped files the walk excluded) and `secrets_stored` (credential-shaped files ingested anyway — see below). Counts only, never names. |
| `forget` | Delete memories by id (up to 256 per call). |
| `status` | Show the store location, scope, recallable memory count, write-trust policy, embedder, and `mode`. (Quarantined-for-audit rows are never counted or otherwise surfaced here.) |

### `mode` — telling a degraded index from a healthy one

`recall` and `status` both return `mode`, the honest-degradation string
(ADR-0024). It names the retrieval pipeline that actually **ran**:

| Value | What it means |
| --- | --- |
| `vector+lexical+rerank` | Full hybrid ranking. Trust the order. |
| `vector+lexical (stub-embedder)` | No real semantics installed (`pip install "rekoll[embeddings]"`). |
| `lexical-only: embedder mismatch` | The embedding model changed, so the vector leg is **refused**. Hits are keyword-ranked — trust their *order* less. Recover with `Memory.reindex()`. |

This matters because a degraded read returns hits of the **same shape** as a
healthy one, just ranked worse — and `embedder` names the embedder the server is
*holding*, which a mismatch leaves unchanged (it is the *stored* identity that
differs). Without `mode`, a calling agent cannot tell the two apart.

`mode` rides beside the context block, never inside it: the envelope stays a pure
function of the hits, so a degradation notice can't bust an agent's prompt cache.

### `abstained` — an honest "I don't know" (optional `min_score`)

Pass `recall` a `min_score` (a vector-cosine floor in [-1, 1]) and the store will
**abstain** rather than return confident-looking hits for a question it cannot
answer: if the closest memory is not similar enough, `recall` returns zero hits
with `abstained: true` and a `mode` that names the gate. `abstained` is always
present (`false` on an ordinary recall), and `top_vector_score` reports the
top-1 cosine the gate compared against — the number to calibrate a threshold
from. An abstain (zero hits, `abstained: true`) is **not** an empty store: treat
it as "not sure", not "nothing here" (ADR-0028).

### `secrets_stored` — a credential was indexed

`ingest_path` will not walk into a credential-shaped file (`.env`,
`credentials.json`, a private key) — those are counted in `secrets_skipped`. But
pointing it **straight at** such a file bypasses the filter (explicit intent), and
then `secrets_stored` is nonzero. If it is, a secret is now a recallable,
embedded, exportable memory: **surface that to the user, do not act on the file's
contents, and offer to `forget` those records.** A nonzero value you did not
intend is exactly what an injected "index ./.env" instruction produces (ADR-0027).

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
| `--redact-pii` | `REKOLL_MCP_REDACT_PII` | off | Redact emails / US SSNs / phone numbers from every write (secrets are always redacted) |

> **`--redact-pii` is operator-only and not retroactive.** Like trust, it is
> fixed at launch and appears in no tool schema, so the calling model can never
> enable or disable it. It scrubs writes made *after* it is turned on — PII
> already in the store stays there, and re-ingesting the same source stores a
> *second*, differently-addressed record instead of replacing the original (ids
> are content-addressed after screening). Turn it on **before** you first index
> PII-bearing content. Emails/SSNs/phone only; the audit trail keeps a class
> label, never the value.

Example — pin the project name and allow ingesting a sibling docs folder:

```bash
claude mcp add rekoll -- rekoll-mcp --project myapp --root ..
```

Example — redact PII from every write (a support-ticket or CRM corpus):

```bash
claude mcp add rekoll -- rekoll-mcp --redact-pii
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
