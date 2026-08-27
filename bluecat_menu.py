#!/usr/bin/env python3
"""Shared interactive numbered-menu chooser, prompting seam, and config file.

bluecat_discover.py's wizard prompts and bluecat_export.py's interactive
drill-down each hand-rolled their own "print a numbered list, read a line,
accept a number or a name, reprompt on garbage" loop. This module is the one
implementation both import. It knows nothing about BAM or HTTP, only
terminal I/O - plus the read/write of the one settings file
(~/.bluecat_discover.json) both scripts share, since that file is what lets
either one avoid retyping saved credentials.
"""
import contextlib
import getpass
import json
import os
import select
import sys

from bluecat_core import BAMError

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:          # e.g. Windows: no cbreak mode available there
    _HAS_TERMIOS = False


@contextlib.contextmanager
def _cbreak_mode(fd):
    """Puts `fd` into cbreak mode for the duration of the block, and ALWAYS
    restores the original terminal attributes in a `finally` - including
    when the caller raises KeyboardInterrupt or EOFError partway through a
    keystroke, since leaving a caller's shell in cbreak (no echo, no line
    buffering) until they blind-type `stty sane` is worse than any prompt
    bug this module could otherwise cause.

    `tty.setcbreak`, never `tty.setraw`: setraw also clears ISIG, which
    would swallow Ctrl-C instead of raising KeyboardInterrupt the way the
    rest of this tool's `except (KeyboardInterrupt, EOFError)` handlers
    expect.
    """
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class RealStdin:
    """Reads the real stdin/tty, exactly as bluecat_discover.py's `_prompt`
    and bluecat_export.py's `prompt`/`prompt_secret` did before this seam
    existed. `isatty()` is what lets a pipe or CI run skip interactive
    prompting entirely; keep it delegating to the live `sys.stdin` object
    (looked up per call, not cached) so tests that still monkeypatch
    `sys.stdin` keep working unchanged."""

    def __init__(self):
        # One byte read while peeking for a stray Enter that turned out to
        # belong to the NEXT prompt instead (a fast double-keypress with no
        # pause between them) - replayed on the next keypress() call instead
        # of being lost.
        self._pending_byte = None

    def isatty(self):
        return sys.stdin.isatty()

    def readline(self):
        """Mirrors `sys.stdin.readline()` but raises EOFError on a closed
        stream (readline() returning "") instead of returning it."""
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    def input(self, prompt_text):
        """Mirrors the builtin `input()`: prompt goes to stdout, one line
        comes back with no trailing newline, EOFError on closed stdin."""
        return input(prompt_text)

    def secret(self, prompt_text, stream=None):
        """Mirrors `getpass.getpass()`: never echoes, reads from the
        controlling tty when there is one. `stream` only changes where the
        prompt text itself is written."""
        return getpass.getpass(prompt_text, stream=stream)

    def keypress(self, prompt_text, stream=None):
        """Reads exactly one keystroke - no Enter required - for the short,
        unambiguous menus (y/n questions, 1-9 digit menus) where every valid
        answer is a single character.

        Falls back to a full line read (`self.readline()`, after writing
        `prompt_text` to `stream` itself) whenever a single keystroke can't
        safely be read: no termios/tty on this platform, `self.isatty()`
        says stdin isn't a terminal, or `sys.stdin.fileno()`/cbreak mode
        raises for any reason (e.g. a test double standing in for stdin
        with no real file descriptor). This is what keeps a pipe, a CI run,
        or a test's fake stdin on exactly today's line-reading behaviour.

        `stream` mirrors `secret()`: where the prompt text and the echoed
        keystroke are written (defaults to stdout, matching `input()`);
        bluecat_discover.py passes `stream=sys.stderr` to keep stdout clean
        for --json/--csv output.

        A bare Enter is returned as "" - the same as an empty typed line -
        so a caller's existing "Enter takes the default" handling needs no
        change. Ctrl-D raises EOFError, matching `readline()`'s contract;
        Ctrl-C raises KeyboardInterrupt (cbreak leaves ISIG enabled, so the
        terminal driver normally delivers SIGINT before the byte is even
        read - the explicit check below is a defensive backstop, not the
        primary mechanism). After a non-Enter key is accepted, one
        immediately-following bare Enter is swallowed without blocking -
        muscle memory keeps producing one for weeks, and a leaked Enter
        would otherwise silently answer the NEXT prompt with its default.
        """
        stream = stream or sys.stdout
        if not _HAS_TERMIOS or not self.isatty():
            stream.write(prompt_text)
            stream.flush()
            return self.readline()
        try:
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                raise OSError("stdin has no real tty fd")
        except (AttributeError, OSError, ValueError):
            stream.write(prompt_text)
            stream.flush()
            return self.readline()

        stream.write(prompt_text)
        stream.flush()
        with _cbreak_mode(fd):
            ch = self._read_byte(fd)
            if ch in (b"", b"\x04"):
                stream.write("\n")
                stream.flush()
                raise EOFError
            if ch == b"\x03":
                stream.write("\n")
                stream.flush()
                raise KeyboardInterrupt
            if ch in (b"\r", b"\n"):
                stream.write("\n")
                stream.flush()
                return ""
            key = ch.decode(errors="replace")
            stream.write(key + "\n")
            stream.flush()
            self._swallow_enter(fd)
        return key

    def _read_byte(self, fd):
        if self._pending_byte is not None:
            byte = self._pending_byte
            self._pending_byte = None
            return byte
        return os.read(fd, 1)

    def _swallow_enter(self, fd):
        """Non-blocking: consumes one queued Enter if it is already there,
        never waits for one that might still be coming. A queued byte that
        turns out NOT to be Enter is a fast-typed answer to the NEXT
        prompt, not stray input - it is kept in `_pending_byte` instead of
        being discarded."""
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return
        byte = os.read(fd, 1)
        if byte not in (b"\r", b"\n", b""):
            self._pending_byte = byte


