# Dogfood v1 — does `ingest_path -> recall()` find the right file?

Rekoll pointed at four of the owner's real local repos, asked 40 pre-registered
developer questions, scored on whether any top-5 hit's provenance points at the
gold file. **Functional efficacy, not a synthetic benchmark**: the questions
were written from reading the repos and committed BEFORE any recall ran
(commit `10403de` on this branch — the results below landed in a later commit),
so they could not be fitted to retrieval output.

Privacy: this file contains file paths, question ids, and numbers ONLY — no
content from the target repos. The store db lived in a temp directory and was
never committed.

## Verdict

| repo | kind | hit-rate @k=5 | bar (>=7/10) | MRR |
|---|---|---|---|---|
| powered-by-people-website | Vite/React website | 9/10 | PASS | 0.77 |
| jeff-app-for-iphone | Expo/React Native app | 10/10 | PASS | 0.80 |
| rekreate-ai-chat — zero-config (drowned in `env/`, ingest killed) | Flask RAG bot | 6/10 | **FAIL** | 0.55 |
| rekreate-ai-chat--noenv (`env/` excluded) | Flask RAG bot | 10/10 | PASS | 0.83 |
| Trading-Logging-Automation | Python scripts | 10/10 | PASS | 0.93 |

**39/40 unique pre-registered questions hit @5 when the ingest isn't drowned
in a checked-in virtualenv.** The one clean-run MISS (web-09) and the drowned
run's failure are analyzed below. Scope isolation: 76 cross-scope recalls,
**0 leaks, 0 count mismatches**.

## Config card

