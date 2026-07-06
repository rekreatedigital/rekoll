"""The provider HTTP transport (`rekoll.providers._http`), hardened — issue #7.

Everything here runs OFFLINE against scriptable loopback servers (no real key,
nothing leaves the box). Each test class maps to a confirmed finding:

- M2 / M2d  — redirects are never followed (fail closed on every 3xx), so
              credentials cannot be re-sent to another host and a redirect
              cannot pivot the client onto another scheme (ftp://).
- M2b       — response bodies are bounded; error bodies are read bounded.
- M2c       — credential headers refuse to travel over cleartext http://
              unless the host is loopback (keyless local servers unaffected).
- L-ctrl    — server-controlled error text cannot inject control characters
              (ESC/BEL/NUL/C1) into logs via ProviderError messages.
- NIT       — a malicious `Retry-After: nan` cannot crash the retry loop.
"""

from __future__ import annotations

import math
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import pytest

from rekoll.providers import _http
from rekoll.providers._http import ProviderError, post_json


# -- scriptable loopback endpoint ----------------------------------------------


def _make_handler(endpoint: "_Endpoint"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            endpoint.requests.append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": self.rfile.read(length) if length else b"",
            })
            if endpoint._script:
                status, extra, body = endpoint._script.pop(0)
            else:
                status, extra, body = 200, {}, b'{"ok": true}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        do_POST = _serve
        do_GET = _serve  # a followed 302 re-issues POST as GET — record that too

    return Handler


class _Endpoint:
    """A loopback HTTP endpoint that records every request and can be scripted
    (queue exact status/headers/body per request; default is 200 `{"ok": true}`)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._script: list[tuple[int, dict, bytes]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def respond(
        self, status: int, headers: Optional[dict] = None, body: bytes = b'{"ok": true}'
    ) -> None:
        self._script.append((status, headers or {}, body))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def endpoint_factory(monkeypatch):
    # Same proxy neutralization as conftest.fake_provider: env/system proxies
    # must not route or block the loopback endpoints on CI boxes.
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.setattr(urllib.request, "_opener", None)
    made: list[_Endpoint] = []

    def make() -> _Endpoint:
        box = _Endpoint()
        made.append(box)
        return box

    yield make
    for box in made:
        box.close()


class _RecordingErrorBody:
    """Duck-types the only part of HTTPError that _error_detail touches."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.read_amt: object = "never-called"

    def read(self, amt: Optional[int] = None) -> bytes:
        self.read_amt = amt
        return self.payload if amt is None else self.payload[:amt]


# -- M2 / M2d: redirects fail closed -------------------------------------------


def test_cross_host_redirect_never_forwards_credentials(endpoint_factory):
    """A 302 to a different host must NOT be followed with the key attached —
    the call fails closed and the other host never sees a request at all."""
    other = endpoint_factory()
    origin = endpoint_factory()
    origin.respond(302, {"Location": f"{other.url}/v1/embeddings"}, body=b"")

    with pytest.raises(ProviderError) as excinfo:
        post_json(
            f"{origin.url}/v1/embeddings",
            {"input": ["x"]},
            headers={
                "Authorization": "Bearer sk-super-secret",
                "x-goog-api-key": "goog-secret",
            },
            timeout=5.0,
        )

    assert other.requests == []  # nothing reached the redirect target
    message = str(excinfo.value)
    assert excinfo.value.status == 302
    assert "redirect" in message.lower()
    assert "sk-super-secret" not in message  # keys never leak into errors
    assert "goog-secret" not in message


def test_same_host_redirect_also_fails_closed(endpoint_factory):
    """Even a same-host redirect is refused: JSON API endpoints don't
    legitimately redirect, so any 3xx is a misconfiguration or an attack."""
    origin = endpoint_factory()
    origin.respond(302, {"Location": f"{origin.url}/elsewhere"}, body=b"")

    with pytest.raises(ProviderError) as excinfo:
        post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0)

    assert len(origin.requests) == 1  # the redirect was never chased
    assert "redirect" in str(excinfo.value).lower()


