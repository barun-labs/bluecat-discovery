#!/usr/bin/env python3
"""Unit tests for bluecat_menu.choose() -- the numbered-menu chooser shared
by bluecat_discover.py's wizard prompts and bluecat_export.py's interactive
drill-down (see bluecat_menu.py for the two modes: exact=False is the
original filter-and-page behaviour, exact=True is the wizard's short,
single-screen style).

Every test injects its own input function (and, where useful, its own
print_fn) instead of monkeypatching sys.stdin or builtins.input: choose()
takes input_fn/print_fn as plain keyword arguments, so a fake queue of
answers is passed straight in.
"""
import os
import pty
import sys
import termios
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import bluecat_menu as bm


def _answers(*values):
    """An input_fn that yields each value in turn, ignoring the prompt."""
    it = iter(values)
    return lambda prompt="": next(it)


def _sink():
    """A print_fn that records every line instead of touching a stream."""
    lines = []
    return lines, lines.append


def test_bare_enter_returns_default():
    result = bm.choose(
        ["alpha", "beta"], str, "Pick one", exact=True, default="alpha",
        input_fn=_answers(""),
    )
    assert result == "alpha"


def test_number_selects():
    result = bm.choose(
        ["alpha", "beta", "gamma"], str, "Pick one", exact=True,
        input_fn=_answers("2"),
    )
    assert result == "beta"


def test_name_selects():
    result = bm.choose(
        ["alpha", "beta", "gamma"], str, "Pick one", exact=True,
        match_key_fn=str, input_fn=_answers("gamma"),
    )
    assert result == "gamma"


def test_zero_raises_back():
    with pytest.raises(bm.Back):
        bm.choose(
            ["alpha", "beta"], str, "Pick one", exact=True, default="alpha",
            input_fn=_answers("0"),
        )


def test_b_also_raises_back_in_the_non_exact_paged_chooser():
    """'b' is bluecat_export.py's own long-standing back key."""
    with pytest.raises(bm.Back):
        bm.choose(["alpha", "beta"], str, "Pick one", input_fn=_answers("b"))


def test_garbage_reprompts_then_succeeds():
    lines, sink = _sink()
    result = bm.choose(
        ["alpha", "beta"], str, "Pick one", exact=True, default="alpha",
        invalid_msg="try again", print_fn=sink,
        input_fn=_answers("nope", "2"),
    )
    assert result == "beta"
    assert "try again" in lines


def test_extras_verb_returns_its_value():
    result = bm.choose(
        ["alpha", "beta"], str, "Pick one", exact=True, default="alpha",
        extras={"h": "help", "help": "help"}, input_fn=_answers("h"),
    )
    assert result == "help"


def test_non_exact_digit_out_of_range_reprompts():
    """The original export.py chooser: 'out of range' then a valid pick."""
    lines, sink = _sink()
    result = bm.choose(
        ["alpha", "beta"], str, "Pick one", print_fn=sink,
        input_fn=_answers("9", "1"),
    )
    assert result == "alpha"
    assert any("out of range" in ln for ln in lines)


def test_non_exact_text_filters_the_pool():
    """Typed text narrows the pool by substring match; a second pick from
    the narrowed pool then returns the item (bluecat_export.py's original
    two-step filter, unchanged by the discover-menu 'exact' mode)."""
    items = ["apple", "apricot", "banana"]
    result = bm.choose(items, str, "Fruit", input_fn=_answers("ap", "2"))
    assert result == "apricot"


def test_only_one_item_shortcuts_without_prompting():
    """No input_fn is given -- if input() were called this would raise
    OSError/EOFError in a test process, proving the shortcut skipped it."""
    result = bm.choose(["solo"], str, "Pick one")
    assert result == "solo"


# ---------------------------------------------------------------------------
# ScriptedAnswers.keypress() -- mirrors input()/readline(): pops the next
# scripted answer, no byte-level behaviour to fake.
# ---------------------------------------------------------------------------

def test_scripted_answers_keypress_pops_next_answer():
    answers = bm.ScriptedAnswers(["y", "3", ""])
    assert answers.keypress("Q1: ") == "y"
    assert answers.keypress("Q2: ") == "3"
    assert answers.keypress("Q3: ") == ""


def test_scripted_answers_keypress_raises_eof_when_exhausted():
    answers = bm.ScriptedAnswers([])
    with pytest.raises(EOFError):
        answers.keypress("Q: ")


# ---------------------------------------------------------------------------
# RealStdin.keypress() -- real cbreak-mode reading, via a genuine pty so the
# fd is a real terminal (os.isatty()/termios calls all need one). A plain
# fake stdin object (no real fd) is covered separately below: it must fall
# back to a full line read instead of raising.
# ---------------------------------------------------------------------------

class _PtyStdin:
    """Stands in for sys.stdin: a real pty slave fd, claiming to be a tty,
    exactly like a real terminal (unlike the test doubles used elsewhere in
    this project, which fake isatty() but have no fd at all)."""

    def __init__(self, fd):
        self._fd = fd

    def isatty(self):
        return True

    def fileno(self):
        return self._fd

    def readline(self):
        raise AssertionError("must not fall back to line reading on a real tty")


