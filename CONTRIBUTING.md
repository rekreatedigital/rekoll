# Contributing to Rekoll

Thanks for helping build a private, trustworthy memory layer. This is a small
team — response times vary — but every good PR and issue is welcome.

## Ground rules

- **Sign your commits (DCO).** Add `Signed-off-by: Your Name <you@example.com>`
  to each commit (`git commit -s`). We use the Developer Certificate of Origin,
  not a CLA — your contribution stays yours.
- **Read [NON_GOALS.md](NON_GOALS.md) first.** Features that conflict with the
  non-goals will be declined regardless of quality.
- **Security issues go private** — see [SECURITY.md](SECURITY.md), never a public issue.

## Dev setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The foundation has **zero runtime dependencies** (standard library only). Please
keep it that way unless a dependency is clearly justified in a PR discussion.

## Writing a storage adapter

Any backend must pass the shared contract:

```python
from rekoll.conformance import run_all
from rekoll.embedding import StubEmbedder
run_all(lambda: MyAdapter(...), StubEmbedder())
```

`run_all` ships *assertions*, not test files, so first- and third-party adapters
are held to the identical contract. Advertise only the `capabilities` you truly
support — unsupported operations must raise `UnsupportedCapabilityError`, never
silently degrade.

## Conventions

- Keyword-only public methods; typed dataclasses for results (no raw dicts).
- No unbounded JSON blobs in storage — flat scalars or bounded child tables.
- Provenance + trust are NOT-NULL and never inferred by an LLM.
- Add an ADR under `docs/adr/` for any load-bearing decision.
- New behavior needs a test; new contract guarantees go in `conformance.py`.