def test_redirect_without_location_fails_closed(endpoint_factory):
    origin = endpoint_factory()
    origin.respond(302, body=b"")

    with pytest.raises(ProviderError) as excinfo:
        post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0)

    assert excinfo.value.status == 302
    assert "redirect" in str(excinfo.value).lower()


def test_redirect_to_ftp_never_connects(endpoint_factory):
    """stdlib urllib happily follows a redirect onto ftp:// (an SSRF pivot);
    failing closed on 3xx means the ftp port never even sees a connection."""
    decoy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        decoy.bind(("127.0.0.1", 0))
        decoy.listen(1)
        decoy.settimeout(0.5)
        ftp_port = decoy.getsockname()[1]

        origin = endpoint_factory()
        origin.respond(302, {"Location": f"ftp://127.0.0.1:{ftp_port}/pwn"}, body=b"")

        with pytest.raises(ProviderError):
            post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=1.5)

        with pytest.raises(TimeoutError):  # no connection was ever attempted
            decoy.accept()
    finally:
        decoy.close()


# -- M2b: bounded reads ---------------------------------------------------------


def test_response_body_over_cap_is_refused(endpoint_factory, monkeypatch):
    cap = 64 * 1024
    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", cap)
    origin = endpoint_factory()
    origin.respond(200, body=b"x" * (cap + 1))

    with pytest.raises(ProviderError) as excinfo:
        post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0)

    assert "response body exceeded" in str(excinfo.value)


def test_response_body_exactly_at_cap_still_parses(endpoint_factory, monkeypatch):
    cap = 64 * 1024
    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", cap)
    origin = endpoint_factory()
    payload = b'{"ok": true}'
    origin.respond(200, body=payload + b" " * (cap - len(payload)))  # valid JSON + pad

    assert post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0) == {
        "ok": True
    }


def test_error_body_read_is_bounded():
    """_error_detail must never slurp an unbounded error body into memory."""
    fake = _RecordingErrorBody(b'{"error": {"message": "boom"}}')
    detail = _http._error_detail(fake)  # duck-typed: only .read() is touched
    assert detail == "boom"
    assert isinstance(fake.read_amt, int), (
        f"exc.read() was called with amt={fake.read_amt!r} — an unbounded read"
    )
    assert fake.read_amt == _http._ERROR_BODY_LIMIT


# -- M2c: no credentials over cleartext http:// to a remote host ----------------