@pytest.fixture
def a_pty():
    master, slave = pty.openpty()
    try:
        yield master, slave
    finally:
        os.close(master)
        os.close(slave)


def _type_after(fd, data, delay=0.05):
    """Writes `data` to the pty master shortly after this is called, from a
    background thread, instead of before -- keypress() opens the slave in
    cbreak mode with TCSAFLUSH, which (correctly, so a stale keystroke from
    before the prompt was shown can't answer it) discards whatever was
    already queued. Typing has to happen while the read is genuinely
    blocked, exactly like a real terminal."""
    threading.Timer(delay, os.write, args=(fd, data)).start()


def test_keypress_accepted_without_enter(monkeypatch, a_pty):
    """A single typed byte, with no trailing Enter at all, is returned
    immediately -- the whole point of this feature."""
    master, slave = a_pty
    monkeypatch.setattr(sys, "stdin", _PtyStdin(slave))
    stdin = bm.RealStdin()
    _type_after(master, b"y")
    assert stdin.keypress("Use saved credentials? [Y/n]: ") == "y"


def test_keypress_bare_enter_returns_empty_string(monkeypatch, a_pty):
    """Enter still works, as one of the single keystrokes -- it means
    "take the default", exactly like an empty typed line used to."""
    master, slave = a_pty
    monkeypatch.setattr(sys, "stdin", _PtyStdin(slave))
    stdin = bm.RealStdin()
    _type_after(master, b"\r")
    assert stdin.keypress("Q: ") == ""


def test_keypress_swallows_a_stray_trailing_enter(monkeypatch, a_pty):
    """Muscle memory keeps producing an Enter after the keypress; it must
    be swallowed instead of leaking into the next prompt and silently
    picking its default there."""
    master, slave = a_pty
    monkeypatch.setattr(sys, "stdin", _PtyStdin(slave))
    stdin = bm.RealStdin()
    _type_after(master, b"y\r")     # the keystroke, plus a stray Enter
    assert stdin.keypress("Q1: ") == "y"
    assert stdin._pending_byte is None   # the \r was consumed, not queued
    # Proof the swallow actually happened, not just that nothing crashed:
    # the very next prompt must see ITS OWN key, not a leftover blank
    # "Enter" from the previous prompt's stray keystroke.
    _type_after(master, b"n")
    assert stdin.keypress("Q2: ") == "n"


def test_keypress_does_not_block_when_no_stray_enter_follows(monkeypatch, a_pty):
    """The swallow-Enter check must never wait for a keystroke that never
    arrives -- proven here by a bare key with nothing queued after it."""
    master, slave = a_pty
    monkeypatch.setattr(sys, "stdin", _PtyStdin(slave))
    stdin = bm.RealStdin()
    _type_after(master, b"y")
    assert stdin.keypress("Q: ") == "y"       # returns; does not hang
    assert stdin._pending_byte is None


def test_keypress_ctrl_d_raises_eof(monkeypatch, a_pty):
    master, slave = a_pty
    monkeypatch.setattr(sys, "stdin", _PtyStdin(slave))
    stdin = bm.RealStdin()
    _type_after(master, b"\x04")
    with pytest.raises(EOFError):
        stdin.keypress("Q: ")


def test_keypress_falls_back_to_line_input_when_not_a_tty(monkeypatch):
    """Gated on isatty(), exactly like the rest of this seam: a pipe/CI run
    never attempts a keystroke read at all."""
    class _NotATTY:
        def isatty(self):
            return False

        def readline(self):
            return "hello\n"

    monkeypatch.setattr(sys, "stdin", _NotATTY())
    stdin = bm.RealStdin()
    assert stdin.keypress("Prompt: ") == "hello"


def test_keypress_falls_back_when_fd_is_not_a_real_terminal(monkeypatch):
    """isatty() lying (or a test double with no real fd) must not crash --
    it must fall back to a full line read, same as a non-tty. This is the
    exact shape of _TTYStdin in test_bluecat_discover.py/test_bluecat.py:
    claims isatty()==True but has no fileno() at all."""
    class _ClaimsTTYNoFd:
        def isatty(self):
            return True

        def readline(self):
            return "3\n"

    monkeypatch.setattr(sys, "stdin", _ClaimsTTYNoFd())
    stdin = bm.RealStdin()
    assert stdin.keypress("Prompt: ") == "3"


def test_keypress_restores_terminal_after_exception_mid_prompt(monkeypatch, a_pty):
    """A BAMError/timeout mid-prompt is a real path this tool hits
    routinely; the terminal must come back to normal (echo + line
    buffering) even when the read itself blows up, not just on success."""
    master, slave = a_pty
    original = termios.tcgetattr(slave)
    monkeypatch.setattr(sys, "stdin", _PtyStdin(slave))
    stdin = bm.RealStdin()

    def boom(fd):
        raise RuntimeError("simulated failure mid-keystroke")

    monkeypatch.setattr(stdin, "_read_byte", boom)
    with pytest.raises(RuntimeError):
        stdin.keypress("Q: ")
    assert termios.tcgetattr(slave) == original
