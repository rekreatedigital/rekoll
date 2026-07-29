# ADR-0049 — Secret files are skipped by NAME; redaction by shape is the second layer, not the first

**Status:** Accepted · **Date:** 2026-07-30 · **Fixes:** GHSA-mcv6-g76w-c6px ·
**Extends:** ADR-0027 (ingest hygiene filters, issue #29) ·
**Interacts with:** ADR-0013 (injection firewall), ADR-0016 (ingest trust default),
ADR-0033 (unreversible PII redaction tags), ADR-0048 (warn, don't restrict)

## Context

ADR-0027 added a filename-level ingest filter and put `credentials.json` on it,
because a real Google OAuth `credentials.json` had been chunked, embedded and
stored as an ordinary retrievable record (issue #29).

A field report from a production project found the same class of bug with a
different filename. The project kept a filled-in `credentials.md` at its repo
root — gitignored, holding Supabase keys, a Meta system-user token, a Discord
bot token and ten webhook URLs, a Cloudflare API token and two passwords.
`.md` is in `DEFAULT_INCLUDE_EXT`; `credentials.md` was on no skip-list. The
documented quickstart command, `rekoll ingest .`, would have stored the lot.

Two assumptions failed together, and the second is the interesting one.

**The skip-list assumed secrets live in machine-readable files.** Every entry —
`credentials.json`, `id_rsa`, `*.pem`, `.env`, `token.pickle` — is a config or
key file. But Rekoll's whole pitch is memory over **documentation**, and a
project that documents its credentials writes `credentials.md`. We listed the
formats a *program* reads and missed the format *we* read.

**The redaction pass assumed config syntax.** The reasoning at the time was
that filename filtering is belt-and-braces because the firewall redacts secrets
on the way in regardless. Measured against that project's real file, only one
of nine credentials was redacted — the Supabase JWT, caught by the `jwt`
pattern. The generic `credential_assignment` pattern was:

```
(?:api[_-]?key|secret|password|passwd|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+]{12,}['\"]?
```

It matches `DB_PASSWORD=value` and `password: value`. It does not match:

| Written as | Why it missed |
|---|---|
| ``- **DB password:** `value` `` | `**` sits where `\s*` was expected |
| ``password: `value` `` | a backtick is not in `['\"]?` |
| `Bot token: value` | bare/spaced `token` is not in the alternation |
| `- **OPS_TOKEN:** value` | `<WORD>_TOKEN` is not in the alternation |

So the fallback layer was tuned for the input format Rekoll does **not**
primarily ingest. Prose was the blind spot in both layers at once, which is why
neither caught it.

There were also no patterns at all for Meta Graph tokens, Discord bot tokens,
Discord webhook URLs or Cloudflare API tokens — though `slack_webhook` had been
there since early on, and Discord is at least as common for dev alerting.

## Decision

**1. Name-based skipping is the primary defense for credential files. Shape-based
redaction is the second layer, and neither is allowed to be the reason the other
stays weak.**

The layers fail differently and that is the point. Skipping by name does not
need to guess what a secret looks like — it needs the file to be conventionally
named. Redaction by shape does not need the file to be named anything — it
needs the secret to look like something we have seen. A credentials file in
prose defeats shape matching; an oddly-named file defeats name matching. Both
layers are required, and a gap in one is a bug in that one.

**2. The skip-list covers documentation formats, not just config formats.**
Added: `credentials.md`, `credentials.*.md`, `credentials.txt`, `secrets.md`,
`secrets.*.md`, `secrets.txt`. The `.*.` variants matter — a team copy of a
vault is conventionally `credentials.team.md`.

**3. `credential_assignment` is markdown-aware**, because documentation is the
primary ingest target. It now tolerates emphasis and list punctuation between
key and separator, accepts a backtick as a quote character, and recognises
`<WORD>_TOKEN`, `bot token` and `api token` as credential keys.

Bare `token` is deliberately **not** a key. `token: foo` is ordinary prose,
`OPS_TOKEN:` is not, and the widening must not turn documentation into
redaction soup. A regression test pins a set of benign prose lines that must
never redact.

**4. Vendor patterns added** for Meta Graph (`EAA…`), Discord bot tokens,
Discord webhook URLs and Cloudflare (`cfut_…`).

## What this does NOT decide

**Ingest still does not read `.gitignore`.** A user marking a file never-commit
is the strongest sensitivity signal available, and `cli.py::_ensure_gitignore`
already writes to that file, so the concept exists in the codebase — it is just
never read back. Honouring it would have caught this case without anyone having
to predict the filename.

It is left out of this change on purpose. Rekoll ships with `dependencies = []`
and `pathspec` is the obvious implementation, so the honest options are a
`git check-ignore --stdin` subprocess when inside a repo, or a hand-rolled
matcher — a real design decision with a real failure mode (a subprocess per
ingest, and different behavior inside and outside a work tree). It deserves its
own ADR rather than a rider on a security fix. Tracked separately.

Until then the skip-list is doing work that `.gitignore` could do for free, and
that is a known and stated weakness of this decision, not an oversight.

## Consequences

- A project documenting credentials in markdown no longer leaks them to `ingest`.
  The skip is announced by the existing single warning that names the files, so
  it is visible rather than silent (ADR-0027, ADR-0048).
- `credentials.md` in a doc tree is now skipped by default. Pointing `ingest_path`
  straight at it, or passing `skip_files`, still works — explicit intent is never
  blocked, and storing a secret-named file is still warned. No locked door
  (ADR-0048 §1).
- Redaction gets slightly more aggressive on prose. That is the intended
  direction: a false redaction costs one recallable sentence, a false negative
  costs a live credential.
- **No `.env.*` glob was added.** These are `fnmatch` globs with no negation
  syntax, so `.env.*` would also swallow `.env.example` — a template of key
  names, meant to be read. The variants need no entry: `.env.local` has suffix
  `.local`, which is not in `DEFAULT_INCLUDE_EXT`, so the walk never offers it.
- Anyone who ran `ingest` on a tree containing a filled-in `credentials.md`
  before this release has cleartext secrets in their store. There is no
  `rekoll doctor` check that would tell them, and no purge path — `forget` needs
  ids they cannot easily discover. Tracked separately; see the advisory.