- rekoll @ `20a0efb` (branch base; run via this checkout's `src/`), Python 3.12.6, Windows 11, 16 logical CPUs
- embedder: fastembed **0.8.0**, `BAAI/bge-small-en-v1.5`, dim **384** (identity asserted at runtime by the harness; the stub embedder is refused with a hard exit)
- reranker: ON (auto-resolved) — `CrossEncoderReranker`, `Xenova/ms-marco-MiniLM-L-6-v2`
- recall: `k=5`, `kind=None` (no filter), hybrid vector+lexical fused with RRF `k=60`, read-time tamper verification on
- resolved mode string on every scoring recall: `vector+lexical+rerank`
- scopes: `tenant=default`, `agent=default`, `project=<repo name>`; ONE shared sqlite db in a temp dir
- ingest trust: default (`UNVERIFIED` — firewall screening active; 0 records quarantined across all repos)
- harness: `benchmarks/dogfood/run_dogfood.py`; reproduce with

```
python benchmarks/dogfood/run_dogfood.py --db <tmp>/dogfood.db --out <tmp>/results.json
```

## Ingest (zero-config unless stated; exact invocations as recorded)

| repo | git HEAD | invocation | files | chunks | skipped | records | wall |
|---|---|---|---|---|---|---|---|
| powered-by-people-website | `4627fe91` | `ingest_path(<root>)` | 89 | 826 | 0 | 826 | 51.9s |
| jeff-app-for-iphone | `61de4b16` | `ingest_path(<root>)` | 42 | 963 | 0 | 963 | 66.3s |
| rekreate-ai-chat | `5ce83770` | `ingest_path(<root>)` | 520 of ~1,160 | 9,106 | n/a | 9,106 | **KILLED at ~20 min** |
| rekreate-ai-chat--noenv | `5ce83770` | `ingest_path(<root>, skip_dirs=DEFAULT_SKIP_DIRS \| {'env'})` | 8 | 52 | 1 | 47 | **2.2s** |
| Trading-Logging-Automation | `826a7627` | `ingest_path(<root>)` | 7 | 84 | 0 | 69 | 6.6s |

(records < chunks where identical chunks within a file dedup via
content-addressed ids. The noenv `skipped=1` is the repo's UTF-16
`requirements.txt` — non-UTF-8 text is silently counted as skipped.)

### The drowning run (killed as pathological — this is a first-class finding)

Zero-config `ingest_path` on rekreate-ai-chat walked the repo's **checked-in
`env/` virtualenv**: `DEFAULT_SKIP_DIRS` covers `venv`/`.venv` but not `env`
(nor `site-packages`). Measured before the kill:

- 13.6 minutes of continuous batch writes (~20 min process wall including
  model load), **9,106 records from 520 files — 99.7% of records from
  `env/Lib/site-packages/`**, on pace for ~30-60 min and ~20k records total.
- Top chunk producers at kill time: `env/.../idna/uts46data.py` (374),
  `env/.../typing_extensions.py` (236), `env/.../click/core.py` (194).
- The same repo with `skip_dirs=DEFAULT | {'env'}`: **2.2 seconds, 47
  records, 10/10 recall** — a ~500x wall-clock difference for identical
  first-party content.
- Peak ingest process memory ~8.9 GB (stable, not leaking — ONNX runtime
  arena; noted because it is the zero-config cost on a laptop).

### How other vendored noise was handled (measured, zero-config)

- **`node_modules`, `dist`, `.git` skipped by default** — both JS repos ingested only first-party trees. Good default.
- **Lockfiles are NOT skipped**: `package-lock.json` alone is **439/826 chunks (53%)** of the website store and **715/963 chunks (74%)** of the app store. Ranking largely absorbed it, but not fully: lockfile chunks took **7 of the 100 top-5 slots** across the two JS repos (web-05 ranks 3-4, web-08 ranks 3-5, jef-04 rank 2, jef-10 rank 3 — every intrusion on a tooling/config question, where lockfile vocabulary genuinely overlaps). 3/4 of the app's store is dead weight paid for at embed time — its 66s ingest is mostly lockfile.
- **Secrets files are ingested**: `Trading-Logging-Automation/credentials.json` (a Google OAuth client-secrets file) became an ordinary store record under the default include-extensions. Nothing in the pipeline flags credentials at rest (the firewall screens for injection, not secrets). It never surfaced in any scoring top-5, but it is sitting in the store.
- **Non-UTF-8 text is skipped silently**: rekreate's UTF-16 `requirements.txt` → `skipped` count, no per-file diagnostics.

## Per-repo scoring (rank of gold file in top-5, else MISS)

Final full-store run; where a rank differed from the first run it is noted.

### powered-by-people-website — 9/10, MRR 0.77

| q | difficulty | gold | rank |
|---|---|---|---|
| web-01 | keyword | src/components/ContactSection.tsx | 5 |
| web-02 | paraphrase | GOOGLE_SHEET_SETUP.md | 1 |
| web-03 | keyword | src/components/ServicesSection.tsx | 2 |
| web-04 | paraphrase | src/components/AboutSection.tsx | 1 |
| web-05 | keyword | src/App.tsx | 1 |
| web-06 | paraphrase | src/components/Navigation.tsx | 1 |
| web-07 | paraphrase | src/components/HeroSection.tsx | 1 |
| web-08 | keyword | vite.config.ts | 1 |
| web-09 | keyword | tailwind.config.ts | **MISS** |
| web-10 | conceptual | src/components/ScrollToTop.tsx | 1 (was 2 before other projects ingested — see isolation notes) |

**web-09 MISS** — "Where are the brand fonts and custom animation keyframes
registered as utility classes?" Surfaced instead: `src/components/Footer.tsx`,
`src/components/AboutSection.tsx`, `src/components/ui/chart.tsx`,
`src/components/ServicesSection.tsx`, `src/components/Footer.tsx`.
Analysis: the answer is a terse config object (`tailwind.config.ts`) sharing
almost no vocabulary with the question; components that *use* font/animation
utility classes outrank the file that *defines* them. Definition-vs-usage
failure on config-as-code.

**web-01 rank 5 (near-miss)** — ranks 1-4 were all `GOOGLE_SHEET_SETUP.md`,
which *documents* the form backend the question asks about; the asked-for
component barely made the window.

### jeff-app-for-iphone — 10/10, MRR 0.80

| q | difficulty | gold | rank |
|---|---|---|---|
| jef-01 | paraphrase | hooks/use-speech.ts | 1 |
| jef-02 | paraphrase | data/mockNews.ts | 2 |
| jef-03 | keyword | types/news.ts | 5 |
| jef-04 | paraphrase | app/(tabs)/_layout.tsx | 3 |
| jef-05 | keyword | constants/theme.ts | 1 |
| jef-06 | paraphrase | app/modal.tsx | 1 |
| jef-07 | conceptual | components/haptic-tab.tsx | 1 |
| jef-08 | keyword | app/(tabs)/finance.tsx | 1 |
| jef-09 | paraphrase | FUTURE_ENHANCEMENTS.md | 1 |
| jef-10 | keyword | app.json | 1 |

Notes: jef-03 (rank 5) lost to screens that *consume* the category
definitions (settings/empire/index) — definition-vs-usage again, inside the
window. jef-04 and jef-10 had lockfile intrusions (`package-lock.json` at
ranks 2 and 3 respectively).

### rekreate-ai-chat — zero-config drowned store: 6/10, MRR 0.55 (**below bar**)

Scored against the killed partial store (520/1,160 files; all 6 first-party
root `.py` files ingested, `static/` never reached — `env/` walks first).

| q | difficulty | gold | rank | cause of miss |
|---|---|---|---|---|
| rag-01 | keyword | app.py | 2 | |
| rag-02 | paraphrase | ingest.py | 1 | |
| rag-03 | paraphrase | chatbot.py | 1 | |
| rag-04 | conceptual | static/script.js | **MISS** | gold never ingested (kill-time coverage); top-5 = flask internals |
| rag-05 | paraphrase | static/modal.js | **MISS** | gold never ingested (kill-time coverage); top-5 = test.py x3 + openai internals |
| rag-06 | keyword | app.py | **MISS** | **gold WAS in store; drowned** — all 5 hits are `env/.../flask_cors/*` library internals |
| rag-07 | paraphrase | test_api.py | **MISS** | **gold WAS in store; drowned** — `env/.../flask/config.py`, `env/.../httpx/_config.py`, `env/.../openai/__init__.py` outrank it |
| rag-08 | paraphrase | test.py | 1 | |
| rag-09 | conceptual | chatbot.py | 1 | |
| rag-10 | paraphrase | app.py | 1 | |

rag-06/rag-07 are the clean drowning signal: the golds were present and the
question asked about *the app's own* CORS/keys, but the vendored *library
that implements* CORS/config outranked the two-line first-party usage.

### rekreate-ai-chat--noenv — 10/10, MRR 0.83

| q | difficulty | gold | rank |
|---|---|---|---|
| rag-01 | keyword | app.py | 2 |
| rag-02 | paraphrase | ingest.py | 1 |
| rag-03 | paraphrase | chatbot.py | 1 |
| rag-04 | conceptual | static/script.js | 1 |
| rag-05 | paraphrase | static/modal.js | 1 |
| rag-06 | keyword | app.py | 3 |
| rag-07 | paraphrase | test_api.py | 2 |
| rag-08 | paraphrase | test.py | 1 |
| rag-09 | conceptual | chatbot.py | 1 |
| rag-10 | paraphrase | app.py | 1 |

Same questions, same repo, `env/` excluded: every drowned miss recovers.
(The pre-registered app.py-vs-app_backup.py near-duplicate hazard showed up
only as sub-1 ranks — rag-01 lost rank 1 to test.py, rag-06 to its duplicate.)

### Trading-Logging-Automation — 10/10, MRR 0.93

| q | difficulty | gold | rank |
|---|---|---|---|
| trd-01 | keyword | scrape_content_investorplace.py | 1 |
| trd-02 | paraphrase | scrape_content_mastersintrading.py | 1 |
| trd-03 | keyword | scrape_content_mastersintrading.py | 1 |
| trd-04 | paraphrase | scrape_content_investorplace.py | 1 |
| trd-05 | paraphrase | bird.py | 1 |
| trd-06 | paraphrase | pacman.py | 1 |
| trd-07 | paraphrase | scrape_content_investorplace.py | 3 |
| trd-08 | conceptual | backup.py | 1 |
| trd-09 | keyword | scrape_content_mastersintrading.py | 1 |
| trd-10 | conceptual | bird.py | 1 |

Notes: trd-07 (rank 3) is the pre-registered near-duplicate hazard playing
out — `backup.py` (an older copy of the same scraper flow) took ranks 2 and 5.
trd-08 ("which file is the older, simpler backup...") hit at rank 1 — better
than expected for what is essentially a filename-only signal.

## Scope isolation

Every isolation-flagged question (22 across the 4 repos) was re-run in every
other repo's project scope (the two rekreate scopes are never cross-checked
against each other — they deliberately hold the same repo):

