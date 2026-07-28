# ADR-0048 — Warn, don't restrict — and where that stops

**Status:** Accepted · **Date:** 2026-07-29 · **Implements:** issue #120 ·
**Records:** a posture in force since 2026-07-14, already shipped by ADR-0039,
ADR-0040, ADR-0041, ADR-0044 and ADR-0046 ·
**Corrects:** the warn-posture citations in ADR-0037, ADR-0042, ADR-0043 and
ADR-0044, which all named ADR-0033 (the PII-redaction-tag decision) ·
**Interacts with:** ADR-0013 (injection firewall), ADR-0016 (ingest trust
default), ADR-0017/0034 (directive vouch + standing channel), ADR-0023
(trust-monotonic upsert), ADR-0024 (embedder mismatch: refuse and degrade),
ADR-0028 (abstain on weak recall)

## Context

This posture is one of the most frequently invoked principles in the codebase
and the last major one with no decision record. It was stated by the owner on
2026-07-14, it is implemented in five places, it decided at least four design
questions — and it had no number.

So the ADRs that needed to cite it cited **ADR-0033**, which is
`0033-unreversible-pii-redaction-tags.md`: "PII redaction stores a class-only,
non-reversible audit tag". That decision has nothing to do with this one. The
mistake was plausible — ADR-0033 is from roughly the right date, and ADR-0013
and ADR-0022 cite it correctly for genuine PII-tag reasons — and it propagated
by copy-paste, each new ADR inheriting the previous one's header. Twelve
references across four records. ADR-0046 declined to make it thirteen and said
why; this ADR is the number it wanted.

A record written now has to do more than restate the slogan. **A principle with
no boundary gets cited to excuse anything**, and this one has already been
reached for at its edge: ADR-0037 §6 had to stop and explain why refusing
`remember --to` with `kind=directive` is not a violation. That explanation
belongs here, once, rather than in every ADR that touches a refusal.

## Decision

The posture has two clauses, and the second is not an exception to the first.
They divide on one question: **who is harmed by the choice.**

### 1. The user's own data, on the user's own machine, is the user's call — loudly informed

Where the only person who can be hurt by a choice is the person making it,
Rekoll **informs and hands over the controls**. It does not block, filter,
hide, truncate, auto-switch, auto-merge or auto-fix. Secure defaults, honest
warnings, no locked door.

This governs how much to store, whether to ingest something noisy, whether to
keep a degraded index, whether to run without the firewall, whether to leave a
store split across scopes. All of those make the operator's own data less safe
or less useful, and all of them are theirs to decide.

The warning is not the consolation prize for a refusal we wanted to make. It is
the whole intervention, and it has to be good enough to stand alone: name the
condition, name the consequence, and hand over the exact command that changes
it.

### 2. A warning does not unlock a defence, because the person harmed is not always the person choosing

Rekoll still quarantines untrusted ingested content, still refuses a directive
from an unvouched source, still declines to execute what stored content or a
config points at, still refuses to serve records under a mismatched embedder
identity. None of those is a door the user opens by being warned.

The reason is not paternalism, and ADR-0042 §3 states it in the cleanest
available form: three of its four reasons not to commit a store are **costs**,
"and a user is entitled to accept costs" — but the fourth is that a committed
store loads as memory in every clone, and that "is not the committer's risk to
accept on behalf of everyone who clones."

That is the line. **Clause 1 applies exactly when the blast radius stops at the
person consenting.** When it does not — a teammate who clones, a colleague
whose PII is in the text, an agent that will act on a forged instruction —
consent from one party is not consent, and warn-don't-restrict has nothing to
say about it.

### What this does not license

Three misreadings, named so they can be refused by citation:

- **"Warn instead of fixing."** A security defect is a defect. Documenting a
  hole is not closing it, and no warning converts an unfixed vulnerability into
  an informed choice — the person at risk never reads the docstring. ADR-0044
  shipped with an over-claim and was amended rather than annotated; that is the
  precedent.
- **"The user consented, so the defence is optional."** Clause 2. Defaults may
  be loosened by a conscious, per-instance act where the ADR that owns the
  defence says so (ADR-0017's vouch, ADR-0016's trust default) — that is a
  designed gate, not this posture overriding one.
- **"Any refusal violates it."** It does not govern API surface. Refusing an
  unsupported flag combination that would silently do less than it says is
  honesty, not restriction (ADR-0037 §6). Nor does it govern *not building*
  something: declining to ship a feature restricts nobody's data.

## Where this already ships

The posture is checkable, not aspirational. Each of these was decided by it,
and each would have to change if this ADR were wrong:

- **The relevance footer informs and never filters** (ADR-0039). No hits are
  hidden and no default `min_score` ships. Its rejected alternative says so in
  the posture's own terms: a floor "is a *filter*, which under
  warn-don't-restrict needs overwhelming evidence the data does not supply."
