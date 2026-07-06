"""Dogfood harness: does ``ingest_path -> recall()`` surface the right FILE for
realistic developer questions on real local repos?

This is functional efficacy, not a synthetic benchmark: the questions are
pre-registered (committed before any recall ran — see ``questions_<repo>.json``
in this directory), each with a gold file path a returning developer would need.

Protocol (see RESULTS_dogfood_v1.md for the run this shipped with):
  1. ONE temp store db, ``Memory(path=<tmp>, project="<repo-name>")`` per repo,
     real embedder REQUIRED (fail loudly on the stub).
  2. ``ingest_path(<repo root>)`` — zero-config unless the repo entry in
     ``REPOS`` says otherwise; the exact invocation is part of the result.
  3. Score: ``recall(question, k=5)``; HIT = any top-5 hit's provenance points
     at the gold file (a chunk of the gold file counts).
  4. Scope isolation: questions flagged ``isolation`` are re-run in every OTHER
     repo's project scope — zero cross-project hits allowed; per-project
     ``count()`` must be unaffected by other repos' ingests.
  5. ``health()`` after each ingest, recorded verbatim.

Privacy: the emitted JSON/state contains file PATHS and numbers only — never
content from the target repos. The temp db is never committed.

Usage (from the repo root; stdlib + rekoll only, no new deps):

    python benchmarks/dogfood/run_dogfood.py --db <tmpdir>/dogfood.db \
        --out <tmpdir>/results.json [--repo <name> ...] [--skip-ingest]

Deterministic given the same repo state: same chunks, same vectors, same RRF
fusion; the reranker is a deterministic ONNX cross-encoder.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Pin rekoll to THIS checkout's src (not whatever editable install is around).
_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from rekoll import Memory  # noqa: E402
from rekoll.memory import DEFAULT_SKIP_DIRS  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_REPOS_ROOT = Path("C:/Users/user/Documents/GitHub")

# repo name -> (directory name under --repos-root, extra ingest kwargs).
# Empty kwargs == ZERO-CONFIG ingest (the measured default behavior).
# "rekreate-ai-chat--noenv" is the SAME repo re-scoped with the checked-in
# ``env/`` virtualenv excluded, to measure how much the vendored noise costs —
# the exact invocation difference is the point and is reported as a result.
REPOS: dict[str, tuple[str, dict]] = {
    "powered-by-people-website": ("powered-by-people-website", {}),
    "jeff-app-for-iphone": ("jeff-app-for-iphone", {}),
    "rekreate-ai-chat": ("rekreate-ai-chat", {}),
    "rekreate-ai-chat--noenv": (
        "rekreate-ai-chat",
        {"skip_dirs": sorted(DEFAULT_SKIP_DIRS | {"env"})},
    ),
    "Trading-Logging-Automation": ("Trading-Logging-Automation", {}),
}

K = 5


def die(msg: str) -> "NoReturn":  # noqa: F821 - py3.9-friendly annotation
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(2)


def require_real_embedder(mem: Memory) -> None:
    """The whole exercise is meaningless on hash vectors: fail loudly."""
    ident = mem.embedder.identity()
    if ident.name == "stub-hash":
        die(
            "resolved embedder is the stub (no semantics) — install the "
            "'embeddings' extra: pip install 'rekoll[embeddings]'"
        )
    if ident.dim != 384:
        die(f"unexpected embedder dim {ident.dim} (want 384 / bge-small-en-v1.5)")


def git_head(repo_path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() if out.returncode == 0 else "no-git"
    except Exception:
        return "no-git"


def hit_paths(result) -> list[str]:
    """Provenance file paths of the hits, rank order (paths only, no content)."""
    return [h.record.provenance.source_file or h.record.provenance.source_uri
            for h in result.hits]


def gold_rank(result, gold_path: str) -> int | None:
    """1-based rank of the first hit whose provenance points at the gold file."""
    want = gold_path.casefold()
    for i, h in enumerate(result.hits, start=1):
        got = (h.record.provenance.source_file or "").casefold()
        if got == want:
            return i
    return None


def open_memory(db: str, project: str) -> Memory:
    return Memory(path=db, project=project)


def load_questions(repo: str) -> list[dict]:
    # A "--variant" scope (e.g. rekreate-ai-chat--noenv) re-scores the SAME
    # repo's pre-registered questions under a different ingest configuration.
    qfile = HERE / f"questions_{repo.split('--')[0]}.json"
    if not qfile.exists():
        die(f"missing pre-registered questions file: {qfile}")
    data = json.loads(qfile.read_text(encoding="utf-8"))
    qs = data["questions"]
    if len(qs) < 10:
        die(f"{qfile.name}: expected >= 10 pre-registered questions, got {len(qs)}")
    return qs


def ingest_repo(db: str, repo: str, repos_root: Path) -> dict:
    dirname, kwargs = REPOS[repo]
    repo_path = repos_root / dirname
    if not repo_path.is_dir():
        die(f"target repo not found: {repo_path}")
    mem = open_memory(db, repo)
    require_real_embedder(mem)
    t0 = time.perf_counter()
    stats = mem.ingest_path(str(repo_path), **kwargs)
    wall = time.perf_counter() - t0
    health = mem.health().to_dict()
    invocation = (
        f"Memory(path=<tmp db>, project={repo!r}).ingest_path({str(repo_path)!r}"
        + ("".join(f", {k}={v!r}" for k, v in kwargs.items())) + ")"
    )
    out = {
        "repo": repo,
        "repo_dir": str(repo_path),
        "git_head": git_head(repo_path),
        "invocation": invocation,
        "ingest": stats,
        "wall_seconds": round(wall, 2),
        "count_after_own_ingest": mem.count(),
        "health_after_ingest": health,
    }
    mem.close()
    print(f"[ingest] {repo}: {stats} in {wall:.1f}s  health.ok={health['ok']}")
    return out


def score_repo(db: str, repo: str) -> dict:
    questions = load_questions(repo)
    mem = open_memory(db, repo)
    require_real_embedder(mem)
    rows = []
    mode = None
    for q in questions:
        result = mem.recall(q["question"], k=K)
        mode = result.mode
        rank = gold_rank(result, q["gold_path"])
        rows.append({
            "id": q["id"],
            "difficulty": q.get("difficulty", "unspecified"),
            "question": q["question"],
            "gold_path": q["gold_path"],
            "rank": rank,
            "hit": rank is not None,
            "surfaced_paths": hit_paths(result),
        })
    hits = sum(r["hit"] for r in rows)
    mem.close()
    print(f"[score] {repo}: {hits}/{len(rows)} hits @k={K}  mode={mode!r}")
    for r in rows:
        tag = f"rank {r['rank']}" if r["hit"] else "MISS"
        print(f"    {r['id']:>8}  {tag:>7}  {r['gold_path']}")
    return {"repo": repo, "mode": mode, "k": K,
            "hits": hits, "total": len(rows), "questions": rows}


def isolation_checks(db: str, repos: list[str],
                     expected_counts: dict[str, int]) -> dict:
    """Re-run each repo's isolation-flagged questions in every OTHER scope:
    a hit on the asking repo's gold file, or ANY record from another project,
    is a leak. Also: per-project count() must match what it was right after
    that project's own ingest (other ingests must not disturb it)."""
    leaks: list[dict] = []
    count_mismatches: list[dict] = []
    checks = 0
    counts_now: dict[str, int] = {}
    for repo in repos:
        mem = open_memory(db, repo)
        counts_now[repo] = mem.count()
        mem.close()
        want = expected_counts.get(repo)
        if want is not None and counts_now[repo] != want:
            count_mismatches.append(
                {"repo": repo, "count_after_own_ingest": want,
                 "count_after_all_ingests": counts_now[repo]})
    for repo in repos:
        iso_qs = [q for q in load_questions(repo) if q.get("isolation")]
        if len(iso_qs) < 3:
            die(f"{repo}: need >= 3 isolation-flagged questions, got {len(iso_qs)}")
        for other in repos:
            if other == repo:
                continue
            # The two rekreate scopes deliberately hold the same repo's files —
            # cross-checking those two would "leak" by construction; skip.
            if REPOS[repo][0] == REPOS[other][0]:
                continue
            mem = open_memory(db, other)
            for q in iso_qs:
                result = mem.recall(q["question"], k=K)
                checks += 1
                for h in result.hits:
                    if h.record.scope.project != other:
                        leaks.append({"question_id": q["id"], "asked_in": other,
                                      "foreign_project": h.record.scope.project})
                    if (h.record.provenance.source_file or "").casefold() == \
                            q["gold_path"].casefold():
                        leaks.append({"question_id": q["id"], "asked_in": other,
                                      "gold_path_surfaced": q["gold_path"]})
            mem.close()
    print(f"[isolation] {checks} cross-scope recalls, {len(leaks)} leak(s), "
          f"{len(count_mismatches)} count mismatch(es)")
    return {"cross_scope_recalls": checks, "leaks": leaks,
            "count_mismatches": count_mismatches, "counts_now": counts_now}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="temp sqlite store path (never commit)")
    ap.add_argument("--out", required=True, help="results JSON path (paths+numbers only)")
    ap.add_argument("--repos-root", default=str(DEFAULT_REPOS_ROOT))
    ap.add_argument("--repo", action="append", choices=sorted(REPOS),
                    help="repeatable; default = all")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="score/isolate against an already-built db")
    ap.add_argument("--skip-isolation", action="store_true")
    args = ap.parse_args()

    repos = args.repo or list(REPOS)
    repos_root = Path(args.repos_root)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    results: dict = {
        "started": started,
        "k": K,
        "cpu_count": os.cpu_count(),
        "repos": repos,
        "ingest": [],
        "scores": [],
        "isolation": None,
    }
    # Merge into an existing --out (supports running repos one at a time
    # against the same db without losing earlier phases' numbers).
    out_path = Path(args.out)
    if out_path.exists():
        prior = json.loads(out_path.read_text(encoding="utf-8"))
        results["ingest"] = prior.get("ingest", [])
        results["scores"] = prior.get("scores", [])
        results["isolation"] = prior.get("isolation")

    def flush() -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    if not args.skip_ingest:
        for repo in repos:
            results["ingest"] = [e for e in results["ingest"] if e["repo"] != repo]
            results["ingest"].append(ingest_repo(args.db, repo, repos_root))
            flush()
    for repo in repos:
        results["scores"] = [e for e in results["scores"] if e["repo"] != repo]
        results["scores"].append(score_repo(args.db, repo))
        flush()
    if not args.skip_isolation:
        expected = {e["repo"]: e["count_after_own_ingest"]
                    for e in results["ingest"]}
        results["isolation"] = isolation_checks(args.db, repos, expected)
        flush()
    print(f"[done] results -> {out_path}")


if __name__ == "__main__":
    main()
