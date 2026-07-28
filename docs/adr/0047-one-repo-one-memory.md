# ADR-0047 — One repo, one memory: `init` pins the MCP door's scope

**Status:** Accepted · **Date:** 2026-07-29 · **Implements:** issue #83 (the
design half) · **Builds on:** ADR-0040 (the loud scope-split detector) ·
**Interacts with:** ADR-0035 §6 (no discovery in v1 — see §3), ADR-0036 (plain
`init` asks nothing), ADR-0037 §2 (relative paths for anything inside the repo),
ADR-0041 (`doctor` vouches for what it reports; and does not nag CLI-only
users), ADR-0042 §2 (the ingest root is normative for teams)

## Context

Rekoll has three doors onto one store, and two of them default to a **different
project scope in the same folder**:

| Door | Default project | Where |
| --- | --- | --- |
| CLI | `"default"` | `cli.py`, the shared `--project` argparse default |
| SDK | `"default"` | `Memory(project="default")` |
| MCP server | **the launch folder's NAME** | `mcp_server._derived_project` |

So in a repo called `my-cool-app`, an agent writing through MCP lands in
`default/my-cool-app/default` while `rekoll recall` reads
`default/default/default`. Same file, two compartments, no error — scope
isolation working exactly as designed, on a scope nobody chose.

Reproduced by the conductor, and by three independent field reports (#82, #101,
plus the maintainers' own repo). ADR-0040 made it **loud** in v0.1.3: `status`,
bare `recall` and `doctor` now name the other scope and print the command that
reads it. That fixed the silence. It did not fix the split: every new user who
follows the quickstart's `.mcp.json` —

```json
{ "mcpServers": { "rekoll": { "command": "rekoll-mcp", "args": [] } } }
```

— still falls in, and still has to repair it by hand. #101's tester reached 110
memories in five minutes and then hit this; it is the one sharp edge on an
otherwise clean onboarding.

Two things had blocked the fix for two waves:

1. **Changing either default re-scopes existing stores into invisibility** —
   this bug, inflicted deliberately, on people who already have memories.
2. **ADR-0035 §6 bans every discovery mechanism for v1** in as many words, so a
   `.rekoll/scope.json` that *Rekoll* reads needs a superseding ADR plus a
   threat analysis.

## Decision

**`rekoll init` writes the MCP *client's* config with this project's scope
pinned explicitly.** Nothing about how Rekoll resolves scope changes at any
door; what changes is that the file the user was going to write by hand now
gets written correctly.

```json
{
  "mcpServers": {
    "rekoll": {
      "command": "rekoll-mcp",
      "args": ["--tenant", "default", "--project", "default", "--agent", "default"]
    }
  }
}
```

The pinned scope is **the scope `init` itself operated in** — the shared
`--tenant/--project/--agent` args, i.e. `default/default/default` unless the
operator said otherwise. That is what makes all three doors agree, and it needs
no change to the CLI, the SDK or the model layer (§5).

### 1. Why the *client's* config is the door that could be opened

`.mcp.json` is read by the MCP client, which already reads it, and which already
launches whatever command it names. Rekoll gains **no** discovery mechanism: the
server still requires explicit arguments and still reads no config file, walks
no directory tree, and auto-detects nothing new. The scope stops being derived
and becomes **visible, reviewable, and version-controlled by the operator's own
choice** — which is strictly better than a value invented at every launch.

### 2. Sticky identity, not live derivation

Issue #83's owner-ratified requirement (2026-07-25, after the
talkativeAI→rekounts rename incident) is that the project identity be *"chosen
once and **stored** … not re-derived from the folder name at every launch.
Sticky identity survives renames; live derivation doesn't."*

Pinning satisfies that in the strongest available form. A folder-derived project
is rename-fragile **by construction**: after that rename a bare MCP launch would
have derived `project=rekounts` and silently stopped seeing 2,747 memories
stored under `talkativeai`. The repo survived only because #82's author had
pinned `--project` by hand. A pinned `default` is not merely stored — it is not
derived from the folder at all, so there is nothing for a rename to change.

### 3. Threat analysis — why this is not ADR-0035 §6's redirect attack

§6 is verbatim (`docs/adr/0035-live-project-board.md:137-138`):

> **Every discovery mechanism is REJECTED for v1** — no config file in the
> repo, no upward directory search, no environment auto-detection, no second
> database, no daemon.

and its remedy sentence, one paragraph down (`:144-146`), states the property in
**path** terms:

> The store path stays **operator-only input** (a flag/env the operator sets,
> never data found in the working tree), exactly the posture `rekoll-mcp`
> already takes for trust and redaction settings.

