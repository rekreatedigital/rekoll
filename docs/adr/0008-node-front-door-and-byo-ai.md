# ADR-0008 — Node-first front door; two separate AI slots; learning off by default

**Status:** Accepted · **Date:** 2026-06-23

## Context
The target user is often a non-technical "vibe coder." Pressure-testing showed a
default-Python tool fails them in the first five minutes (Python missing, wrong
version, unreadable stack trace) — exactly where MemPalace (hidden Python + 300 MB
download) and Hindsight (Docker + an API key up front) both stumble. That gap is
our opening.

## Decisions
1. **Front door = Node/`npx` MCP**, not pip: `claude mcp add rekoll -- npx -y
   rekoll-mcp`. Every Cursor/Claude Code user already has Node; the wrapper
   launches the bundled Python engine under the hood. `pip install rekoll` is
   door #2 (for people who already code); self-host is door #3.
   *(Shipped first: the Python stdio MCP server `rekoll-mcp` — `pip install
   "rekoll[mcp]"`, see docs/MCP.md — is the engine the future Node/`npx`
   wrapper will wrap, so Door 1's memory boundary is real and tested today
   while the no-Python wrapper is still pending.)*
2. **Two separate AI slots**, never conflated: (a) an **embedding** model —
   default **local**, no key; (b) an optional **learning/consolidation LLM** —
   bring-your-own. This prevents the "I'll use Claude for everything" footgun
   (Anthropic/Groq sell no embeddings → auto-fall-back to free local, documented).
3. **Learning loop OFF by default.** Save + search need no AI and no key; the
   product is fully useful with zero providers configured.
4. **Plain-English errors only** at the install/first-run surface (no stack
   traces), QA'd on a clean Windows box with no Python.

## Consequences
- "Easy for non-techies" becomes true, not aspirational — the differentiator.
- Requires building/maintaining a small `rekoll-mcp` Node wrapper (accepted).
- The "free, bring any AI" promise is honest with one documented asterisk (the
  embedding slot).
