"""Shared test fixtures: a local fake AI-provider HTTP server.

Provider tests must pass OFFLINE with no API keys: the fake server binds
127.0.0.1 on an ephemeral port — loopback only, so even the zero-egress
invariant's spirit holds (nothing ever leaves the box). Real-key smoke tests
live in tests/test_providers_smoke.py and skip unless their env var is set.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import pytest


def deterministic_vector(text: str, dim: int) -> list[float]:
    """Same text → same vector, so tests can assert exact round-trips."""
    seed = sum(text.encode("utf-8")) or 1
    return [((seed * (i + 3)) % 97) / 97.0 for i in range(dim)]


class FakeProvider:
    """A programmable OpenAI/Gemini/Voyage-shaped endpoint that records requests.

    - ``requests``: every request as ``{"path", "headers", "json"}``.
    - ``fail_next(status, times=, headers=)``: queue forced error responses.
    - ``chat_content`` / ``chat_response``: script the chat completion reply.
    - ``/embeddings`` items are returned in REVERSED order with correct
      ``index`` fields, so a client that trusts list order fails loudly.
    """

    def __init__(self) -> None:
        self.dim = 8
        self.chat_content = "consolidated summary."
        self.chat_response: Optional[dict] = None  # overrides chat_content verbatim
        self.requests: list[dict] = []
        self._forced: list[tuple[int, dict]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"{self.root_url}/v1"

    @property
    def root_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def fail_next(self, status: int, *, times: int = 1, headers: Optional[dict] = None) -> None:
        for _ in range(times):
            self._forced.append((status, headers or {}))

    def vector_for(self, text: str, dim: Optional[int] = None) -> list[float]:
        return deterministic_vector(text, dim or self.dim)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _make_handler(box: FakeProvider):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            box.requests.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "json": payload,
            })
            if box._forced:
                status, extra = box._forced.pop(0)
                self._send(status, {"error": {"message": f"forced {status}"}}, extra)
            elif self.path.endswith("/embeddings"):
                texts = payload.get("input", [])
                # OpenAI spells it "dimensions"; Voyage spells it "output_dimension".
                dim = payload.get("dimensions") or payload.get("output_dimension") or box.dim
                items = [
                    {"index": i, "embedding": deterministic_vector(t, dim)}
                    for i, t in enumerate(texts)
                ]
                self._send(200, {"data": list(reversed(items)), "model": payload.get("model")})
            elif ":batchEmbedContents" in self.path:
                embeddings = []
                for req in payload.get("requests", []):
                    text = req["content"]["parts"][0]["text"]
                    dim = req.get("outputDimensionality") or box.dim
                    embeddings.append({"values": deterministic_vector(text, dim)})
                self._send(200, {"embeddings": embeddings})
            elif self.path.endswith("/chat/completions"):
                if box.chat_response is not None:
                    self._send(200, box.chat_response)
                else:
                    self._send(200, {
                        "choices": [
                            {"message": {"role": "assistant", "content": box.chat_content}}
                        ]
                    })
            else:
                self._send(404, {"error": {"message": f"no route {self.path}"}})

        def _send(self, status, obj, extra_headers=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture()
def fake_provider(monkeypatch):
    # urllib honors proxies from env vars AND (on Windows/macOS) system
    # settings; behind a corporate proxy that would route or block the
    # loopback fake. Neutralize every proxy source for the test and drop
    # urllib's cached opener so a proxy-free one is built — production
    # behavior (real proxies honored) is untouched.
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.setattr(urllib.request, "_opener", None)
    box = FakeProvider()
    yield box
    box.close()


@pytest.fixture()
def scrub_provider_env(monkeypatch):
    """Clear every provider API-key env var so key-resolution tests are hermetic
    even on a machine that has real keys set."""
    from rekoll.providers.gemini import GeminiEmbedder
    from rekoll.providers.openai_compat import PRESETS
    from rekoll.providers.voyage import VoyageEmbedder

    env_vars = {p.env_var for p in PRESETS.values() if p.env_var}
    env_vars.update(GeminiEmbedder._ENV_VARS)
    env_vars.add(VoyageEmbedder.ENV_VAR)
    for var in sorted(env_vars):
        monkeypatch.delenv(var, raising=False)
