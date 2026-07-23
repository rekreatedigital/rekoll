# ADR-0036 — The init wizard is opt-in: `--wizard` asks, plain `init` never does

**Status:** Accepted · **Date:** 2026-07-23 · **Extends:** ADR-0017 (directive explicit trust), ADR-0034 (standing-directive channel) · **Interacts with:** ADR-0006 (content-addressed ids), ADR-0023 (trust-aware upsert)

## Context

Onboarding's core promise is that preferences are captured **once** and replayed
to every AI session forever after (the ADR-0034 standing-directive channel). The
natural moment to capture them is first run — an interview at `rekoll init`. But
DESIGN.md also promises a **zero-config first run (no questions)**: `init` runs
in CI, in scripts, in agent shells, and a question there hangs a pipeline or
reads garbage. The two promises collide only if the interview is the default.

## Decision

Plain `rekoll init` stays byte-identical: silent, non-interactive, zero-config,
everywhere (pinned by test). The interview ships as an explicit opt-in flag —
`rekoll init --wizard` — and degrades to plain init (one plain stderr line,
exit 0) when stdin is not an interactive terminal, using the SAME
`_stdin_is_interactive()` oracle as the directive vouch gate — one oracle, not
two.

- **At most 3 questions, at most 3 rules per run** (explain-style, project
  context, tone), each skippable with Enter; answers are trimmed at 500
  characters, announced before the summary. The ADR-0034 channel surfaces the
  OLDEST five directives on every recall, so one interview must never be able
  to flood the cap — and every stored character is a permanent per-read token
  cost.
- **Answers are stored as clear standing rules** ("Explain things simply, in
  plain language, and avoid jargon."), never raw answer fragments — the stored
  text is injected verbatim into every session's instruction channel.
- **One summary confirmation** (`Save these? [y/N]`; declining saves nothing —
  not even the store file) replaces three per-answer vouch prompts. The wizard
  mints through the SDK with an explicit `trust=TrustTier.OWNER`, which is
  precisely the conscious act ADR-0017 demands; the summary — which says
  plainly that these rules ride every future session until `rekoll forget` —
  keeps the act conscious at the human level while staying friendly.
- **Choosing "normally" mints nothing:** default behavior needs no rule, and a
  no-op directive would burn one of the five surfaced slots on every future
  recall.

## Consequences

- The zero-config promise stays literally true; DESIGN.md's plan line now names
  the opt-in wizard beside it.
- Re-running the wizard with identical answers is a no-op (content-addressed
  ids, ADR-0006; trust never silently falls, ADR-0023). A CHANGED answer mints
  an **additional** rule, and the oldest-first cap keeps the old one — so the
  honest way to change your mind is `rekoll forget <old id>` first; the
  wizard's closing copy says exactly that.
- The wizard opens a `Memory` (plain init never does), so with the embeddings
  extra installed the first save may download the small local model; the wizard
  prints a plain "one moment" line first.

## Alternatives rejected

- **Interview by default, a flag to silence it.** Breaks the zero-config
  promise for every script that already calls `rekoll init`, and TTY-detection
  as the only guard would flip behavior on environment, not on intent.
- **Per-answer vouch prompts (reuse the `remember` gate verbatim).** Three
  consecutive standing-rule warnings turn a welcome into a gauntlet; one honest
  summary showing the exact rules is the same informed consent with less fear.
- **A config file instead of directives.** Rules stored as memory ride every
  door (SDK/CLI/MCP) and every recall today, scoped, trusted, and forgettable;
  a config file would need a second delivery channel and would be none of that.
