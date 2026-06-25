"""Structure-aware chunking — keep one coherent unit per chunk.

Markdown splits on heading boundaries so a section/rule stays whole; other text
falls back to size-bounded splitting that prefers paragraph/line boundaries.
AST-based code chunking (tree-sitter) is a later P1 increment; this module is the
honest text/markdown baseline that already beats the naive char-window the dogfood
script started with.
"""

from __future__ import annotations

import re

__all__ = ["chunk_text", "chunk_markdown", "chunk_file", "DEFAULT_SIZE", "DEFAULT_OVERLAP"]

DEFAULT_SIZE = 800
DEFAULT_OVERLAP = 100
DEFAULT_MIN = 50
MD_MAX = 1500

_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)


def chunk_text(
    text: str,
    *,
    size: int = DEFAULT_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    min_size: int = DEFAULT_MIN,
) -> list[str]:
    """Split text into overlapping chunks, breaking on paragraph/line boundaries."""
    text = text.strip()
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        end = min(i + size, n)
        if end < n:
            window = text[i:end]
            br = window.rfind("\n\n")
            if br < size // 2:
                br = window.rfind("\n")
            if br >= size // 2:
                end = i + br
        piece = text[i:end].strip()
        if piece and (len(piece) >= min_size or end >= n):
            out.append(piece)
        if end >= n:
            break
        i = end - overlap if end - overlap > i else end
    return out


def chunk_markdown(
    text: str,
    *,
    max_size: int = MD_MAX,
    size: int = DEFAULT_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """Split markdown into heading-delimited sections; size-split oversized ones."""
    text = text.strip()
    if not text:
        return []
    starts = [m.start() for m in _HEADING.finditer(text)]
    if not starts:
        return chunk_text(text, size=size, overlap=overlap)

    sections: list[str] = []
    if starts[0] > 0:
        sections.append(text[: starts[0]])  # preamble before the first heading
    for a, b in zip(starts, starts[1:] + [len(text)]):
        sections.append(text[a:b])

    out: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= max_size:
            out.append(section)
        else:
            out.extend(chunk_text(section, size=size, overlap=overlap))
    return out


def chunk_file(name: str, text: str) -> list[str]:
    """Pick a chunker by filename extension."""
    if name.lower().endswith((".md", ".markdown")):
        return chunk_markdown(text)
    return chunk_text(text)