- **76 cross-scope recalls; 0 leaks.** No hit ever carried another project's
  scope, and no asking-repo's gold path ever surfaced in a foreign scope.
- **`count()` invariance: 0 mismatches.** After all five ingests:
  website 826, jeff-app 963, rekreate (partial) 9,106, rekreate--noenv 47,
  Trading 69 — each equal to its count immediately after its own ingest
  (the killed scope has no "after own ingest" reference; its count is
  reported for completeness).
- One subtle observed side effect (not a leak): two questions' gold ranks
  shifted by one between a scoring run done when the db held 2 projects and
  the final run with all 5 (e.g. web-10 rank 2 -> 1). The FTS index is a
  shared table, so BM25 corpus statistics — and therefore lexical-leg score
  *margins* — are not fully independent of other scopes' ingests, even though
  result membership strictly is. Worth knowing for multi-project stores;
  results themselves never crossed scopes.

## Health outputs (verbatim `health().to_dict()` after each ingest)

| repo | ok | identity | mode | total | checked | embedded | retrievable | notes |
|---|---|---|---|---|---|---|---|---|
| powered-by-people-website | True | match | vector+lexical+rerank | 826 | 3 | 3 | 3 | — |
| jeff-app-for-iphone | True | match | vector+lexical+rerank | 963 | 3 | 3 | 3 | — |
| rekreate-ai-chat (killed mid-ingest) | True | match | vector+lexical+rerank | 9106 | 3 | 3 | 3 | — |
| rekreate-ai-chat--noenv | True | match | vector+lexical+rerank | 47 | 3 | 3 | 3 | — |
| Trading-Logging-Automation | True | match | vector+lexical+rerank | 69 | 3 | 3 | 3 | — |

