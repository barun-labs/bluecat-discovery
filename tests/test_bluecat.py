#!/usr/bin/env python3
"""Unit tests for bluecat.py - the launcher that routes to bluecat_discover.py
or bluecat_export.py. Every dispatch target is monkeypatched to a stub, so
no real BAM/network/interactive session ever runs here."""
import os
import sys

os.environ["NO_COLOR"] = "1"  # force plain output before importing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import bluecat as bc
from bluecat_menu import ScriptedAnswers


def _answers(monkeypatch, *values, isatty=True):
    monkeypatch.setattr(bc, "STDIN", ScriptedAnswers(list(values), isatty=isatty))


def _stub(monkeypatch, module_attr):
    """Replace bc.<module_attr>.main with a spy that records argv and
    returns 0, so routing can be asserted without running the real tool."""
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(getattr(bc, module_attr), "main", fake_main)
    return calls


# ---------------------------------------------------------------------------
# --help / -h
# ---------------------------------------------------------------------------

def test_help_flag_exits_zero_and_names_both_tools(capsys):
    assert bc.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "bluecat_discover.py" in out
    assert "bluecat_export.py" in out


def test_short_help_flag_exits_zero(capsys):
    assert bc.main(["-h"]) == 0


# ---------------------------------------------------------------------------
# argument forwarding - no menu shown, exact argv forwarded verbatim
# ---------------------------------------------------------------------------

def test_discover_forwards_argv_verbatim(monkeypatch):
    calls = _stub(monkeypatch, "bluecat_discover")
    assert bc.main(["discover", "views", "--json"]) == 0
    assert calls == [["views", "--json"]]


def test_export_forwards_argv_verbatim(monkeypatch):
    calls = _stub(monkeypatch, "bluecat_export")
    assert bc.main(["export", "-c", "zones", "-y"]) == 0
    assert calls == [["-c", "zones", "-y"]]


def test_discover_help_forwarded_not_launcher_help(monkeypatch):
    """`bluecat.py discover --help` must reach bluecat_discover.py's own
    parser, not the launcher's usage note."""
    calls = _stub(monkeypatch, "bluecat_discover")
    bc.main(["discover", "--help"])
    assert calls == [["--help"]]


def test_forwards_return_code(monkeypatch):
    monkeypatch.setattr(bc.bluecat_export, "main", lambda argv: 3)
    assert bc.main(["export", "--bogus"]) == 3


def test_unknown_tool_is_a_usage_error(capsys):
    rc = bc.main(["frobnicate"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "frobnicate" in err
    assert "discover" in err and "export" in err


# ---------------------------------------------------------------------------
# no arguments - tty prompts, non-tty refuses to prompt
# ---------------------------------------------------------------------------

def test_no_args_non_tty_prints_usage_and_exits_nonzero(monkeypatch, capsys):
    _answers(monkeypatch, isatty=False)
    rc = bc.main([])
    assert rc != 0
    out, err = capsys.readouterr()
    assert out == ""  # no menu, no prompt attempted
    assert "bluecat_discover.py" in err
    assert "bluecat_export.py" in err


def test_no_args_non_tty_never_reads_stdin(monkeypatch):
    class ExplodingStdin(ScriptedAnswers):
        def input(self, prompt_text):
            raise AssertionError("must not prompt when stdin is not a tty")

    monkeypatch.setattr(bc, "STDIN", ExplodingStdin([], isatty=False))
    bc.main([])  # would raise via the stub above if it prompted


def test_no_args_tty_menu_routes_to_discover_by_number(monkeypatch):
    calls = _stub(monkeypatch, "bluecat_discover")
    _answers(monkeypatch, "1")
    assert bc.main([]) == 0
    assert calls == [[]]  # handed off with no further arguments


def test_no_args_tty_menu_routes_to_export_by_number(monkeypatch):
    calls = _stub(monkeypatch, "bluecat_export")
    _answers(monkeypatch, "2")
    assert bc.main([]) == 0
    assert calls == [[]]


def test_no_args_tty_menu_routes_by_typed_name(monkeypatch):
    calls = _stub(monkeypatch, "bluecat_export")
    _answers(monkeypatch, "export")
    assert bc.main([]) == 0
    assert calls == [[]]


def test_no_args_tty_menu_shows_filenames(monkeypatch, capsys):
    _answers(monkeypatch, "1")
    _stub(monkeypatch, "bluecat_discover")
    bc.main([])
    out = capsys.readouterr().out
    assert "bluecat_discover.py" in out
    assert "bluecat_export.py" in out


def test_no_args_tty_menu_back_cancels_cleanly(monkeypatch):
    discover_calls = _stub(monkeypatch, "bluecat_discover")
    export_calls = _stub(monkeypatch, "bluecat_export")
    _answers(monkeypatch, "0")
    assert bc.main([]) == 0
    assert discover_calls == []
    assert export_calls == []


def test_no_args_tty_menu_eof_cancels_with_130(monkeypatch, capsys):
    _answers(monkeypatch)  # no answers queued -> EOFError on first read
    assert bc.main([]) == 130
