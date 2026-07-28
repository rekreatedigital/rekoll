"""``rekoll init`` writes the MCP client's config with this project's scope
PINNED — one repo, one memory (issue #83, ADR-0047).

The bug these tests close: a hand-written ``.mcp.json`` carrying ``"args": []``
lets the MCP server derive its project from the launch FOLDER'S NAME, while the
CLI and SDK default to ``project="default"``. Same store file, two scopes,
invisible to each other, no error. Since v0.1.3 that split is *detected*
(ADR-0040); here it stops happening.

Two properties are load-bearing throughout and each has its own test:

* nothing rekoll writes may make an existing user's memories invisible, and
* whatever init writes must pass ``rekoll doctor`` on the same machine.
"""

from __future__ import annotations

import codecs
import json
import sys
from pathlib import Path

import pytest

from rekoll.cli import main
from rekoll.embedding import StubEmbedder

CONFIG = ".mcp.json"
DB = "./.rekoll/memory.db"


@pytest.fixture(autouse=True)
def _pin_stub_embedder_and_no_reranker(monkeypatch):
    """Deterministic and offline even on machines WITH the 'embeddings' extra."""
    monkeypatch.setattr("rekoll.memory._auto_embedder", lambda: StubEmbedder())
    monkeypatch.setattr("rekoll.memory._auto_reranker", lambda: None)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """An empty project directory that is also the cwd (the CLI's default world)."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def mcp_installed(monkeypatch):
    """Pin the 'is an MCP server installed?' gate ON.

    init only writes a client config on a machine that HAS the mcp extra (see
    ``test_init_writes_no_config_without_the_mcp_extra`` for why). CI's core
    matrix installs ``[dev]`` only, so every test that expects a file must pin
    this rather than inherit whatever the runner happens to have — the same
    reason the init tests pin ``_semantic_extra_installed``.
    """
    monkeypatch.setattr("rekoll.cli._mcp_sdk_state", lambda: (True, "1.3.0"))


class _TtyNeverRead:
    """An interactive stdin whose every read is a test failure."""

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:  # pragma: no cover - reaching this IS the failure
        raise AssertionError("plain init must never read stdin")


def _entry(cwd: Path, name: str = CONFIG) -> dict:
    blob = json.loads((cwd / name).read_text(encoding="utf-8"))
    return blob["mcpServers"]["rekoll"]


def _pinned_flags(entry: dict) -> list[str]:
    """The registration's args with any ``-m <module>`` prefix removed, i.e.
    exactly what ``mcp_server.load_config`` would be handed."""
    args = [str(a) for a in entry["args"]]
    return args[2:] if args[:1] == ["-m"] else args


def _mcp_scope(argv: list[str]) -> str:
    """The scope the MCP server would ACTUALLY use for these args."""
    from rekoll.mcp_server import load_config

    cfg = load_config(argv, environ={})
    return f"{cfg.tenant}/{cfg.project}/{cfg.agent}"


# -- the discriminating tests ------------------------------------------------

def test_init_writes_an_mcp_config_pinning_the_cli_scope(project, capsys):
    """THE discriminating test: on main, plain `rekoll init` writes no
    `.mcp.json` at all, so an agent configured from the quickstart's
    `"args": []` lands in a folder-named scope the CLI cannot see."""
    assert main(["init"]) == 0
    assert (project / CONFIG).is_file()               # RED on main: no such file
    entry = _entry(project)
    assert _pinned_flags(entry) == [
        "--tenant", "default", "--project", "default", "--agent", "default",
    ]
    out = capsys.readouterr().out
    assert "created .mcp.json" in out
    assert "--tenant default --project default --agent default" in out


def test_the_generated_config_closes_the_scope_split(project):
    """The behavioural claim, asked of the MCP server's own config loader rather
    than of a string: the generated args resolve to the SAME scope the CLI and
    SDK default to - and the quickstart's bare `"args": []` still does not,
    which is what makes this test discriminate rather than tautologise."""
    from rekoll.model import Scope

    assert main(["init"]) == 0
    pinned = _pinned_flags(_entry(project))
    cli_default = Scope().key()

    assert _mcp_scope(pinned) == cli_default             # the fix
    assert _mcp_scope([]) != cli_default                 # the bug, still there
    assert _mcp_scope([]) == f"default/{project.name}/default"   # ...and it is the folder name


def test_an_agent_at_the_pinned_scope_is_read_by_the_bare_cli(project, capsys):
    """End-to-end, one store file: a write at the scope the generated config
    pins is found by a BARE `rekoll recall` with no flags. This is the field
    report's exact failing sequence (#82/#101), now passing."""
    from rekoll.memory import Memory

    assert main(["init"]) == 0
    tenant, proj, agent = _mcp_scope(_pinned_flags(_entry(project))).split("/")
    mem = Memory(path=DB, tenant=tenant, project=proj, agent=agent)
    try:
        mem.remember("we chose Postgres over BigQuery for cost")
    finally:
        mem.close()
    capsys.readouterr()
    assert main(["recall", "why postgres?"]) == 0      # RED on main: exit 1, no matches
    assert "Postgres" in capsys.readouterr().out


