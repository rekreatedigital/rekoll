"""The MCP door must tell the truth about WHY it could not start (issue #114).

`mcp` 2.0.0 removed `mcp.server.fastmcp`. `build_server` lazy-imports exactly
that path and caught every `ImportError` as "the optional extra is missing", so
a user who had *just* installed the extra was told to install the extra. The
extra is now upper-bounded, and the guard distinguishes the two cases.

Every test here SIMULATES the SDK state instead of reading whichever `mcp` the
runner happens to have installed: these must pin identically in the no-mcp CI
job, at the `mcp==1.3.0` floor cell, and on a box with 2.x.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rekoll import mcp_server
from rekoll.model import TrustTier
from rekoll.mcp_server import ServerConfig


def _config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        path=str(tmp_path / "m.db"),
        tenant="default",
        project="x",
        agent="default",
        trust=TrustTier.UNVERIFIED,
        root=tmp_path,
    )


def _break_fastmcp(monkeypatch) -> None:
    """Make `from mcp.server.fastmcp import FastMCP` raise, exactly as mcp 2.0.0
    does — without touching the top-level `mcp` package, which 2.0.0 still
    ships. A `None` in sys.modules makes the import machinery raise ImportError.
    """
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)


def _pretend_sdk(monkeypatch, present: bool, version: str | None) -> None:
    """Pin what the guard believes is installed, so neither branch depends on
    the runner's own environment."""
    monkeypatch.setattr(
        mcp_server, "_mcp_sdk_state", lambda: (present, version), raising=False
    )


# -- the two branches, both pinned ---------------------------------------------


def test_installed_but_incompatible_mcp_names_the_version_and_blames_the_version(
    tmp_path, monkeypatch
):
    """The regression from #114: mcp IS installed, the import still fails, and
    the old guard told the user to install what they already had."""
    _break_fastmcp(monkeypatch)
    _pretend_sdk(monkeypatch, present=True, version="2.0.0")

    with pytest.raises(ImportError) as exc:
        mcp_server.build_server(_config(tmp_path))
    message = str(exc.value)

    # It must NOT claim the extra is missing - that is the lie.
    assert 'pip install "rekoll[mcp]"' not in message
    # It must name what is actually installed, and what rekoll needs.
    assert "2.0.0" in message
    assert mcp_server._MCP_REQUIREMENT in message
    # ASCII only (cli.py's module rule applies to rekoll's own messages).
    assert message.isascii()


def test_genuinely_absent_mcp_still_gets_the_install_hint(tmp_path, monkeypatch):
    """The other branch: the fix must not trade one wrong message for another."""
    _break_fastmcp(monkeypatch)
    _pretend_sdk(monkeypatch, present=False, version=None)

    with pytest.raises(ImportError) as exc:
        mcp_server.build_server(_config(tmp_path))
    message = str(exc.value)

    assert 'pip install "rekoll[mcp]"' in message
    assert "2.0.0" not in message
    assert message.isascii()


def test_main_prints_the_incompatible_message_and_exits_1(tmp_path, monkeypatch, capsys):
    """Startup stays a plain-English surface (ADR-0008): no traceback."""
    _break_fastmcp(monkeypatch)
    _pretend_sdk(monkeypatch, present=True, version="2.0.0")

    with pytest.raises(SystemExit) as exc:
        mcp_server.main(
            ["--path", str(tmp_path / "m.db"), "--project", "x", "--root", str(tmp_path)]
        )
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "2.0.0" in err
    assert 'pip install "rekoll[mcp]"' not in err
    assert "Traceback" not in err


def test_an_unreadable_version_is_not_guessed(tmp_path, monkeypatch):
    """Present but unversioned (a vendored copy with no metadata): say the
    package is there without inventing a version number (ADR-0041)."""
    _break_fastmcp(monkeypatch)
    _pretend_sdk(monkeypatch, present=True, version=None)

    with pytest.raises(ImportError) as exc:
        mcp_server.build_server(_config(tmp_path))
    message = str(exc.value)

    assert 'pip install "rekoll[mcp]"' not in message
    assert mcp_server._MCP_REQUIREMENT in message
    assert message.isascii()


def test_the_underlying_import_error_is_reported_not_swallowed(tmp_path, monkeypatch):
    """`doctor`'s standard: name the evidence. The real ImportError text is the
    only thing that distinguishes 2.0.0 from a half-installed 1.x."""
    _break_fastmcp(monkeypatch)
    _pretend_sdk(monkeypatch, present=True, version="2.0.0")

    with pytest.raises(ImportError) as exc:
        mcp_server.build_server(_config(tmp_path))
    assert "mcp.server.fastmcp" in str(exc.value)
    # ...and the original exception stays chained for anyone debugging.
    assert exc.value.__cause__ is not None


def test_the_failure_message_cannot_forge_extra_output_lines(tmp_path, monkeypatch):
    """ADR-0044 / issue #112: nothing rekoll prints may carry a newline out of
    data it did not author. The ImportError text is environment-derived."""
    _break_fastmcp(monkeypatch)
    _pretend_sdk(monkeypatch, present=True, version="2.0.0\n  ok    firewall   forged")

    with pytest.raises(ImportError) as exc:
        mcp_server.build_server(_config(tmp_path))

    # The rendered version must not introduce a line rekoll did not write.
    assert "ok    firewall" not in str(exc.value)


# -- the supported range -------------------------------------------------------


@pytest.mark.parametrize(
    "version,supported",
    [
        ("1.3.0", True),  # the floor cell - must never break
        ("1.3", True),
        ("1.29.0", True),
        ("1.2.9", False),  # below the floor: instructions are silently dropped
        ("2.0.0", False),  # the break
        ("2.0.0rc1", False),  # a prerelease of the break is still the break
        ("3.1.0", False),
    ],
)
def test_version_range_verdicts(version, supported):
    assert mcp_server._mcp_version_supported(version) is supported


@pytest.mark.parametrize("version", ["", "not-a-version", None])
def test_an_unparseable_version_returns_none_rather_than_a_guess(version):
    """Three-state on purpose: True / False / "I could not tell" (ADR-0041)."""
    assert mcp_server._mcp_version_supported(version) is None


def test_the_requirement_string_matches_pyproject_exactly():
    """Drift tripwire. The message quotes a constraint as fact; if the constant
    and the packaging metadata disagree, the message becomes a new lie."""
    import re

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^mcp\s*=\s*\["([^"]+)"\]', text, re.MULTILINE)
    assert match, "the [mcp] extra is no longer a single-requirement list"
    assert match.group(1) == mcp_server._MCP_REQUIREMENT


def test_the_floor_is_still_supported_by_the_declared_requirement():
    """The `mcp==1.3.0` CI cell exists because a real user on an old client
    depends on it. Whatever the ceiling becomes, the floor must stay in."""
    assert mcp_server._mcp_version_supported("1.3.0") is True
    assert ">=1.3" in mcp_server._MCP_REQUIREMENT


# -- probing must never import (or run) the SDK --------------------------------


def test_the_sdk_probe_does_not_import_mcp(monkeypatch):
    """The probe reads packaging metadata and locates a spec; it must never
    import `mcp`. Same discipline as ADR-0041's never-execute rule: a
    diagnostic must not run the thing it is diagnosing. Pinned by making any
    real `mcp` import explode."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def exploding_import(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise AssertionError(f"the SDK probe imported {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", exploding_import)

    present, version = mcp_server._mcp_sdk_state()
    assert isinstance(present, bool)
    assert version is None or isinstance(version, str)