Honesty note: the drowned, killed-mid-ingest store also reads `ok=True` —
correctly, per health()'s contract (newest records embedded + retrievable).
health() measures index freshness, not corpus quality; a drowned store is
"healthy". Nothing in the API surface tells you your store is 99.7% vendored
noise.

## Findings (wins AND losses)

1. **Recall on first-party code is strong**: 39/40 pre-registered questions
   hit @5 across four repos when ingest wasn't drowned, most at rank 1 —
   including paraphrase and conceptual questions with no verbatim vocabulary
   overlap (e.g. "why do the tab buttons vibrate" -> haptic-tab.tsx at 1).
2. **Zero-config ingest has one genuinely pathological blind spot**: a
   checked-in `env/` virtualenv (`DEFAULT_SKIP_DIRS` has `venv`/`.venv` but
   not `env` or `site-packages`). Cost: ~500x wall-clock and a 6/10 vs 10/10
   recall difference on identical questions. Below-bar; issue proposed.
3. **Definition-vs-usage is the systematic ranking weakness**: the only clean
   MISS (web-09) plus the low in-window ranks (jef-03, rag-06 even in the
   clean store) were definition files (config/types) outranked by their
   consumers — and in the drowned store, by the vendored *implementation* of
   the very feature being asked about (rag-06/07).
4. **Lockfiles are expensive dead weight**: 53-74% of the two JS stores'
   chunks, 7/100 top-5 slots on tooling questions, majority of embed time.
