"""The high-level ``Memory`` facade — the drop-in SDK (Door 2).

Ties the whole engine together behind two verbs so a user never wires adapters,
embedders, the firewall, retrieval, and the reranker by hand::

    from rekoll import Memory
    mem = Memory(project="myapp")                 # local, private, firewall on
    mem.remember("we chose Postgres over BigQuery for cost")
    print(mem.recall("why postgres?").context())  # LLM-ready, safe data envelope

Defaults: local SQLite store, real local embeddings + reranker if the
``embeddings`` extra is installed (else the stub), firewall ON, reads call no LLM.

Beyond the two verbs, the facade carries the memory-quality loop:
``mark_used``/``informed_by`` (the was-it-used usage signal), ``health()``
(source-vs-index freshness), ``self_test()`` (golden probe), and honest
degradation via ``RecallResult.mode`` (ADR-0024).
"""

from __future__ import annotations

import fnmatch
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Sequence, Union

from .adapters.base import (
    BOARD_METADATA_KEY,
    BOARD_TAG_MAJOR,
    BOARD_TAG_PENDING,
    CAP_LEXICAL,
    QueryHit,
    StorageAdapter,
    UnsupportedCapabilityError,
)
from .adapters.registry import get_adapter
from .board import DEFAULT_BOARD_RULES_LIMIT, build_board_payload
from .chunking import chunk_file
from .consolidation import Consolidator
from .embedding import Embedder, StubEmbedder, compare_identity
from .firewall import (
    DIRECTIVE_FLOOR,
    ContextEnvelope,
    build_envelope,
    sanitize_unicode,
    screen_pieces,
    screened_record,
)
from .ledger import LedgerEntry, RecallLedger
from .model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from .retrieval import hybrid_search

__all__ = [
    "Memory",
    "RecallResult",
    "HealthReport",
    "BoardResult",
    "DEFAULT_INGEST_TRUST",
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_CHUNKS_PER_DOC",
    "DEFAULT_MAX_PINNED_DIRECTIVES",
]

# Files and bulk documents are third-party by nature: ingestion defaults to
# UNVERIFIED so the firewall can quarantine injection markers (quarantine only
# fires at trust <= UNVERIFIED). Only first-person ``remember()`` follows the
# constructor's ``default_trust``. Pass ``trust=`` to vouch for a source you
# control (ADR-0016).
DEFAULT_INGEST_TRUST = TrustTier.UNVERIFIED

# Resource limits (ADR-0018). A single un-chunked memory: past ~100k chars it is
# a document, not a fact — chunk it via ingest_text/ingest_path instead. A single
# ingested file/document: 10 MiB of TEXT (~2 500 pages) — bigger inputs are
# almost never prose and reading them unbounded is a memory-exhaustion vector.
# The byte cap bounds BYTES, not WORK: a heading-per-line markdown document
# chunks at ~0.25 chunks/byte (~2.6M chunks at the byte cap), so one document's
# CHUNK COUNT is capped too. 25k clears the largest legitimate yield (a 10 MiB
# plain-text file at the default stride is ~15k chunks) with headroom; past it
# the ingest is REJECTED (ingest_text raises; ingest_path skips + counts the
# file) — never silently truncated.
DEFAULT_MAX_CONTENT_CHARS = 100_000
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CHUNKS_PER_DOC = 25_000

# The standing-directive channel's cap (ADR-0034). On EVERY recall, up to this
# many active, in-scope directives at/above the directive floor ALWAYS surface in
# the envelope's instruction channel, independent of the query — so a saved rule
# ("always explain simply") never silently vanishes just because it did not rank
# into the top-k. BOUNDED on purpose: the product sells cheap, bounded reads, and
# unbounded pinning would re-introduce token cost on every read. Under the cap the
# read is oldest-first, so the foundational rules survive; 0 disables the channel
# (rank-only, exactly the pre-ADR-0034 behavior). Overridable per Memory via
# ``max_pinned_directives`` (ADR-0018-style: a knob, never silently unbounded).
DEFAULT_MAX_PINNED_DIRECTIVES = 5

DEFAULT_INCLUDE_EXT = {
    ".py", ".md", ".markdown", ".txt", ".rst", ".toml", ".yml", ".yaml",
    ".json", ".cfg", ".ini", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
}
DEFAULT_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".rekoll", "node_modules",
    "dist", "build", "site-packages", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