The asset §6 protects is therefore the **store path**. The attack it names is a
hostile clone retargeting where memory lives: reads served from an attacker's
store (planted "rules" and history), writes exfiltrated into a file the attacker
later collects. A generated `.mcp.json` carries a project **NAME**, and a name
cannot do any of that:

* It cannot leave the store file the operator chose. Scope is a partition
  *within* one SQLite file, so the worst a hostile name achieves is pointing the
  agent at an **empty** scope of the operator's own store.
* It plants nothing. Putting rows in the operator's store needs the store file,
  which is the separate and already-known "hostile repo commits
  `.rekoll/memory.db`" attack — unchanged by this ADR, and the reason scope
  names read from a store are sanitized (ADR-0040, ADR-0044).
* It exfiltrates nothing: writes still land in the operator's local file.

So the design is **constrained to keep it that way**: the generator emits scope
names and **never a `--path`**. Outside the standard `./.rekoll` layout it writes
nothing at all and says so — the same line `_ensure_gitignore` already draws for
the same layout. A test pins the absence of `--path`.

`.gitignore` is explicitly **not** relied on as a defense: it has no effect on an
already-tracked file, so a hostile repo can commit a config and every cloner
receives it. That is true today, of a file the MCP client already honours and
which can already name *any* command — `.mcp.json` is the client's trust
boundary and a client asks for approval on project-supplied servers. This ADR
neither widens that boundary nor claims to fix it.

**The honest tension, named rather than tripped over.** §6 also bans
"environment auto-detection", and `mcp_server._derived_project` auto-detects the
project from `cwd` **today** — v1 shipped with one foot over that line already.
This ADR does not remove that code (removing it would re-scope every existing
MCP user, §5), but every config Rekoll generates from now on makes it dead: an
explicit flag wins, so the derivation never runs. The direction of travel is
away from §6's ban, not toward it.

The docs' promise that **"v1 has no discovery on purpose"** stays literally
true — Rekoll discovers nothing new — and QUICKSTART now says which file the
scope lives in, so the sentence is not doing quiet work it cannot support.

### 4. Never clobber, never guess — the five refusals

Exactly one outcome writes a file. Every other reports itself and writes
nothing, in this order:

| Guard | Why it refuses |
| --- | --- |
| no `mcp` installed | This machine never opened the MCP door. A config for a server it cannot run would also make `doctor` start warning about that door — the nagging ADR-0041 §2 refuses to do. It prints how to get one instead. |
| `--path` outside `./.rekoll` | The generator emits names only (§3), so it declines rather than describe this store wrongly. |
| `.mcp.json` exists | The user's file. Never merged, never rewritten — we do not own that schema. Enforced by an **exclusive create**, not only by the check. |
| `.cursor/mcp.json` or `.vscode/mcp.json` already registers rekoll | A second registration with a different scope would *create* the split this closes. |
| the store holds memories, none in this scope | **The hard constraint.** Pinning here would point the agent at an empty scope and hide what is already stored. Nothing written, nothing moved, nothing guessed; the operator is sent to the ADR-0040 note, which names the populated scopes. |

`--no-mcp-config` turns the write off outright.

Two consequences worth stating. First, **`init` writing into the repo root is
not new**: `_ensure_gitignore` already appends `.rekoll/` to `.gitignore`,
creates it when absent, refuses on UTF-16, and reports one of four outcomes.
This reuses that idiom — including UTF-8-without-BOM output, because
`_find_mcp_registrations` reports UTF-16 as invalid JSON and an unreadable
config starts no server at all.

Second, **plain `init` gains stdout but asks nothing.** ADR-0036 §Decision
(`docs/adr/0036-opt-in-init-wizard.md:16`) says:

> Plain `rekoll init` stays byte-identical: silent, non-interactive,
> zero-config, everywhere (pinned by test).

