"""Content addressing and stable identifiers.

Rekoll IDs are *content-addressed*: a record's primary id is derived from its
scope + source + kind + a hash of its (normalized) content. Re-ingesting the
same content into the same scope yields the same id, which makes imports
idempotent by construction (ADR-0006); ``kind`` is part of the address
(ADR-0026) because each kind lives in its own physical table — one id shared
across two tables cross-wired metadata, the lexical index, and deletion. A
separate human-facing ``MEM-NNNN`` id keeps the git-auditable views legible.
"""

from __future__ import annotations

import hashlib
import unicodedata

__all__ = ["normalize_content", "content_hash", "record_id", "human_id"]


def normalize_content(content: str) -> str:
    """Normalize text before hashing so trivial differences don't change the id.

    NFC-normalize unicode, collapse CRLF/CR to LF, and strip surrounding
    whitespace. NFC also neutralizes some homoglyph/compatibility tricks at the
    addressing layer (the firewall does the heavier normalization in P2).

    Lone surrogates (Cs) are dropped: they are not valid Unicode scalars and would
    raise UnicodeEncodeError at ``.encode('utf-8')`` in ``content_hash`` below.
    The firewall's ``sanitize_unicode`` already strips them on the screened path;
    this covers a ``screen=False`` write that vouches for its own (invalid) bytes.
    """
    text = "".join(ch for ch in content if unicodedata.category(ch) != "Cs")
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def content_hash(content: str) -> str:
    """SHA-256 of the normalized content, hex-encoded."""
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def record_id(scope_key: str, source_uri: str, kind: str, chash: str) -> str:
    """Deterministic, content-addressed record id.

    ``rk_`` + first 24 hex chars of
    sha256(scope_key | source_uri | kind | content_hash).

    ``kind`` (the ``Kind`` value string, e.g. ``"raw_fact"``) is part of the
    address (ADR-0026): kinds live in SEPARATE physical tables (ADR-0001), so
    identical content stored as two kinds must be two records with two ids —
    one shared id cross-wired metadata/FTS (keyed by record id) and made
    ``forget(one_id)`` delete both. Idempotency is unchanged where it matters:
    same content + kind + source (in the same scope) is the same id.
    """
    payload = f"{scope_key}\x00{source_uri}\x00{kind}\x00{chash}".encode("utf-8")
    return "rk_" + hashlib.sha256(payload).hexdigest()[:24]


def human_id(n: int) -> str:
    """Stable, legible human id, e.g. ``MEM-0042``."""
    if n < 0:
        raise ValueError("human_id sequence must be non-negative")
    return f"MEM-{n:04d}"
