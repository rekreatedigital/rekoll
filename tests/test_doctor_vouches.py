"""``rekoll doctor`` must not report health it cannot vouch for (#104, #84).

Three field incidents, one disease:

* **#104** - a stale 0.1.1 sat earlier on PATH than a freshly installed 0.1.3,
  so a careful tester filed two bug reports against code they were not running.
  Both "bugs" were already fixed in the version they believed they had. doctor
  printed the true version all along, as ``ok``, and said nothing about the
  other copies.
* **#84 / #82** - a valid ``.mcp.json`` sat in a repo whose MCP client had
  never loaded it, and a twelve-hour agent session ran with no rekoll tools
  while doctor printed nine green checks and never mentioned MCP.
* **#84 (second incident)** - a folder rename left ``.mcp.json`` pointing at a
  console-script shim that no longer existed. Every session lost its rekoll
  tools. doctor stayed green.

These tests are hermetic: PATH, PATHEXT and the working directory are all
controlled, so they assert the CHECK rather than whatever this machine happens
to have installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rekoll.cli import (
    _check_install,
    _check_mcp,
    _find_mcp_registrations,
    _version_of_install,
    main,
)
from rekoll.embedding import StubEmbedder

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


# -- #104: install identity ---------------------------------------------------

def _fake_install(root: Path, version, *, editable: bool = False) -> Path:
    """A directory laid out like a real environment holding rekoll, returning
    its scripts dir. Windows layout, because that is what the incident ran on;
    ``test_a_posix_layout_is_understood`` covers the other one."""
    scripts = root / "Scripts"
    site = root / "Lib" / "site-packages"
    scripts.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    for stem in ("rekoll", "rekoll-mcp"):
        exe = scripts / f"{stem}.exe"
        exe.write_bytes(b"not a real launcher")
        exe.chmod(0o755)
    if editable:
        # setuptools' src-layout editable install: a .pth naming the source
        # tree, and no `rekoll/` package inside site-packages at all.
        source = root / "src"
        (source / "rekoll").mkdir(parents=True, exist_ok=True)
        (source / "rekoll" / "_version.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
        (site / "__editable__.rekoll-0.0.0.pth").write_text(str(source), encoding="utf-8")
        (site / "rekoll-0.0.0.dist-info").mkdir(exist_ok=True)
    elif version is not None:
        (site / "rekoll").mkdir(exist_ok=True)
        (site / "rekoll" / "_version.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
    return scripts


def _only_on_path(monkeypatch, *script_dirs: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in script_dirs))
    monkeypatch.setenv("PATHEXT", ".EXE")


def test_a_stale_shadowing_install_is_a_WARN(tmp_path, monkeypatch):
    """THE #104 failure: another version answers when you type 'rekoll'."""
    stale = _fake_install(tmp_path / "stale", "0.1.1")
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, stale)
    level, detail = _check_install()
    assert level == "WARN"
    assert "0.1.1" in detail and "0.1.3" in detail   # BOTH versions named
    assert str(stale / "rekoll.exe") in detail       # and the offending path
    assert "uninstall" in detail.lower()             # and how to fix it


def test_matching_versions_stay_quiet(tmp_path, monkeypatch):
    same = _fake_install(tmp_path / "same", "0.1.3")
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, same)
    level, detail = _check_install()
    assert level == "ok"
    assert "0.1.3" in detail


def test_doctor_says_which_copy_it_is_speaking_for(tmp_path, monkeypatch):
    """#104's third criterion: a diagnostic that cannot vouch for its own
    identity is the whole failure mode."""
    same = _fake_install(tmp_path / "same", "0.1.3")
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, same)
    _level, detail = _check_install()
    assert "0.1.3 at " in detail
    assert "rekoll" in detail


def test_an_editable_checkout_reports_its_source_version(tmp_path, monkeypatch):
    """An editable install RUNS its source tree, so the source is the honest
    answer. Reading the dist-info instead would report the install-time version
    (0.0.0 here) and cry wolf at every developer on every run."""
    editable = _fake_install(tmp_path / "dev", "0.1.3", editable=True)
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, editable)
    level, detail = _check_install()
    assert level == "ok"
    assert "0.0.0" not in detail  # the stale metadata is never quoted as truth


def test_a_bumped_editable_checkout_still_disagrees_loudly(tmp_path, monkeypatch):
    editable = _fake_install(tmp_path / "dev", "0.2.0", editable=True)
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, editable)
    level, detail = _check_install()
    assert level == "WARN"
    assert "0.2.0" in detail
    assert "editable checkout" in detail  # says WHY the two differ