# Filename-level ingest filter (ADR-0027) — the glob twin of DEFAULT_SKIP_DIRS.
# Applied by the directory WALK only: pointing ingest_path straight at one file
# is explicit intent and is never blocked. Two tiers, different intents:
#
# - Lockfiles are machine-generated dependency pins — in real JS repos they were
#   53-74% of all stored chunks for zero recall value (issue #28). Skipped
#   silently, like any other non-content file.
# - Secret-named files (a real Google OAuth credentials.json was chunked,
#   embedded, and stored as an ordinary retrievable record — issue #29) are
#   skipped AND warned: the skip must be visible, and ingesting one anyway
#   (via override or direct file path) is warned too — never silent.
#
# Both lists are defaults for the ``skip_files`` parameter: pass your own set
# to replace them, or ``skip_files=set()`` to disable filename filtering.
DEFAULT_SKIP_LOCKFILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "bun.lockb", "Gemfile.lock", "composer.lock",
})
DEFAULT_SKIP_SECRETS = frozenset({
    "credentials.json", "id_rsa", "id_ed25519", "*.pem", "*.key",
    ".env", "service-account*.json", "token.pickle",
})
DEFAULT_SKIP_FILES = DEFAULT_SKIP_LOCKFILES | DEFAULT_SKIP_SECRETS


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitively glob-match a bare filename against ``patterns``.

    ``fnmatchcase`` on lowered strings keeps behavior identical across
    platforms (plain ``fnmatch`` is case-sensitive on POSIX only), and these
    well-known names are conventions, not case-exact identifiers.
    """
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pat.lower()) for pat in patterns)


def _utf8_safe(text: str) -> str:
    """Drop lone surrogates (invalid UTF-8) so ``.encode('utf-8')`` in the embedder
    never crashes on host content. ``sanitize_unicode`` strips them on the screened
    path; this guards the raw-content embed on the ``screen=False`` path (a host
    may vouch for its own writes, but must not be able to crash a read/write with
    an un-encodable string). Round-trips through surrogatepass so only the invalid
    surrogate bytes are dropped; everything else is byte-identical."""
    return text.encode("utf-8", "surrogatepass").decode("utf-8", "ignore")

# health() retrievability probe. FULL content (capped) — a short head slice made
# the probe blind on near-duplicate corpora (e.g. a repo ingest where chunks
# share a license-header prefix): the discriminating tail tokens never entered
# the query and healthy stores read stale. 2048 covers every chunker output
# (MD_MAX=1500 / CODE_MAX=2000) whole. The membership window is widened to
# ≥20 so near-ties — and, later, approximate ANN backends whose self-match
# isn't guaranteed top-5 — don't read as dead ingestion.
_PROBE_MAX_CHARS = 2048
_PROBE_MIN_POOL = 20


# -- filesystem containment (a walk must never read outside its root) ----------
# A planted link inside an ingested tree can point anywhere on disk. The load-
# bearing guard is REAL-PATH CONTAINMENT: resolve every directory we would
# descend and every file we would read, and refuse anything whose real location
# is not inside the resolved root. ``os.path.realpath`` resolves symlinks AND
# NTFS junctions (a directory reparse point; ``is_symlink()`` returns False for
# one, so a walk that only trusted it would descend a junction and leak), so
# containment catches symlinks, junctions, and any redirecting reparse point on
# every OS — not a hard-coded list.

def _real_path(path) -> Path:
    """The fully link-resolved real path (``os.path.realpath`` resolves symlinks
    AND Windows junctions/reparse points; on a missing path it just absolutizes)."""
    return Path(os.path.realpath(os.fspath(path)))


def _within(root_real: Path, path) -> bool:
    """True iff ``path`` resolves (link-followed) to inside ``root_real`` (which
    must already be a real path). Fail-safe: an unresolvable path reads as
    outside, so the caller skips/refuses it rather than reading it."""
    try:
        real = _real_path(path)
    except OSError:  # pragma: no cover - resolution failure is treated as escape
        return False
    return real == root_real or real.is_relative_to(root_real)


def _redirects_out(path) -> bool:
    """True iff ``path`` is a link that RESOLVES ELSEWHERE than where it sits — a
    symlink, an NTFS junction, or a mount point. Used only to decide whether a
    directly-pointed target should warn+skip.

    Deliberately NOT "is this a reparse point": a non-redirecting reparse point
    (a OneDrive Files-On-Demand placeholder, a Windows Dedup stub) has its real
    path equal to its own location and is a legitimate in-tree file to read —
    flagging those on the reparse attribute alone would silently drop real source
    files. We instead ask whether resolving the leaf lands somewhere other than
    ``<resolved parent>/<name>``. Comparing against the resolved PARENT (not the
    literal abspath) means a symlinked ANCESTOR — e.g. macOS ``/tmp`` ->
    ``/private/tmp`` — does not false-positive the leaf."""
    p = Path(os.path.abspath(os.fspath(path)))
    try:
        if p.is_symlink():  # POSIX symlinks (and the monkeypatched skip tests)
            return True
        here = os.path.join(os.path.realpath(p.parent), p.name)
        return os.path.normcase(os.path.realpath(p)) != os.path.normcase(here)
    except OSError:  # pragma: no cover - unresolvable: treat as a redirect (skip)
        return True


def _quarantine_split_marker(record: MemoryRecord, marker_count: int) -> None:
    """Quarantine a chunk flagged by the whole-document marker scan
    (``firewall.screen_pieces``): a marker the chunker SPLIT across a boundary
    trips no per-chunk screen, so the document-level decision is propagated
    here — mirroring exactly what ``screened_record`` does when a chunk trips
    its own screen (status + trust to QUARANTINED, ``injection_flags`` noted).
    Runs at the ingestion boundary, deterministic and LLM-free (ADR-0002/0013).
    """
    record.status = Status.QUARANTINED
    record.trust_tier = TrustTier.QUARANTINED
    md = dict(record.metadata)
    md["injection_flags"] = max(int(md.get("injection_flags") or 0), marker_count)
    record.metadata = md


def _auto_embedder() -> Embedder:
    try:
        from .embedding import FastEmbedEmbedder

        embedder = FastEmbedEmbedder()
        _ = embedder.dim  # force model load now so a failure falls back cleanly
        return embedder
    except Exception:
        return StubEmbedder()


def _auto_reranker():
    try:
        from .reranking import CrossEncoderReranker

        return CrossEncoderReranker()
    except Exception:
        return None


@dataclass(frozen=True)
class RecallResult:
    """What ``Memory.recall`` returns: ranked hits + helpers to use them safely.

    ``mode`` names exactly what ran to produce these hits — the honest-
    degradation contract ("don't bluff a broken index"): a caller or agent can
    always tell a full hybrid ranking (``"vector+lexical+rerank"``) from a
    degraded one (``"lexical-only: embedder mismatch"``) or a semantics-free
    one (``"vector+lexical (stub-embedder)"``), instead of treating every
    result list as equally trustworthy. ``mode`` is deliberately NOT rendered
    into :meth:`context` — the envelope stays a pure function of the hits so
    agent prompt caches aren't busted (see ``ContextEnvelope.render``).

    ``mode`` reports the pipeline that RAN, not a completeness guarantee:
    read-time tamper verification (``retrieval._verify_hits``, ADR-0019)
    withholds any hit whose stored hash no longer matches its content, so even
    a full-hybrid mode can arrive with fewer hits than ``k`` — or none (see
    :meth:`Memory.recall`: ``k`` is an upper bound, not a promise).

    ``abstained`` is True when a ``min_score`` gate refused the query outright
    (ADR-0028): nothing was close enough, so nothing is returned. This is the
    one case where zero hits does NOT mean "the store had nothing" — and it is
    exactly why the flag exists. ``mode`` names it too, with the numbers.

    ``top_vector_score`` is the top-1 cosine similarity from the vector leg,
    captured before fusion, over hits that were allowed to surface — the
    quantity ``min_score`` is compared against. It is None whenever no
    cosine-metric vector leg produced a surfacable candidate. It is NOT
    ``hits[0].score`` (that is an RRF or reranker score); read it here to pick
    a threshold from your own data.

    ``pinned_directives`` is the STANDING-DIRECTIVE CHANNEL (ADR-0034): the
    active, in-scope ``Kind.DIRECTIVE`` records at/above the directive floor,
    fetched deterministically on the recall that produced this result so a saved
    rule ALWAYS surfaces in :meth:`envelope`'s / :meth:`context`'s instruction
    channel — even for an UNRELATED query, and even when the recall abstained
    (``abstained=True``, zero hits). They ride ONLY the directive channel: they
    deliberately do NOT appear in :meth:`ids` / :meth:`records` / :meth:`texts`
    (those stay the RANKED hits — ``forget(*recall(q).ids())`` must never delete a
    standing rule) and are NOT counted by ``len(result)``.
    """

    hits: tuple[QueryHit, ...]
    mode: str = "unspecified"
    abstained: bool = False
    top_vector_score: Optional[float] = None
    #: Standing directives that ALWAYS surface (ADR-0034); see the class docstring.
    #: Empty by default so a bare ``RecallResult(hits)`` behaves as before.
    pinned_directives: tuple[MemoryRecord, ...] = ()

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def texts(self) -> list[str]:
        return [h.record.content for h in self.hits]

    def ids(self) -> list[str]:
        """Record ids in rank order — e.g. ``mem.forget(*mem.recall(q).ids())``."""
        return [h.record.id for h in self.hits]

    def records(self) -> list[MemoryRecord]:
        return [h.record for h in self.hits]

    def sources(self) -> list[Optional[dict]]:
        """Where each hit CAME FROM, in rank order — parallel to :meth:`ids`
        (ADR-0037 §8).

        One entry per ranked hit, positionally aligned with ``ids()``: a
        ``{"file": str, "chunk": int | None}`` dict for a hit that was ingested
        from a file, or ``None`` for one that was not. ``remember``ed records
        legitimately have no file, so ``None`` is an ordinary answer, not a
        degradation — the entry is present either way so the list length always
        equals ``len(ids())``.

        The point of the pointer is CORRECTION AT THE SOURCE: when a recalled
        memory is wrong and it came from a file, the fix belongs in that file —
        edit the index instead and the file re-poisons it on the next ingest.

        ``chunk`` is the chunk index within that file and is nullable in its own
        right: :class:`~rekoll.model.Provenance` allows a ``source_file`` with no
        ``chunk_index``, so the payload reports that honestly rather than
        inventing a 0.

        This method IS the SDK door, and it is also the ONE builder behind the
        other two doors' ``sources`` key (CLI ``recall --json``, MCP ``recall``)
        — the same discipline :meth:`ids` and :meth:`directives` follow, so the
        three cannot drift.

        Unrelated to ADR-0037's *tracked sources* registry (a planned
        ``Memory``-level surface); this is per-hit provenance, read-side only.
        """
        out: list[Optional[dict]] = []
        for hit in self.hits:
            prov = hit.record.provenance
            out.append(
                None
                if prov.source_file is None
                else {"file": prov.source_file, "chunk": prov.chunk_index}
            )
        return out

    def envelope(self) -> ContextEnvelope:
        """The framed DATA envelope: standing directives (``pinned_directives``)
        plus ranked hits, split into the instruction and evidence channels
        (:func:`rekoll.firewall.build_envelope`)."""
        return build_envelope(self.hits, pinned=self.pinned_directives)

    def context(self) -> str:
        """LLM-ready string: memories framed as DATA, never as instructions."""
        return self.envelope().render()

    def directives(self) -> list[str]:
        """The recall envelope's directive channel as a list — the standing
        (always-surfaced) rules plus any ranked directives, neutralized and
        deduped exactly as :meth:`context` renders them (ADR-0034). The
        machine-readable twin of the ``# Trusted directives`` block, exposed
        identically across the SDK, CLI ``--json`` and MCP ``recall`` doors."""
        return list(self.envelope().directives)


@dataclass(frozen=True)
class HealthReport:
    """What ``Memory.health`` returns: source-of-truth-vs-index freshness.

    ``ok`` is True when the newest checked records are both embedded and
    retrievable AND the embedder identity matches; False when anything is
    stale/degraded; None when there was nothing checkable (empty scope, or the
    adapter can't enumerate newest records).
    """

    ok: Optional[bool]
    identity: str  # "match" | "mismatch" | "unknown"
    mode: str  # what recall() runs right now (honest-degradation string)
    total: int  # records in scope
    checked: int  # newest ACTIVE records actually checked
    embedded: int  # of checked, how many carry a vector
    retrievable: int  # of checked, how many an actual search surfaces
    stale_ids: tuple[str, ...] = ()  # checked records that failed either leg
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-safe view — the seam ``rekoll doctor`` (CLI) renders from."""
        return {
            "ok": self.ok,
            "identity": self.identity,
            "mode": self.mode,
            "total": self.total,
            "checked": self.checked,
            "embedded": self.embedded,
            "retrievable": self.retrievable,
            "stale_ids": list(self.stale_ids),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class BoardResult:
    """What :meth:`Memory.board` returns: the live project board (ADR-0035).

    A thin, typed view over :func:`rekoll.board.build_board_payload` — the ONE
    builder every door renders, so the SDK, CLI and MCP boards cannot drift.
    This class re-shapes that payload, it never recomputes a leg: ``to_dict()``
    reproduces the builder's dict exactly, including key order, so
    ``json.dumps`` of either is BYTE-IDENTICAL for the same store (pinned by
    test). Hosts can therefore keep the builder's cheap change check — byte-
    compare two payloads — while holding a typed object.

    ``rules`` are the standing directives (the same records recall pins into
    its instruction channel); ``majors``/``recent`` are entry dicts (the fixed
    key set ``id/kind/trust/created_at/board/text``, oldest-first and
    newest-first respectively); ``pending_open`` is the FULL count of open
    pending items, not capped by ``major_limit``; ``latest`` is a freshness
    HINT (see :meth:`Memory.board`).

    The legs are tuples of READ-ONLY mappings — in-place mutation of an entry
    (``result.majors[0]["text"] = ...``) raises ``TypeError``, and the entries
    are copies detached from the builder's dicts — because a board handed to
    several concurrent sessions must not be mutable under any of them (pinned
    by test). ``to_dict()`` hands back plain, freshly-copied lists/dicts for
    JSON.
    """

    rules: tuple[str, ...]
    majors: tuple[Mapping[str, object], ...]
    recent: tuple[Mapping[str, object], ...]
    pending_open: int
    latest: Optional[str]

    def to_dict(self) -> dict:
        """JSON-safe view — byte-identical to ``build_board_payload``'s dict.

        Key order is the builder's constant key set, and each entry dict is
        copied in its stored insertion order, so neither this method nor the
        builder can serialize differently for the same rows.
        """
        return {
            "rules": list(self.rules),
            "majors": [dict(entry) for entry in self.majors],
            "recent": [dict(entry) for entry in self.recent],
            "pending_open": self.pending_open,
            "latest": self.latest,
        }


class Memory:
    def __init__(
        self,
        path: str = "./.rekoll/memory.db",
        *,
        project: str = "default",
        tenant: str = "default",
        agent: str = "default",
        backend: str = "sqlite",
        embedder: Optional[Union[Embedder, str]] = None,
        reranker: object = "auto",
        screen: bool = True,
        default_trust: TrustTier = TrustTier.OWNER,
        redact_pii: bool = False,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_chunks_per_doc: int = DEFAULT_MAX_CHUNKS_PER_DOC,
        max_pinned_directives: int = DEFAULT_MAX_PINNED_DIRECTIVES,
    ) -> None:
        """``default_trust`` applies to first-person ``remember()`` calls ONLY.

        Bulk ingestion (``ingest_text`` / ``ingest_path``) always defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) regardless of this setting, so a
        high default can never silently exempt third-party files from the
        firewall's quarantine (ADR-0016).

        ``redact_pii`` (default False, ADR-0022) opts into scrubbing emails, US
        SSNs, and phone numbers from the CONTENT of every write, on top of the
        always-on secret redaction. It is off by default because code and git
        history are full of legitimate emails and number sequences that
        default-on redaction would corrupt, gutting recall and provenance.

        SCOPE — content only. Redaction rewrites the stored CONTENT; it does NOT
        touch the caller-supplied ``source`` / ``metadata`` or an ingested file's
        path, which are structural provenance stored verbatim (scrubbing a path
        like ``src/jane/util.py`` would corrupt "which file did this come from?").
        So do not place PII in a ``source=`` label, a ``metadata`` value, or a
        filename you ingest under ``redact_pii=True`` — scrub those yourself.

        RETROACTIVE TRAP — turning ``redact_pii`` on is NOT retroactive. It
        scrubs writes made AFTER it is set; PII already stored while it was off
        stays in the store verbatim (find and ``forget()`` those records to
        remove it). And re-ingesting the same source to "apply" redaction does
        NOT replace the old record: ids are content-addressed on the
        POST-screening content, so the redacted copy hashes to a DIFFERENT id and
        is stored ALONGSIDE the un-redacted original — you end up with both. Set
        ``redact_pii=True`` BEFORE first ingesting PII-bearing content.

        ``max_content_chars`` caps one ``remember()`` record; ``max_file_bytes``
        caps one ingested file/document's bytes; ``max_chunks_per_doc`` caps how
        many chunks one document may yield (bytes alone don't bound work — a
        heading-per-line document chunks at ~0.25 chunks/byte). All ADR-0018:
        overridable, never disable-able to zero.

        ``max_pinned_directives`` (default 5, ADR-0034) caps the STANDING-DIRECTIVE
        channel: on EVERY recall, up to this many active, in-scope directives
        at/above the directive floor ALWAYS surface in the envelope's instruction
        channel, independent of the query — so a saved rule never silently
        vanishes just because it did not rank into the top-k. Bounded on purpose
        (unbounded pinning would re-introduce token cost on every read); under the
        cap the oldest (foundational) directives are kept. Set it higher if you
        maintain more than five standing rules, or ``0`` to disable the channel
        (recall reverts to surfacing a directive only when it ranks in).
        """
        if path is None or not str(path).strip():
            # An empty (or None — e.g. an unset env var passed straight in)
            # path used to fall through to the ':memory:' branch: the store
            # LOOKED fine but was ephemeral, and every write evaporated on
            # close. Ephemeral must be an explicit opt-in, never a typo.
            raise ValueError(
                "path is empty; pass a real database file path, or ':memory:' "
                "to explicitly opt into an ephemeral in-memory store"
            )
        if max_content_chars <= 0 or max_file_bytes <= 0 or max_chunks_per_doc <= 0:
            raise ValueError(
                "max_content_chars, max_file_bytes and max_chunks_per_doc must be positive"
            )
        if max_pinned_directives < 0:
            # 0 is legal — it disables the standing-directive channel (rank-only,
            # pre-ADR-0034). Negative is a bug (an unbounded/negative cap has no
            # meaning), so refuse it loudly rather than silently clamping.
            raise ValueError(
                "max_pinned_directives must be >= 0 (0 disables the "
                "standing-directive channel)"
            )
        self.scope = Scope(tenant=tenant, project=project, agent=agent)
        self._screen = screen
        self._default_trust = default_trust
        self._redact_pii = redact_pii
        if redact_pii and not screen:
            # Warn, never block (project posture): redact_pii runs INSIDE the
            # firewall screen, so screen=False makes it a silent no-op — the host
            # asked to scrub PII AND to disable the firewall, and the latter wins.
            # (consolidate() force-screens regardless, so its output is unaffected.)
            warnings.warn(
                "[rekoll] redact_pii=True has NO EFFECT while screen=False: the "
                "firewall that performs redaction is disabled, so PII (and secrets) "
                "are stored unredacted. Set screen=True to redact, or drop "
                "redact_pii=True to silence this.",
                stacklevel=2,
            )
        self._max_content_chars = max_content_chars
        self._max_file_bytes = max_file_bytes
        self._max_chunks_per_doc = max_chunks_per_doc
        self._max_pinned_directives = max_pinned_directives

        if backend == "sqlite" and path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.adapter: StorageAdapter = get_adapter(backend, path=str(path))
        elif backend == "sqlite":
            self.adapter = get_adapter(backend, path=":memory:")
        else:
            self.adapter = get_adapter(backend)

        if isinstance(embedder, str):
            # Spec string, e.g. "openai:text-embedding-3-small" — the explicit
            # opt-in that may reach rekoll.providers. The default (None) never does.
            from .embedders import get_embedder

            self.embedder = get_embedder(embedder)
        else:
            self.embedder = embedder or _auto_embedder()
        # Reranker resolution is DYNAMIC, not frozen here (ADR-0029). Under the
        # default reranker='auto' the cross-encoder attaches ONLY when the scope
        # is degraded to lexical-only by an embedder mismatch — its one measured
        # win (MRR +0.158, p=2.6e-03) — and stays OFF in normal hybrid, where the
        # ablation found +60% read latency for no detectable lift. The live
        # decision is the ``reranker`` property, which re-reads _identity_state
        # every access (so a runtime reindex() clearing a mismatch turns it back
        # off) and constructs the auto model lazily — never at import, and never
        # in normal hybrid. An explicit reranker= is honored verbatim, including
        # None. Resolving 'auto' here would freeze the wrong choice: _identity_
        # state is not computed until below, and reindex() can flip it later.
        self._reranker_auto = reranker == "auto"
        self._reranker = None if self._reranker_auto else reranker
        self._auto_reranker_resolved = False
        #: Process-local was-it-used ledger: which ids each recall surfaced.
        self.ledger = RecallLedger()

        existing = self.adapter.get_embedder_identity(scope=self.scope)
        current = self.embedder.identity()
        if existing is None:
            # Fresh scope: the current embedder claims it — a match from here on.
            self.adapter.set_embedder_identity(scope=self.scope, identity=current)
            self._identity_state = "match"
        else:
            self._identity_state = compare_identity(existing, current)
        if self._identity_state == "mismatch":
            # Refuse-and-degrade (ADR-0024): a silent model/config swap is the
            # classic silent recall killer — vectors from two embedders are not
            # comparable, so ranking across them returns confidently-wrong
            # results. We refuse the vector leg (reads go lexical-only, writes
            # store no vector) instead of bluffing, and instead of hard-failing
            # the whole store. Show the FULL identity (name + dim + config) — a
            # dim/config-only swap under the same model name would otherwise
            # print an identical-looking message. Routed through warnings so
            # hosts can filter/capture it.
            warnings.warn(
                f"[rekoll] this scope was embedded with {existing.name!r} "
                f"(dim={existing.dim}, config={existing.config_hash}), but the current embedder "
                f"is {current.name!r} (dim={current.dim}, config={current.config_hash}). "
                f"The vector leg is REFUSED for this scope (ADR-0024): recall degrades to "
                f"lexical-only (see RecallResult.mode) and new writes are stored without "
                f"vectors. To restore vector recall, call Memory.reindex() to re-embed this "
                f"scope with the current embedder (do NOT just re-ingest: identical content is "
                f"content-addressed and re-ingesting it under the mismatch stores no vector). "
                f"Or open a separate scope for the new embedder.",
                stacklevel=2,
            )

    @property
    def reranker(self):
        """The reranker a recall would use RIGHT NOW — a DYNAMIC decision (ADR-0029).

        An explicit ``reranker=`` passed to the constructor is returned verbatim,
        including ``None``: explicit intent is never second-guessed. Under the
        default ``reranker='auto'`` the cross-encoder attaches ONLY when this
        scope is degraded to lexical-only by an embedder mismatch (ADR-0024) —
        the one place the ablation found it measurably helps (MRR +0.158,
        p=2.6e-03) — and is OFF in normal hybrid, where it cost +60% read
        latency for no detectable quality lift (issue #37).

        The value is re-read every access, so it is never frozen at construction:
        a scope that starts mismatched and is repaired by :meth:`reindex` stops
        reranking with no rebuild. The auto model is constructed lazily the first
        time a degraded scope asks for it (then memoized) — never at import, and
        never in normal hybrid, so the zero-dep default path stays cost-free.

        Read-only: the resolved reranker follows the scope's live state, so it is
        not a settable attribute — pass ``reranker=`` to the constructor instead.
        """
        if not self._reranker_auto:
            return self._reranker
        if self._identity_state != "mismatch":
            return None
        if not self._auto_reranker_resolved:
            # Lazy + memoized: the model loads at most once, and only in a scope
            # that is actually degraded — the +60% latency is never paid, and no
            # heavy import happens, on the normal hybrid path.
            self._reranker = _auto_reranker()
            self._auto_reranker_resolved = True
        return self._reranker

    # -- write --------------------------------------------------------------
    def remember(
        self,
        content: str,
        *,
        kind: Kind = Kind.RAW_FACT,
        source: str = "user",
        trust: Optional[TrustTier] = None,
        metadata: Optional[dict] = None,
        board: Optional[str] = None,
    ) -> MemoryRecord:
        """Store one memory (screened by default). Returns the stored record.

        ``metadata`` values must be flat scalars (str/int/float/bool/None);
        nested or list values are rejected (ADR-0001, no unbounded JSON).

        ``board="major"`` / ``board="pending"`` is sugar for the curated
        live-project-board tag (ADR-0035): it merges ``{"board": <value>}`` into
        ``metadata``, nothing more. Any other value raises ``ValueError`` naming
        the two legal ones, rather than storing a tag the board would silently
        read as untagged. Passing the keyword AND a DIFFERENT board tag in
        ``metadata`` also raises — the caller stated two intents, and guessing
        which one wins is how boards go quietly wrong. (An agreeing tag is
        redundant, not a conflict, and is accepted.)

        Tagging is METADATA, which is outside the content address, so it changes
        no record id (pinned by test). Being curated ALSO needs trust at or
        above ``firewall.BOARD_FLOOR`` — the tag alone never promotes a
        low-trust row onto the board.

        ``kind=Kind.DIRECTIVE`` requires an explicit ``trust=``: directives at
        or above ``TrustTier.TRUSTED_SOURCE`` render in the recall envelope's
        *instruction* channel, so minting one must be a conscious act of
        vouching, never an inherited default (ADR-0017).
        """
        if board is not None:
            if board not in (BOARD_TAG_MAJOR, BOARD_TAG_PENDING):
                raise ValueError(
                    f"board must be {BOARD_TAG_MAJOR!r} or {BOARD_TAG_PENDING!r} "
                    f"(the curated live-project-board legs), got {board!r}"
                )
            existing = (metadata or {}).get(BOARD_METADATA_KEY)
            if existing is not None and existing != board:
                raise ValueError(
                    f"conflicting board tag: board={board!r} but "
                    f"metadata[{BOARD_METADATA_KEY!r}]={existing!r}. Pass one or the "
                    "other — rekoll will not guess which you meant."
                )
            metadata = {**(metadata or {}), BOARD_METADATA_KEY: board}
        if kind is Kind.DIRECTIVE and trust is None:
            raise ValueError(
                "kind=DIRECTIVE writes to the instruction channel of the recall "
                "envelope and must carry an explicit trust= (e.g. "
                "trust=TrustTier.OWNER for a rule you authored). Directives "
                "below TrustTier.TRUSTED_SOURCE are stored but render as "
                "evidence, never as instructions (ADR-0017)."
            )
        if len(content) > self._max_content_chars:
            raise ValueError(
                f"content is {len(content):,} chars, over the "
                f"max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a document belongs in ingest_text()/ingest_path() "
                "(which chunk it), or raise max_content_chars (ADR-0018)."
            )
        record = self._make_record(
            content=content,
            kind=kind,
            provenance=Provenance(source_uri=source, adapter_name="memory"),
            trust=self._default_trust if trust is None else trust,
            metadata=metadata,
        )
        # The cap governs what is STORED. The raw-length check above is only a
        # fast fail: firewall sanitization NFKC-normalizes, and compatibility
        # codepoints can EXPAND (U+FDFA becomes 18 chars — an 18x amplifier), so
        # in-cap input can produce over-cap stored content. Enforce on the
        # post-sanitization record before it reaches storage (ADR-0018).
        if len(record.content) > self._max_content_chars:
            raise ValueError(
                f"content is {len(record.content):,} chars after firewall "
                f"sanitization (NFKC normalization expands some codepoints), over "
                f"the max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a document belongs in ingest_text()/ingest_path() "
                "(which chunk it), or raise max_content_chars (ADR-0018)."
            )
        self._embed_and_store([record])
        return record

    def ingest_text(
        self,
        text: str,
        *,
        name: str = "doc.txt",
        source: Optional[str] = None,
        kind: Kind = Kind.RAW_FACT,
        trust: Optional[TrustTier] = None,
        batch: int = 256,
    ) -> int:
        """Chunk a document and store it. Returns the number of chunks stored.

        Ingested text is third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0016). Pass
        ``trust=`` explicitly to vouch for a source you control.

        A document over ``max_file_bytes`` OR chunking into more than
        ``max_chunks_per_doc`` pieces raises (ADR-0018): bytes alone don't
        bound work — a heading-per-line document yields ~0.25 chunks/byte.
        Chunks are embedded + stored in bounded batches of ``batch``, so peak
        memory tracks the batch, never the document.
        """
        # A BYTES limit — measure bytes, not chars. ``surrogatepass`` so a lone
        # surrogate (invalid UTF-8) in the input can't crash the size check before
        # per-piece screening strips it (the screened path) — see ``_utf8_safe``.
        n_bytes = len(text.encode("utf-8", "surrogatepass"))
        if n_bytes > self._max_file_bytes:
            raise ValueError(
                f"document is {n_bytes:,} bytes, over the "
                f"max_file_bytes={self._max_file_bytes:,} ingestion limit; "
                "split it or raise max_file_bytes (ADR-0018)."
            )
        pieces = chunk_file(name, text)
        if len(pieces) > self._max_chunks_per_doc:
            raise ValueError(
                f"document chunked into {len(pieces):,} pieces, over the "
                f"max_chunks_per_doc={self._max_chunks_per_doc:,} ingestion "
                "limit — rejected rather than silently truncated; split the "
                "document or raise max_chunks_per_doc (ADR-0018)."
            )
        src = source or f"text://{name}"
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        # Boundary-split markers (L-chunk-split): a marker the chunker split in
        # two trips NEITHER per-chunk screen. Screen the WHOLE document once and
        # quarantine the affected pieces — under the same trust rule as the
        # per-chunk screen (markers quarantine only untrusted input, ADR-0016).
        split_hits = (
            screen_pieces(text, pieces)
            if self._screen and trust <= TrustTier.UNVERIFIED
            else {}
        )
        stored = 0
        pending: list[MemoryRecord] = []
        for i, piece in enumerate(pieces):
            if self._screen and not sanitize_unicode(piece):
                continue  # nothing survives screening (e.g. only zero-width chars)
            record = self._make_record(
                content=piece,
                kind=kind,
                provenance=Provenance(
                    source_uri=src, adapter_name="memory", source_file=name, chunk_index=i
                ),
                trust=trust,
                metadata={"path": name},
            )
            if i in split_hits and record.status is not Status.QUARANTINED:
                _quarantine_split_marker(record, split_hits[i])
            pending.append(record)
            stored += 1
            if len(pending) >= batch:
                self._embed_and_store(pending)
                pending = []
        if pending:
            self._embed_and_store(pending)
        return stored

    def ingest_path(
        self,
        path: str,
        *,
        include_ext: Optional[Iterable[str]] = None,
        skip_dirs: Optional[Iterable[str]] = None,
        skip_files: Optional[Iterable[str]] = None,
        trust: Optional[TrustTier] = None,
        batch: int = 256,
        follow_symlinks: bool = False,
    ) -> dict:
        """Index a file or directory (code + docs).

        Returns ``{files, chunks, skipped, filtered, secrets_skipped,
        secrets_stored, total}`` — ``skipped`` counts files passed over
        (symlink, over ``max_file_bytes``, over ``max_chunks_per_doc``,
        undecodable, or unreadable); ``filtered`` counts walk candidates
        excluded by the filename filter (see below); ``secrets_skipped`` counts
        credential-shaped files the walk excluded, and ``secrets_stored`` counts
        credential-shaped files that were ingested anyway (via override or a
        direct path). The two secrets counts carry the "#29 never silently"
        signal for callers that cannot see ``warnings`` — e.g. across the MCP
        door (issue #41). Counts only, never names.

        Filtering is one two-level system (ADR-0027). Directory level:
        ``skip_dirs`` (default ``DEFAULT_SKIP_DIRS``) prunes directories by
        NAME, and any directory containing a ``pyvenv.cfg`` is pruned as a
        virtualenv regardless of its name. File level: ``skip_files`` is a set
        of case-insensitive filename globs (default ``DEFAULT_SKIP_FILES`` =
        machine-generated lockfiles + well-known secrets files). Pass
        ``skip_files=None`` (the default) for the default list, your own set to
        replace it, or an empty set to disable filename filtering. The filter
        applies to the directory WALK only — pointing ``path`` straight at a
        single file is explicit intent and is never blocked. Skipping a
        secret-named file emits one warning naming what was skipped; ingesting
        one (by override or direct path) also warns — never silent
        (issue #29).

        Linked files are skipped unless ``follow_symlinks=True``: a planted link
        in a third-party tree can point anywhere on disk (e.g. ``~/.ssh/id_rsa``),
        and a bulk walk must not read outside the tree it was pointed at.
        Containment is by REAL path (``os.path.realpath`` + ``is_relative_to``),
        so symlinks, NTFS junctions, and any other reparse point are all caught —
        ``is_symlink()`` alone misses a junction. Directory links (symlink or
        junction) are never descended, even under ``follow_symlinks=True``.

        Files on disk are third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0016). Pass
        ``trust=`` explicitly to vouch for a tree you control.
        """
        include = set(include_ext) if include_ext else DEFAULT_INCLUDE_EXT
        skip = set(skip_dirs) if skip_dirs else DEFAULT_SKIP_DIRS
        # ``is None`` (not falsy) on purpose: skip_files=set() means "no
        # filename filtering", which a falsy check would silently turn back
        # into the defaults (ADR-0027).
        skip_names = DEFAULT_SKIP_FILES if skip_files is None else set(skip_files)
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        root = Path(path).expanduser()
        if not follow_symlinks and _redirects_out(root):
            # A directly-pointed link (symlink OR junction) is skipped, not read
            # — it resolves outside the tree it names. Say so: the caller almost
            # certainly expected it to be ingested. (A directory symlink or
            # junction pointed at directly used to be walked silently; this
            # closes that.)
            warnings.warn(
                f"[rekoll] ingest_path was pointed at a symlink or junction "
                f"({path!r}); it was skipped because a link can point outside "
                "the intended tree. Pass follow_symlinks=True to read it.",
                stacklevel=2,
            )
            return {
                "files": 0, "chunks": 0, "skipped": 1, "filtered": 0,
                "secrets_skipped": 0, "secrets_stored": 0, "total": self.count(),
            }
        root_real = _real_path(root)
        single_file = root.is_file()
        targets = [root] if single_file else list(self._walk(root, include, skip))
        files = 0
        chunks = 0
        skipped = 0
        filtered = 0
        secrets_skipped: list[str] = []
        secrets_ingested: list[str] = []
        pending: list[MemoryRecord] = []
        for fp in targets:
            if not single_file and _matches_any(fp.name, skip_names):
                # Filename filter (ADR-0027): walk candidates only — a single
                # file passed as ``path`` is explicit intent, never blocked.
                filtered += 1
                if _matches_any(fp.name, DEFAULT_SKIP_SECRETS):
                    secrets_skipped.append(fp.relative_to(root).as_posix())
                continue
            try:
                if not follow_symlinks and (
                    fp.is_symlink() or not _within(root_real, fp)
                ):
                    # Skip a symlinked file (a planted link can point outside the
                    # tree — pinned regardless of where it resolves) AND, defense-
                    # in-depth, anything whose REAL path escapes root (a reparse
                    # file or a mid-walk TOCTOU swap). A non-redirecting reparse
                    # point that stays in-root (OneDrive/dedup) is read normally.
                    skipped += 1
                    continue
                if fp.stat().st_size > self._max_file_bytes:
                    skipped += 1  # fast-path skip for a known-oversized file
                    continue
                # Bounded read: the file may have GROWN since stat() (TOCTOU), so
                # never pull more than the limit (+1 to detect overflow) into
                # memory regardless of what stat reported.
                with fp.open("rb") as fh:
                    raw = fh.read(self._max_file_bytes + 1)
                if len(raw) > self._max_file_bytes:
                    skipped += 1
                    continue
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, OSError):
                skipped += 1
                continue
            rel = fp.name if single_file else fp.relative_to(root).as_posix()
            pieces = chunk_file(rel, text)
            if len(pieces) > self._max_chunks_per_doc:
                # Chunk-count explosion (bytes don't bound work, ADR-0018):
                # reject THIS document — whole, never truncated — and keep
                # walking, mirroring the max_file_bytes skip above.
                skipped += 1
                continue
            if not pieces:
                continue
            files += 1
            is_secret_named = _matches_any(fp.name, DEFAULT_SKIP_SECRETS)
            # Same whole-document screen as ingest_text: a boundary-split
            # marker trips no per-chunk screen (L-chunk-split).
            split_hits = (
                screen_pieces(text, pieces)
                if self._screen and trust <= TrustTier.UNVERIFIED
                else {}
            )
            file_chunks = 0
            for i, piece in enumerate(pieces):
                if self._screen and not sanitize_unicode(piece):
                    continue  # nothing survives screening (e.g. only zero-width chars)
                record = self._make_record(
                    content=piece,
                    kind=Kind.RAW_FACT,
                    provenance=Provenance(
                        source_uri=f"file://{rel}", adapter_name="memory",
                        source_file=rel, chunk_index=i,
                    ),
                    trust=trust,
                    metadata={"path": rel},
                )
                if i in split_hits and record.status is not Status.QUARANTINED:
                    _quarantine_split_marker(record, split_hits[i])
                pending.append(record)
                chunks += 1
                file_chunks += 1
                if len(pending) >= batch:
                    self._embed_and_store(pending)
                    pending = []
            if is_secret_named and file_chunks:
                # Count a credential-shaped file as STORED only if it produced >=1
                # retrievable record (explicit override or direct path — never
                # silently, #29). A file whose every chunk sanitizes to empty (e.g.
                # all zero-width bytes) stores NOTHING; counting it made
                # secrets_stored claim a retrievable credential exists when none
                # does — the #41 honesty signal (and its warning) lied.
                secrets_ingested.append(rel)
        if pending:
            self._embed_and_store(pending)
        if secrets_skipped:
            shown = ", ".join(sorted(secrets_skipped))
            warnings.warn(
                f"[rekoll] ingest_path skipped {len(secrets_skipped)} file(s) "
                f"that look like credentials or private keys: {shown}. Secrets "
                "do not belong in a memory store — they would be embedded, "
                "retrievable, and travel with any export. To ingest one anyway, "
                "point ingest_path at the file directly, or pass skip_files= "
                "without its name.",
                stacklevel=2,
            )
        if secrets_ingested:
            shown = ", ".join(sorted(secrets_ingested))
            warnings.warn(
                f"[rekoll] ingest_path STORED {len(secrets_ingested)} file(s) "
                f"whose name suggests credentials or private keys: {shown}. "
                "They are now retrievable records and will travel with any "
                "export. Use forget() on their records if this was unintended.",
                stacklevel=2,
            )
        # ``secrets_skipped`` / ``secrets_stored`` are COUNTS, never names (the
        # names are the operator's business and are exactly what an injected
        # instruction would want echoed back — L-mcp-rootleak, ADR-0027). They
        # carry the "#29 never silently" signal across a boundary that warnings
        # cannot cross (stdio): a folder ingest that quietly excluded a
        # credential-shaped file reports secrets_skipped>0, and an explicit
        # override or direct-path ingest that STORED one reports
        # secrets_stored>0 — the count the warnings above already computed
        # (issue #41).
        return {
            "files": files, "chunks": chunks, "skipped": skipped,
            "filtered": filtered, "secrets_skipped": len(secrets_skipped),
            "secrets_stored": len(secrets_ingested), "total": self.count(),
        }

    def forget(self, *ids: str) -> int:
        """Delete memories by id; returns how many were removed."""
        return self.adapter.delete(scope=self.scope, ids=list(ids))

    # -- the live project board (ADR-0035) -----------------------------------
    # SEAM: the CLI/MCP board doors render BoardResult.to_dict(); it is
    # byte-identical to build_board_payload's dict, which is what pins the
    # three doors to one payload. Keep both signatures stable.
    def board(
        self,
        *,
        recent_limit: int = 10,
        major_limit: int = 10,
        rules_limit: int = DEFAULT_BOARD_RULES_LIMIT,
        min_trust: Optional[int] = None,
    ) -> BoardResult:
        """The shared current-state board for this scope — what every concurrent
        session on this store should see (ADR-0035).

        A plain, bounded, ZERO-LLM, ZERO-EMBEDDING read: it builds no query
        vector, so it never constructs or touches the embedder (pinned by
        test). It also credits NOTHING to the was-it-used ledger — only
        :meth:`recall` records there, and this verb does not route through it —
        so polling the board can never inflate :meth:`informed_by` and fake
        evidence that a memory informed an action (pinned by test).

        Delegates every leg to :func:`rekoll.board.build_board_payload`, so the
        SDK board is the same payload the CLI and MCP doors serve. Parameter
        names mirror that builder deliberately (cross-layer consistency): 0
        disables a leg, and a negative or over-ceiling limit raises
        ``ValueError`` rather than silently clamping.

        ``min_trust`` gates the Tier-1 activity feed ONLY, and ``None`` means
        "the builder's default" (UNVERIFIED — low-trust rows appear, labelled
        with their tier, and their ``text`` is withheld below the board floor).
        The Tier-2 floor is NOT a caller preference: curated majors/pending and
        the open-pending count always apply ``firewall.BOARD_FLOOR``, because a
        ``board`` metadata tag is data any writer can attach.

        ``latest`` is a freshness hint, not a change token: it can step
        BACKWARD when the newest entry is resolved. Byte-compare
        ``to_dict()`` payloads to detect change.

        Raises ``UnsupportedCapabilityError`` — naming the adapter — on storage
        that cannot serve a board. There is no board to degrade to, so this
        fails honestly instead of returning a plausible empty one.
        """
        kwargs = {} if min_trust is None else {"min_trust": int(min_trust)}
        try:
            payload = build_board_payload(
                self.adapter,
                self.scope,
                recent_limit=recent_limit,
                major_limit=major_limit,
                rules_limit=rules_limit,
                **kwargs,
            )
        except UnsupportedCapabilityError as exc:
            raise UnsupportedCapabilityError(
                f"the storage adapter in use ({type(self.adapter).__name__}) does not "
                "serve the live project board, so there is no board to show. The "
                "bundled SQLite adapter does — open this Memory on a sqlite store "
                f"(original error: {exc})"
            ) from exc
        # Copy-then-proxy: dict(entry) detaches from the builder's dicts,
        # MappingProxyType makes in-place mutation raise — the frozen dataclass
        # alone only blocks attribute REBINDING, not entry mutation (a review
        # finding on PR #57: one session's edit would silently rewrite the
        # board every other holder of this instance serializes).
        return BoardResult(
            rules=tuple(payload["rules"]),
            majors=tuple(MappingProxyType(dict(e)) for e in payload["majors"]),
            recent=tuple(MappingProxyType(dict(e)) for e in payload["recent"]),
            pending_open=payload["pending_open"],
            latest=payload["latest"],
        )

    def resolve(self, *record_ids: str) -> int:
        """Mark board items DONE; returns how many actually transitioned.

        The product policy over the adapter's general ``set_status`` (ADR-0035
        §5): this verb performs ACTIVE -> SUPERSEDED and nothing else, so it
        takes no status argument — a facade caller cannot use it to resurrect a
        row, mint a PROPOSED one, or reach any other transition.

        Resolve MARKS, it never deletes (contrast :meth:`forget`): every byte
        stays in the store and stays ``get``-able for audit; the item simply
        leaves the board's legs, its open-pending count, and recall.

        Non-transitions are silent PER ID — an unknown id, an already-resolved
        item, a row in another scope, or one the effective-status gate refuses
        (quarantined/forged/proposed) each just don't count. The return value
        is the honest report: ``resolve(a, b)`` returning 1 means exactly one
        moved. Resolving the same id twice returns 1 then 0.

        An adapter without ``set_status`` raises ``UnsupportedCapabilityError``
        naming the adapter — the same honest failure as :meth:`board` (there is
        nothing to quietly succeed at).
        """
        resolved = 0
        for record_id in record_ids:
            try:
                moved = self.adapter.set_status(
                    scope=self.scope, record_id=record_id, status=Status.SUPERSEDED.value
                )
            except UnsupportedCapabilityError as exc:
                raise UnsupportedCapabilityError(
                    f"the storage adapter in use ({type(self.adapter).__name__}) does "
                    "not serve the live project board, so there is nothing to "
                    "resolve. The bundled SQLite adapter does — open this Memory on "
                    f"a sqlite store (original error: {exc})"
                ) from exc
            if moved:
                resolved += 1
        return resolved

    # -- write-side learning (explicit opt-in; never on the read path) -------
    def consolidate(
        self,
        ids: Optional[Sequence[str]] = None,
        *,
        query: Optional[str] = None,
        k: int = 20,
        consolidator: Consolidator,
        min_source_trust: TrustTier = TrustTier.TRUSTED_SOURCE,
        metadata: Optional[dict] = None,
    ) -> MemoryRecord:
        """Merge existing memories into ONE derived observation via YOUR LLM.

        Explicit opt-in, write-side only: ``recall()`` never calls a
        consolidator (reads stay LLM-free, ADR-0007), and ``Memory`` holds no
        ambient consolidator — you pass one per call. Select sources with
        ``ids=[...]`` or ``query="..."`` (top-``k``). The consolidator's text
        flows through the ingest firewall — ALWAYS, even for a store built
        with ``screen=False``: a host may vouch for its own writes, never for
        what a model emits (#7.5, ADR-0015) — and is stored with:

         - ``kind=OBSERVATION``,
         - ``provenance.derived_from`` = the source record ids,
         - ``declared_transformations=("llm_summary",)``,
         - trust capped at the MINIMUM trust of the sources — the LLM never
           chooses trust, so low-trust input can't launder itself (ADR-0002).

        Quarantined records are never fed to the model. Sources below
        ``min_source_trust`` are skipped (DESIGN §L3: trusted-tier facts only);
        loosen deliberately with ``min_source_trust=TrustTier.UNVERIFIED``.
        """
        if not callable(getattr(consolidator, "summarize", None)):
            raise TypeError(
                "consolidator must provide .summarize(texts) — e.g. "
                "rekoll.providers.OpenAICompatibleConsolidator('gpt-4o-mini')"
            )
        if (ids is None) == (query is None):
            raise ValueError("pass exactly one of ids=[...] or query='...'")
        if ids is not None:
            wanted = list(dict.fromkeys(ids))
            found = {r.id: r for r in self.adapter.get(scope=self.scope, ids=wanted).records}
            missing = [i for i in wanted if i not in found]
            if missing:
                raise KeyError(f"no such memory in this scope: {missing}")
            records = [found[i] for i in wanted]
        else:
            records = [hit.record for hit in self.recall(query, k=k)]
        floor = max(min_source_trust, TrustTier.UNVERIFIED)  # quarantined NEVER reaches the LLM
        sources = [
            r for r in records
            if r.status is not Status.QUARANTINED and r.trust_tier >= floor
        ]
        if not sources:
            raise ValueError(
                "no consolidation-eligible sources (need status != quarantined and "
                f"trust >= {floor.name.lower()})"
            )
        summary = consolidator.summarize([r.content for r in sources])
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("consolidator returned no text")
        summary = summary.strip()
        # The third write door respects the same per-record bound as remember()
        # (ADR-0018): a summary should be SHORTER than its sources, so exceeding
        # the cap means the consolidator failed to condense — fail loud rather
        # than store an unbounded LLM output.
        if len(summary) > self._max_content_chars:
            raise ValueError(
                f"consolidator returned {len(summary):,} chars, over the "
                f"max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a consolidation summary should be shorter than its "
                "sources — raise max_content_chars if this is intended (ADR-0018)."
            )
        name = str(getattr(consolidator, "name", type(consolidator).__name__))
        record = self._make_record(
            content=summary,
            kind=Kind.OBSERVATION,
            provenance=Provenance(
                source_uri=f"consolidator://{name}",
                adapter_name="memory",
                derived_from=tuple(r.id for r in sources),
            ),
            trust=min(r.trust_tier for r in sources),
            metadata={**(metadata or {}), "consolidator": name, "source_count": len(sources)},
            declared_transformations=("llm_summary",),
            # LLM output is never exempt from the firewall (#7.5): screening is
            # forced even when the store was built with screen=False, keeping
            # ADR-0015's "flows through the ingest firewall" true by
            # construction. The trust rule is unchanged — markers quarantine
            # only when the summary's (source-derived) trust is <= UNVERIFIED.
            force_screen=True,
        )
        # Same post-sanitization cap rule as remember(): NFKC can EXPAND, so
        # re-check what would actually be stored (ADR-0018).
        if len(record.content) > self._max_content_chars:
            raise ValueError(
                f"consolidator output is {len(record.content):,} chars after "
                f"firewall sanitization, over the "
                f"max_content_chars={self._max_content_chars:,} limit for one "
                "memory (ADR-0018)."
            )
        self._embed_and_store([record])
        return record

    # -- read ---------------------------------------------------------------
    def recall(
        self,
        query: str,
        *,
        k: int = 5,
        kind: Optional[Kind] = None,
        rerank: bool = True,
        call_id: Optional[str] = None,
        min_score: Optional[float] = None,
    ) -> RecallResult:
        """Hybrid + reranked search. Quarantined memory is excluded; reads call no LLM.

        On EVERY recall, the active in-scope directives at/above the directive
        floor also ride the result's STANDING-DIRECTIVE channel
        (``RecallResult.pinned_directives``, rendered in the envelope's
        instruction block) — independent of ``query``, of the ``kind`` filter, and
        of the abstain gate — so a saved rule never silently vanishes just because
        it did not rank into the top-k (ADR-0034). This is a bounded, zero-LLM
        scoped DB read (see ``max_pinned_directives``); it does NOT add to
        ``.ids()`` / ``.records()`` / ``len(result)``, which stay the ranked hits.

        The query is firewall-sanitized and truncated to
        ``retrieval.MAX_QUERY_CHARS`` before embedding (DESIGN §7, ADR-0018).

        May return FEWER than ``k`` hits: quarantined memory is excluded, and any
        candidate that fails content-hash verification (direct-DB tampering) is
        withheld with a warning (ADR-0019). ``k`` is an upper bound, not a promise.

        ``min_score`` (opt-in, default off) turns on the **abstain gate**
        (ADR-0028): a floor on the vector leg's top-1 COSINE similarity. If the
        closest surfacable memory is not at least this similar, recall returns
        NO hits and sets ``abstained=True`` — an honest "I don't know" instead
        of ``k`` confident-looking hits for a question the store cannot answer.
        An abstain is never confusable with an empty store: it says so in
        ``mode`` and in ``abstained``.

        ``min_score`` is a cosine in [-1, 1], not a fused score. Read
        ``RecallResult.top_vector_score`` (populated on every ordinary recall)
        to choose a threshold from your own corpus; on a frozen 1,000-doc
        fixture, 0.70 separated answerable from unanswerable queries well
        (top-1 cosine AUC 0.931). Your corpus and embedder will differ —
        measure, don't copy the number.

        ``RecallResult.mode`` names exactly what ran (honest degradation),
        including whether the gate abstained, or could not be evaluated at all.
        The surfaced ids are recorded in the was-it-used ledger; pass
        ``call_id`` to attribute this recall to one host action so
        :meth:`informed_by` can join them later. An abstain surfaces no ids, so
        it credits nothing to the ledger.
        """
        result = self._search(
            query, k=k, kind=kind, rerank=rerank, min_score=min_score,
            pin_directives=True,
        )
        try:
            self.ledger.record(
                [h.record.id for h in result.hits], query=query, call_id=call_id
            )
        except Exception:
            pass  # even a host-swapped, raising ledger must never break a read
        return result

    def context(self, query: str, *, k: int = 5) -> str:
        """Shortcut: the LLM-ready, firewall-framed context string for a query."""
        return self.recall(query, k=k).context()

    def count(self) -> int:
        return self.adapter.count(scope=self.scope)

    def close(self) -> None:
        self.adapter.close()

    # -- was-it-used loop -----------------------------------------------------
    def mark_used(self, *ids: str) -> int:
        """Report that these memories actually informed an action; returns how
        many were credited.

        This is the loop-closing usage signal: recall metrics say "we surfaced
        it", ``mark_used`` lets the host say "we acted on it". Each credited
        record's ``proof_count`` is incremented — a PROMOTION-ONLY signal:
        usage may extend a memory's standing, it never shortens another's, and
        it never touches trust_tier or status (trust is set at the ingestion
        boundary and immutable to output, ADR-0002). Unknown / out-of-scope /
        quarantined ids are ignored.

        The increment is a TARGETED, atomic ``proof_count += 1`` at the adapter
        (``bump_proof_count``), not a read-modify-write of the whole row: two
        concurrent credits both land, and a concurrent change to any OTHER
        column is never reverted. (An adapter that hasn't specialized the bump
        falls back to the read-modify-write, correct under a single writer.)
        """
        return self.adapter.bump_proof_count(scope=self.scope, ids=list(ids))

    def informed_by(self, call_id: Optional[str] = None, *, limit: int = 5) -> list[LedgerEntry]:
        """The recent recalls (ids + query + ts) that plausibly informed an
        action being finished right now — for hosts that attach usage evidence
        to their own receipts/logs instead of calling :meth:`mark_used`
        directly. With ``call_id``, only recalls recorded under that call_id
        are returned (no cross-conversation credit). Best-effort: [] on any
        ledger failure.
        """
        return self.ledger.entries(call_id, limit=limit)

    # -- health -----------------------------------------------------------------
    # SEAM: the CLI's `rekoll doctor` calls Memory.health() (and Memory.self_test())
    # and renders HealthReport.to_dict() — keep these signatures stable.
    def health(self, *, n: int = 3, k: int = 5) -> HealthReport:
        """Source-of-truth-vs-index freshness check (read-only).

        Asserts the newest ``n`` ACTIVE records are (a) EMBEDDED (carry a
        vector) and (b) RETRIEVABLE (an actual search over their own content
        surfaces them within a membership window of ``max(k, 20)`` — widened so
        near-duplicate corpora and approximate vector indexes don't read as
        dead ingestion). The check runs store-vs-index, not index-only — an
        index-only "is the corpus healthy?" query reads green forever over dead
        ingestion, because it can only see what already made it in. Also
        reports the embedder-identity state and the exact recall mode, so a
        degraded scope can't look healthy.

        Fail-soft, always: health must never take the host down. An empty or
        unsupported store, a storage read that errors, or a retrievability probe
        that raises (a broken index leg) all degrade to an honest report (``ok``
        is ``None`` when nothing was checkable, ``False`` when a checked record
        failed) with a diagnostic note — never a propagated exception.
        """
        notes: list[str] = []
        mode = self._mode()
        try:
            total = self.count()
        except Exception as exc:  # a store that can't even be counted is not "ok"
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=0,
                checked=0, embedded=0, retrievable=0,
                notes=(f"could not read the store ({type(exc).__name__}) — health unknown",),
            )
        if total == 0:
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=0,
                checked=0, embedded=0, retrievable=0,
                notes=("empty scope — nothing to check",),
            )
        try:
            # Over-fetch so quarantined/superseded rows don't eat the sample.
            newest = self.adapter.newest(scope=self.scope, n=max(n * 3, n)).records
        except UnsupportedCapabilityError:
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=total,
                checked=0, embedded=0, retrievable=0,
                notes=(
                    f"adapter '{self.adapter.name}' cannot enumerate newest records — "
                    "freshness unknown",
                ),
            )
        except Exception as exc:
            # Fail-soft: ANY other storage error is reported, never raised — a
            # health check must never take the host down (the whole point).
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=total,
                checked=0, embedded=0, retrievable=0,
                notes=(f"could not enumerate records ({type(exc).__name__}) — health unknown",),
            )
        active_all = [r for r in newest if r.status is Status.ACTIVE]
        active = active_all[:n]
        skipped = len(newest) - len(active_all)
        if skipped:
            notes.append(f"skipped {skipped} non-active record(s) in the newest sample")
        if not active:
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=total,
                checked=0, embedded=0, retrievable=0,
                notes=(*notes, "no active records in the newest sample — nothing checkable"),
            )
        embedded = 0
        retrievable = 0
        stale: list[str] = []
        tampered: list[str] = []  # stale AND content-hash fails: direct-DB tampering
        probe_errors = 0
        for record in active:
            has_vector = record.embedding is not None
            embedded += int(has_vector)
            # Retrievability probe: search the record's own content through the
            # real read path (no reranker — membership in the window is the
            # check, not order; no ledger — probes must not claim usage credit).
            # Fail-soft per record: a probe that RAISES (a broken index leg) must
            # degrade this record to stale + not-ok, never propagate and take the
            # host down. That is exactly the "index is broken" state health exists
            # to surface honestly.
            try:
                probe = self._search(
                    record.content[:_PROBE_MAX_CHARS],
                    k=max(k, _PROBE_MIN_POOL),
                    rerank=False,
                )
                found = record.id in {h.record.id for h in probe.hits}
            except Exception:
                found = False
                probe_errors += 1
            retrievable += int(found)
            if not (has_vector and found):
                stale.append(record.id)
                # WHY it is stale decides the note (issue #24). A content-hash
                # mismatch means the stored row was edited outside the write path
                # (ADR-0019): read-time verification WITHHELD it from the probe —
                # ingestion is not dead, the row is tampered. health() already
                # holds the record, so record.verify() classifies this with no
                # extra retrieval plumbing. Tamper takes precedence over a missing
                # vector: re-embedding cannot repair a tampered row (its content
                # no longer matches its hash), so a record that is BOTH unembedded
                # AND tampered is reported as tampered — re-ingest or delete it.
                if not record.verify():
                    tampered.append(record.id)
        if probe_errors:
            notes.append(
                f"{probe_errors} retrievability probe(s) raised — the search path "
                "may be broken; treating those records as not retrievable"
            )
        if self._identity_state == "mismatch":
            notes.append(
                "embedder identity mismatch — vector leg refused (ADR-0024); "
                "call Memory.reindex() to re-embed this scope with the current embedder"
            )
        ok = not stale and self._identity_state != "mismatch"
        # Two distinct causes, two distinct notes (issue #24) — never point a
        # `rekoll doctor` reader at "ingestion may be dead" when the real cause
        # is content-hash verification withholding a tampered row. Tamper first
        # (more actionable): those ids need re-ingest/delete, not a reindex.
        if tampered:
            notes.append(
                "newest record(s) failed content-hash verification — possible "
                "direct-DB tampering (ADR-0019); re-ingest or delete them"
            )
        dead = [rid for rid in stale if rid not in set(tampered)]
        if dead:
            notes.append(
                "newest record(s) not fully indexed — ingestion/embedding may be dead"
            )
        return HealthReport(
            ok=ok, identity=self._identity_state, mode=mode, total=total,
            checked=len(active), embedded=embedded, retrievable=retrievable,
            stale_ids=tuple(stale), notes=tuple(notes),
        )

    def self_test(self, *, k: int = 3) -> dict:
        """Golden-probe end-to-end self-test: store a known record, assert a
        known query returns it at rank 1, then remove it.

        Exercises the REAL write→embed→index→search path in whatever mode the
        scope is currently in (a lexical-only degraded scope still passes if
        lexical recall works — the probe tests the system you actually have,
        and ``mode`` in the result names it). Unlike :meth:`health` this
        WRITES (one sentinel record, removed afterwards; the id is
        content-addressed so a crashed probe re-run is idempotent).

        Returns ``{"ok", "rank", "mode"}`` — ``rank`` is 1-based or None when
        the sentinel didn't surface at all.
        """
        sentinel = (
            "Rekoll golden-probe sentinel: the amethyst lighthouse indexes "
            "maple syllables."
        )
        query = "amethyst lighthouse maple syllables"
        record = self.remember(sentinel, source="rekoll://self-test")
        try:
            result = self._search(query, k=k)  # no ledger: probes claim no usage
            ids = [h.record.id for h in result.hits]
            rank = ids.index(record.id) + 1 if record.id in ids else None
            return {"ok": rank == 1, "rank": rank, "mode": result.mode}
        finally:
            self.forget(record.id)

    # -- recovery ----------------------------------------------------------------
    def reindex(self, *, batch: int = 256) -> int:
        """Re-embed EVERY record in this scope with the current embedder, then
        rebind the scope's stored embedder identity to it. Returns how many
        records were re-embedded.

        This is the real recovery from an embedder-identity mismatch (ADR-0024):
        after a model/config swap the scope is refused the vector leg and reads
        go lexical-only. ``reindex()`` rewrites every stored vector with the
        embedder you are holding NOW and *then* claims the scope for it, so the
        vector leg comes back and :meth:`health` reads green again — WITHOUT the
        recovery trap of "just re-ingest" (re-ingesting identical content under
        the mismatch stores no vector; this method computes them).

        Order matters and is deliberate: vectors are written FIRST, the identity
        is rebound LAST. A crash midway leaves the scope still-mismatched (safe,
        degraded) rather than identity-clean over half-stale vectors — the write
        is re-runnable and idempotent (same content-addressed ids, unchanged
        trust, so the trust-monotonic upsert updates each row in place, ADR-0023).
        Re-running IS the only resume mechanism: there is no checkpoint, so an
        interrupted reindex re-embeds from the first record again — fine for
        recovery, but budget for a full pass on very large scopes.

        Re-embedding is only skipped when the embedder is already a match AND no
        record is missing a vector (a genuine no-op); otherwise every in-scope
        record — active, superseded, or quarantined — is refreshed so no stale
        vector family is left behind.
        """
        total = self.count()
        current = self.embedder.identity()
        if total == 0:
            # Empty scope: nothing to embed, just (re)claim it for this embedder.
            self.adapter.set_embedder_identity(scope=self.scope, identity=current)
            self._identity_state = "match"
            return 0
        try:
            # Records MUST be the adapter's stored rows (newest()), never
            # rebuilt via create(): reindex deliberately refreshes quarantined/
            # superseded rows too, and a create()-built record defaults
            # status=ACTIVE, which the same-id upsert would write through —
            # resurrecting quarantined rows. Status (unlike proof_count, which
            # the adapter keeps monotonic) has no adapter-side guard.
            records = list(self.adapter.newest(scope=self.scope, n=total).records)
        except UnsupportedCapabilityError as exc:
            raise UnsupportedCapabilityError(
                f"adapter '{self.adapter.name}' cannot enumerate its records, so "
                "reindex() cannot re-embed them; recover by re-ingesting this "
                "scope's sources with the current embedder into a fresh store"
            ) from exc
        name, dim = current.name, self.embedder.dim
        done = 0
        for start in range(0, len(records), batch):
            chunk = records[start : start + batch]
            vectors = self.embedder.embed([_utf8_safe(r.content) for r in chunk])
            for record, vector in zip(chunk, vectors):
                record.with_embedding(vector, name=name, dim=dim)
            # Upsert re-embedded rows BEFORE rebinding identity: while the stored
            # identity still mismatches, the trust-monotonic same-id upsert
            # updates the embedding column in place (ADR-0023) — it neither drops
            # trust nor nulls the vector we just computed.
            self.adapter.upsert(records=chunk)
            done += len(chunk)
        # Vectors are all current: NOW claim the scope for this embedder so reads
        # use the vector leg again.
        self.adapter.set_embedder_identity(scope=self.scope, identity=current)
        self._identity_state = "match"
        return done

    # -- internals ----------------------------------------------------------
    def _search(
        self,
        query: str,
        *,
        k: int,
        kind: Optional[Kind] = None,
        rerank: bool = True,
        min_score: Optional[float] = None,
        pin_directives: bool = False,
    ) -> RecallResult:
        """The one read path recall/health/self_test share (no ledger write).

        health/self_test never pass ``min_score``: a probe of the index must not
        be able to abstain, or a healthy scope could read as a broken one.

        ``pin_directives`` attaches the standing-directive channel (ADR-0034) to
        the result. Only ``recall`` sets it: health/self_test look at ``.hits``
        ids alone, never the envelope, so they skip the extra scoped read.
        """
        use_vector = self._identity_state != "mismatch"
        reranker = self.reranker if rerank else None
        result = hybrid_search(
            self.adapter, scope=self.scope, query=query, embedder=self.embedder,
            k=k, kind=kind, reranker=reranker, use_vector=use_vector,
            min_score=min_score,
        )
        if result.abstained:
            # Only the vector leg ran before the gate refused; naming the lexical
            # or rerank legs here would claim work that never happened.
            mode = self._mode(reranked=False, lexical=False) + (
                f": abstained (top-1 cosine {result.top_vector_score:.3f} "
                f"< min_score {min_score:.3f})"
            )
        else:
            mode = self._mode(reranked=reranker is not None)
            if result.gate.startswith("unavailable"):
                reason = result.gate.split(": ", 1)[1]
                mode += f"; min_score not applied ({reason})"
        return RecallResult(
            hits=tuple(result.hits),
            mode=mode,
            abstained=result.abstained,
            top_vector_score=result.top_vector_score,
            # The standing directives are fetched INDEPENDENTLY of the ranked hits
            # (a plain scoped DB read) and INDEPENDENTLY of the abstain gate, so a
            # saved rule surfaces even here, where ``result.hits`` is empty because
            # the gate refused (invariant 7, abstain-proof).
            pinned_directives=self._pinned_directives() if pin_directives else (),
        )

    def _mode(
        self, *, reranked: Optional[bool] = None, lexical: Optional[bool] = None
    ) -> str:
        """Compose the honest-degradation string: exactly what a read runs.

        ``reranked=None`` (health/introspection) describes the default recall
        configuration; a bool describes one concrete search. ``lexical=None``
        means "whatever the adapter advertises"; pass False when a concrete
        search refused the lexical leg (an abstain short-circuits before it).
        """
        if lexical is None:
            lexical = self.adapter.supports(CAP_LEXICAL)
        rerank = (self.reranker is not None) if reranked is None else reranked
        if self._identity_state == "mismatch":
            base = "lexical-only" if lexical else "none"
            if rerank and lexical:
                base += "+rerank"
            return f"{base}: embedder mismatch"
        legs = ["vector"] + (["lexical"] if lexical else []) + (["rerank"] if rerank else [])
        mode = "+".join(legs)
        if isinstance(self.embedder, StubEmbedder):
            mode += " (stub-embedder)"  # deterministic hash vectors, no semantics
        return mode

    def _pinned_directives(self) -> tuple[MemoryRecord, ...]:
        """The STANDING-DIRECTIVE CHANNEL (ADR-0034): active, in-scope directives
        at/above the directive floor, always surfaced in the recall envelope's
        instruction channel — independent of the query and of the abstain gate.

        A plain, scoped, deterministic DB read — no embedding, no LLM (ADR-0007) —
        bounded to ``max_pinned_directives`` and ordered oldest-first by the
        adapter. Fail-soft on every axis, because a standing rule surfacing is a
        best-effort enhancement that must NEVER break a recall:

          * ``max_pinned_directives == 0`` disables the channel with no read.
          * An adapter that cannot serve ``active_directives`` (or ANY read error)
            degrades to the pre-ADR-0034 rank-only behavior — the rule still
            surfaces when it ranks in — never a raised exception on the read path.
          * Each candidate is content-hash verified before it can render as a RULE
            (ADR-0019). The pinned read bypasses ``hybrid_search``'s ``_verify_hits``,
            and a tampered directive is the highest-stakes thing to surface as an
            instruction, so a hash mismatch withholds it (one warning names the
            ids), exactly as the ranked path would.

        (Trust gating, scope isolation, and the effective-status ACTIVE filter are
        enforced in the adapter read; ``build_envelope`` re-checks the floor +
        quarantine as defense in depth. This method owns tamper-verify and the
        cap/disable/degrade policy.)
        """
        if self._max_pinned_directives <= 0:
            return ()
        try:
            records = self.adapter.active_directives(
                scope=self.scope,
                limit=self._max_pinned_directives,
                min_trust=int(DIRECTIVE_FLOOR),
            ).records
        except Exception:
            return ()  # unsupported adapter or read error: degrade to rank-only
        verified: list[MemoryRecord] = []
        tampered: list[str] = []
        for record in records:
            if record.verify():
                verified.append(record)
            else:
                tampered.append(record.id)
        if tampered:
            warnings.warn(
                f"[rekoll] {len(tampered)} standing directive(s) failed content-hash "
                "verification and were withheld from the instruction channel "
                "(possible direct-DB tampering; re-ingest or delete them): "
                f"{', '.join(sorted(tampered))}",
                stacklevel=2,
            )
        return tuple(verified)

    def _make_record(
        self, *, content, kind, provenance, trust, metadata, force_screen=False, **kwargs
    ):
        # force_screen: LLM output (consolidate) is screened even when the
        # store was built with screen=False — a host may vouch for its OWN
        # writes; it cannot vouch for what a model emits (#7.5, ADR-0015).
        if self._screen or force_screen:
            return screened_record(
                scope=self.scope, kind=kind, content=content,
                provenance=provenance, trust_tier=trust, metadata=metadata,
                redact_pii=self._redact_pii, **kwargs,
            )
        return MemoryRecord.create(
            scope=self.scope, kind=kind, content=content,
            provenance=provenance, trust_tier=trust, metadata=metadata or {}, **kwargs,
        )

    def _embed_and_store(self, records: list[MemoryRecord]) -> None:
        if not records:
            return
        if self._identity_state == "mismatch":
            # Under a mismatch we do NOT embed NEW content with the current
            # embedder (ADR-0024): a second vector family in the scope would be
            # unqueryable (the leg is refused) and deepen the very corruption the
            # guard exists to stop. Lexical indexing still covers the content;
            # health() flags the scope until reindex() re-embeds it.
            #
            # But ids are content-addressed, so re-ingesting IDENTICAL content
            # lands on a row that may ALREADY carry a good (pre-swap) vector. The
            # upsert would rewrite that row with embedding=NULL — silently
            # destroying the very vectors reindex() needs. Carry the stored
            # embedding forward so the in-place rewrite preserves it verbatim.
            self._preserve_existing_embeddings(records)
        else:
            to_embed = [r for r in records if r.embedding is None]
            if to_embed:
                vectors = self.embedder.embed([_utf8_safe(r.content) for r in to_embed])
                name, dim = self.embedder.identity().name, self.embedder.dim
                for record, vector in zip(to_embed, vectors):
                    record.with_embedding(vector, name=name, dim=dim)
        self.adapter.upsert(records=records)

    def _preserve_existing_embeddings(self, records: list[MemoryRecord]) -> None:
        """Copy any already-stored vector onto an embedding-less incoming record
        so an in-place upsert never nulls a good vector (ADR-0024, the recovery
        trap). Only touches records that would otherwise write NULL; genuinely
        new content stays vector-free under the mismatch.

        Facade-enforced ONLY — this invariant is deliberately NOT part of the
        StorageAdapter contract (no conformance check): an adapter stores what
        it is handed, and ``SQLiteAdapter._write_one`` writes ``embedding=NULL``
        for a record carrying none. A new write path that can hand ``upsert``
        already-stored content without a vector must either embed first (as
        ``_embed_and_store`` does) or route through here."""
        needing = [r for r in records if r.embedding is None]
        if not needing:
            return
        stored = {
            r.id: r
            for r in self.adapter.get(scope=self.scope, ids=[r.id for r in needing]).records
        }
        for record in needing:
            prior = stored.get(record.id)
            if prior is not None and prior.embedding is not None:
                record.embedding = prior.embedding
                record.embedder_name = prior.embedder_name
                record.embedder_dim = prior.embedder_dim

    @staticmethod
    def _walk(root: Path, include: set, skip: set):
        # os.walk(followlinks=False) already refuses to DESCEND directory
        # symlinks, but it happily descends a junction (is_symlink()==False). So
        # prune any directory whose REAL path escapes the root before os.walk
        # recurses into it — catching junctions, reparse points, and dir symlinks
        # alike. This is unconditional (like "dir symlinks are never descended"):
        # an out-of-tree directory is never walked, even under follow_symlinks.
        #
        # CYCLE GUARD (issue #15): an IN-ROOT junction cycle (root/loop -> root)
        # PASSES that containment — it never leaves the tree — yet os.walk still
        # descends it, re-walking the same real directories until the OS
        # path-length limit: an availability blow-up with every file re-read at
        # every level. So also track the REAL path of every directory we agree
        # to descend and prune one we've already seen — a directory reachable
        # twice (a cycle, or a junction to an in-root sibling) is walked
        # exactly once. Real paths are compared normcased (Windows-caseless).
        root_real = _real_path(root)
        seen: set[str] = {os.path.normcase(str(root_real))}
        for dirpath, dirnames, filenames in os.walk(root):
            kept = []
            for d in dirnames:
                if d in skip or d.endswith(".egg-info"):
                    continue
                full = os.path.join(dirpath, d)
                if not _within(root_real, full):
                    # LOAD-BEARING and unconditional (H1, PR #14): an out-of-
                    # tree directory is never walked. Checked FIRST — never
                    # stat inside a directory whose real path already escaped
                    # the root.
                    continue
                if os.path.isfile(os.path.join(full, "pyvenv.cfg")):
                    # A virtualenv by STRUCTURE: ``python -m venv X`` drops
                    # ``pyvenv.cfg`` at X's root whatever X is called, so this
                    # catches every venv a name list would miss ("env",
                    # "myvenv", ...) — issue #27's ~500x ingest blow-up. The
                    # name entries in DEFAULT_SKIP_DIRS remain as a fast path.
                    continue
                real = os.path.normcase(os.path.realpath(full))
                if real in seen:
                    continue  # already walked via another route: cycle/duplicate
                seen.add(real)
                kept.append(d)
            dirnames[:] = kept
            for name in filenames:
                fp = Path(dirpath) / name
                if fp.suffix.lower() in include:
                    yield fp