def test_init_respects_explicit_scope_flags(project):
    """The pinned scope is the scope init itself operated in - so `--project
    alpha` is what lands in the file, and the MCP door joins THAT memory."""
    assert main(["init", "--project", "alpha", "--agent", "bot"]) == 0
    pinned = _pinned_flags(_entry(project))
    assert _mcp_scope(pinned) == "default/alpha/bot"


# -- never clobber ----------------------------------------------------------

def test_init_never_clobbers_an_existing_mcp_config(project, capsys):
    """A hand-written config is the user's file. Byte-identical afterwards, and
    init says what the args SHOULD pin rather than editing them: merging into a
    client config whose schema we do not own is how you break someone's agent."""
    existing = '{ "mcpServers": { "rekoll": { "command": "rekoll-mcp", "args": [] } } }'
    (project / CONFIG).write_text(existing, encoding="utf-8")
    assert main(["init"]) == 0
    assert (project / CONFIG).read_text(encoding="utf-8") == existing
    out = capsys.readouterr().out
    assert "already exists - left untouched" in out
    assert "--tenant default --project default --agent default" in out


def test_init_is_idempotent_about_the_config(project, capsys):
    """Re-running init is advertised as safe. The second run must report the
    file it wrote itself as pre-existing, not rewrite it."""
    assert main(["init"]) == 0
    before = (project / CONFIG).read_bytes()
    capsys.readouterr()
    assert main(["init"]) == 0
    assert (project / CONFIG).read_bytes() == before
    assert "already exists - left untouched" in capsys.readouterr().out


def test_a_project_already_registered_through_cursor_gets_no_second_config(project, capsys):
    """`.cursor/mcp.json` already registers rekoll. Adding `.mcp.json` with a
    different scope would CREATE the split this lane closes, so init writes
    nothing and prints what to pin in the config that already exists."""
    cursor = project / ".cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(
        '{ "mcpServers": { "rekoll": { "command": "rekoll-mcp" } } }', encoding="utf-8"
    )
    assert main(["init"]) == 0
    assert not (project / CONFIG).exists()
    out = capsys.readouterr().out
    assert ".cursor/mcp.json already registers rekoll" in out
    assert "--tenant default --project default --agent default" in out


def test_an_unrelated_editor_config_does_not_block_the_write(project):
    """The block above is about a rekoll registration, not about the file
    existing: a `.vscode/mcp.json` for somebody else's server is none of our
    business and must not suppress the fix."""
    vscode = project / ".vscode"
    vscode.mkdir()
    (vscode / "mcp.json").write_text(
        '{ "mcpServers": { "postgres": { "command": "pg-mcp" } } }', encoding="utf-8"
    )
    assert main(["init"]) == 0
    assert (project / CONFIG).is_file()


# -- no existing user's memories may become invisible ------------------------

def test_init_writes_nothing_when_the_store_holds_memories_elsewhere(project, capsys):
    """The hard constraint. This store's memories live under `default/other/
    default`; pinning `default/default/default` here would point the MCP door
    at an empty scope and hide them - this very bug, inflicted deliberately.
    Nothing is written, nothing is moved, nothing is guessed."""
    assert main(["remember", "the deploy runs on a VPS", "--project", "other"]) == 0
    capsys.readouterr()
    assert main(["init"]) == 0
    assert not (project / CONFIG).exists()
    out = capsys.readouterr().out
    assert ".mcp.json not created" in out
    assert "under 1 other scope(s)" in out
    assert "rekoll status" in out                 # the ADR-0040 note names them


def test_init_writes_when_the_store_already_holds_memories_in_this_scope(project):
    """The counterpart, and the common upgrade path: a CLI-only user who now
    wants an agent. Their memories are already in the scope being pinned, so
    pinning it hides nothing - and the agent joins the memory they have."""
    assert main(["remember", "we chose Postgres over BigQuery for cost"]) == 0
    assert main(["init"]) == 0
    assert (project / CONFIG).is_file()
    assert _pinned_flags(_entry(project))[3] == "default"


def test_a_custom_store_path_writes_no_config(project, capsys):
    """The generator emits scope NAMES and never a store path (ADR-0047 §3), so
    it cannot describe a store outside the standard ./.rekoll layout - the same
    line `_ensure_gitignore` draws for the same layout - and declines rather
    than describing it wrongly."""
    assert main(["init", "--path", "elsewhere/mem.db"]) == 0
    assert not (project / CONFIG).exists()
    assert "custom store path" in capsys.readouterr().out


