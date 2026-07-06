"""Minimal JSON-over-HTTP transport for the opt-in provider layer (ADR-0015).

Standard library only (``urllib.request``): bringing your own cloud AI adds
ZERO pip dependencies. Importing this module opens no socket — network I/O
happens only inside :func:`post_json`, which is reachable only through a
provider the user explicitly constructed or named.

Egress hardening (issue #7). The transport treats the remote as untrusted:

* **Redirects fail closed.** A cloud embedding/chat POST endpoint has no
  legitimate reason to answer with a 3xx; following one would re-send the
  API key to the ``Location`` host and could pivot the client onto another
  scheme (``ftp://``). Every 3xx is refused, so credential headers never
  travel anywhere but the host the caller named.
* **Bodies are bounded.** Both success and error bodies are read with an
  explicit cap, so a hostile or wedged endpoint cannot make the client
  buffer an unbounded response.
* **No API key over cleartext.** A request that carries a credential header
  is refused on ``http://`` unless the host is loopback — keeping keyless
  local servers (ollama/LM Studio) working while never putting a real key on
  the wire in the clear.
* **Error text is log-safe.** Server-controlled error strings are stripped of
  C0/C1 control characters *and* bidi/zero-width format characters before they
  reach a :class:`ProviderError`, so an endpoint cannot inject ANSI escapes /
  NUL / BEL, nor "Trojan Source" bidi overrides, into logs.
"""

from __future__ import annotations

import ipaddress
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Mapping, Optional, Sequence, TypeVar

__all__ = ["ProviderError", "post_json", "batched"]

#: Statuses worth retrying: rate limits and transient server errors.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 10.0
_ERROR_DETAIL_LIMIT = 400
#: How many bytes of an *error* body we're willing to read to extract a message.
#: Well above any real error JSON, far below "unbounded".
_ERROR_BODY_LIMIT = 64 * 1024
#: Hard cap on a *success* body. Generous: the largest legitimate embedding
#: batch (~2048 inputs x 3072-dim float vectors as JSON) is ~90 MB; 128 MiB
#: clears that with headroom while still bounding a hostile/wedged endpoint
#: that would otherwise stream until the process runs out of memory.
_MAX_RESPONSE_BYTES = 128 * 1024 * 1024

#: Bidi/format characters that are NOT "whitespace" (so the ``split()`` collapse
#: below can't catch them) yet forge or deceive in a log or terminal: the bidi
#: controls drive "Trojan Source"-style visual reordering (a server could make
#: ``key<RLO>gnp.evil`` render as a trusted host), and the zero-width set hides
#: or fragments text. Neutralize them alongside the C0/C1 controls. (Line and
#: paragraph separators U+2028/U+2029 ARE Unicode whitespace, so ``split()``
#: already collapses those — no need to list them here.)
_LOG_UNSAFE_FORMAT_CHARS = (
    0x061C,  # ARABIC LETTER MARK
    0x200B, 0x200C, 0x200D,  # ZERO WIDTH SPACE / NON-JOINER / JOINER
    0x200E, 0x200F,  # LEFT-TO-RIGHT / RIGHT-TO-LEFT MARK
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embeddings / overrides
    0x2060,  # WORD JOINER
    0x2066, 0x2067, 0x2068, 0x2069,  # bidi isolates (LRI/RLI/FSI/PDI)
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (BOM)
)

#: Neutralize C0 controls, DEL, the C1 range, and the bidi/zero-width format
#: characters above — all to a space. Printable text (any non-ASCII >= U+00A0
#: that isn't one of the format chars listed) is untouched; the whitespace
#: collapse that follows a ``translate`` merges the runs these leave behind.
_CONTROL_TRANSLATION = {
    codepoint: " "
    for codepoint in (
        *range(0x00, 0x20), 0x7F, *range(0x80, 0xA0), *_LOG_UNSAFE_FORMAT_CHARS
    )
}

T = TypeVar("T")


