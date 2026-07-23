# Quickstart — pick your door

Rekoll gives your project a private, durable memory that AI tools can use but
can't be tricked by. Everything below runs on your machine: **no API key,
nothing leaves your box** — semantic mode downloads its small model once at
first use, then saving and searching work fully offline.

Rekoll isn't on PyPI yet. Until it is, install from git:

```bash
pip install "rekoll[embeddings] @ git+https://github.com/ryankyleocampo-github/rekoll"
```

That gives you real semantic search (a small local model, downloaded once).
If you leave off `[embeddings]` you get a zero-dependency install with basic
keyword search — fine for trying things out. Choose **before** you store
memories; switching search modes later means re-ingesting.

---

## Door 1: any repo, via the CLI (no Python code)

Works for a website, a mobile app, a data project — anything in a folder.

```bash
cd your-project
rekoll init
```

`init` creates `./.rekoll/` (the memory store), adds it to your `.gitignore`,
and tells you in plain language whether you're in semantic or keyword mode.
It's safe to run twice.

Prefer a guided start? `rekoll init --wizard` adds a short optional interview
(three questions, Enter skips any): how AI tools should explain things to you,
what every session should know about you or this project, and your preferred
tone. Nothing is stored until you confirm once at the end — each answer then
becomes a **standing rule** that every AI session using this store is told to
follow, on every recall, until you remove it (only your *oldest five* rules
ride each recall). The wizard prints each rule's id when it saves;
`rekoll forget <id>` removes one. Re-running the wizard *adds* rules
(identical answers are stored only once) — it never edits old ones, and older
rules win the five-rule limit, so remove the outdated rule rather than piling
up replacements. Plain `rekoll init` never asks anything, so scripts and CI
stay non-interactive.

Then, day to day:

```bash
rekoll remember "we chose Postgres over BigQuery for cost"   # save one fact
rekoll recall "why postgres?"                                # search by meaning
rekoll ingest .                                              # index the whole repo (code + docs)
rekoll status                                                # what's stored, and how
rekoll doctor                                                # something wrong? start here
```

Useful flags:

- `rekoll recall "query" --context` — prints a safe, LLM-ready block. Paste it
  into ChatGPT/Claude/Cursor, or pipe it into your own prompts. Retrieved
  memory is framed as *data*, never as instructions — that's the injection
  firewall working.
- `rekoll recall "query" --ids` + `rekoll forget <id>` — delete things:

  ```bash
  rekoll forget $(rekoll recall "old decision" --ids)
  ```

- `rekoll recall "query" --json` — one JSON object for scripts and agents:
  `{context, directives, ids, mode, count, abstained, top_vector_score}` (the same
  shape the MCP `recall` tool returns). `directives` is your project's **standing
  rules** — the always-on instructions returned on *every* recall, whatever you
  searched for, so a saved rule never vanishes just because it didn't rank in
  (ADR-0034). `mode` names the search that actually ran — `vector+lexical+rerank`
  is a full hybrid ranking, while `lexical-only: embedder mismatch` means the
  semantic leg is switched off and you should trust the *order* less. It still
  prints the object (and still exits `1`, like `grep`) when nothing matched, so a
  script can always read `mode`. Run `rekoll doctor` if `mode` surprises you.

  ```bash
  rekoll recall "why postgres" --json | python -c "import json,sys; d=json.load(sys.stdin); print(d['mode'], d['ids'])"
  ```

- `rekoll remember --kind directive "always use tabs"` — kinds are
  `raw_fact` (default), `observation`, `directive`, `episode`. A directive is a
  **standing rule**, not an ordinary memory: every AI session that uses this
  memory store is told to follow it, automatically, on every recall, until you
  `rekoll forget` it. Because that is the most powerful thing you can store,
  the CLI prints a warning first and — when you're at a terminal — asks you to
  confirm (`y/N`; answering no stores nothing and exits `1`). In scripts, pass
  `--yes` (or `-y`) to skip the question; the warning still prints, and a
  script with no terminal proceeds with the warning rather than hanging —
  loud, never locked. Storing a directive at `--trust unverified` skips the
  question entirely: it stays below the trust floor, so it is kept as plain
  data and never applied as a rule (ADR-0017). Re-typing an *existing* rule at
  a lower trust does not demote it — trust never silently falls (ADR-0023), and
  the CLI tells you the rule is still active; to actually remove a rule, use
  `rekoll forget`.