def test_the_generated_config_never_pins_a_store_path(project):
    """The threat-model pin: `--path` is the flag ADR-0035 §6 keeps as
    operator-only input, because a file in a cloned repo that redirects the
    store is the hostile-clone attack. A generated config carries names only."""
    assert main(["init"]) == 0
    entry = _entry(project)
    assert "--path" not in [str(a) for a in entry["args"]]
    assert not any(str(a).startswith("--path") for a in entry["args"])


# -- the opt-outs and the CLI-only user -------------------------------------

def test_init_writes_no_config_without_the_mcp_extra(project, capsys, monkeypatch):
    """A CLI-only machine has not opened the MCP door. Writing a registration
    there would hand it a config for a server it cannot run AND make `rekoll
    doctor` start warning about that door - the nagging ADR-0041 §2 refuses to
    do. It says how to get one instead."""
    monkeypatch.setattr("rekoll.cli._mcp_sdk_state", lambda: (False, None))
    assert main(["init"]) == 0
    assert not (project / CONFIG).exists()
    out = capsys.readouterr().out
    assert "no MCP server installed" in out
    assert 'rekoll[mcp]' in out and "again" in out


def test_a_generated_config_plus_cli_only_use_does_warn_and_that_is_intended(project, capsys):
    """The accepted interaction with ADR-0041 §2, pinned deliberately so it is a
    decision and not a discovery.

    Someone who installed the `mcp` extra and then never connected an agent IS
    the population that check was built for - the 12-hour silent failure in
    field report #82 - so `doctor` telling them nothing has come through the
    door is correct, and its own wording already carries the escape hatch. A
    machine WITHOUT the extra gets no config and no line at all, which is what
    keeps genuinely CLI-only users unbothered (see the test above)."""
    from rekoll.cli import _check_mcp

    assert main(["init"]) == 0
    assert main(["remember", "we chose Postgres over BigQuery for cost"]) == 0
    level, text = _check_mcp(object())
    assert level == "WARN"
    assert "nothing has EVER been written through the MCP door" in text
    assert "Harmless if you only use the CLI" in text
    capsys.readouterr()
    assert main(["doctor"]) == 0          # a WARN, never a FAIL: nothing is broken


def test_no_mcp_config_flag_writes_nothing(project, capsys):
    assert main(["init", "--no-mcp-config"]) == 0
    assert not (project / CONFIG).exists()
    assert "--no-mcp-config given" in capsys.readouterr().out


def test_memory_path_init_writes_no_config(project):
    """`--path :memory:` sets nothing up at all; it must not leave a config
    pointing at a store that vanishes when the command exits."""
    assert main(["init", "--path", ":memory:"]) == 0
    assert not (project / CONFIG).exists()


# -- the shape must be one doctor accepts -----------------------------------

