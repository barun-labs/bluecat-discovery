#!/usr/bin/env python3
"""bluecat.py - one entry point in front of bluecat_discover.py and
bluecat_export.py.

Both tools talk to the same read-only BAM API; the only real difference is
what happens to the result. bluecat_discover.py is screen-first: it prints a
table (or --json/--csv/--yaml) and treats writing a file as an afterthought.
bluecat_export.py is file-first: every scenario writes a CSV/JSON file and
never shows a table. Neither banner says so, which is why this launcher
exists - everything below it is unchanged and still runs standalone.

Routing rule (deliberately simple - no argparse subparser magic):
  bluecat.py                    no args, stdin is a terminal -> ask which
                                 tool you want, then hand off to it with no
                                 further arguments (that tool's own no-args
                                 behaviour - banner, wizard, scenario menu -
                                 takes over from there)
  bluecat.py                    no args, stdin is NOT a terminal (piped or
                                 redirected) -> print a usage note naming
                                 both tools and exit non-zero, no prompting
  bluecat.py discover ARGS...   forward ARGS... to bluecat_discover.main()
                                 verbatim (every discover flag/subcommand
                                 works exactly as it does run directly)
  bluecat.py export ARGS...     forward ARGS... to bluecat_export.main()
                                 verbatim (every export flag works exactly
                                 as it does run directly)
  bluecat.py -h | --help        prints this routing rule and exits 0
  anything else                 usage error to stderr, exit 2

This file only routes argv to one of the two real entry points; it never
parses discover/export flags itself, so -c/--collection, -y, --list, and
every discover subcommand keep working unchanged whether invoked directly
or through `bluecat.py discover ...` / `bluecat.py export ...`.
"""
import sys

import bluecat_discover
import bluecat_export
from bluecat_menu import Back, STDIN, choose

# Worded around intent, not filenames - the filename each routes to is
# still named in the row so the mapping stays discoverable.
TOOLS = (
    ("discover", "Look around (prints results)  -> bluecat_discover.py"),
    ("export", "Export to a file               -> bluecat_export.py"),
)

USAGE = (
    "usage: bluecat.py [discover|export] [ARGS...]\n"
    "\n"
    "Two read-only BlueCat BAM tools live here:\n"
    "  bluecat_discover.py  screen-first - prints a table (or --json/--csv/--yaml)\n"
    "  bluecat_export.py    file-first - every scenario writes a CSV/JSON file\n"
    "\n"
    "  bluecat.py discover ARGS...   forward ARGS to bluecat_discover.py\n"
    "  bluecat.py export ARGS...     forward ARGS to bluecat_export.py\n"
    "  bluecat.py                    on a terminal: ask which one you want\n"
    "\n"
    "Run 'bluecat.py discover --help' or 'bluecat.py export --help' for each\n"
    "tool's own flags."
)


def _prompt(text):
    """Prompt through the shared STDIN seam so tests can script an answer
    instead of touching a real terminal. One keystroke picks a tool - no
    Enter needed (STDIN.keypress() falls back to a full line read on its
    own whenever a keystroke can't safely be read: non-tty, no termios,
    a test double standing in for stdin, ...)."""
    return STDIN.keypress(text)


def _run_menu():
    """Ask which tool is wanted, then hand off with no further arguments -
    the chosen tool's own no-args behaviour takes over from here."""
    width = max(len(name) for name, _ in TOOLS)
    try:
        name, _label = choose(
            TOOLS, lambda t: f"{t[0]:<{width}}  {t[1]}",
            "BlueCat BAM - which tool do you want?",
            exact=True, match_key_fn=lambda t: t[0], show_count=False,
            index_width=1, prompt_text="Pick 1-2: ",
            invalid_msg="Pick 1, 2, 'discover', or 'export'.",
            input_fn=_prompt,
        )
    except Back:
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 130
    if name == "discover":
        return bluecat_discover.main([])
    return bluecat_export.main([])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)

    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    if argv:
        tool, rest = argv[0], argv[1:]
        if tool == "discover":
            return bluecat_discover.main(rest)
        if tool == "export":
            return bluecat_export.main(rest)
        print(f"bluecat.py: unknown tool {tool!r} - use 'discover' or "
              f"'export' (see --help).", file=sys.stderr)
        return 2

    if not STDIN.isatty():
        print(USAGE, file=sys.stderr)
        return 2

    return _run_menu()


if __name__ == "__main__":
    sys.exit(main())
