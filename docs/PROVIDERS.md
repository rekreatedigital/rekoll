# Bring your own AI — provider setup

Rekoll needs **no API key**: saving and searching memory runs on a local
embedder, free and private. That default never changes. This page is for the
*optional* upgrades — plugging your own cloud (or local-server) AI into
Rekoll's two AI slots (ADR-0008):

| Slot | Default | Bring-your-own |
|---|---|---|
| **Embeddings** (used by save + search) | Local, no key | Any provider below |
| **Consolidation LLM** (used ONLY by explicit `mem.consolidate(...)` calls) | Off | Any OpenAI-compatible chat model |

Everything here is **opt-in and zero-dependency**: providers speak
standard-library HTTP, nothing is imported (and no key env var is read) until
*you* construct or name a provider, and reads still never call an LLM.

```python
from rekoll import Memory

mem = Memory(embedder="openai:text-embedding-3-small")   # key from OPENAI_API_KEY
```

> **Switching embedders on an existing scope:** vectors from different models
> don't mix. Rekoll's identity guard warns and skips incompatible-dim vectors
> rather than corrupting results — re-ingest the scope or use a fresh
> `project=` when you switch.

## "Can I use my Claude (Anthropic) key?"

- **Embeddings: no.** Anthropic sells no embeddings API. Use **Voyage** (the
  provider Anthropic itself recommends), Gemini, or keep the free local
  default.
- **Consolidation: yes.** Anthropic serves an OpenAI-compatible chat endpoint:

```python
from rekoll.providers import OpenAICompatibleConsolidator

consolidator = OpenAICompatibleConsolidator("claude-haiku-4-5", provider="anthropic")
mem.consolidate(query="what did we decide about the database?", consolidator=consolidator)
```

## Embedding providers

Key resolution is always: explicit `api_key=` argument **>** the provider's
environment variable. Missing key → a clear error naming the exact variable.

### OpenAI-compatible (one class, many providers)

```python
from rekoll.providers import OpenAICompatibleEmbedder

emb = OpenAICompatibleEmbedder()                                  # OpenAI default model
emb = OpenAICompatibleEmbedder("text-embedding-v4", provider="qwen")
emb = OpenAICompatibleEmbedder("nomic-embed-text", provider="ollama")   # local, keyless
mem = Memory(embedder=emb)          # or: Memory(embedder="qwen:text-embedding-v4")
```

| `provider=` | Base URL (override with `base_url=`) | Key env var | Embeddings? | Chat (consolidation)? |
|---|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | ✅ | ✅ |
| `qwen` (DashScope)¹ | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | ✅ | ✅ |
| `mistral` | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | ✅ | ✅ |
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | — ² | ✅ |
| `minimax` | `https://api.minimax.io/v1` | `MINIMAX_API_KEY` | — ² | ✅ |
| `moonshot` / `kimi` | `https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` | — ² | ✅ |
| `xai` | `https://api.x.ai/v1` | `XAI_API_KEY` | — ² | ✅ |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | — ² | ✅ |
| `anthropic` | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` | — (use Voyage) | ✅ |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | — ² | ✅ |
| `ollama` | `http://localhost:11434/v1` | *(none — local)* | ✅ | ✅ |
| `lmstudio` | `http://localhost:1234/v1` | *(none — local)* | ✅ | ✅ |
| `custom` | *(you pass `base_url=`)* | *(optional `api_key=`)* | your server | your server |

¹ Mainland-China endpoints exist for qwen/minimax/moonshot — pass the regional
URL via `base_url=`.
² No embeddings endpoint as of this table; these presets are still useful for
the consolidation slot. If the provider adds embeddings later it just works —
capability columns only shape error messages, they never block a call.

Useful knobs: `dimensions=` (OpenAI v3 shortened vectors), `dim=` (skip the
one-off dimension probe), `batch_size=`, `timeout=`, `retries=`,
`extra_headers=` (e.g. OpenRouter attribution headers).

`provider="custom"` covers vLLM, LiteLLM proxies, llama.cpp server, Xiaomi
MiMo, or anything else speaking the OpenAI wire:

```python
emb = OpenAICompatibleEmbedder("my-model", provider="custom",
                               base_url="http://localhost:8000/v1")
```

### Gemini (Google)

```python
from rekoll.providers import GeminiEmbedder

emb = GeminiEmbedder()                            # gemini-embedding-001
emb = GeminiEmbedder(output_dimensionality=768)   # shortened vectors
mem = Memory(embedder=emb)                        # or: Memory(embedder="gemini")
```

Key: `api_key=` > `GEMINI_API_KEY` > `GOOGLE_API_KEY`. Auth goes in the
`x-goog-api-key` header — never a `?key=` URL parameter (those leak into logs).

### Voyage (the Anthropic-ecosystem choice)

```python
from rekoll.providers import VoyageEmbedder

emb = VoyageEmbedder()                             # voyage-3.5, VOYAGE_API_KEY
emb = VoyageEmbedder("voyage-3-large", output_dimension=1024)
mem = Memory(embedder=emb)                         # or: Memory(embedder="voyage")
```

## The consolidation slot (explicit, write-side only)

`mem.consolidate(...)` merges existing memories into ONE derived observation
using a chat model you choose. It never runs on its own and never inside
`recall()` — you call it, per call:

```python
from rekoll.providers import OpenAICompatibleConsolidator

consolidator = OpenAICompatibleConsolidator("gpt-4o-mini")            # or any preset above
record = mem.consolidate(query="database decisions", k=20, consolidator=consolidator)
# or pin exact sources:  mem.consolidate(ids=[r1.id, r2.id], consolidator=consolidator)
```

What you get back is a first-class, auditable record — not an opaque blob:

- `kind=OBSERVATION`, content firewall-screened like every ingest;
- `provenance.derived_from` = the source record ids;
- `declared_transformations=("llm_summary",)`;
- **trust = the MINIMUM trust of the sources** — the LLM never chooses trust,
  and quarantined memories are never even sent to it.

By default only `TRUSTED_SOURCE`-and-above records are eligible; loosen
deliberately with `min_source_trust=TrustTier.UNVERIFIED`.

## Registering your own embedder

In code:

```python
from rekoll import Memory, register_embedder

register_embedder("acme", lambda model=None, **kw: AcmeEmbedder(model or "acme-v1", **kw))
mem = Memory(embedder="acme:acme-v2")
```

Or as an installable package — Rekoll discovers the entry point with no core
change (resolution order: explicit registration > entry point > built-ins):

```toml
[project.entry-points."rekoll.embedders"]
acme = "acme_rekoll:AcmeEmbedder"     # called as AcmeEmbedder(model_or_None, **kwargs)
```

Your class just implements the `Embedder` protocol: `dim`, `identity()`
(truthful `EmbedderIdentity` — real name, real dim, config hash, never the
key), and `embed(texts)`.

## Troubleshooting

- **`ValueError: ... needs an API key`** — set the env var it names, or pass
  `api_key=...` explicitly.
- **`HTTP 401`** — the key is wrong/expired; the message includes the server's
  explanation but never your key.
- **`HTTP 404 ... may not offer an embeddings endpoint`** — that provider is
  chat-only (see the table); pick Voyage/Gemini/local for embeddings.
- **"this scope was embedded with X, but the current embedder is Y"** — you
  switched models mid-scope; re-ingest or use a fresh `project=`.
- **Testing without spending money** — the offline suite fakes every provider
  locally; real-key smoke tests run only with `REKOLL_SMOKE=1` set.
