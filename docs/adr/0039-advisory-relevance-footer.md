# ADR-0039 — An advisory relevance footer on the human recall door

**Status:** Accepted · **Date:** 2026-07-25 · **Implements:** issue #73 · **Extends:** ADR-0028 (abstain on weak recall), ADR-0031 (abstain door parity) · **Interacts with:** ADR-0013 (envelope byte-identity), ADR-0019 (read-time verification), ADR-0024 (honest degradation)

## Context

ADR-0031 §2 put `top_vector_score` on the wire for both machine doors "because it
is a number an agent needs in order to calibrate a threshold". The human door
computed the same number and dropped it. Issue #73's field report is exactly that
asymmetry: three memories, ask about the capital of France, get all three back,
ranked, exit 0 — and the one audience with no access to the relevance signal is
the person, who is also the one who has not read the calibration docs at memory
number three.

A read-only investigation measured the alternatives and **eliminated every
filtering option**:

- The answerable/unanswerable cosine bands **overlap** on bge-small: answerable
  near-topic min 0.515 < strict-nonsense max 0.519 < unanswerable-but-adjacent
  0.565. No single floor classifies correctly even on a small friendly corpus.
- Scores are **flat in corpus size** (the France query scores 0.462 at n = 1, 3, 5
  and 10). Small-n "it returned everything" is `k >= n` arithmetic, not a scoring
  failure — and the abstain gate is query-level (ADR-0028 §2), so no gate can ever
  fix it for a legitimate query.
- On the stub embedder the signal **inverts** ("the" scores 0.632; an on-topic
  paraphrase 0.0), so any shipped default floor would break the zero-dependency
  install.
- The gate does not misfire — it **ships off** (`min_score=None` on every door,
  ADR-0028). It was never armed, not mistuned.

## Decision

The human CLI recall renders **one advisory footer line** after the hit list,
built only from data the recall already computed.

1. **It informs; it never filters.** No hits are hidden, no default `min_score`
   ships, exit codes are unchanged, and `--json` / MCP / `ContextEnvelope.render()`
   / `--ids` are byte-identical. A future lane must not promote the advisory
   threshold into a gate without redoing the calibration work #73 states was not
   done.
2. **The advisory threshold lives in one named constant with a hedge band**, and
   its comment states it is a starting point, not a measured constant. Inside the
   measured overlap band the wording hedges ("borderline") instead of asserting;
   above the line it passes no judgment at all and prints only the number.
3. **Embedder-aware honesty.** No `top_vector_score` → no similarity claim at all
   (the fragment is dropped whole rather than printing a bluffed 0.00). On the stub
   embedder the number is relabelled "word overlap" and carries no verdict, because
   its cosine measures token overlap and the signal inverts. Stub is detected by
   **type**, not by sniffing `mode`: `Memory._mode` returns early on the
   embedder-mismatch branch and never appends the stub suffix there, so the string
   is a description of the pipeline, not a predicate about the embedder.
4. **The scope total is the effective-status active count**, not `Memory.count()`
   (which includes quarantined-for-audit rows recall can never surface) — the same
   predicate `retrieval._surfacable` applies to the hits, so the two agree by
   construction. It fails soft to count-free wording rather than fabricating a
   total, and it is fetched while the store is still open, since `cmd_recall`
   closes its `Memory` before rendering.
5. **The line goes to stderr**, where this CLI's other explanations of a recall's
   outcome (`Abstained: …`, `No memories found`) already go — keeping the stdout
   hit list byte-identical to before. A `stdout.flush()` precedes it so a merged
   redirect (`2>&1 | less`) cannot print the footer before the hits it annotates.
6. **The reassurance is scoped to the footer.** The line says "this line hides
   nothing", never "hits are never hidden": ADR-0019 read-time verification
   genuinely does withhold a tampered hit, and it can do so on this very line
   (`showing 2 of 3 …` while one hit was withheld). An absolute claim would be
   false exactly when a user most needs the truth.

## Consequences

- The `k >= n` "it returned everything" shape is explained where it is seen, which
  no gate could do.
- The advisory threshold is a documented wording knob, not a hidden policy.
- Cost is one bounded `COUNT(*)` per **human** recall — measured at 0.50 ms p50
  against a 34 ms recall on a 2,000-record store (1.5%), and never paid by a
  machine door. The read path stays zero-LLM and zero-write.
- The Quickstart gains a `--min-score` calibration recipe (measure
  `top_vector_score` on a known-answerable and a known-nonsense query, set the
  floor between them), written in `bash` fences because the snippet runner
  executes every `python` fence under the stub embedder, where the signal inverts.

## Alternatives rejected

- **An absolute score floor, on by default** — eliminated by measurement three
  times over (overlapping bands, stub inversion, and it is a *filter*, which under
  warn-don't-restrict needs overwhelming evidence the data does not supply). It
  would also flip the script-facing exit code on a number shown to be
  uncalibratable in general.
- **A floor only at small n** — the premise is empirically false (scores do not
  degrade at small n), and it adds behavior drift: the same query on the same store
  would change answers as the store grows past the cutoff.
- **Docs only** — necessary but insufficient; the repro user has not read the
  calibration docs at memory three, and the human door would still render
  confident-looking output with no signal. Folded in as the recipe.