- **The scope-split detector warns and never moves data** (ADR-0040). It names
  the other scopes and hands over a runnable command; "no auto-switching, no
  merging" is in its own "what this deliberately does not do", and the `doctor`
  check is **WARN, not FAIL — nothing is broken** (ADR-0040 §2).
- **`doctor` reports, it does not gate** (ADR-0041). Its rejected-alternatives
  section spells out the rule: "FAIL instead of WARN. Nothing here is broken or
  lost; the operator is misinformed. Warn-don't-restrict: say it clearly, never
  block." WARN keeps exit 0, so no script's behaviour changes.
- **The wrap point reflows and never truncates** (ADR-0046). "Truncate long
  content, or refuse to print it" is rejected in one line: never hide the
  operator's own data.
- **Four docstrings in the code**, which are where the posture lived before it
  had a number (`src/rekoll/cli.py`, cited by function because a parallel lane
  is moving that file):
  - `_vouch_standing_rule` — the directive vouch warns loudly and asks only on
    a terminal: "with `--yes`, or with no terminal to ask on, the write
    proceeds and the warning still prints — informing is free, blocking is what
    we avoid."
  - `_run_init_wizard` — with a piped stdin the wizard prints one stderr line
    and exits **0**: "a `--wizard` copy-pasted into a script must inform, not
    break the pipeline."
  - `_scope_split_lines` — "this INFORMS. It never switches scope, never
    merges, never hides — the operator decides."
  - `_check_scopes` — WARN not FAIL, "we inform and hand over the command,
    never block or auto-switch."

## The pattern worth recording

Both times this posture has decided a *rendering* question, it argued for
showing the user **more**, not for removing a restriction.

ADR-0044 considered refusing to display a record whose content was shaped to
forge rekoll's own output, and declined: "never hide the operator's own data.
Showing it inert is strictly better than hiding it." ADR-0046 considered
refusing to print content long enough to reach the terminal's wrap boundary,
and declined for the same reason — then closed the class by **adding** a
continuation marker at column 0.

That is the shape of a correct application: the posture usually costs a line of
output, not a defence. When invoking it appears to cost a defence, the
invocation is probably clause-2 work wearing clause-1 clothes.

## Alternatives rejected

- **Leave it as prose in whichever ADR needs it.** That is the status quo, and
  the status quo produced twelve wrong citations in four permanent records. The
  next design lane would have copied a thirteenth.
- **Amend ADR-0033 to also carry the posture.** It would make the real
  citations ambiguous — ADR-0013 and ADR-0022 point at ADR-0033 for the
  redaction tag, and a reader could no longer tell which half was meant. The
  bug was one number meaning two things; two things need two numbers.
- **Write it unbounded ("Rekoll never blocks").** Verifiably false of the
  shipped code: the vouch gate can cancel a write, ingest quarantines,
  ADR-0024 refuses on an embedder-identity mismatch. An ADR the code
  contradicts is worse than no ADR, because it can be cited against the code.
- **Write it as a value statement with no test.** "Respect the user" decides
  nothing. The who-is-harmed question is the part that does work.

## Consequences

- There is a number to cite, and the twelve references now cite it.
- Every future "should this block?" has one written test — *who is harmed if
  the user chooses wrong* — and a worked answer at the hard edge (ADR-0042 §3's
  fourth reason).
- The record is falsifiable. It names five shipped behaviours; if any of them
  starts filtering, hiding or blocking, this ADR is wrong and must be amended
  rather than quietly out-voted.
- Nothing in the code changes. This ADR records a posture that was already
  implemented; it does not introduce or relax a single behaviour.

## What stays open

- **Nothing enforces this.** No test asserts "rekoll never blocks", and none
  could — the property is about intent, and its counter-examples are the
  designed gates in clause 2. The five behaviours above are individually
  pinned by their own ADRs' tests; the posture itself is a convention, held by
  being cited.
- **The boundary is argued, not computed.** Clause 1 and clause 2 divide on a
  judgment about blast radius, and a genuinely mixed case will need an argument
  rather than a lookup. One is already in the tree: `Memory(redact_pii=True,
  screen=False)` warns and lets the disabled firewall win (`src/rekoll/memory.py`,
  "Warn, never block (project posture)"). The host chose it for their own
  store, which is clause 1 — but the PII in the text may be a third party's,
  which is clause 2's shape. It is warned about today and is not re-decided
  here.
- **The three prose statements in ADR-0039, ADR-0040 and ADR-0046 keep their
  wording.** They state the posture correctly without a number, and this ADR
  cites them, which is the direction a reader needs. The one edit made to
  ADR-0046 is to a sentence this ADR makes false ("it has no ADR number of its
  own"), not a cross-reference added for tidiness.
- **Issue #83's lane (ADR-0047) is in flight** and touches surfaces this
  posture governs. If it lands citing ADR-0033 for the posture, that is a
  thirteenth reference and should be corrected to this one.