@pytest.mark.parametrize(
    "credential_headers",
    [
        {"Authorization": "Bearer sk-x"},
        {"x-goog-api-key": "gk-x"},
        {"api-key": "azure-style"},
    ],
)
def test_credentials_over_cleartext_http_to_remote_host_refused(
    monkeypatch, credential_headers
):
    """A keyed request to http://<remote> must be refused BEFORE any network
    activity — not even a DNS lookup happens for it."""
    resolutions: list[str] = []

    def _no_dns(host, *args, **kwargs):
        resolutions.append(host)
        raise socket.gaierror("DNS disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)

    with pytest.raises(ProviderError) as excinfo:
        post_json(
            "http://embeddings.example/v1/embeddings",
            {"input": ["x"]},
            headers=credential_headers,
            retries=0,
        )

    assert resolutions == []  # refused before DNS, let alone a socket
    message = str(excinfo.value)
    assert "cleartext" in message
    assert "https://" in message
    for secret in credential_headers.values():
        assert secret not in message


def test_keyed_http_to_loopback_still_works(endpoint_factory):
    """Local proxies (LiteLLM/ollama-style, possibly keyed) stay usable: the
    cleartext refusal exempts loopback hosts."""
    origin = endpoint_factory()
    result = post_json(
        f"{origin.url}/v1/embeddings",
        {"input": ["x"]},
        headers={"Authorization": "Bearer local-master-key"},
        timeout=5.0,
    )
    assert result == {"ok": True}
    assert origin.requests[0]["headers"]["authorization"] == "Bearer local-master-key"


def test_keyless_http_to_remote_host_is_not_blocked(monkeypatch):
    """Keyless cleartext (e.g. ollama on another LAN box) must keep working:
    with no credential header there is nothing to protect, so the request
    proceeds to normal connection handling."""
    resolutions: list[str] = []

    def _no_dns(host, *args, **kwargs):
        resolutions.append(host)
        raise socket.gaierror("DNS disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)

    with pytest.raises(ProviderError) as excinfo:
        post_json("http://ollama.lan:11434/v1/embeddings", {"input": ["x"]}, retries=0)

    assert resolutions == ["ollama.lan"]  # it tried to connect — not refused
    assert "cleartext" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("host", "is_loopback"),
    [
        ("localhost", True),
        ("app.localhost", True),
        ("127.0.0.1", True),
        ("127.254.1.2", True),  # all of 127/8 is loopback
        ("::1", True),
        ("127.evil.com", False),  # a DOMAIN starting with 127. is not an IP
        ("localhost.attacker.io", False),
        ("example.com", False),
        ("10.0.0.5", False),  # private LAN is exactly where MITM lives
        ("", False),
        (None, False),
    ],
)
def test_loopback_host_predicate(host, is_loopback):
    assert _http._is_loopback_host(host) is is_loopback


# -- L-ctrl: control characters never reach ProviderError messages --------------


def test_error_detail_strips_control_and_escape_chars(endpoint_factory):
    origin = endpoint_factory()
    # JSON-escaped in the body: u001b=ESC (ANSI), u0007=BEL, u0000=NUL, u009b=C1 CSI.
    origin.respond(
        400,
        body=b'{"error": {"message": "bad\\u001b[31mkey\\u0007\\u0000\\u009bzap"}}',
    )

    with pytest.raises(ProviderError) as excinfo:
        post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0)

    message = str(excinfo.value)
    for forbidden in ("\x1b", "\x07", "\x00", "\x9b"):
        assert forbidden not in message
    assert "bad" in message and "zap" in message  # printable content survives


def test_error_detail_strips_controls_from_non_json_bodies():
    fake = _RecordingErrorBody(b"oops\x1b[2Jwiped\x07")
    detail = _http._error_detail(fake)
    assert "\x1b" not in detail and "\x07" not in detail
    assert "oops" in detail and "wiped" in detail


# The full set of bidi / zero-width format chars the transport must neutralize.
# Built from integer code points (this source embeds no literal invisibles) and
# hardcoded here rather than imported from the module, so dropping one from the
# module's own list is caught by a failing test, not silently masked. U+202E is
# the "Trojan Source" right-to-left override; U+2066/U+2069 are bidi isolates;
# U+200B and U+FEFF are zero-width.
_EXPECTED_FORMAT_CODEPOINTS = (
    0x061C,                                  # ARABIC LETTER MARK
    0x200B, 0x200C, 0x200D,                  # ZERO WIDTH SPACE / NON-JOINER / JOINER
    0x200E, 0x200F,                          # LEFT-TO-RIGHT / RIGHT-TO-LEFT MARK
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embeddings / overrides
    0x2060,                                  # WORD JOINER
    0x2066, 0x2067, 0x2068, 0x2069,          # bidi isolates (LRI/RLI/FSI/PDI)
    0xFEFF,                                  # BOM / zero-width no-break space
)
_LOG_UNSAFE_SAMPLES = "".join(chr(c) for c in _EXPECTED_FORMAT_CODEPOINTS)