class ProviderError(Exception):
    """A provider call failed. The message never contains credentials."""

    def __init__(
        self, message: str, *, status: Optional[int] = None, provider: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. Returning ``None`` from ``redirect_request`` makes
    urllib surface the 3xx as an :class:`~urllib.error.HTTPError` instead of
    chasing ``Location`` — so credential headers are never re-sent to the
    redirect target and the client can't be pivoted onto ``ftp://`` etc."""

    def redirect_request(self, *args, **kwargs):
        return None


def batched(seq: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Yield ``seq`` in order, ``size`` items at a time (3.10-compatible)."""
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


def _is_loopback_host(host: Optional[str]) -> bool:
    """True only for genuinely local hosts.

    An IP literal is loopback iff it lives in 127.0.0.0/8 or is ``::1``. A
    *name* is loopback only when it is ``localhost`` or a ``*.localhost``
    subdomain (RFC 6761 reserves those for loopback). Crucially, a domain that
    merely *looks* numeric (``127.evil.com``) or *starts with* ``localhost``
    (``localhost.attacker.io``) is NOT loopback — it resolves wherever its
    owner points it.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    return host == "localhost" or host.endswith(".localhost")


def _is_credential_header(name: str) -> bool:
    """Whether a header name carries a secret we must not leak in cleartext.

    Covers ``Authorization`` (Bearer keys) and every API-key spelling in use or
    likely to appear: ``x-goog-api-key`` (Gemini), ``x-api-key``, ``api-key``
    (Azure). Matching on the ``api-key``/``apikey`` substring keeps this robust
    to providers added later without another edit here."""
    lowered = name.lower()
    return lowered == "authorization" or "api-key" in lowered or "apikey" in lowered


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Extract a short, log-safe message from an error body.

    The body is read BOUNDED — a hostile endpoint can't make us slurp gigabytes
    out of an error path — and every C0/C1 control plus bidi/zero-width format
    character is neutralized before truncation, so a server-chosen error string
    can't smuggle ANSI escapes / NUL / BEL, nor "Trojan Source" bidi overrides,
    into logs or tracebacks.
    """
    try:
        raw = exc.read(_ERROR_BODY_LIMIT).decode("utf-8", errors="replace")
    except Exception:
        return ""
    detail = ""
    try:
        parsed = json.loads(raw)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict) and err.get("message"):
            detail = str(err["message"])
        elif isinstance(err, str):
            detail = err
    except ValueError:
        pass
    text = (detail or raw).translate(_CONTROL_TRANSLATION)
    return " ".join(text.split())[:_ERROR_DETAIL_LIMIT]


def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
    if retry_after:
        try:
            value = float(retry_after)
        except ValueError:
            pass  # HTTP-date form of Retry-After; fall back to exponential
        else:
            # A hostile 'nan'/'inf'/negative parses fine but would poison
            # time.sleep(); only honor a finite, non-negative delay.
            if math.isfinite(value) and value >= 0.0:
                return min(value, _MAX_BACKOFF_SECONDS)
    return min(0.5 * (2**attempt), _MAX_BACKOFF_SECONDS)


def post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    retries: int = 2,
    provider: Optional[str] = None,
) -> dict:
    """POST JSON and return the parsed JSON object; retry 429/5xx with backoff.

    Raises :class:`ProviderError` on HTTP errors, connection failures, and
    non-JSON / non-object responses. Error text carries the server's message
    (trimmed) but never the request headers, so an API key cannot leak into
    logs or tracebacks.

    Redirects are never followed and credential-bearing requests are refused
    over cleartext ``http://`` to a non-loopback host (see the module docstring).
    """
    if not url.startswith(("http://", "https://")):
        raise ProviderError(f"{provider or 'provider'}: unsupported URL scheme in {url!r}")
    label = provider or url

    # A real API key must not cross the network in the clear. Refuse a keyed
    # request on http:// unless the peer is loopback (keeps keyless local
    # servers working; a keyless request has nothing to protect, so it passes).
    if url.startswith("http://") and any(
        _is_credential_header(name) for name in (headers or {})
    ):
        host = urllib.parse.urlsplit(url).hostname
        if not _is_loopback_host(host):
            raise ProviderError(
                f"{label}: refusing to send a credential header over cleartext "
                f"http:// to {host!r}; use https:// (loopback hosts are exempt)",
                provider=provider,
            )

    body = json.dumps(payload).encode("utf-8")
    # A non-redirecting opener: build it per call so live proxy settings are
    # honored (and neutralizable in tests) exactly as urlopen would.
    opener = urllib.request.build_opener(_NoRedirectHandler)
    attempts = max(retries, 0) + 1
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            if 300 <= status < 400:
                # Fail closed: never chase a redirect with the key attached.
                # We read nothing from the 3xx, so release its socket now
                # rather than leaving it for the garbage collector.
                exc.close()
                raise ProviderError(
                    f"{label}: refused to follow HTTP {status} redirect "
                    f"(a provider endpoint must not redirect a keyed POST)",
                    status=status,
                    provider=provider,
                ) from None
            detail = _error_detail(exc)
            if status in _RETRY_STATUSES and attempt + 1 < attempts:
                time.sleep(_retry_delay(exc.headers.get("Retry-After"), attempt))
                continue
            suffix = f": {detail}" if detail else ""
            raise ProviderError(
                f"{label}: HTTP {status}{suffix}", status=status, provider=provider
            ) from None
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"{label}: connection failed ({exc.reason})", provider=provider
            ) from None
        except OSError as exc:  # timeouts and low-level socket errors
            raise ProviderError(f"{label}: connection failed ({exc})", provider=provider) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError(
                f"{label}: response body exceeded {_MAX_RESPONSE_BYTES} bytes",
                provider=provider,
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(
                f"{label}: invalid JSON in response ({exc})", provider=provider
            ) from None
        if not isinstance(parsed, dict):
            raise ProviderError(f"{label}: unexpected non-object JSON response", provider=provider)
        return parsed
    raise AssertionError("unreachable: the retry loop always returns or raises")