def test_the_generated_config_passes_doctor(project, capsys):
    """Non-negotiable: `rekoll doctor` must not report a file rekoll itself
    wrote as broken. docs/MCP.md documents two command shapes and only one is
    right per install, so this is the test that catches picking wrong."""
    from rekoll.cli import _check_mcp, _mcp_entry_command_resolves

    assert main(["init"]) == 0
    ok, detail = _mcp_entry_command_resolves(_entry(project))
    assert ok, detail
    level, text = _check_mcp(object())
    assert level == "ok", text
    capsys.readouterr()
    assert main(["doctor"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_a_project_virtualenv_gets_a_relative_module_command(project, monkeypatch):
    """In a project venv a bare `rekoll-mcp` is not on the client's PATH, and
    the console-script shim embeds the absolute path of the environment that
    created it - so a folder rename kills it silently (twice, in the field).
    The module form with a RELATIVE interpreter survives the rename, because
    MCP clients launch the server with the project as its cwd.

    The shim is planted here and must still be ignored: `shutil.which` would
    find it inside an active venv, and preferring it is exactly the mistake."""
    bindir = project / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bindir.mkdir(parents=True)
    interpreter = bindir / ("python.exe" if sys.platform == "win32" else "python")
    interpreter.write_bytes(b"")
    shim = bindir / "rekoll-mcp"
    shim.write_bytes(b"")
    monkeypatch.setattr(sys, "prefix", str(project / ".venv"))
    monkeypatch.setattr(sys, "base_prefix", str(project / ".venv" / "base"))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr("rekoll.cli.shutil.which", lambda _name: str(shim))

    assert main(["init"]) == 0
    entry = _entry(project)
    assert entry["command"] == f".venv/{bindir.name}/{interpreter.name}"
    assert "\\" not in entry["command"]                  # one portable spelling
    assert entry["args"][:2] == ["-m", "rekoll.mcp_server"]
    assert "rekoll-mcp" != entry["command"]


def test_a_global_install_gets_the_bare_command(project, monkeypatch):
    """pipx / global pip: the client resolves `rekoll-mcp` on PATH, which is
    the shape docs/MCP.md and the README lead with."""
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    monkeypatch.setattr("rekoll.cli.shutil.which", lambda _name: "/usr/local/bin/rekoll-mcp")
    assert main(["init"]) == 0
    entry = _entry(project)
    assert entry["command"] == "rekoll-mcp"
    assert entry["args"][0] == "--tenant"      # no `-m` prefix on this shape


def test_a_venv_outside_the_project_is_named_by_full_path_and_says_so(project, capsys, monkeypatch):
    """A venv that is not inside the project cannot be named relatively, and
    must not be named by its shim either. The absolute path is rename-fragile,
    so init says that out loud instead of shipping a quiet time bomb."""
    monkeypatch.setattr(sys, "prefix", "/opt/envs/x")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", sys.executable)
    assert main(["init"]) == 0
    assert Path(_entry(project)["command"]).is_absolute()
    assert "names this Python by full path" in capsys.readouterr().out


def test_the_config_is_utf8_without_a_bom(project):
    """`_find_mcp_registrations` tolerates a UTF-8 BOM but reports UTF-16 as
    invalid JSON, and an unreadable config starts no server at all. Pin the
    encoding rather than trusting the platform default."""
    assert main(["init"]) == 0
    data = (project / CONFIG).read_bytes()
    assert not data.startswith(codecs.BOM_UTF8)
    assert not data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    assert json.loads(data.decode("utf-8"))          # valid JSON, decodable as UTF-8
    assert data.endswith(b"\n")


# -- rekoll's own output discipline -----------------------------------------

def test_plain_init_still_asks_nothing_and_keeps_stderr_empty(project, capsys, monkeypatch):
    """ADR-0036 promises plain `rekoll init` is PROMPT-free, not output-free -
    it already appends to `.gitignore` and creates the store. This lane adds a
    stdout line, so pin the promise it must not break: stdin is never read and
    stderr stays exactly empty, even in a real terminal."""
    monkeypatch.setattr(sys, "stdin", _TtyNeverRead())
    assert main(["init"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "wizard" not in captured.out.lower()
    assert "interview" not in captured.out.lower()


def test_the_new_line_is_pinned_byte_for_byte(project, capsys):
    """The byte pin the wizard suite's own stdout assertions do NOT provide
    (`tests/test_cli.py` pins stderr exactly, stdout only by substring). If a
    later change reshapes this line, this test - not a silent drift - is what
    reports it."""
    assert main(["init"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert ("  created .mcp.json  (your AI agent's MCP config - scope pinned: "
            "--tenant default --project default --agent default)") in lines


def test_a_scope_name_a_command_line_cannot_carry_is_never_typeset(project, capsys):
    """`Scope` accepts more than a command line can carry unquoted, and these
    three values are printed for a human to RETYPE. A part with a space is
    described, never typeset as a flag string - while the file itself still
    pins it correctly, because JSON needs no shell quoting."""
    assert main(["init", "--project", "two words"]) == 0
    out = capsys.readouterr().out
    assert "--project two words" not in out
    assert "this project's tenant/project/agent" in out
    assert _mcp_scope(_pinned_flags(_entry(project))) == "default/two words/default"


# -- the tripwire the wizard suite needs ------------------------------------

def test_the_cli_sdk_and_model_scope_defaults_are_still_one_scope(project):
    """A tripwire, not a feature test (issue #83's false-green trap).

    Twelve wizard tests in `tests/test_cli.py` assert against a scope they
    never name: six read `adapter.count(scope=Scope())` - the MODEL default -
    and six read directives through `Memory(path=DB)` - the SDK default -
    while the command under test writes at the CLI's argparse default. They are
    honest only while all three are the SAME scope.

    ADR-0047 deliberately moved none of them: unifying the defaults at the
    model layer is the most natural reading of "one repo, one memory" and it is
    the change that would turn all twelve GREEN while the scope they assert
    against moved underneath them - certifying a change that could make an
    existing user's memories invisible. This test goes RED instead."""
    from rekoll.cli import _build_parser
    from rekoll.model import Scope

    args = _build_parser().parse_args(["init"])
    assert (args.tenant, args.project, args.agent) == ("default", "default", "default")
    assert Scope().key() == "default/default/default"
    assert Scope().key() == f"{args.tenant}/{args.project}/{args.agent}"

    # ...and the CLI really does write where `Scope()` reads, which is the
    # assumption the six `_store_is_empty()` tests rest on.
    from rekoll.adapters.registry import get_adapter

    assert main(["init"]) == 0
    assert main(["remember", "we chose Postgres over BigQuery for cost"]) == 0
    adapter = get_adapter("sqlite", path=DB)
    try:
        assert adapter.count(scope=Scope()) == 1
    finally:
        adapter.close()
