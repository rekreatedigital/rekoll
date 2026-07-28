"""`rekoll doctor` should explain a broken MCP door, not just a broken
registration (issue #114, ADR-0041).

Before this, doctor reported whether MCP was *registered* while staying silent
about whether the server could *start* — so the one machine state that broke
every new install in #114 produced a clean bill of health.

Gating rule: the line appears only when the `mcp` extra is actually installed.
A CLI-only user never opened this door and must not be nagged about it — the
same rule ADR-0041 applies to the registration check.
"""

from __future__ import annotations

import pytest

from rekoll import cli


def _pretend_sdk(monkeypatch, present: bool, version: str | None) -> None:
    monkeypatch.setattr(
        cli, "_mcp_sdk_state", lambda: (present, version), raising=False
    )


def test_no_line_at_all_when_the_extra_was_never_installed(monkeypatch):
    """CLI-only users opened no door; doctor must not invent one."""
    _pretend_sdk(monkeypatch, present=False, version=None)
    assert cli._check_mcp_sdk() is None


def test_an_incompatible_sdk_warns_and_names_both_versions(monkeypatch):
    _pretend_sdk(monkeypatch, present=True, version="2.0.0")

    result = cli._check_mcp_sdk()
    assert result is not None
    level, detail = result

    assert level == "WARN"
    assert "2.0.0" in detail  # what you have
    assert ">=1.3" in detail  # what rekoll needs
    assert detail.isascii()


def test_a_supported_sdk_reports_ok_without_overclaiming(monkeypatch):
    """It verified a VERSION RANGE, not a successful startup. ADR-0041: report
    only what was actually checked."""
    _pretend_sdk(monkeypatch, present=True, version="1.29.0")

    result = cli._check_mcp_sdk()
    assert result is not None
    level, detail = result

    assert level == "ok"
    assert "1.29.0" in detail
    # Must not promise something a version read cannot establish.
    for overclaim in ("server starts", "server can start", "working", "healthy"):
        assert overclaim not in detail.lower()


def test_the_floor_cell_is_reported_ok(monkeypatch):
    """`mcp==1.3.0` is a supported configuration and must never be warned about."""
    _pretend_sdk(monkeypatch, present=True, version="1.3.0")

    result = cli._check_mcp_sdk()
    assert result is not None
    assert result[0] == "ok"


def test_an_unreadable_version_is_reported_as_unknown_not_as_ok(monkeypatch):
    _pretend_sdk(monkeypatch, present=True, version=None)

    result = cli._check_mcp_sdk()
    assert result is not None
    level, detail = result

    assert level == "WARN"
    assert detail.isascii()
    # It must not print the word "None" as if that were a version.
    assert "None" not in detail


def test_a_hostile_version_string_cannot_forge_a_doctor_line(monkeypatch):
    """ADR-0044 / issue #112: doctor's output is column-formatted, so anything
    it echoes must not be able to reproduce its own layout on a new line."""
    _pretend_sdk(monkeypatch, present=True, version="2.0.0\n  ok    firewall   all clear")

    result = cli._check_mcp_sdk()
    assert result is not None
    _level, detail = result

    assert "\n" not in detail
    assert "ok    firewall" not in detail


def test_doctor_prints_the_mcp_sdk_line(monkeypatch, tmp_path, capsys):
    """End of the wire: the check reaches actual doctor output."""
    _pretend_sdk(monkeypatch, present=True, version="2.0.0")

    args = type("A", (), {"path": str(tmp_path / "m.db")})()
    for attr in ("tenant", "project", "agent"):
        setattr(args, attr, "default")

    cli.cmd_doctor(args)
    out = capsys.readouterr().out

    assert "2.0.0" in out


def test_the_doctor_check_never_imports_mcp(monkeypatch):
    """ADR-0041's never-execute rule, one step earlier: doctor must not import
    the package it is reporting on. Importing runs module-level code; a
    diagnostic reads, it does not run."""
    import builtins

    real_import = builtins.__import__

    def exploding_import(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise AssertionError(f"doctor imported {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", exploding_import)

    cli._check_mcp_sdk()  # must not raise
