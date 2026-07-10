"""Ingest hygiene filters (ADR-0027): one filtering system for issues #27/#28/#29.

The load-bearing properties under test:

- #27 — a checked-in virtualenv never drowns the store: ``env`` and
  ``site-packages`` are pruned by NAME, and any directory containing a
  ``pyvenv.cfg`` is pruned by STRUCTURE, whatever it is called.
- #28 — machine-generated lockfiles are filtered from the walk by default.
- #29 — well-known secrets files are skipped by default with ONE warning that
  names them; an explicit override (direct file path, or a ``skip_files``
  override) CAN ingest them, but never silently.

Filtering applies to the directory WALK only: pointing ``ingest_path`` straight
at a single file is explicit intent and is never blocked.
"""

from __future__ import annotations

import warnings

import pytest

from rekoll import Memory
from rekoll.embedding import StubEmbedder
from rekoll.retrieval import hybrid_search

SECRET_WORDS = "credentials or private keys"  # both warning flavors carry this


def _mem(**kwargs) -> Memory:
    return Memory(path=":memory:", embedder=StubEmbedder(), reranker=None, **kwargs)


def _stored_texts(mem: Memory, query: str) -> str:
    """All matching stored content INCLUDING quarantined (recall hides those)."""
    result = hybrid_search(
        mem.adapter, scope=mem.scope, query=query, embedder=mem.embedder,
        k=50, include_quarantined=True,
    )
    return " ".join(h.record.content for h in result.hits)


