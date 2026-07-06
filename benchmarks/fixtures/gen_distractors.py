#!/usr/bin/env python
"""Deterministic, seeded filler-distractor generator for the semantic_v1
corpus-size sweep (pre-registered: sizes 100/1k/10k, seed 20260707).

The 100-doc corpus is fully committed in ``semantic_v1.json``; only the
1k/10k *filler* comes from here, to keep repo weight sane. Determinism is
frozen by tests/test_semantic_fixture.py pinning the sha256 of the generated
lists, so the generator cannot drift silently.

HONESTY CONSTRAINTS (why the vocabulary looks the way it does):
- Filler uses eight fictitious project names DISJOINT from the five fixture
  projects, so no generated doc can masquerade as a project-scoped gold.
- Template topics deliberately EXCLUDE anything that could form an unlabeled
  valid answer to a fixture query: no caching/certificate incidents, no
  datastore-choice decisions, no rate limits/throttles, no cost-savings notes,
  no refund/idempotency/webhook-verification/SLA/secrets-injection content.
  Shared tech nouns (kafka, redis, postgres, ...) DO appear in other,
  non-answering contexts — filler is meant to be lexically close (hard),
  just never gold-equivalent.
- The vocabulary contains none of the negative-control terms (erlang,
  mainframe, blockchain, biometric, fingerprints, iceland, salesforce,
  quantum, fortran, zurich, seating, oracle, nvidia, mars, cobol, payroll,
  helsinki), so control answers stay absent at every corpus size (asserted
  programmatically by the integrity test).

Pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
import random

SEED = 20260707

PROJECTS = ["quartz", "onyx", "maple", "cedar", "harbor", "lumen", "sable", "willow"]

SERVICES = [
    "the api gateway", "the ingest worker", "the report builder", "the media proxy",
    "the session service", "the export daemon", "the metrics collector",
    "the notification relay", "the auth shim", "the batch scheduler",
]

TOOLS = [
    "eslint", "prettier", "mypy", "ruff", "gradle", "webpack", "vite", "pnpm",
    "poetry", "buildkite", "grafana", "prometheus", "sentry", "datadog",
]

TECH = [
    "kafka", "redis", "postgres", "elasticsearch", "kubernetes", "terraform",
    "airflow", "spark", "sqlite", "rabbitmq", "nginx", "graphql",
]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]

# Each template is dev-memory-shaped but deliberately non-answering (see
# module docstring). Slots: {p} project, {s} service, {t} tool, {x} tech,
# {n}/{m} numbers, {d} weekday.
TEMPLATES = [
    "{p} note: {s} was upgraded to {t} {n}.{m} last sprint; the changelog is linked from the team wiki.",
    "{p} convention: pull requests need one approving review and a green {t} run before merge; drafts are exempt.",
    "{p} note: unit tests for {s} were flaky on {d}s because of a shared fixture; the suite now uses per-test databases.",
    "{p} how-to: to get access to the {x} staging namespace, file a request in the access portal and tag your team lead.",
    "{p} note: {s} logs are retained for {n} days in the central store; debug level is sampled at 1 percent.",
    "{p} convention: standup is async in the team channel on {d}s; the written update replaces the meeting.",
    "{p} note: the {x} client library was pinned to {n}.{m} after a regression in connection reuse.",
    "{p} how-to: regenerate the {s} API client by running the codegen task; generated files are never edited by hand.",
    "{p} note: dashboards for {s} live in {t}; the golden signals row is maintained by the platform team.",
    "{p} convention: feature branches are named {p}-<issue-number>-<slug> and squash-merged with a conventional commit title.",
    "{p} note: {s} moved from {n} to {m} replicas after the load test; CPU headroom is now about 40 percent.",
    "{p} how-to: local development uses docker compose with seeded {x} data; run the bootstrap script before first start.",
    "{p} note: the {d} maintenance window for {s} is 06:00 to 07:00 UTC; a banner is posted an hour before.",
    "{p} convention: TODO comments must reference an issue id; the linter fails the build on bare TODOs.",
    "{p} note: {s} emits structured JSON logs; the old plaintext format was removed after the parser migration.",
    "{p} how-to: rotate your personal access token from the developer settings page; tokens older than {n} days are revoked automatically.",
    "{p} note: the {x} connection pool for {s} is capped at {n}; raising it needs a review from the database group.",
    "{p} convention: design docs are one-pagers in the shared drive, reviewed asynchronously; silence for {n} days counts as approval.",
    "{p} note: {t} caching cut the {s} build from {n} minutes to {m}; the cache key includes the lockfile hash.",
    "{p} note: error budgets for {s} reset monthly; burning half the budget triggers a slowdown of feature work.",
    "{p} how-to: to profile {s}, enable the sampling profiler flag and pull the flamegraph from the diagnostics endpoint.",
    "{p} note: the {x} topic naming scheme is {p}.<domain>.<event>; retention defaults to {n} days.",
    "{p} convention: interface changes to {s} require a deprecation notice in the release notes one version ahead.",
    "{p} note: on-duty triage rotates every {d}; unresolved tickets hand off with a written summary.",
    "{p} note: the vendor invoice for {t} is reviewed quarterly; unused seats are reclaimed automatically.",
    "{p} how-to: schema fixtures for {x} tests live under testdata; refresh them with the snapshot task, never by hand.",
    "{p} note: {s} health checks probe every {n} seconds with a {m}-second timeout; three failures mark the instance unhealthy.",
    "{p} convention: all times in {s} APIs are UTC ISO-8601 strings; client-local rendering happens in the UI layer.",
]


def generate(count: int, seed: int = SEED) -> list[dict]:
    """Deterministically generate `count` filler docs: [{"key","text"}...]."""
    rng = random.Random(seed)
    docs = []
    for i in range(count):
        tpl = rng.choice(TEMPLATES)
        text = tpl.format(
            p=rng.choice(PROJECTS), s=rng.choice(SERVICES), t=rng.choice(TOOLS),
            x=rng.choice(TECH), n=rng.randint(2, 90), m=rng.randint(2, 30),
            d=rng.choice(WEEKDAYS),
        )
        docs.append({"key": f"f-{i:05d}", "text": text})
    return docs


def corpus_hash(docs: list[dict]) -> str:
    blob = json.dumps(docs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    for n in (900, 9900):
        docs = generate(n)
        print(f"filler n={n}: sha256={corpus_hash(docs)}")
        print(f"  sample: {docs[0]['text']}")
