"""Battle-tester red-team v1 — ReDoS / DoS regression harness.

Offense measured wall time (not eyeballing) across every regex reachable from
untrusted input, at the ingest content cap and well beyond. NOTHING was superlinear
— the prior waves' bounded-quantifier hardening holds. This harness LOCKS THAT IN:
it feeds adversarial floods engineered to trigger catastrophic backtracking in each
pattern family and asserts screen() stays fast. A future edit that regresses a
marker/secret regex to nonlinear backtracking fails here (complements test_limits).

Budgets are deliberately generous (worst observed at the 100k cap was ~0.33s); they
catch a catastrophic (seconds-to-minutes) blowup, not micro-fluctuations, so they do
not flake on a busy CI box.
"""

from __future__ import annotations

import time

import pytest

from rekoll.firewall import screen, screen_pieces
from rekoll.chunking import chunk_file
from rekoll.model import TrustTier

CAP = 100_000  # matches DEFAULT_MAX_CONTENT_CHARS: the most one screen() sees.

# (label, builder) — each returns a ~CAP-char adversarial string aimed at one
# pattern family's worst case (repeated prefixes / near-miss floods / dot-in-class
# domain bait / unbounded-class bait / lazy-gap floods).
_FLOODS = {
    "pem_header_flood":      lambda n: "-----BEGIN PRIVATE KEY-----" * (n // 27),
    "pem_body_space_bait":   lambda n: "-----BEGIN " + "A " * (n // 2),
    "jwt_eyJ_flood":         lambda n: "eyJ" * (n // 3),
    "connstr_scheme_flood":  lambda n: "a" * 30 + "://" + "u" * (n - 33),
    "connstr_dotdash_flood": lambda n: "ab.-" * (n // 4),
    "email_dash_flood":      lambda n: "1-" * (n // 2) + "@",
    "email_dotdomain_bait":  lambda n: ("x@" + "a." * 127 + " ") * (n // 260),
    "sk_prefix_flood":       lambda n: "sk-" * (n // 3),
    "slack_xox_flood":       lambda n: "xoxb-" + "a-" * (n // 2),
    "cred_assign_flood":     lambda n: "password=" + "A" * (n - 9),
    "marker_ignore_flood":   lambda n: "ignore all " * (n // 11),
    "marker_override_flood": lambda n: "override your " * (n // 14),
    "marker_reveal_lazy":    lambda n: "show me the x " * (n // 14),
    "role_tag_flood":        lambda n: "<system>" * (n // 8),
    "pipe_token_flood":      lambda n: "<|a|>" * (n // 5),
    "zh_marker_flood":       lambda n: "忽略" * (n // 2),
    "whitespace_flood":      lambda n: " \t" * (n // 2),
    "header_markup_flood":   lambda n: ">#*=_~- " * (n // 8),
}

# Generous: a catastrophic-backtracking regression turns these into seconds→minutes.
_BUDGET_S = 3.0


@pytest.mark.parametrize("label", sorted(_FLOODS))
@pytest.mark.parametrize("redact_pii", [False, True])
def test_screen_is_linear_on_adversarial_flood(label, redact_pii):
    blob = _FLOODS[label](CAP)
    t0 = time.perf_counter()
    screen(blob, source_trust=TrustTier.UNVERIFIED, redact_pii=redact_pii)
    dt = time.perf_counter() - t0
    assert dt < _BUDGET_S, f"screen() on {label} took {dt:.2f}s (>= {_BUDGET_S}s: ReDoS?)"


def test_screen_pieces_bounded_on_marker_dense_document():
    # The whole-document scan over the largest ingestible marker-dense doc (10MB
    # bytes / 25k chunk caps). Post-fix it is O((pieces+spans) log spans); the old
    # O(pieces x spans) took minutes here. Budget bounds a quadratic regression.
    doc = "<user>\n" * 300_000  # ~2.1MB, well within max_file_bytes
    pieces = chunk_file("d.txt", doc)
    t0 = time.perf_counter()
    screen_pieces(doc, pieces)
    dt = time.perf_counter() - t0
    assert dt < 3.0, f"screen_pieces took {dt:.2f}s on a marker-dense doc (quadratic regression?)"


def test_screen_pieces_scales_subquadratically():
    def t(n):
        doc = "<user>\n" * n
        pieces = chunk_file("d.txt", doc)
        t0 = time.perf_counter()
        screen_pieces(doc, pieces)
        return time.perf_counter() - t0

    t1 = t(100_000)
    t2 = t(200_000)
    # Quadratic -> ~4x on a 2x input; linearithmic -> ~2x. Allow 3.0 for CI noise.
    assert t2 < t1 * 3.0 + 0.05, f"screen_pieces superlinear: {t1:.3f}s -> {t2:.3f}s"