def test_unreadable_copies_are_never_claimed_to_agree(tmp_path, monkeypatch):
    """The honesty rule this lane exists for: when doctor could not read
    another copy's version, it must not imply it verified one."""
    mystery = _fake_install(tmp_path / "mystery", None)
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, mystery)
    level, detail = _check_install()
    assert level == "ok"
    assert "could not be read" in detail
    assert "all 0.1.3" not in detail


def test_no_rekoll_on_path_says_so(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, empty)
    level, detail = _check_install()
    assert level == "ok"
    assert "no 'rekoll' command on PATH" in detail


def test_a_posix_layout_is_understood(tmp_path):
    env = tmp_path / "posixenv"
    (env / "bin").mkdir(parents=True)
    site = env / "lib" / "python3.12" / "site-packages" / "rekoll"
    site.mkdir(parents=True)
    (site / "_version.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    exe = env / "bin" / "rekoll"
    exe.write_bytes(b"stub")
    exe.chmod(0o755)
    assert _version_of_install(exe) == ("0.2.0", False)


def test_a_pipx_style_shim_is_read_through_its_shebang(tmp_path):
    """pipx (what QUICKSTART recommends, and what the #104 incident used) puts
    the command in ~/.local/bin while the package lives in a venv elsewhere, so
    the ordinary <env>/Scripts -> <env>/Lib walk finds nothing. The launcher's
    embedded '#!' line is read as BYTES to bridge that."""
    venv = tmp_path / "pipx" / "venvs" / "rekoll"
    (venv / "Scripts").mkdir(parents=True)
    site = venv / "Lib" / "site-packages" / "rekoll"
    site.mkdir(parents=True)
    (site / "_version.py").write_text('__version__ = "0.1.1"\n', encoding="utf-8")
    interpreter = venv / "Scripts" / "python.exe"
    interpreter.write_bytes(b"pretend interpreter")

    shim = tmp_path / "bin" / "rekoll.exe"
    shim.parent.mkdir(parents=True)
    # The real layout: launcher stub, then the shebang, then the zip payload.
    shim.write_bytes(
        b"MZ" + b"\x00" * 64
        + b'#!"' + str(interpreter).encode() + b'"\r\n'
        + b"PK\x03\x04" + b"\x00" * 16
    )
    shim.chmod(0o755)
    assert _version_of_install(shim) == ("0.1.1", False)


def test_a_posix_script_shim_is_read_through_its_shebang(tmp_path):
    """The POSIX half of the pipx case: a text console script whose '#!' names
    an interpreter in a venv somewhere else entirely."""
    venv = tmp_path / "venvs" / "rekoll"
    (venv / "bin").mkdir(parents=True)
    site = venv / "lib" / "python3.11" / "site-packages" / "rekoll"
    site.mkdir(parents=True)
    (site / "_version.py").write_text('__version__ = "0.1.1"\n', encoding="utf-8")
    interpreter = venv / "bin" / "python"
    interpreter.write_bytes(b"pretend interpreter")

    shim = tmp_path / "bin" / "rekoll"
    shim.parent.mkdir(parents=True)
    shim.write_text(f"#!{interpreter}\nimport rekoll.cli\n", encoding="utf-8")
    shim.chmod(0o755)
    assert _version_of_install(shim) == ("0.1.1", False)


def test_one_stale_environment_is_named_once_not_per_console_script(tmp_path, monkeypatch):
    """A stale install ships BOTH `rekoll` and `rekoll-mcp`; listing the same
    directory twice buries the finding in its own output."""
    stale = _fake_install(tmp_path / "stale", "0.1.1")
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, stale)
    _level, detail = _check_install()
    assert detail.count(str(stale)) == 1