- Ingested files are screened at `unverified` trust by default — the firewall
  treats them as content you didn't write, which is right for vendored code,
  scraped docs, and other people's notes. If a folder is entirely your own
  work, vouch for it explicitly: `rekoll ingest . --trust owner`.
- `--project`, `--tenant`, `--agent` — keep separate memory spaces in one store.
- `rekoll remember "..." --redact-pii` / `rekoll ingest . --redact-pii` — also
  redact emails, US SSNs, and phone numbers before storing (off by default, so a
  normal code ingest keeps author emails intact; secrets are always redacted
  regardless). **It is not retroactive:** it only scrubs *new* writes, and
  re-ingesting the same source afterwards stores a *second*, differently-addressed
  record rather than replacing the original (ids are content-addressed after
  screening). Turn it on before you first index PII-bearing content, and the audit
  trail keeps only a class label (`email`), never the value.

For scripts: results go to stdout, messages to stderr; exit code `0` = success,
`1` = nothing found / operational problem, `2` = bad usage. `recall` exits `1`
when there are no matches, like `grep`.

## Door 2: a Python app, via the SDK

```python
from rekoll import Memory

mem = Memory()                    # local SQLite at ./.rekoll/memory.db, firewall on

mem.remember("we chose Postgres over BigQuery for cost")
mem.ingest_path("docs/")                        # chunk + index files
best = mem.recall("why postgres?", k=3)         # ranked hits
print(best.texts()[0])                          # plain strings
print(best.context())                           # safe, LLM-ready envelope
print(best.directives())                        # standing rules that always apply
mem.forget(*best.ids())                         # delete by id
```

Everything the CLI does goes through this same `Memory` class, and the defaults
match — so `Memory()` sees exactly what `rekoll remember` stored. Constructor
knobs you'll actually use: `path=` (where the SQLite file lives),
`project=`/`tenant=`/`agent=` (separate memory spaces; pair with the CLI's
`--project` etc. if you use both), `trust=`/`kind=` per call on `remember`, and
`redact_pii=True` to scrub emails/SSNs/phone from stored **content** (off by
default; content only — not file paths or `source`/`metadata` labels, so don't put
PII there; and **not retroactive** — enable it before you first store PII-bearing
content, since turning it on later leaves already-stored PII in place and
re-ingesting a source creates a second, differently-addressed record instead of
replacing the first).

## Door 3: AI agents via MCP

Rekoll ships an MCP server — any MCP-capable agent (Claude Code, Cursor,
Windsurf, …) can use this project's memory, no Python code to write:

```bash
pip install "rekoll[mcp] @ git+https://github.com/ryankyleocampo-github/rekoll"   # or: pip install -e "/path/to/rekoll[mcp]"
claude mcp add rekoll -- rekoll-mcp   # Claude Code; other clients: see MCP.md
```

The agent gets five tools (`remember`, `recall`, `ingest_path`, `forget`,
`status`) over this project's store, with scope and trust pinned server-side.
Setup for Cursor + generic clients, the trust model, and all knobs:
**[MCP.md](MCP.md)**. Only the no-Python Node/`npx` wrapper is still planned —
today the server needs a Python environment. (An agent without MCP support can
still shell out to the CLI: `rekoll recall "<question>" --context`.)

---

## Common questions

- **Do I need an AI/API key?** No. Saving and searching use a small local
  model (or plain keyword matching) — free, offline, private.
- **Where is my data?** In `./.rekoll/memory.db`, a normal SQLite file you can
  inspect, back up, or delete. It never leaves your machine.
- **Should I commit `.rekoll/`?** No — keep it out of git; `rekoll init`
  git-ignores it for you. Ingested files can be re-indexed anytime, but facts
  you `remember` live only in this store — back it up if they matter.
- **Keyword vs semantic?** Keyword mode matches words; semantic mode matches
  meaning ("why postgres?" finds "we chose Postgres over BigQuery"). Get
  semantic by reinstalling with the `[embeddings]` extra (the install line at
  the top), ideally before you store anything — `rekoll doctor` shows your
  current mode.
- **Something's broken.** `rekoll doctor` — it checks Python, the search
  extras, storage, and the firewall, in plain language.