def _repo(tmp_path):
    """Two legit files — every test's 'the real project still gets ingested'."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("VALUE = 'alpha-marker'\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text(
        "# Guide\n\nbeta-marker prose here.\n", encoding="utf-8"
    )
    return repo


def _secret_warnings(record) -> list:
    return [w for w in record if SECRET_WORDS in str(w.message)]


# ---- issue #27: virtualenvs and vendored trees ------------------------------

def test_default_skip_dirs_covers_env_and_site_packages():
    from rekoll.memory import DEFAULT_SKIP_DIRS

    assert "env" in DEFAULT_SKIP_DIRS
    assert "site-packages" in DEFAULT_SKIP_DIRS


def test_env_dir_is_skipped_by_name(tmp_path):
    repo = _repo(tmp_path)
    lib = repo / "env" / "lib"
    lib.mkdir(parents=True)  # deliberately NO pyvenv.cfg: isolates the name rule
    (lib / "dep.py").write_text("envdep_marker = 1\n", encoding="utf-8")

    mem = _mem()
    stats = mem.ingest_path(str(repo))
    assert stats["files"] == 2
    assert "envdep_marker" not in _stored_texts(mem, "envdep_marker")
    assert "alpha-marker" in _stored_texts(mem, "alpha-marker")
    mem.close()


def test_site_packages_dir_is_skipped_by_name(tmp_path):
    repo = _repo(tmp_path)
    pkg = repo / "vendored" / "site-packages" / "somelib"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("sitepkg_marker = 1\n", encoding="utf-8")

    mem = _mem()
    stats = mem.ingest_path(str(repo))
    assert stats["files"] == 2
    assert "sitepkg_marker" not in _stored_texts(mem, "sitepkg_marker")
    mem.close()


def test_pyvenv_cfg_marks_a_virtualenv_regardless_of_name(tmp_path):
    # The durable fix vs. name whack-a-mole: `python -m venv anything` drops
    # pyvenv.cfg at its root. A dir named "myvenv" is on NO name list — only
    # the structural detection can prune it.
    repo = _repo(tmp_path)
    venv = repo / "myvenv"
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv / "lib" / "dep.py").write_text("venvdep_marker = 1\n", encoding="utf-8")
    # Control: same shape WITHOUT pyvenv.cfg must still be walked — proves the
    # marker file is what does the pruning.
    tools = repo / "mytools"
    tools.mkdir()
    (tools / "tool.py").write_text("tooldep_marker = 1\n", encoding="utf-8")

    mem = _mem()
    stats = mem.ingest_path(str(repo))
    assert stats["files"] == 3  # app.py + guide.md + mytools/tool.py
    assert "venvdep_marker" not in _stored_texts(mem, "venvdep_marker")
    assert "tooldep_marker" in _stored_texts(mem, "tooldep_marker")
    mem.close()


# ---- issue #28: machine-generated lockfiles ---------------------------------

def test_lockfiles_filtered_by_default(tmp_path):
    repo = _repo(tmp_path)
    (repo / "package-lock.json").write_text(
        '{"name": "site", "lockfileVersion": 3, "packages": '
        '{"node_modules/lockfile-marker-npm": {"version": "1.0.0"}}}\n',
        encoding="utf-8",
    )
    (repo / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\npackages:\n  lockfile-marker-pnpm@1.0.0: {}\n",
        encoding="utf-8",
    )

    mem = _mem()
    stats = mem.ingest_path(str(repo))
    assert stats["files"] == 2
    assert stats["filtered"] == 2
    assert "lockfile-marker-npm" not in _stored_texts(mem, "lockfile-marker-npm")
    assert "lockfile-marker-pnpm" not in _stored_texts(mem, "lockfile-marker-pnpm")
    mem.close()


def test_lock_suffix_files_filtered_under_broadened_include_ext(tmp_path):
    # yarn.lock / Cargo.lock (.lock suffix) are not walk candidates under the
    # default include_ext; the filename filter must still hold when a caller
    # broadens the extensions.
    repo = _repo(tmp_path)
    (repo / "yarn.lock").write_text(
        'lockfile-marker-yarn@^1.0.0:\n  version "1.0.0"\n', encoding="utf-8"
    )
    (repo / "Cargo.lock").write_text(
        '[[package]]\nname = "lockfile-marker-cargo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    mem = _mem()
    stats = mem.ingest_path(str(repo), include_ext={".py", ".lock"})
    assert stats["files"] == 1  # app.py only (guide.md is outside include_ext now)
    assert stats["filtered"] == 2
    assert "lockfile-marker-yarn" not in _stored_texts(mem, "lockfile-marker-yarn")
    assert "lockfile-marker-cargo" not in _stored_texts(mem, "lockfile-marker-cargo")
    mem.close()


# ---- issue #29: secrets — skip + warn, override possible, never silent ------

def _plant_secrets(repo):
    (repo / "credentials.json").write_text(
        '{"installed": {"client_id": "oauth-client-marker.apps.example.com", '
        '"client_secret": "gocspx-oauth-secret-marker"}}\n',
        encoding="utf-8",
    )
    (repo / "service-account-key.json").write_text(
        '{"type": "service_account", "private_key_id": "svcacct-secret-marker"}\n',
        encoding="utf-8",
    )


def test_secrets_skipped_with_single_warning_naming_them(tmp_path):
    repo = _repo(tmp_path)
    _plant_secrets(repo)

    mem = _mem()
    with pytest.warns(UserWarning) as caught:
        stats = mem.ingest_path(str(repo))
    hits = _secret_warnings(caught)
    assert len(hits) == 1, "exactly ONE secrets warning per ingest_path call"
    message = str(hits[0].message)
    assert "credentials.json" in message
    assert "service-account-key.json" in message

    assert stats["files"] == 2
    assert stats["filtered"] == 2
    texts = _stored_texts(mem, "oauth-secret-marker svcacct-secret-marker")
    assert "oauth-secret-marker" not in texts
    assert "svcacct-secret-marker" not in texts
    assert "alpha-marker" in _stored_texts(mem, "alpha-marker")
    mem.close()


def test_no_secret_warning_on_a_clean_tree(tmp_path):
    repo = _repo(tmp_path)
    mem = _mem()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mem.ingest_path(str(repo))
    assert not _secret_warnings(caught)
    mem.close()


def test_direct_file_path_bypasses_filter_but_warns_stored(tmp_path):
    # Pointing ingest_path straight at ONE file is explicit intent: the
    # filename filter never blocks it — but storing a secret-named file is
    # still announced, never silent.
    repo = _repo(tmp_path)
    _plant_secrets(repo)

    mem = _mem()
    with pytest.warns(UserWarning) as caught:
        stats = mem.ingest_path(str(repo / "credentials.json"))
    hits = _secret_warnings(caught)
    assert len(hits) == 1
    message = str(hits[0].message)
    assert "STORED" in message and "credentials.json" in message

    assert stats["files"] == 1
    assert stats["filtered"] == 0
    assert "oauth-secret-marker" in _stored_texts(mem, "oauth-secret-marker")
    mem.close()


def test_skip_files_empty_set_disables_filtering_and_still_warns(tmp_path):
    # skip_files=set() means "no filename filtering" — it must NOT fall back
    # to the defaults the way a falsy check would. Secrets ingested through
    # the override still get the STORED warning (never silent, issue #29).
    repo = _repo(tmp_path)
    _plant_secrets(repo)
    (repo / "package-lock.json").write_text(
        '{"packages": {"node_modules/lockfile-marker-npm": {}}}\n', encoding="utf-8"
    )

    mem = _mem()
    with pytest.warns(UserWarning) as caught:
        stats = mem.ingest_path(str(repo), skip_files=set())
    hits = _secret_warnings(caught)
    assert len(hits) == 1
    message = str(hits[0].message)
    assert "STORED" in message
    assert "credentials.json" in message and "service-account-key.json" in message
    assert "package-lock.json" not in message  # a lockfile is not a secret

    assert stats["files"] == 5  # 2 legit + 2 secrets + 1 lockfile
    assert stats["filtered"] == 0
    assert "lockfile-marker-npm" in _stored_texts(mem, "lockfile-marker-npm")
    assert "oauth-secret-marker" in _stored_texts(mem, "oauth-secret-marker")
    mem.close()


def test_skip_files_custom_set_replaces_the_defaults(tmp_path):
    repo = _repo(tmp_path)
    (repo / "package-lock.json").write_text(
        '{"packages": {"node_modules/lockfile-marker-npm": {}}}\n', encoding="utf-8"
    )

    mem = _mem()
    stats = mem.ingest_path(str(repo), skip_files={"*.md"})
    assert stats["files"] == 2  # app.py + package-lock.json (defaults replaced)
    assert stats["filtered"] == 1  # guide.md
    assert "beta-marker" not in _stored_texts(mem, "beta-marker")
    assert "lockfile-marker-npm" in _stored_texts(mem, "lockfile-marker-npm")
    mem.close()


def test_suffixless_secrets_filtered_under_broadened_include_ext(tmp_path):
    # Under the DEFAULT include_ext, id_rsa / .env / token.pickle / *.pem /
    # *.key never even reach the walk (wrong or no suffix) — so this guard
    # only becomes observable when a caller broadens include_ext. It must
    # hold there: the skip is by NAME, not by the narrowness of the defaults.
    repo = _repo(tmp_path)
    for name, content in [
        ("id_rsa", "-----BEGIN OPENSSH PRIVATE KEY----- sshkey-marker"),
        (".env", "API_TOKEN=dotenv-secret-marker"),
        ("token.pickle", "pickle-token-marker"),
        ("server.pem", "-----BEGIN CERTIFICATE----- pem-marker"),
        ("private.key", "keyfile-marker"),
    ]:
        (repo / name).write_text(content + "\n", encoding="utf-8")
    # Control: a suffix-less NON-secret file proves filtering is by name, not
    # a side effect of the broadened extension set.
    (repo / "NOTICE").write_text("notice-marker legal text.\n", encoding="utf-8")

    mem = _mem()
    with pytest.warns(UserWarning) as caught:
        stats = mem.ingest_path(
            str(repo), include_ext={".py", "", ".pickle", ".pem", ".key"}
        )
    hits = _secret_warnings(caught)
    assert len(hits) == 1
    message = str(hits[0].message)
    for name in ("id_rsa", ".env", "token.pickle", "server.pem", "private.key"):
        assert name in message

    assert stats["filtered"] == 5
    assert stats["files"] == 2  # app.py + NOTICE
    texts = _stored_texts(
        mem, "sshkey-marker dotenv-secret-marker pickle-token-marker pem-marker keyfile-marker"
    )
    for marker in (
        "sshkey-marker", "dotenv-secret-marker", "pickle-token-marker",
        "pem-marker", "keyfile-marker",
    ):
        assert marker not in texts
    assert "notice-marker" in _stored_texts(mem, "notice-marker")
    mem.close()


# ---- the returned counts stay coherent --------------------------------------

def test_returned_counts_are_coherent(tmp_path):
    repo = _repo(tmp_path)
    _plant_secrets(repo)
    (repo / "package-lock.json").write_text(
        '{"packages": {"node_modules/lockfile-marker-npm": {}}}\n', encoding="utf-8"
    )
    venv = repo / "env"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv / "dep.py").write_text("envdep_marker = 1\n", encoding="utf-8")

    mem = _mem()
    with pytest.warns(UserWarning):
        stats = mem.ingest_path(str(repo))
    assert set(stats) == {"files", "chunks", "skipped", "filtered", "total"}
    assert stats["files"] == 2  # the two legit files
    assert stats["chunks"] >= 2
    assert stats["skipped"] == 0  # nothing errored — filtering is not 'skipped'
    assert stats["filtered"] == 3  # lockfile + two secrets (walk candidates only:
    # env/ was pruned at directory level and never became a candidate)
    assert stats["total"] == mem.count()
    mem.close()