class ScriptedAnswers:
    """Test seam: a fixed sequence of answers standing in for the operator,
    so a wizard or export test supplies "here are the answers" instead of
    building an object that impersonates sys.stdin's isatty()/readline().

    `isatty=False` reproduces a pipe/CI run, for tests that must prove the
    non-interactive path is taken instead of prompting."""

    def __init__(self, answers, isatty=True):
        self._answers = list(answers)
        self._isatty = isatty

    def isatty(self):
        return self._isatty

    def _next(self):
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)

    def readline(self):
        return self._next()

    def input(self, prompt_text):
        return self._next()

    def secret(self, prompt_text, stream=None):
        return self._next()

    def keypress(self, prompt_text, stream=None):
        """Same queue as every other read here: a scripted test hands this
        "y", "3", "f", "" (bare Enter), or even a whole word like "yes" -
        RealStdin's byte-at-a-time reading and stray-Enter swallowing are
        production-terminal concerns this stand-in has no need to model."""
        return self._next()


# The one seam bluecat_discover.py and bluecat_export.py read operator
# answers through. Production code never constructs a RealStdin of its own;
# tests swap this module attribute for a ScriptedAnswers instance.
STDIN = RealStdin()

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RESET = "\033[0m" if COLOR else ""
BOLD = "\033[1m" if COLOR else ""
DIM_GRAY = "\033[90m" if COLOR else ""
YELLOW = "\033[33m" if COLOR else ""


class Back(Exception):
    """The user asked to go back to the previous menu."""


def match_items(items, needle, label_fn):
    """Substring match, case-insensitive, over a chooser's rendered labels."""
    needle = needle.lower()
    return [it for it in items if needle in label_fn(it).lower()]


