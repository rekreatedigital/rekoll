# ADR-0019 — Versioned attack corpus with containment + ASR regression gates

**Status:** Accepted · **Date:** 2026-07-02

## Context

DESIGN §9 promises "a versioned injection-attack corpus with an attack-success-
rate regression gate (ASR may only go DOWN)." The round-2 audit (P1-5) found
only a 4-string "mini corpus" inline in `tests/test_firewall.py` — not
versioned, not categorized, no homoglyph/invisible/markdown-forgery/multilingual
coverage, and no ASR gate. The headline security claim rested on four strings.

## Decision

Ship `benchmarks/attack_corpus.json` — a versioned, categorized corpus (63
attacks + 3 benign controls at v1) across `override`, `role_hijack`,
`prompt_exfil`, `new_instructions`, `role_tag_forgery`, `markdown_forgery`,
`homoglyph`, `invisible_char`, `multilingual`, and `secret_exfil`. Each entry
carries `detected` — whether the ingest tripwire is expected to quarantine it.

Two independent gates (`tests/test_attack_corpus.py`):

1. **Containment (the wall, 100% required).** For every untrusted attack —
   detected or not — the recall envelope's `directives` tuple stays empty and
   no forged frame (section header, role tag, `[n]` index) survives rendering.
   This is the structural guarantee; it does not depend on the marker list.
2. **Detection / ASR (the tripwire, ratchet only).** Every `detected: true`
   entry must be quarantined (per-entry asserts — a marker regression fails CI
   on the exact payload), the aggregate detection rate must meet
   `BASELINE_DETECTION_RATE`, and the corpus `detected` flags must match live
   behavior (no stale corpus).

To move the tripwire up, marker detection was extended (validated against a
benign multilingual false-positive set, quarantine is untrusted-only + audited):
English exfiltration (`reveal/print your instructions`), override
(`override your guidelines`), jailbreak framing (DAN / developer mode /
`from now on`), more forged channel tags (`<im_start>`, `[INST]`), and a
curated multilingual "ignore/forget previous instructions" set (de/es/fr/it/
pt/zh). Secret redaction now covers the **whole PEM private-key block**, not
just the header line. Observed: 79% detection / 21% ASR — the remaining ~21%
are deliberately included, uncaught-but-contained payloads that prove the wall
carries the load, per DESIGN §6 ("the regex screen is a tripwire, not the wall").

## Consequences

- The corpus is the artifact new attack ideas are added to; the baseline only
  ratchets up. Adding a harder uncaught attack lowers the aggregate rate, which
  forces either a marker improvement or an explicit, reviewed baseline decision.
- Multilingual markers carry a small false-positive risk on untrusted content;
  accepted because quarantine is reversible, auditable, and untrusted-only, and
  the patterns require an adversarial verb + object within a bounded gap.
- All marker quantifiers stay bounded/literal-anchored; the ReDoS gate (P2-8)
  guards the additions.