Read strictly, "byte-identical" would freeze `init`'s output forever, and it has
not been read that way: plain `init`'s stdout has already grown twice since that
ADR merged — the store-creation lines (`0ab2cb6`, issue #71) and the privacy
promise (`5d51095`, W5), both descendants of ADR-0036's own commit. In context
the sentence bounds the **wizard's** blast radius — the interview must not leak
into the default path — and the live invariant is the rest of that line:
non-interactive and zero-config. This ADR keeps that invariant exactly and does
not amend ADR-0036.

Its pin is also weaker than it looks. `tests/test_cli.py:430` asserts only that
stdin is never read, that stdout holds no "wizard"/"interview", and that
**stderr is exactly empty** — it does not pin stdout bytes, so a new line there
would sail past a green suite either way. A byte pin for the new line plus a
fresh no-prompt/empty-stderr assertion therefore ship with this ADR.

An opt-in flag was rejected: a flag the user must discover does not fix the
**default** path, which is where every field report started.

Nothing prints a JSON block for the human to copy. Since ADR-0046, human output
is hard-wrapped at the terminal's width with `|` marking continuations — which
would corrupt pasted JSON — so refusals print the **flags** to pin on one
logical line instead, and only when every part is safe to typeset (`Scope`
accepts spaces and leading dashes; the file itself needs no shell quoting, so it
still pins them correctly).

### 5. What was deliberately NOT changed

* **The CLI's argparse default** stays `"default"`, and **`model.py` is
  untouched.** Moving either would re-scope existing users, and the suite would
  not catch it honestly: twelve wizard tests assert against a scope they never
  name — six via `adapter.count(scope=Scope())` (the model default), six via
  `Memory(path=DB)` (the SDK default). Move only argparse and six go red, six go
  **false-green**. Unify at the model layer — the most natural reading of "one
  repo, one memory" — and **all twelve stay green while the scope they assert
  against moves underneath them**, certifying a change that could make an
  existing user's memories invisible. A tripwire test now pins CLI == SDK ==
  model, so a later attempt goes red instead of quiet.
* **`_derived_project` stays** (§3). Removing it would re-scope every existing
  MCP user who has no pinned config — precisely the harm this ADR forbids.
* **The SDK door is unchanged, and that is a residual, not an oversight.**
  `Memory(project="default")` has no config file to generate and no launcher to
  pin; it matches the pinned default, so a project that keeps `init`'s scope has
  all three doors agreed. A project that pins a *non-default* project must pass
  `project=` to `Memory()` itself. Said in QUICKSTART, not hidden.
* **Existing split stores are informed, never migrated.** ADR-0040's detector
  already names the other scope and prints the command. This ADR adds no repair
  verb that moves data: `forget`/re-ingest under the wanted scope is the only
  path, and nothing here guesses on the operator's behalf.

### 6. The command shape is detected, because the wrong one cannot start

`docs/MCP.md` documents two shapes and picking wrong writes a registration that
`rekoll doctor` would report as broken — a file Rekoll itself wrote. So:

1. **A virtualenv inside the project** → `.venv/Scripts/python.exe -m
   rekoll.mcp_server`, with the interpreter path **relative** and POSIX-spelled.
   The console-script shim is ignored *even though `shutil.which` finds it*: it
   embeds the absolute path of the environment that created it, so a folder
   rename kills it silently — twice, in the field (`docs/MCP.md:84`). A relative
   path survives, because clients launch the server with the project as its cwd.
   Same posture as ADR-0037 §2.
2. **A virtualenv elsewhere** → this interpreter's absolute path with `-m`. It
   cannot be named relatively and must not be named by its shim, so `init` says
   out loud that moving the folder or the venv breaks it.
3. **pipx / global pip** → `rekoll-mcp` with no `-m`, the shape MCP.md and the
   README lead with.

The server key stays the literal string `rekoll`: `_find_mcp_registrations` only
recognises an entry whose key, command or joined args mention it, so the name is
part of the contract with `doctor`.

## Consequences

* A new project's three doors share one memory from the first command. The
  footgun is gone from the documented path, and QUICKSTART now says "`init`
  wrote it" instead of "paste this JSON".
* Machine payloads are byte-unchanged: no new keys in `recall --json`, `board
  --json` or any MCP tool. (There is no `rekoll status --json`.)
* `doctor` needs no change — `_mcp_server_target` already parses
  `--project/--tenant/--agent` out of a registration, in both argparse
  spellings. Verified: a generated config reports `ok`.
* **`doctor` gains a line where it previously had none, and one of them is a
  WARN.** With a registration present, the ADR-0041 §2 check now speaks: `ok`
  while the store is empty, and `WARN` once the store holds memories of which
  none arrived through the MCP door. That WARN reaches someone who installed the
  `mcp` extra and uses only the CLI — and it is **intended**: they are exactly
  the population that check was built for (the 12-hour silent failure in #82),
  the wording already ends *"Harmless if you only use the CLI"*, and it is a
  WARN, so `doctor` still exits 0. A machine **without** the extra gets no
  config and no line at all, which is what keeps genuinely CLI-only users
  unbothered — that is why the gate is the extra rather than nothing. Both legs
  are pinned by test, and `test_no_registration_means_no_line` now arranges its
  premise with `--no-mcp-config` instead of assuming it.
* **Residual (order of operations).** A user who runs `init` *before* installing
  the `mcp` extra gets no config; if they then hand-write one from the docs they
  land back in the split. `init` says so on that machine and is idempotent, so
  re-running it writes the file. Documented, not silent.
* **Residual (the SDK).** §5. A non-default pinned project needs `project=` at
  the SDK door.
* **Residual (existing split stores).** Informed by ADR-0040, never migrated.