def test_the_install_check_never_executes_another_binary(tmp_path, monkeypatch):
    """Asking a stranger's binary for its version by RUNNING it is the obvious
    implementation and a remote-code-execution footgun: anything named 'rekoll'
    anywhere on PATH would be executed by a diagnostic. This tripwire fails if
    that ever changes."""
    stale = _fake_install(tmp_path / "stale", "0.1.1")
    _only_on_path(monkeypatch, stale)
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")

    def _boom(*a, **k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("doctor must never execute anything it finds")

    for name in ("run", "check_output", "check_call", "call", "Popen"):
        monkeypatch.setattr(subprocess, name, _boom)
    for name in ("system", "popen", "execv", "spawnv"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _boom)

    level, _detail = _check_install()
    assert level == "WARN"  # still did its job, having run nothing


def test_path_inspection_failure_degrades_softly(monkeypatch):
    monkeypatch.setattr(
        "rekoll.cli._rekoll_commands_on_path",
        lambda: (_ for _ in ()).throw(OSError("PATH exploded")),
    )
    level, detail = _check_install()
    assert level == "ok"  # a diagnostic never dies inspecting the machine
    assert " at " in detail


def test_a_hostile_install_path_cannot_forge_doctor_lines(tmp_path, monkeypatch):
    """Paths and version strings are display DATA. A directory or a
    _version.py chosen by someone else must not smuggle newlines or terminal
    escapes into doctor's output (#98), and must not blow the ASCII-only rule.
    """
    stale = _fake_install(tmp_path / "stale", "0.1.1")
    (stale.parent / "Lib" / "site-packages" / "rekoll" / "_version.py").write_text(
        '__version__ = "9.9\\n  ok    rekoll     forged\\x1b[31m line"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, stale)
    _level, detail = _check_install()
    assert "\n" not in detail and "\r" not in detail
    assert "\x1b" not in detail
    assert detail.isascii()


def test_doctor_renders_the_install_warning_end_to_end(project, capsys, monkeypatch):
    stale = _fake_install(project / "stale", "0.1.1")
    monkeypatch.setattr("rekoll.cli.__version__", "0.1.3")
    _only_on_path(monkeypatch, stale)
    assert main(["doctor"]) == 0  # WARN, never FAIL: nothing is broken
    out = capsys.readouterr().out
    assert "WARN" in out and "rekoll" in out
    assert "with notes" in out  # the summary stops saying a flat "All passed"


# -- #84: MCP registration ----------------------------------------------------

def _write_mcp(project: Path, config: dict, name: str = ".mcp.json") -> None:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


def test_no_registration_means_no_line(project):
    """CLI-only users must never be nagged about a door they never opened."""
    assert main(["init"]) == 0
    assert _check_mcp() is None


def test_someone_elses_mcp_server_is_ignored(project):
    _write_mcp(project, {"mcpServers": {"postgres": {"command": "pg-mcp", "args": []}}})
    assert _check_mcp() is None


def test_a_dead_command_path_is_a_WARN(project):
    """The rename incident: .mcp.json pointed at an absolute path that no
    longer existed, every session lost its rekoll tools, nothing said so."""
    dead = str(project / "gone" / ".venv" / "Scripts" / "rekoll-mcp.exe")
    _write_mcp(project, {"mcpServers": {"rekoll": {"command": dead, "args": []}}})
    result = _check_mcp()
    assert result is not None
    level, detail = result
    assert level == "WARN"
    assert "does not exist" in detail
    assert "python -m rekoll.mcp_server" in detail  # the rename-proof fix


def test_a_command_missing_from_PATH_is_a_WARN(project, monkeypatch):
    monkeypatch.setenv("PATH", str(project / "empty"))
    _write_mcp(project, {"mcpServers": {"rekoll": {"command": "rekoll-mcp", "args": []}}})
    result = _check_mcp()
    assert result is not None
    assert result[0] == "WARN"
    assert "not on PATH" in result[1]


def test_a_resolvable_command_stays_quiet_about_the_command(project):
    real = shutil.which("python") or sys.executable
    _write_mcp(
        project,
        {"mcpServers": {"rekoll": {"command": real, "args": ["-m", "rekoll.mcp_server"]}}},
    )
    result = _check_mcp()
    assert result is not None
    assert result[0] == "ok"


def test_a_store_path_in_a_vanished_folder_is_a_WARN(project):
    _write_mcp(
        project,
        {
            "mcpServers": {
                "rekoll": {
                    "command": sys.executable,
                    "args": [
                        "-m", "rekoll.mcp_server",
                        "--path", str(project / "gone" / "memory.db"),
                    ],
                }
            }
        },
    )
    result = _check_mcp()
    assert result is not None
    assert result[0] == "WARN"
    assert "--path" in result[1]


def test_a_store_not_created_yet_is_not_a_problem(project):
    """The server creates the store on first write, so warning here would nag
    every new user on their very first run."""
    _write_mcp(
        project,
        {
            "mcpServers": {
                "rekoll": {
                    "command": sys.executable,
                    "args": ["-m", "rekoll.mcp_server", "--path", str(project / "memory.db")],
                }
            }
        },
    )
    result = _check_mcp()
    assert result is not None
    assert result[0] == "ok"


def test_a_malformed_config_is_a_WARN(project):
    """An invalid config starts NO server - the same silence, earlier cause."""
    (project / ".mcp.json").write_text('{"mcpServers":{"rekoll":{,}}}', encoding="utf-8")
    result = _check_mcp()
    assert result is not None
    level, detail = result
    assert level == "WARN"
    assert "not valid JSON" in detail


def test_a_working_registration_admits_what_it_cannot_see(project):
    """THE twelve-hour silence (#82). Every fact a config check can verify was
    TRUE and the server still never ran, so an unqualified 'ok' would repeat
    the original failure in a new place."""
    _write_mcp(
        project,
        {"mcpServers": {"rekoll": {"command": sys.executable, "args": ["-m", "rekoll.mcp_server"]}}},
    )
    result = _check_mcp()
    assert result is not None
    level, detail = result
    assert level == "ok"
    assert "cannot tell whether your client LOADED it" in detail
    assert "list its tools" in detail  # the actionable step


def test_editor_config_locations_are_found(project):
    _write_mcp(
        project,
        {"mcpServers": {"rekoll": {"command": str(project / "gone.exe"), "args": []}}},
        name=".cursor/mcp.json",
    )
    result = _check_mcp()
    assert result is not None
    assert result[0] == "WARN"
    assert "mcp.json" in result[1]


def test_the_vscode_servers_key_is_understood(project):
    _write_mcp(
        project,
        {"servers": {"rekoll": {"command": str(project / "gone.exe"), "args": []}}},
        name=".vscode/mcp.json",
    )
    result = _check_mcp()
    assert result is not None
    assert result[0] == "WARN"


def test_a_hostile_config_cannot_forge_doctor_output(project):
    """`.mcp.json` is REPO-CONTROLLED data - it arrives with a clone. A hostile
    one must not be able to inject newlines (forging extra doctor lines), ANSI
    escapes (#98), or megabytes of padding into the terminal."""
    _write_mcp(
        project,
        {
            "mcpServers": {
                "rekoll\n  ok    firewall   totally fine": {
                    "command": "/nope/\x1b[31mred\r\n  ok    rekoll     forged " + "A" * 5000,
                    "args": [],
                }
            }
        },
    )
    result = _check_mcp()
    assert result is not None
    level, detail = result
    assert level == "WARN"
    assert "\n" not in detail and "\r" not in detail
    assert "\x1b" not in detail
    assert detail.isascii()
    assert len(detail) < 1000  # capped, not megabytes of padding
    # doctor renders its report in columns ("  WARN  mcp   ..."), so surviving
    # as printable ASCII is NOT enough: with runs of spaces intact, a hostile
    # config can reproduce that layout and pad until a wrapping terminal starts
    # a visual line on the forgery. Whitespace runs must be collapsed.
    assert "ok    firewall" not in detail
    assert "  " not in detail.split(" - so an MCP client")[0].lstrip()


def test_the_mcp_check_never_executes_the_configured_command(project, monkeypatch):
    """The load-bearing one. `.mcp.json` can name any command at all, so
    launching it to 'see if it works' would turn doctor into an
    arbitrary-code-execution primitive for any repo you clone."""
    _write_mcp(
        project,
        {"mcpServers": {"rekoll": {"command": sys.executable, "args": ["-m", "rekoll.mcp_server"]}}},
    )

    def _boom(*a, **k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("doctor must never launch what a config points at")

    for name in ("run", "check_output", "check_call", "call", "Popen"):
        monkeypatch.setattr(subprocess, name, _boom)
    for name in ("system", "popen", "execv", "spawnv"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _boom)

    result = _check_mcp()
    assert result is not None
    assert result[0] == "ok"


def test_a_config_that_is_a_directory_is_fail_soft(project):
    (project / ".mcp.json").mkdir()
    registrations, unreadable = _find_mcp_registrations()
    assert registrations == []
    assert unreadable == []


def test_a_non_object_config_is_reported_not_swallowed(project):
    (project / ".mcp.json").write_text("[1, 2, 3]", encoding="utf-8")
    result = _check_mcp()
    assert result is not None
    assert result[0] == "WARN"
    assert "not a JSON object" in result[1]


def test_doctor_shows_the_mcp_line_end_to_end(project, capsys):
    assert main(["init"]) == 0
    capsys.readouterr()
    _write_mcp(
        project,
        {"mcpServers": {"rekoll": {"command": str(project / "gone.exe"), "args": []}}},
    )
    assert main(["doctor"]) == 0  # WARN, never FAIL
    out = capsys.readouterr().out
    assert "mcp" in out
    assert "WARN" in out
    assert "You're good to go" in out


def test_doctor_survives_every_hostile_input_at_once(project, capsys, monkeypatch):
    """Fail-soft, end to end: a corrupt config, an unreadable install and a
    broken PATH together must still produce a clean, ASCII, exit-0 report."""
    (project / ".mcp.json").write_text("{not json at all", encoding="utf-8")
    _fake_install(project / "mystery", None)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([
            str(project / "mystery" / "Scripts"),
            'C:\\nope\\<>:"|?*',        # invalid on Windows: stat() raises
            str(project / "does-not-exist"),
            "A" * 4000,                 # absurd, but a PATH really can hold it
        ]),
    )
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert "You're good to go" in out