def test_error_detail_strips_bidi_and_zero_width_from_json_message():
    # A server reordering its error text to impersonate a trusted host: the
    # "key" + <RLO> + "gnp.evil" body renders right-to-left as "key live.png".
    rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
    fake = _RecordingErrorBody(('{"error":{"message":"key' + rlo + 'gnp.evil"}}').encode("utf-8"))
    detail = _http._error_detail(fake)
    assert rlo not in detail
    assert "key" in detail and "gnp.evil" in detail  # the real text is preserved


def test_error_detail_strips_every_bidi_and_zero_width_char():
    """None of the known bidi/zero-width vectors may survive, from either the
    parsed-message path or the raw-body fallback."""
    for body in (
        ('{"error":{"message":"a' + _LOG_UNSAFE_SAMPLES + 'b"}}').encode("utf-8"),
        ("plain " + _LOG_UNSAFE_SAMPLES + " body").encode("utf-8"),  # non-JSON fallback
    ):
        detail = _http._error_detail(_RecordingErrorBody(body))
        leaked = [f"U+{ord(c):04X}" for c in detail if c in _LOG_UNSAFE_SAMPLES]
        assert not leaked, f"format chars leaked into error detail: {leaked}"


def test_bidi_body_over_the_wire_yields_clean_provider_error(endpoint_factory):
    """End-to-end: a real 400 whose body carries bidi/zero-width chars produces
    a ProviderError message with none of them."""
    origin = endpoint_factory()
    origin.respond(
        400,
        body=('{"error":{"message":"bad ' + _LOG_UNSAFE_SAMPLES + ' key"}}').encode("utf-8"),
    )
    with pytest.raises(ProviderError) as excinfo:
        post_json(f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0)
    message = str(excinfo.value)
    assert not any(c in message for c in _LOG_UNSAFE_SAMPLES)
    assert "bad" in message and "key" in message


def test_control_translation_covers_the_documented_ranges():
    """Guard the table itself so a future trim can't silently reopen a vector:
    every C0, DEL, C1, and expected bidi/zero-width code point maps to a space,
    and ordinary printable text is left alone."""
    table = _http._CONTROL_TRANSLATION
    expected = [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0), *_EXPECTED_FORMAT_CODEPOINTS]
    for codepoint in expected:
        assert table.get(codepoint) == " ", f"U+{codepoint:04X} not neutralized"
    for keep in ("A", "z", chr(0xC0), chr(0x4F60), chr(0x1F642), " "):  # ascii/accent/CJK/emoji/space
        assert ord(keep) not in table


# -- NIT: hostile Retry-After values --------------------------------------------


def test_retry_delay_never_returns_nonfinite_or_negative():
    for hostile in ("nan", "inf", "-inf", "1e400", "-1", "  nan  "):
        delay = _http._retry_delay(hostile, attempt=0)
        assert math.isfinite(delay) and 0.0 <= delay <= _http._MAX_BACKOFF_SECONDS, (
            f"Retry-After {hostile!r} produced unusable delay {delay!r}"
        )


def test_retry_delay_sane_values_still_honored():
    assert _http._retry_delay("2", attempt=0) == 2.0
    assert _http._retry_delay("999", attempt=0) == _http._MAX_BACKOFF_SECONDS
    assert _http._retry_delay(None, attempt=2) == 2.0  # exponential: 0.5 * 2**2
    # HTTP-date form falls back to exponential, as before.
    assert _http._retry_delay("Wed, 21 Oct 2015 07:28:00 GMT", attempt=1) == 1.0


def test_retry_after_nan_does_not_crash_the_retry_loop(endpoint_factory):
    """A 429 with `Retry-After: nan` used to escape post_json as an uncaught
    ValueError from time.sleep(nan); it must retry (with backoff) instead."""
    origin = endpoint_factory()
    origin.respond(429, {"Retry-After": "nan"}, body=b'{"error": {"message": "slow"}}')

    result = post_json(
        f"{origin.url}/v1/embeddings", {"input": ["x"]}, timeout=5.0, retries=1
    )

    assert result == {"ok": True}
    assert len(origin.requests) == 2  # the retry actually happened
