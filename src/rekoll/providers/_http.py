"""Minimal JSON-over-HTTP transport for the opt-in provider layer (ADR-0015).

Standard library only (``urllib.request``): bringing your own cloud AI adds
ZERO pip dependencies. Importing this module opens no socket — network I/O
happens only inside :func:`post_json`, which is reachable only through a
provider the user explicitly constructed or named.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Iterator, Mapping, Optional, Sequence, TypeVar

__all__ = ["ProviderError", "post_json", "batched"]

#: Statuses worth retrying: rate limits and transient server errors.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 10.0
_ERROR_DETAIL_LIMIT = 400

T = TypeVar("T")


class ProviderError(Exception):
    """A provider call failed. The message never contains credentials."""

    def __init__(
        self, message: str, *, status: Optional[int] = None, provider: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


def batched(seq: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Yield ``seq`` in order, ``size`` items at a time (3.10-compatible)."""
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Extract a short human-readable message from an error body, safely."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
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
    detail = " ".join((detail or raw).split())
    return detail[:_ERROR_DETAIL_LIMIT]


def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_BACKOFF_SECONDS)
        except ValueError:
            pass  # HTTP-date form of Retry-After; fall back to exponential
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
    """
    if not url.startswith(("http://", "https://")):
        raise ProviderError(f"{provider or 'provider'}: unsupported URL scheme in {url!r}")
    body = json.dumps(payload).encode("utf-8")
    label = provider or url
    attempts = max(retries, 0) + 1
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
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
