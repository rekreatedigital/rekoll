# ADR-0012 — Code chunking: stdlib `ast` for Python now, tree-sitter later

**Status:** Accepted · **Date:** 2026-06-23

## Context
Size-based chunking splits code mid-function, which buries the thing you're
searching for. Code should chunk on semantic boundaries (function/class). The
design called for tree-sitter, but tree-sitter adds dependencies and per-language
grammar packages — at odds with the "core imports with zero required deps" rule.

## Decision
- **Python chunking uses the stdlib `ast`** (zero new dependency): each top-level
  function/class becomes one chunk (decorators included), module-level code is
  grouped, oversized units fall back to size-splitting, and unparseable code falls
  back to `chunk_text`. Python is also our own dogfood corpus, so this is the
  highest-value language to do first.
- **Other languages** continue through `chunk_text` for now. **tree-sitter** is
  deferred to an optional `[code]` extra (like `[embeddings]`), so multi-language
  AST chunking never bloats the core install.

## Consequences
- Code recall improves immediately for Python with no new dependency.
- `chunk_file` dispatches `.py` → `chunk_python`, `.md` → `chunk_markdown`, else
  `chunk_text`. Adding a language later is "add an extra + a dispatch branch."
- A function/class larger than `CODE_MAX` is still size-split (rare; acceptable).
