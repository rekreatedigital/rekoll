# ADR-0015 — The opt-in BYO-AI provider layer (cloud embedders + consolidator)

**Status:** Accepted · **Date:** 2026-07-02

## Context

ADR-0008 fixed the product decision: Rekoll works with NO API key by default
(local embeddings — the moat vs. key-first competitors), and there are two
separate AI slots — an embedding model and an optional learning/consolidation
LLM. What was missing is the layer that lets a user who *wants* cloud AI plug
in any provider's key without weakening a single default-path invariant
(zero required deps, zero egress, reads never call an LLM, LLM output never
sets trust).

## Decisions

1. **Standard-library HTTP, zero new dependencies.** Providers speak JSON over
   `urllib.request` (`rekoll/providers/_http.py`). No client SDKs, no
   `httpx`/`requests`, and therefore **no `[providers]` extra** — `pip install
   rekoll` alone can reach any provider the user opts into. Embedding and chat
   endpoints are simple JSON POSTs; retries (429/5xx, `Retry-After`-aware) and
   trimmed, credential-free error messages are implemented in ~100 lines.
   Trade-off accepted: no streaming and no connection pooling — irrelevant for
   batch embeddings and a single consolidation call.

2. **One OpenAI-compatible embedder class + a preset table.** Most providers
   (and local servers) expose OpenAI's wire format, so
   `OpenAICompatibleEmbedder(model, provider=..., base_url=...)` plus
   `PRESETS` (base URL + key env var + capability hints) covers OpenAI,
   DeepSeek, Qwen/DashScope, MiniMax, Moonshot/Kimi, Mistral, xAI, OpenRouter,
   Anthropic, Groq, Ollama, LM Studio, and `provider="custom"` for any
   self-hosted endpoint. Google's API differs → dedicated `GeminiEmbedder`
   (header auth, never `?key=` in the URL). `VoyageEmbedder` is the documented
   answer to "can I use my Claude key?" — Anthropic sells no embeddings API,
   so: embeddings → Voyage/Gemini/local; consolidation → yes, via Anthropic's
   OpenAI-compatible chat endpoint (`provider="anthropic"`).

3. **Opt-in is structural, not conventional.** `rekoll.providers` is imported
   ONLY when the user constructs a provider or names one
   (`Memory(embedder="openai:...")`). API-key env vars are read only inside
   that explicit construction — never on the default path. Constructing a
   provider opens no socket; the first network call happens on use (embedding,
   or the one-off dimension probe unless `dim=` is passed). CI gates:
   `rekoll.providers` absent from `sys.modules` after a no-args `Memory()`
   write+read cycle.

4. **An embedder registry mirroring the adapter registry.** `rekoll/embedders.py`
   resolves `"name"` / `"name:model"` specs with the same precedence as
   `rekoll.adapters.registry`: explicit `register_embedder` > entry-point group
   `rekoll.embedders` > built-ins (`stub`, `fastembed`, and the lazy provider
   names). Third-party packages ship `myname = "my_pkg:MyEmbedder"` (called as
   `factory(model_or_None, **kwargs)`) with no core change. `Memory._auto_embedder()`
   is untouched and local-only.

5. **Truthful identity for cloud vectors.** Every provider returns a real
   `EmbedderIdentity` — `name="provider:model"`, the actual dim, and a config
   hash over (base_url, model, dimension knobs); never the API key. The
   per-scope identity guard and the mixed-dim skip therefore work unchanged,
   and a declared `dim=` that disagrees with what the server returns raises
   instead of storing a lie.

6. **A minimal write-side consolidation seam.** `rekoll/consolidation.py`
   defines the dependency-free `Consolidator` Protocol (`summarize(texts) -> str`,
   mirroring `Reranker`); `rekoll.providers.OpenAICompatibleConsolidator` is the
   one reference implementation. `Memory.consolidate(ids=|query=, consolidator=...)`
   is the sole caller and is explicit-call-only: `Memory` holds no ambient
   consolidator, and nothing on the read path can invoke one. Output flows
   through the EXISTING ingest firewall and is stored as `kind=OBSERVATION`
   with `provenance.derived_from=<source ids>`,
   `declared_transformations=("llm_summary",)`, and **trust = MIN(source
   trusts)** — the LLM never chooses trust (ADR-0002), and quarantined records
   are never fed to it. The full L3 loop ({creates, updates, deletes} proposals
   with reasons, graduation) remains future work; this seam is deliberately the
   smallest thing that lets providers plug in today.

## Consequences

- "Bring any AI, or none" is now shipped, not aspirational — with the zero-dep,
  zero-key default intact and CI-gated.
- Offline tests exercise every provider against a local fake HTTP server;
  real-key smoke tests require an explicit `REKOLL_SMOKE=1` opt-in, so a stale
  key in a dev shell can never make the suite touch the network.
- We own a small HTTP transport (retries, error shaping) instead of an SDK
  dependency tree — accepted; it is ~100 lines and CI-pinned.
- The preset table will need occasional URL/capability updates as providers
  evolve; capability hints only soften error messages, so a stale hint can
  never block a working endpoint.