def choose(items, label_fn, title, allow_all=False, all_label="ALL of these",
           *, default=None, extras=None, exact=False, match_key_fn=None,
           show_count=True, index_width=4, footer_lines=None,
           prompt_text=None, invalid_msg=None, redraw=True,
           print_fn=print, input_fn=input):
    """Numbered chooser, shared by every menu in this project.

    The default mode (`exact=False`) is bluecat_export.py's original
    behaviour, unchanged: `label_fn(item)` renders one row, long lists are
    paged 20 at a time, and typed text narrows `items` to a re-shown pool by
    substring match instead of picking straight away, since a BAM view can
    hold hundreds of zones.

    `exact=True` is the discover wizard's style instead: a short,
    single-screen menu where typed text must equal one item's
    `match_key_fn(item)` (default: `label_fn`) exactly, and the pick happens
    immediately, no filtering, no paging. `index_width`, `show_count`,
    `footer_lines`, `prompt_text`, and `invalid_msg` let each exact-mode
    caller reproduce its own header, row numbering, extra footer rows (like
    "0) back"), prompt wording, and error line, independently of the others.

    `default` is returned on a bare Enter; None (the original behaviour)
    means Enter is not accepted and falls through to the invalid-input path.
    `extras` maps a typed verb, already compared case-insensitively, straight
    to a return value, e.g. {"h": "help", "help": "help"} for the discover
    command menu's help screen.

    "0" and "b" always raise Back(): the one way every menu here steps back
    to its caller.
    """
    if not items:
        raise BAMError(f"{title}: nothing to choose from")
    if len(items) == 1 and not allow_all:
        only = items[0]
        print_fn(f"{DIM_GRAY}{title}: only one - {label_fn(only)}{RESET}")
        return only

    match_key_fn = match_key_fn or label_fn
    extras = extras or {}
    page_size = 20
    pool = list(items)
    page = 0
    first_pass = True

    while True:
        if not exact or redraw or first_pass:
            if show_count:
                print_fn(f"\n{BOLD}{title}{RESET}  "
                         f"{DIM_GRAY}({len(pool)} item(s)){RESET}")
            else:
                print_fn(title)
            if exact:
                window, start = pool, 1
            else:
                window = pool[page * page_size:(page + 1) * page_size]
                start = page * page_size + 1
            for i, it in enumerate(window, start=start):
                print_fn(f"  {i:>{index_width}}) {label_fn(it)}")
            for line in (footer_lines or ()):
                print_fn(line)
        first_pass = False

        if prompt_text is not None:
            ptxt = prompt_text
        else:
            hints = ["number"]
            if allow_all:
                hints.append("'a' = " + all_label)
            if len(pool) > (page + 1) * page_size:
                hints.append("'m' = more")
            if page:
                hints.append("'p' = previous")
            hints += ["text = filter", "'b' = back"]
            ptxt = f"{DIM_GRAY}  {', '.join(hints)}{RESET}\n> "

        raw = input_fn(ptxt).strip()
        low = raw.lower()

        if low in ("0", "b"):
            raise Back()
        if low in extras:
            return extras[low]
        if not raw and default is not None:
            return default

        if exact:
            try:
                n = int(raw)
            except ValueError:
                n = -1
            if 1 <= n <= len(pool):
                return pool[n - 1]
            for it in pool:
                if match_key_fn(it).lower() == low:
                    return it
            print_fn(invalid_msg)
            continue

        if low == "a" and allow_all:
            return list(pool)
        if low == "m" and len(pool) > (page + 1) * page_size:
            page += 1
            continue
        if low == "p" and page:
            page -= 1
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(pool):
                return pool[idx]
            print_fn(f"{YELLOW}  out of range{RESET}")
            continue
        if raw:
            hits = match_items(items, raw, label_fn)
            if not hits:
                print_fn(f"{YELLOW}  no match for {raw!r}{RESET}")
                continue
            pool, page = hits, 0
            continue
        print_fn(f"{YELLOW}  pick a number{RESET}")


# --------------------------------------------------------------------------
# shared config file (~/.bluecat_discover.json)
#
# bluecat_discover.py and bluecat_export.py both remember settings, and
# optionally a saved username/password, in this one file - one file, so a
# saved password is never typed (or drifts) twice. Each caller keeps its own
# CONFIG dict and CONFIG_PATH override (used by tests); the functions below
# only know how to read/write a dict of known keys to a path.
# --------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"),
                                   ".bluecat_discover.json")


def config_path(override=""):
    """The shared config file's location; `override` (tests only) replaces
    the default `~/.bluecat_discover.json`."""
    return override or DEFAULT_CONFIG_PATH


def default_config():
    """A fresh dict with every key either caller may read or write.

    Returns a new dict (never a shared module-level default) on every call,
    so bluecat_discover.py's and bluecat_export.py's CONFIG dicts never end
    up mutating the same "favorites" list by accident.
    """
    return {"host": "", "format": "table", "view": "", "kind": "all",
            "favorites": [], "user": "", "password": ""}


def load_config(path, config):
    """Read remembered settings from `path` into `config`, in place.

    Unknown keys in the file are ignored; keys `config` already has that are
    missing from the file keep their current value. Any read/parse failure
    (missing file, garbage JSON, JSON that isn't an object) leaves `config`
    untouched.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return
        for key in config:
            if key in data:
                config[key] = data[key]
        if "favorites" in config and not isinstance(config["favorites"], list):
            config["favorites"] = []
    except (OSError, ValueError):
        pass


def save_config(path, config):
    """Atomically write `config` to `path` with 0600 perms (it may hold a
    saved password).

    Returns True on success, False when the write fails (e.g. read-only
    home). A temp file + os.replace means a mid-write crash can never leave
    a truncated config, and the 0600 mode guarantees a file holding a saved
    password is never world-readable even if it is first created here.
    """
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(config, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
