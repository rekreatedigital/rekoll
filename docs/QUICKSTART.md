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

- `rekoll remember --kind directive "always use tabs"` — kinds are
  `raw_fact` (default), `observation`, `directive`, `episode`.
- `rekoll ingest vendor/ --trust unverified` — indexing content you didn't
  write (vendored code, scraped docs, other people's notes)? Lower its trust
  so the injection firewall treats it as untrusted; the default is `owner`,
  meant for your own files.
- `--project`, `--tenant`, `--agent` — keep separate memory spaces in one store.

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
mem.forget(*best.ids())                         # delete by id
```

Everything the CLI does goes through this same `Memory` class, and the defaults
match — so `Memory()` sees exactly what `rekoll remember` stored. Constructor
knobs you'll actually use: `path=` (where the SQLite file lives),
`project=`/`tenant=`/`agent=` (separate memory spaces; pair with the CLI's
`--project` etc. if you use both), `trust=`/`kind=` per call on `remember`.

## Door 3: AI agents via MCP — coming

An MCP server (one command in Claude Code / Cursor / Windsurf, no Python
required) is planned but **not shipped yet** — see the roadmap in
[DESIGN.md](DESIGN.md). Until then, agents can shell out to the CLI: give your
agent a tool that runs `rekoll recall "<question>" --context` and paste the
output into its context.

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