5. **Secrets ingestion**: `credentials.json` entered the store as a normal
   record. The firewall screens injection, not credentials at rest.
6. **Near-duplicate files split rank** (backup.py vs live scraper; app.py vs
   app_backup.py): pre-registered as a hazard, observed as sub-1 ranks and
   one rank-3, never as a miss.
7. **health() is honest about what it measures — and silent about corpus
   composition**: it read `ok=True` on the drowned store, per contract.

## Proposed issues (for the conductor to file; no product source touched)

### Issue A (under-bar finding): zero-config `ingest_path` drowns in a checked-in `env/` virtualenv — first-party recall fails

**Repro**

```
python benchmarks/dogfood/run_dogfood.py --db <tmp>/dogfood.db --out <tmp>/r.json --repo rekreate-ai-chat
# vs
python benchmarks/dogfood/run_dogfood.py --db <tmp>/dogfood.db --out <tmp>/r.json --repo rekreate-ai-chat--noenv
```

(any repo with a committed `env/` virtualenv reproduces; this one has ~1,150
site-packages text files)

**Observed**: `DEFAULT_SKIP_DIRS` skips `venv`/`.venv` but not `env` (a very
common venv name; `python -m venv env` is in half the Flask tutorials) or
`site-packages`. Ingest walked 520+ files / 9,106 records (99.7% vendored) in
20 minutes before being killed, projected ~30-60 min; peak RSS ~8.9 GB. On
the drowned store, 4/10 pre-registered questions missed: 2 golds not yet
reached, and 2 present-but-outranked by the vendored library implementing the
feature asked about (`env/.../flask_cors/*` beat the app's own CORS config in
all 5 slots). With `skip_dirs=DEFAULT_SKIP_DIRS | {'env'}`: 2.2s, 47 records,
10/10.

**Expected**: zero-config ingest of a repo containing a virtualenv named
`env` should behave like one named `venv` — skip it. Suggest adding `env`
and `site-packages` to `DEFAULT_SKIP_DIRS` (matching the existing
`venv`/`.venv` intent), and/or an ingest-summary warning when one directory
subtree produces >N% of records.

**Config**: rekoll @ 20a0efb, fastembed 0.8.0 bge-small-en-v1.5 dim384,
reranker Xenova/ms-marco-MiniLM-L-6-v2, k=5, RRF k=60, sqlite, Windows 11,
16 CPUs.

### Issue B: lockfiles ingested by default — 53-74% of store chunks for zero recall value

**Repro**: `Memory(...).ingest_path(<any JS repo with package-lock.json>)`.

**Observed**: website store 439/826 chunks (53%) and Expo-app store 715/963
chunks (74%) are `package-lock.json`; lockfile chunks took 7/100 scoring
top-5 slots (all on tooling/config questions); the app's 66s ingest wall is
mostly lockfile embedding.

**Expected**: machine-generated lockfiles (`package-lock.json`, `yarn.lock`,
`pnpm-lock.yaml`, `poetry.lock`, `Cargo.lock`, ...) skipped by default, or a
documented default-exclude filename list alongside `DEFAULT_SKIP_DIRS`.

**Config**: as Issue A.

### Issue C: well-known secrets files enter the store as ordinary records

**Repro**: `Memory(...).ingest_path(<repo containing credentials.json>)`.

**Observed**: a Google OAuth client-secrets `credentials.json` was chunked,
embedded, and stored as a normal UNVERIFIED record; nothing flags it, and it
is now retrievable content in the memory db (and would be exported with it).
The firewall's screens target prompt injection, not credentials at rest.

**Expected (discuss)**: a default skip/flag list for well-known secret
filenames (`credentials.json`, `token.pickle`, `id_rsa`, `*.pem`,
`service-account*.json`, ...), or at minimum a documented warning that
ingest_path will store any text secret it can read. Related observation:
`redact_pii=False` is the constructor default.

**Config**: as Issue A.
