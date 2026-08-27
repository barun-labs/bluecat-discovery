#!/usr/bin/env python3
"""BlueCat Address Manager REST v2 - read-only discovery tool.

Finds objectIds (entity ids) and details for DNS views, external hosts,
forward/reverse zones, zones/records/networks by search, and network/IP
details (PTR/linked records, IP state, DHCP status, leases, ranges).

HARD RULE: this tool only ever issues GET requests to the BAM API. The single
exception is the session-create POST in authenticate() - required to obtain
the bearer token, and skipped entirely when $BAM_TOKEN is set. There are no
create/update/delete paths anywhere in this code; _request() refuses any
non-GET method with a ReadOnlyError.

Stdlib only. The HTTP transport (Client, paging, auth) lives in
bluecat_core, record shaping and the query functions in bluecat_query, and
the interactive chooser and prompting seam in bluecat_menu; this file is
the command-line and wizard adapter over them.

Usage examples:
  bluecat_discover.py views
  bluecat_discover.py hosts --view Authoritative
  bluecat_discover.py zones --view Authoritative --kind fwd
  bluecat_discover.py search 'example.com' --kind records
  bluecat_discover.py network 460648 --ips
  bluecat_discover.py network 192.0.2.0/24 --ips --ptrs
"""
import argparse
import getpass
import json
import os
import re
import shutil
import sys
import threading
import time

from bluecat_core import (
    BASE_PATH, PAGE_SIZE, BAMError, BAMTimeout, ReadOnlyError,
    Client as _CoreClient, authenticate, _request,
)
import bluecat_menu
from bluecat_menu import Back, STDIN, choose
from bluecat_query import (
    flatten, ref_label, zone_kind, is_reverse, build_filter, resolve_network,
    walk_zones, linked_records, SEARCH_KINDS, query_views, query_hosts,
    query_zones, query_search, query_network, query_zone, query_records,
    query_ip, query_summary,
)

NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")
RESET = "" if NO_COLOR else "\033[0m"
DIM = "" if NO_COLOR else "\033[90m"
BOLD = "" if NO_COLOR else "\033[1m"
GREEN = "" if NO_COLOR else "\033[32m"
YELLOW = "" if NO_COLOR else "\033[33m"
RED = "" if NO_COLOR else "\033[31m"
CYAN = "" if NO_COLOR else "\033[36m"
MAGENTA = "" if NO_COLOR else "\033[35m"
BRIGHT_BLUE = "" if NO_COLOR else "\033[94m"
BRIGHT_GREEN = "" if NO_COLOR else "\033[92m"
BRIGHT_CYAN = "" if NO_COLOR else "\033[96m"

# In --json/--csv mode, status and progress lines go to stderr so stdout
# carries only the payload (pipes and jq stay clean).
INFO = sys.stderr


# --------------------------------------------------------------------------
# terminal visuals - the overkill layer.
#
# VISUAL is True only when stdout is a real color terminal (never in tests,
# pipes, CI, or --json/--csv runs), so every animation below is a no-op
# elsewhere and machine output stays byte-for-byte clean.
# --------------------------------------------------------------------------
VISUAL = sys.stdout.isatty() and not NO_COLOR

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SMOOTH_BLOCKS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s):
    return len(_ANSI_RE.sub("", str(s)))


def _pad(s, width):
    """Left-justify to `width` counted in visible (ANSI-free) columns."""
    return s + " " * max(0, width - _visible_len(s))


def _gradient(pct):
    """ANSI 24-bit truecolor sweeping green → cyan → magenta as pct goes 0→1."""
    pct = max(0.0, min(1.0, pct))
    if pct < 0.5:
        t = pct * 2
        r, g, b = 0, int(210 + 45 * t), int(60 + 195 * t)
    else:
        t = (pct - 0.5) * 2
        r, g, b = int(210 * t), int(255 - 200 * t), 255
    return f"\033[38;2;{r};{g};{b}m"


def _format_eta(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def render_progress_bar(fetched, total, spin=0, elapsed=0.0):
    """Gradient-filled bar with smooth partial blocks, a rotating braille
    spinner, percentage, rate and ETA."""
    width = 28
    pct = fetched / total if total else 0.0
    filled = pct * width
    whole = int(filled)
    partial = int((filled - whole) * len(_SMOOTH_BLOCKS))
    bar = ""
    for i in range(width):
        if i < whole:
            bar += f"{_gradient((i + 1) / width)}█"
        elif i == whole and partial:
            bar += f"{_gradient(pct)}{_SMOOTH_BLOCKS[partial]}"
        else:
            bar += f"{DIM}░"
    rate = fetched / elapsed if elapsed > 0 else 0
    remaining = max(0, (total or 0) - fetched)
    eta = _format_eta(remaining / rate) if rate > 0 else "?"
    frame = SPINNER_FRAMES[spin % len(SPINNER_FRAMES)]
    return (f" {frame} {_gradient(pct)}[{bar}{RESET}]"
            f" {_gradient(pct)}{int(pct * 100):3d}%{RESET}"
            f" {DIM}{fetched}/{total}{RESET}"
            f" {DIM}· {rate:.0f} rec/s · ETA {eta}{RESET}")


def _cell_color(col, value):
    """Colorize a table cell by column semantics (no-op off a terminal)."""
    v = str(value)
    if not VISUAL or v == "":
        return v
    if col == "kind":
        code = {"fwd": GREEN, "rev": CYAN, "external-hosts": MAGENTA,
                "enum": YELLOW, "other": DIM}.get(v, "")
        return f"{code}{v}{RESET}" if code else v
    if col == "state":
        if v.startswith("DHCP"):
            return f"{YELLOW}{v}{RESET}"
        if v in ("STATIC", "GATEWAY"):
            return f"{GREEN}{v}{RESET}"
        return f"{BRIGHT_GREEN}{v}{RESET}"
    if col == "type":
        return f"{BRIGHT_BLUE}{v}{RESET}"
    if col == "objectId":
        return f"{BRIGHT_CYAN}{v}{RESET}"
    return v


class _Spinner:
    """Spins a braille frame next to a label on stderr while a call blocks."""

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        i = 0
        while not self._stop.wait(0.1):
            i += 1
            frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            print(f"\r{frame} {self.label}\033[K", end="", flush=True,
                  file=sys.stderr)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1.0)
        print("\r\033[K", end="", flush=True, file=sys.stderr)


def set_machine_mode(machine):
    """Route informational prints to stderr when output is machine-readable
    OR when stdout is not a terminal (piped to a file), so the payload/table
    on stdout stays clean."""
    global INFO
    INFO = sys.stderr if machine or not sys.stdout.isatty() else sys.stdout


# Back and choose() (the numbered-menu chooser used below and by the wizard)
# now live in bluecat_menu.py, imported at the top of this file, so this
# module and bluecat_export.py share one implementation.


# --------------------------------------------------------------------------
# HTTP layer (GET-only) - _request, authenticate(), and the paging/stall
# logic now live in _CoreClient (bluecat_core.Client, imported at the top of
# this file); every GET it can make funnels through
# bluecat_core._request("GET", ...). This subclass only re-adds the
# pre-refactor `quiet` toggle on collection()/all(), so callers below are
# unchanged and this module still renders its own gradient bar + spinner
# (bluecat_core.Client knows nothing about either - they're wired in here
# as an on_progress callback).
# --------------------------------------------------------------------------

class Client(_CoreClient):
    """Adds the pre-refactor `quiet` toggle on top of _CoreClient.

    Every GET this class can issue is inherited from _CoreClient (get(),
    _fetch_page_individually()), which funnels through
    bluecat_core._request("GET", ...); this subclass adds no new request
    path, only the on_progress wiring below.
    """

    def collection(self, path, filter_=None, fields=None, order_by=None,
                   page_size=PAGE_SIZE, quiet=False, timeout=30):
        on_progress = None
        start = time.monotonic()
        spin = 0
        if not quiet:
            def on_progress(fetched, total):
                nonlocal spin
                if not total:
                    return
                spin += 1
                if VISUAL:
                    bar = render_progress_bar(fetched, total, spin,
                                              time.monotonic() - start)
                    print(f"\r{bar}", end="", flush=True, file=INFO)
                else:
                    print(f"{DIM}{fetched}/{total} fetched…{RESET}",
                          end="\r", flush=True, file=INFO)
        records, total = super().collection(
            path, on_progress=on_progress, filter_=filter_, fields=fields,
            order_by=order_by, page_size=page_size, timeout=timeout)
        if not quiet:
            print(" " * 80, end="\r", file=INFO)
        return records, total

    def all(self, path, **kw):
        return self.collection(path, page_size=kw.pop("page_size", 1000),
                               quiet=True, **kw)[0]


# --------------------------------------------------------------------------
# classification helpers (pure, unit-tested)
#
# zone_kind, is_reverse, build_filter, and resolve_network now live in
# bluecat_query.py (query logic shared with bluecat_mcp.py) and are imported
# above; resolve_zone_id stays here because its interactive disambiguation
# (Back, _prompt) is CLI-only.
# --------------------------------------------------------------------------

def resolve_zone_id(client, target, interactive=False):
    """Resolve a zone by numeric objectId or by name; returns an int id.

    A name search that returns several zones lists the matches (name +
    objectId) and, when interactive, lets the user pick one by number;
    otherwise it raises with the list so the caller can rerun with an id.
    """
    target = str(target).strip()
    if target.isdigit():
        return int(target)
    if not target:
        raise BAMError("zone objectId or name required")
    # BAM stores the bare label in `name` ("bamzone") and the FQDN in
    # `absoluteName` ("bamzone.example.com"). People type the FQDN, so search that
    # first and only fall back to the label.
    hits = []
    for field in ("absoluteName", "name"):
        hits, _ = client.collection(
            f"{BASE_PATH}/zones",
            filter_=build_filter(field, "contains", target),
            page_size=1000, fields="id,name,absoluteName")
        if hits:
            break
    shown = [(int(z["id"]),
              z.get("absoluteName") or z.get("name") or f"#{z['id']}")
             for z in hits[:8] if z.get("id")]
    if not shown:
        raise BAMError(f"no zone matches {target!r}")
    if len(shown) == 1:
        return shown[0][0]
    if interactive:
        print(f"{DIM}{len(hits)} zones match {target!r}:{RESET}",
              file=sys.stderr)
        for i, (zid, zname) in enumerate(shown, 1):
            print(f"  {i}) {zname} (objectId {zid})", file=sys.stderr)
        while True:
            answer = _prompt(f"Pick 1-{len(shown)} (0 = back, "
                             f"Enter = cancel): ").strip()
            if answer == "0":
                raise Back()
            if not answer:
                raise BAMError(f"cancelled - {len(hits)} zones match "
                               f"{target!r}; rerun with one of the objectIds "
                               f"listed above")
            if answer.isdigit() and 1 <= int(answer) <= len(shown):
                return shown[int(answer) - 1][0]
            print("Pick a number from the list.", file=sys.stderr)
    raise BAMError(f"'{target}' matches {len(hits)} zones - pass the "
                   f"objectId instead: " + ", ".join(
                       f"{zname} #{zid}" for zid, zname in shown))


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def _yaml_scalar(value):
    """One YAML scalar: null/bools/numbers raw, everything else double-quoted."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')\
        .replace("\n", "\\n")
    return f'"{s}"'


def _yaml_dump(rows):
    """Minimal block-style YAML list of mappings (stdlib only)."""
    lines = []
    for row in rows:
        lines.append("-")
        for key, value in row.items():
            lines.append(f"  {key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


LAST_RESULT = None
LAST_COLUMNS = None


def _terminal_columns():
    """Width to fit a table into, or None to keep every column.

    None means "never drop a column" - returned whenever stdout isn't a
    human terminal (piped/redirected/captured), since column-fitting is a
    screen-width courtesy, not something a machine consumer should lose
    fidelity to. On a real terminal, shutil.get_terminal_size() reads
    $COLUMNS/$LINES first and falls back to a live tty query; its own
    fallback=(80, 24) covers the "size unknown" case (e.g. redirected but
    somehow still isatty()).
    """
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def _fit_columns(cols, widths, max_width):
    """Drop rightmost columns until the rendered table fits max_width.

    Never drops the first column, even if it alone would overflow - one
    column of real data beats an empty table. `widths` maps column name to
    its content width (no padding); the arithmetic below mirrors
    emit()'s grid_line() exactly: 1 border char each side, "─" * (w + 2)
    per column, one "┬"-style separator between each pair of columns.

    Returns (visible_cols, hidden_cols), both in original column order.
    """
    def rendered_width(cs):
        return 2 + sum(widths[c] + 2 for c in cs) + max(0, len(cs) - 1)

    visible = list(cols)
    while len(visible) > 1 and rendered_width(visible) > max_width:
        visible.pop()
    return visible, cols[len(visible):]


def emit(records, args, columns=None):
    """Print records as table (default), --json, --csv, or --yaml/--yml.

    The last non-empty result is remembered so the query loop can offer a
    CSV export of it without re-running the command. In machine modes an
    empty result prints nothing to stdout (the 'No records.' note goes to
    stderr) - pipe users should treat empty stdout as zero records.
    """
    global LAST_RESULT, LAST_COLUMNS
    if records:
        LAST_RESULT, LAST_COLUMNS = records, columns
    if not records:
        print(f"{YELLOW}No records.{RESET}", file=INFO)
        return
    if args.format == "json":
        print(json.dumps(records, indent=2))
        return
    rows = [flatten(r) for r in records]
    if args.format in ("yaml", "yml"):
        sys.stdout.write(_yaml_dump(rows))
        return
    if args.format == "csv":
        import csv
        import io
        buf = io.StringIO()
        fields = list(dict.fromkeys(k for r in rows for k in r))
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        sys.stdout.write(buf.getvalue())
        return
    cols = columns or list(dict.fromkeys(k for r in rows for k in r))
    cols = [c for c in cols if any(c in r for r in rows)]

    def cell(row, col):
        """A null field is a blank cell, not the word 'None'.

        flatten() keeps null-valued keys so the column set stays stable
        across rows; the table is where that null becomes empty space.
        """
        value = row.get(col)
        return "" if value is None else str(value)

    widths = {c: max(len(str(c)), *(len(cell(r, c)) for r in rows))
              for c in cols}

    term_width = _terminal_columns()
    if term_width is None:
        # Not a human terminal (piped/redirected) - a pipe into a file or
        # another tool must keep full fidelity, so nothing is ever dropped.
        visible_cols, hidden_cols = cols, []
    else:
        visible_cols, hidden_cols = _fit_columns(cols, widths, term_width)

    def grid_line(left, mid, right):
        return left + mid.join(
            "─" * (widths[c] + 2) for c in visible_cols) + right

    print(grid_line("┌", "┬", "┐"))
    print("│ " + " │ ".join(
        f"{BOLD}{BRIGHT_CYAN}{_pad(c, widths[c])}{RESET}"
        for c in visible_cols) + " │")
    print(grid_line("├", "┼", "┤"))
    for r in rows:
        print("│ " + " │ ".join(
            _pad(_cell_color(c, cell(r, c)), widths[c])
            for c in visible_cols) + " │")
    print(grid_line("└", "┴", "┘"))
    if hidden_cols:
        print(f"{DIM}{len(hidden_cols)} column(s) hidden to fit a "
              f"{term_width}-column terminal: {', '.join(hidden_cols)}. "
              f"See them with -f json (or --csv/--yaml).{RESET}", file=INFO)


EXPORT_FORMATS = ("none", "csv", "json", "yaml", "yml", "table")
OUTPUT_FORMATS = ("table", "json", "csv", "yaml", "yml")


def _stderr_print(s):
    print(s, file=sys.stderr)


def _choose_export():
    """Numbered menu of export formats; Enter picks none (no export),
    '0' goes back (also none)."""
    width = max(len(fmt) for fmt in EXPORT_FORMATS)
    try:
        return choose(
            EXPORT_FORMATS, lambda fmt: f"{fmt:<{width}}",
            "Export result - pick a format:",
            default="none", exact=True, match_key_fn=str,
            show_count=False, index_width=1,
            footer_lines=[f"  0) {'back':<{width}}"],
            prompt_text=f"Pick 1-{len(EXPORT_FORMATS)} "
                        f"(Enter = none, 0 = back): ",
            invalid_msg=f"Pick a number 1-{len(EXPORT_FORMATS)} or a format "
                        f"name.",
            print_fn=_stderr_print, input_fn=_keypress_prompt,
        )
    except Back:
        return "none"


def _choose_format(default="table"):
    """Numbered menu of output formats; Enter keeps the current default,
    '0' goes back to the command menu."""
    width = max(len(fmt) for fmt in OUTPUT_FORMATS)
    return choose(
        OUTPUT_FORMATS, lambda fmt: f"{fmt:<{width}}",
        "Output format - pick one:",
        default=default, exact=True, match_key_fn=str,
        show_count=False, index_width=1,
        footer_lines=[f"  0) {'back':<{width}}"],
        prompt_text=f"Pick 1-{len(OUTPUT_FORMATS)} "
                    f"(Enter = {default}, 0 = back): ",
        invalid_msg=f"Pick a number 1-{len(OUTPUT_FORMATS)} or a format "
                    f"name.",
        print_fn=_stderr_print, input_fn=_keypress_prompt,
    )


def export_result(command, fmt):
    """Write the last emitted result as csv/json/yaml/table to a file."""
    import io
    from contextlib import redirect_stdout
    from datetime import datetime

    ext = {"csv": "csv", "json": "json", "yaml": "yaml", "yml": "yaml",
           "table": "txt"}[fmt]
    fname = (f"bluecat-{command}-"
             f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.{ext}")
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit(LAST_RESULT, argparse.Namespace(format=fmt), LAST_COLUMNS)
    content = buf.getvalue()
    if fmt == "table":
        # The grid renderer colors the header with ANSI codes; the exported
        # file must be plain text.
        import re
        content = re.sub(r"\x1b\[[0-9;]*m", "", content)
    try:
        with open(fname, "w", newline="") as fh:
            fh.write(content)
    except OSError as e:
        print(f"{RED}Could not write {fname}: {e}{RESET}", file=sys.stderr)
        return
    print(f"{DIM}Saved to {fname}{RESET}", file=sys.stderr)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_views(client, args):
    """objectIds for configurations and their DNS views."""
    emit(query_views(client), args,
         ["configurationId", "objectId", "view", "type", "configuration"])


def cmd_hosts(client, args):
    """External host records per DNS view (via each view's ExternalHostsZone)."""
    def note(view, zone, records):
        print(f"{DIM}{view.get('name')}: ExternalHostsZone #{zone['id']} "
              f"= {len(records)} external host(s){RESET}", file=INFO)

    out = query_hosts(client, args.view, on_zone=note)
    emit(out, args, ["view", "viewObjectId", "zoneObjectId", "objectId",
                     "name", "type", "ttl"])


def cmd_zones(client, args):
    """All zones per view, classified fwd/rev/ext, with objectIds."""
    out = query_zones(client, args.view, args.kind)
    emit(out, args, ["view", "viewObjectId", "objectId", "kind", "name",
                     "deployable", "signed"])


def cmd_search(client, args):
    """Substring search over zones, records, networks, addresses, hosts."""
    if args.kind == "all":
        # One query, every kind at once - merged with a 'kind' column.
        result = query_search(client, args.query, "all")
        total = sum(v or 0 for v in result["totals"].values())
        print(f"{DIM}{total or 0} match(es) for {args.query!r} across all "
              f"kinds{RESET}", file=INFO)
        emit(result["matches"], args, ["kind", "objectId", "type", "name"])
        return
    result = query_search(client, args.query, args.kind)
    print(f"{DIM}{result['total'] or 0} match(es) for {args.query!r} in "
          f"{args.kind}{RESET}", file=INFO)
    emit(result["matches"], args,
         ["objectId", "type", "name", "detail", "state", "ttl"])


def cmd_network(client, args):
    """Network details: meta, DHCP ranges, reverse-DNS roles, and IPs."""
    sections = query_network(client, args.target, ips=args.ips or args.all,
                             ptrs=args.ptrs, dhcp=args.dhcp or args.all,
                             roles=args.roles or args.all, limit=args.limit)
    row = sections["network"]

    if args.format == "json":
        print(json.dumps(sections, indent=2))
        return
    emit([row], args, ["objectId", "type", "name", "range", "gateway",
                       "location", "defaultView"])
    if "dhcpRanges" in sections:
        ranges = sections["dhcpRanges"]
        if ranges:
            print(f"\n{BOLD}DHCP ranges{RESET} ({len(ranges)})", file=INFO)
            emit(ranges, args, ["id", "type", "name", "range"])
        else:
            print(f"\n{DIM}No DHCP ranges on this network.{RESET}",
                  file=INFO)
    if "deploymentRoles" in sections:
        roles = sections["deploymentRoles"]
        if roles:
            print(f"\n{BOLD}Deployment roles / reverse-DNS zones{RESET} "
                  f"({len(roles)})", file=INFO)
            emit(roles, args, ["id", "type", "name", "absoluteName"])
        else:
            print(f"\n{DIM}No deployment roles (reverse-DNS zones) on this "
                  f"network.{RESET}", file=INFO)
    if "addresses" in sections:
        addrs = sections["addresses"]
        print(f"\n{BOLD}IP addresses{RESET} ({len(addrs)} shown of "
              f"{sections.get('addressesTotal') or len(addrs)})", file=INFO)
        emit(addrs, args, ["objectId", "address", "state", "name",
                           "macAddress", "location", "ptr", "linked"])


def cmd_zone(client, args):
    """One zone by id + its resource records."""
    result = query_zone(client, args.zone_id, records=args.records or args.all)
    zone = result["zone"]
    records = result.get("records")
    if records is not None:
        if args.format == "json":
            # One document, not two - keeps `jq` happy on stdout.
            print(json.dumps({"zone": zone, "records": records}, indent=2))
            return
        if args.format in ("yaml", "yml", "csv"):
            # Single flat list so csv/yaml stay one document with one header.
            emit([zone] + list(records), args,
                 ["id", "type", "name", "absoluteName", "deployable",
                  "signed", "view", "configuration", "ttl", "rdata",
                  "addresses", "comment"])
            return
    emit([zone], args, ["id", "type", "name", "absoluteName", "deployable",
                        "signed", "view", "configuration"])
    if records is not None:
        print(f"\n{DIM}{result.get('recordCount') or 0} record(s) in "
              f"zone{RESET}", file=INFO)
        emit(records, args, ["id", "type", "absoluteName", "ttl", "rdata",
                             "addresses"])


def cmd_records(client, args):
    """Resource records for one zone or an entire view, with a zone column."""
    # args.zone was already resolved to an objectId by main().
    out = query_records(client, getattr(args, "zone", None), args.view)
    emit(out, args, ["zone", "objectId", "type", "name", "ttl", "rdata",
                     "addresses", "comment"])


def cmd_ip(client, args):
    """Look up one IP address: state, name, and linked host records."""
    result = query_ip(client, args.address)
    row, linked = result["address"], result["linked"]
    if args.format == "json":
        # One document, not two - keeps `jq` happy on stdout.
        print(json.dumps({"address": row, "linked": linked}, indent=2))
        return
    if args.format in ("yaml", "yml", "csv"):
        # Single flat list so csv/yaml stay one document with one header.
        emit([row] + linked, args, ["objectId", "address", "state", "name",
                                    "macAddress", "type"])
        return
    emit([row], args, ["objectId", "address", "state", "name", "macAddress"])
    if linked:
        print(f"\n{DIM}{len(linked)} record(s) at this IP:{RESET}", file=INFO)
        emit(linked, args, ["objectId", "type", "name"])


def cmd_summary(client, args):
    """Quick overview: counts of the main object types."""
    emit(query_summary(client), args, ["item", "count"])


COMMANDS = {
    "views": cmd_views,
    "hosts": cmd_hosts,
    "zones": cmd_zones,
    "search": cmd_search,
    "network": cmd_network,
    "zone": cmd_zone,
    "records": cmd_records,
    "ip": cmd_ip,
    "summary": cmd_summary,
}


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Read-only BlueCat Address Manager discovery: objectIds "
                    "for DNS views, external hosts, zones, and network/IP "
                    "details. GET requests only (auth POST excepted).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Commands:\n"
               "  views                 configurations + DNS views (objectId)\n"
               "  hosts [--view NAME]   external host records per DNS view\n"
               "  zones [--view NAME] [--kind fwd|rev|ext|all]\n"
               "  search QUERY --kind zones|records|networks|addresses|hosts\n"
               "  network TARGET [--ips] [--ptrs] [--dhcp] [--roles] [--all]\n"
               "  zone ID [--records]\n"
               "  records [ZONE] [--view NAME]\n"
               "  ip ADDRESS\n"
               "  summary\n\n"
               "Examples:\n"
               "  %(prog)s                       # no command → prompts for\n"
               "                                 # username/password, runs views\n"
               "  %(prog)s views\n"
               "  %(prog)s hosts --view Authoritative\n"
               "  %(prog)s zones --kind rev\n"
               "  %(prog)s search example.com --kind records\n"
               "  %(prog)s network 192.0.2.0/24 --ips --ptrs --json\n")
    p.add_argument("-H", "--host", default=os.environ.get("BAM_HOST", ""))
    p.add_argument("-u", "--user", default=os.environ.get("BAM_USER", ""),
                   help="username (default: prompt, or $BAM_USER)")
    p.add_argument("--password", default=os.environ.get("BAM_PASSWORD", ""),
                   help="password (default: prompt, or $BAM_PASSWORD)")
    p.add_argument("--token", default=os.environ.get("BAM_TOKEN", ""),
                   help="existing basicAuthenticationCredentials; skips login")
    p.add_argument("-f", "--format",
                   choices=("table", "json", "csv", "yaml", "yml"),
                   default="table")
    p.add_argument("--verify", action="store_true",
                   help="verify TLS certificate (off by default: self-signed)")
    p.add_argument("--forget-credentials", action="store_true",
                   help="remove saved username/password from the config")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("-o", "--output",
                   help="write the result to this file instead of stdout")
    p.add_argument("--json", action="store_true",
                   help="shortcut for --format json")
    jparent = argparse.ArgumentParser(add_help=False)
    jparent.add_argument("--json", action="store_true",
                         help="shortcut for --format json")
    jparent.add_argument("--page-size", type=int, default=PAGE_SIZE,
                         help="collection page size")
    jparent.add_argument("-o", "--output", default=argparse.SUPPRESS,
                         help="write the result to this file instead of stdout")
    jparent.add_argument("-f", "--format",
                         choices=("table", "json", "csv", "yaml", "yml"),
                         default=argparse.SUPPRESS)
    # Running with no command runs `views` - the shortest possible invocation:
    # python3 bluecat_discover.py  →  prompts, then shows the DNS views.
    sub = p.add_subparsers(dest="command")
    p.set_defaults(command="views")
    # On the bare-invocation path no subparser runs, so its attributes would
    # be missing from the namespace - but the commands read them (e.g.
    # args.all in cmd_network/cmd_zone). Mirror the subparser defaults here;
    # an explicit subcommand overrides them with the same values.
    p.set_defaults(all=False, ips=False, ptrs=False, dhcp=False, roles=False,
                   records=False, limit=10000)

    sp = sub.add_parser("views", parents=[jparent],
                        help="configurations + DNS views")

    sp = sub.add_parser("hosts", parents=[jparent],
                        help="external host records per view")
    sp.add_argument("--view")

    sp = sub.add_parser("zones", parents=[jparent],
                        help="zones per view, classified")
    sp.add_argument("--view")
    sp.add_argument("--kind", choices=("all", "fwd", "rev", "ext", "other"),
                    default="all")

    sp = sub.add_parser("search", parents=[jparent],
                        help="substring search")
    sp.add_argument("query")
    sp.add_argument("--kind", choices=tuple(SEARCH_KINDS) + ("all",),
                    default="zones")

    sp = sub.add_parser("network", parents=[jparent],
                        help="network details by id/range/name")
    sp.add_argument("target")
    sp.add_argument("--limit", type=int, default=10000,
                    help="max addresses to enumerate (capped at 10000 per "
                         "request)")
    sp.add_argument("--ips", action="store_true")
    sp.add_argument("--ptrs", action="store_true", help="linked records per IP")
    sp.add_argument("--dhcp", action="store_true", help="DHCP ranges")
    sp.add_argument("--roles", action="store_true",
                    help="deployment roles (reverse-DNS zones)")
    sp.add_argument("--all", action="store_true",
                    help="short for --ips --ptrs --dhcp --roles")

    sp = sub.add_parser("zone", parents=[jparent],
                        help="zone by id/name + records")
    sp.add_argument("zone_id", help="zone objectId or name (searched)")
    sp.add_argument("--records", action="store_true")
    sp.add_argument("--all", action="store_true")

    sp = sub.add_parser("records", parents=[jparent],
                        help="resource records for one zone or a whole view")
    sp.add_argument("zone", nargs="?", default=None,
                    help="zone objectId or name (omit + --view for a view)")
    sp.add_argument("--view")

    sp = sub.add_parser("ip", parents=[jparent],
                        help="look up an IP address")
    sp.add_argument("address", help="IP address, e.g. 192.0.2.5")

    sp = sub.add_parser("summary", parents=[jparent],
                        help="quick overview counts")
    return p.parse_args(argv)


def _prompt(label):
    """Read one line from stdin with the prompt on stderr (keeps stdout clean
    for --json/--csv output piped to jq or files). Raises EOFError on EOF
    (Ctrl-D / closed stdin) so callers cancel instead of looping forever."""
    sys.stderr.write(label)
    sys.stderr.flush()
    return STDIN.readline()


def _keypress_prompt(label):
    """Like `_prompt()`, but for the y/n questions and short digit menus
    that no longer need Enter: reads one keystroke via the STDIN seam
    (STDIN.keypress() falls back to `_prompt()`'s own line-reading whenever
    a keystroke can't safely be read - non-tty, no termios, ...), with the
    prompt kept on stderr so stdout stays clean for --json/--csv output."""
    return STDIN.keypress(label, stream=sys.stderr)


CREDS_PROMPTED = False    # a username/password prompt was shown
CREDS_USED_SAVED = False  # login used credentials saved in the config


def resolve_credentials(args, force_prompt=False):
    """Return (user, password); prompt interactively when not supplied.

    Prompting only happens when stdin is a terminal - a pipe or CI run gets
    a clear error instead of hanging. The password is read with getpass (no
    echo), with the prompt on stderr. On a tty, credentials saved in the
    config are offered first (unless CLI/env values were given - those
    always win). force_prompt skips the saved-credentials question (used
    after a failed login to demand fresh input).
    """
    global CREDS_PROMPTED, CREDS_USED_SAVED
    CREDS_PROMPTED = CREDS_USED_SAVED = False
    user = args.user
    password = args.password
    if STDIN.isatty():
        saved_user = CONFIG.get("user") or ""
        saved_pass = CONFIG.get("password") or ""
        if not force_prompt and not user and not password \
                and saved_user and saved_pass:
            if _ask_yn(f"Use saved credentials for {saved_user}",
                       default=True):
                user, password = saved_user, saved_pass
                CREDS_USED_SAVED = True
        if not user:
            while not user:
                user = _prompt("Username: ")
            CREDS_PROMPTED = True
        if not password:
            password = STDIN.secret("Password: ", stream=sys.stderr)
            CREDS_PROMPTED = True
    else:
        if not user:
            raise BAMError("username required (--user or $BAM_USER)")
        if not password:
            raise BAMError("password required (--password or $BAM_PASSWORD)")
    return user, password


def _maybe_save_credentials(user, password):
    """Offer to persist the just-entered credentials (plain text!)."""
    if not _ask_yn(f"Save username/password to {_config_path()} "
                   f"(plain text, chmod 600) for next time"):
        return
    CONFIG["user"], CONFIG["password"] = user, password
    if save_config():
        print(f"{DIM}Credentials saved to {_config_path()} (chmod 600).{RESET}",
              file=sys.stderr)
        return
    CONFIG["user"] = CONFIG["password"] = ""
    print(f"{RED}Could not save credentials to {_config_path()} "
          f"(read-only? - they were NOT saved).{RESET}", file=sys.stderr)


# --------------------------------------------------------------------------
# interactive wizard (guided mode for a terminal)
# --------------------------------------------------------------------------

MENU = [
    ("views", "show DNS configurations and views"),
    ("hosts", "list external host records per view"),
    ("zones", "list forward/reverse zones per view"),
    ("search", "find things by name (e.g. type: example)"),
    ("network", "inspect a network and its IPs (e.g. 192.0.2.0/24)"),
    ("zone", "inspect one zone + records (e.g. example.com)"),
    ("ip", "look up an IP address (e.g. 192.0.2.5)"),
    ("summary", "quick overview: zones, hosts, networks, views"),
    ("records", "dump resource records for a zone or a whole view"),
    ("favorites", "run a saved query"),
    ("repeat", "run the last query again"),
]
MENU_NAMES = [name for name, _ in MENU]

CONFIG_PATH = ""  # overridable; resolved lazily so a changed $HOME wins


def _config_path():
    """Config file location; tests may override via CONFIG_PATH.

    Shared with bluecat_export.py (bluecat_menu.config_path): one file, so a
    saved password is never typed (or drifts) twice between the two tools.
    """
    return bluecat_menu.config_path(CONFIG_PATH)
CONFIG = bluecat_menu.default_config()

QUERY_ATTRS = ("command", "zone_id", "zone", "target", "query", "address",
               "kind", "view", "ips", "ptrs", "dhcp", "roles",
               "records", "all")
LAST_QUERY = None  # the last run's wizard values, for the repeat option


def load_config():
    """Read remembered settings/favorites from the config file."""
    bluecat_menu.load_config(_config_path(), CONFIG)


def save_config():
    """Atomically write the config with 0600 perms (it may hold credentials).

    Returns True on success, False when the write fails (e.g. read-only home).
    """
    return bluecat_menu.save_config(_config_path(), CONFIG)

BANNER = (
    "╭───────────────────────────────────────────╮\n"
    "│                                           │\n"
    "│   /\\_/\\        BlueCat Discovery          │\n"
    "│  ( o.o )       DNS views · zones · IPs    │\n"
    "│   > ^ <        read-only explorer (GET)   │\n"
    "│                                           │\n"
    "╰───────────────────────────────────────────╯"
)

HELP_TEXT = (
    "Here's what each command does:\n"
    "\n"
    "  views     List the DNS configurations and their views.\n"
    "  hosts     List the external host records in each DNS view.\n"
    "  zones     List forward and reverse zones per view (e.g. example.com).\n"
    "  records   Dump the resource records of one zone or a whole view.\n"
    "  search    Find zones, records, networks, or addresses by name\n"
    "            (kind 'all' searches everything at once).\n"
    "  network   Look at one network: details, DHCP ranges, and IPs.\n"
    "  zone      Look at one zone and its resource records.\n"
    "  ip        Look up an IP address and what points at it.\n"
    "  summary   Quick overview: how many zones, hosts, networks, views.\n"
    "  favorites Run a query you saved earlier.\n"
    "  repeat    Run the last query again with the same settings.\n"
    "\n"
    "After a result you can export it (CSV/JSON/YAML/table), save it as a\n"
    "favorite, and keep running queries until you choose to quit. Your\n"
    "last host/format/view/kind are remembered between sessions. If a\n"
    "query fails you are offered the menu again instead of being thrown\n"
    "out. You can also save your username/password (plain text, chmod 600)\n"
    "in the config file - '--forget-credentials' removes them again.\n"
    "\n"
    "Press Enter to return to the main menu."
)

KAOMOJI = "(=^･ω･^=)"


def print_banner():
    if not VISUAL:
        print(BANNER, file=sys.stderr)
        return
    # Typewriter the banner in, blinking the cat's eyes on the way past.
    for line in BANNER.splitlines():
        if "( o.o )" in line:
            for eyes in ("( o.o )", "( -.- )", "( o.o )"):
                sys.stderr.write("\r" + line.replace("( o.o )", eyes) + "\033[K")
                sys.stderr.flush()
                time.sleep(0.12)
            sys.stderr.write("\r" + line + "\n")
            sys.stderr.flush()
            continue
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        time.sleep(0.025)


def print_help():
    print(HELP_TEXT, file=sys.stderr)


def print_outro():
    save_config()
    if not VISUAL:
        print(f"\n{DIM}Bye! Thanks for using BlueCat Discovery {KAOMOJI}{RESET}",
              file=sys.stderr)
        return
    for eyes in ("(=^･ω･^=)", "(=^-ω-^=)", "(=^･ω･^=)"):
        sys.stderr.write(f"\r{DIM}Bye! Thanks for using BlueCat Discovery "
                         f"{eyes}{RESET}\033[K")
        sys.stderr.flush()
        time.sleep(0.18)
    sys.stderr.write(f"\r{DIM}Bye! Thanks for using BlueCat Discovery "
                     f"{KAOMOJI}{RESET}\033[K\n")
    sys.stderr.flush()


def _ask(label, default=None, choices=None, convert=None, required=False,
         back=False):
    """One interactive question with a bracketed default; retries on bad
    input. Returns the answer, or `default` when Enter is pressed. With
    back=True, entering '0' raises Back so the caller can return to the
    previous menu."""
    suffix = ""
    if choices:
        hint = "/".join(choices)
        suffix = f" [{default if default is not None else hint}]"
    elif default is not None:
        suffix = f" [{default}]"
    if back:
        suffix += " (0 = back)"
    while True:
        answer = _prompt(f"{label}{suffix}: ").strip()
        if back and answer == "0":
            raise Back()
        if not answer:
            if default is not None:
                return default
            if required:
                print("Required - please enter a value.", file=sys.stderr)
                continue
            return ""
        if choices:
            if answer in choices:
                return answer
            print(f"Pick one of: {', '.join(choices)}", file=sys.stderr)
            continue
        if convert is not None:
            try:
                return convert(answer)
            except ValueError:
                print("Invalid value - try again.", file=sys.stderr)
                continue
        return answer


def _ask_yn(label, default=False, back=False):
    """Yes/no question; one keystroke answers it, no Enter needed (Enter
    still works too, taking the default). Retries on junk input. With
    back=True, entering '0' raises Back."""
    suffix = " [Y/n]" if default else " [y/N]"
    if back:
        suffix += " (0 = back)"
    while True:
        answer = _keypress_prompt(f"{label}{suffix}: ").strip().lower()
        if back and answer == "0":
            raise Back()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.", file=sys.stderr)


# The `network` wizard question used to be four consecutive y/n prompts
# (--ips, --ptrs, --dhcp, --roles); NETWORK_FLAGS is the single multi-select
# question's letter -> attribute-name mapping instead, in the order shown.
NETWORK_FLAGS = (("i", "ips"), ("p", "ptrs"), ("d", "dhcp"), ("r", "roles"))


def _parse_network_flags(raw):
    """Parse one answer to the network include-flags question.

    Accepts any combination of the NETWORK_FLAGS letters, case-insensitive,
    optionally separated by commas or spaces ("ip", "i p", and "i,p" all
    mean ips+ptrs); 'a' for all four; or a blank answer for none. Returns a
    {"ips": bool, "ptrs": bool, "dhcp": bool, "roles": bool} dict, or None if
    `raw` contains a character that isn't one of those - the caller
    re-prompts on None.
    """
    letters = [c for c in raw.strip().lower() if c not in (",", " ")]
    valid = {letter for letter, _ in NETWORK_FLAGS} | {"a"}
    if any(c not in valid for c in letters):
        return None
    if "a" in letters:
        return {name: True for _, name in NETWORK_FLAGS}
    chosen = set(letters)
    return {name: (letter in chosen) for letter, name in NETWORK_FLAGS}


def _ask_network_flags(back=False):
    """One multi-select question replacing four consecutive y/n prompts.
    With back=True, entering '0' raises Back."""
    label = "Include: [a]ll [i]ps [p]trs [d]hcp [r]oles (Enter = none)"
    while True:
        answer = _prompt(f"{label}: ")
        if back and answer.strip() == "0":
            raise Back()
        result = _parse_network_flags(answer)
        if result is not None:
            return result
        print("Please answer with any of: a, i, p, d, r (or Enter for "
              "none).", file=sys.stderr)


_COMMAND_EXTRAS = {
    "quit": "quit", "exit": "quit", "q": "quit",
    "help": "help", "h": "help",
    "repeat": "repeat", "r": "repeat",
    "favorites": "favorites", "f": "favorites",
}

# A keypress can't tell "1" from "10", so only the first 9 MENU entries get
# a number; the rest (favorites, repeat) are reached by the letter keys in
# _COMMAND_EXTRAS above instead (typing their full name still works too).
_MENU_NUMBERED = MENU[:9]
_MENU_LETTERED = MENU[9:]


def _choose_command():
    """Explained menu of commands plus help/quit; Enter = views."""
    width = max(len(name) for name, _ in MENU)
    menu_by_name = dict(MENU)
    footer = [f"  {letter}) {name:<{width}}  {menu_by_name[name]}"
              for letter, name in zip("fr", (n for n, _ in _MENU_LETTERED))]
    footer += [
        f"  h) {'help':<{width}}  explain every command",
        f"  0) {'quit':<{width}}  leave the script",
        f"{DIM}Tip: press Enter to accept the default shown in "
        f"brackets.{RESET}",
    ]
    while True:
        try:
            result = choose(
                _MENU_NUMBERED, lambda item: f"{item[0]:<{width}}  {item[1]}",
                "BlueCat discovery - what do you want to do?",
                default="views", extras=_COMMAND_EXTRAS, exact=True,
                match_key_fn=lambda item: item[0],
                show_count=False, index_width=2, footer_lines=footer,
                prompt_text="Command [1]: ",
                invalid_msg=f"Pick a number 1-{len(_MENU_NUMBERED)}, f for "
                            f"favorites, r for repeat, h for help, or 0 to "
                            f"quit.",
                print_fn=_stderr_print, input_fn=_keypress_prompt,
            )
        except Back:
            return "quit"
        if isinstance(result, tuple):    # a MENU entry: (name, description)
            result = result[0]
        if result == "help":
            print_help()
            _prompt("Press Enter to return to the menu: ")
            continue
        return result


def run_wizard(args, argv, ask_host=True):
    """Ask interactively for anything not already given on the command line.

    Only called when stdin is a terminal. Values supplied via flags or the
    environment are never re-asked - command-line input wins. With
    ask_host=False (a repeat round of the query loop) the BAM host is kept
    from the established session instead of being asked again.
    """
    global LAST_QUERY

    def given(name):
        """True if the flag was passed on the CLI - including '--view=X'
        and short-option forms like '-fjson'."""
        for a in argv:
            if a == name or a.startswith(name + "="):
                return True
            if name == "-f" and a.startswith("-f") \
                    and not a.startswith("--"):
                return True
        return False

    cmd_on_cli = any(a in MENU_NAMES for a in argv)

    if ask_host and not (given("-H") or given("--host")):
        # No shipped default: only an env/-H value or a remembered host from
        # a previous session is offered, and only when one exists. A blank
        # Enter with neither leaves args.host empty; main() then refuses to
        # proceed with a clear error instead of silently connecting nowhere.
        default_host = CONFIG["host"] or args.host or None
        args.host = _ask("BAM's Management IP or host", default=default_host)

    while True:
        try:
            if not cmd_on_cli:
                args.command = _choose_command()
            if args.command == "quit":
                return args      # picked quit at the menu - no more questions
            while args.command in ("repeat", "favorites"):
                if args.command == "repeat":
                    if LAST_QUERY is None:
                        print(f"{YELLOW}No previous query yet - run one "
                              f"first.{RESET}", file=sys.stderr)
                        args.command = _choose_command()
                        continue
                    for key, value in LAST_QUERY.items():
                        setattr(args, key, value)
                    return args  # reuse the last settings, no more questions
                favs = CONFIG["favorites"]
                if not favs:
                    print(f"{YELLOW}No favorites yet - after a result answer "
                          f"'y' to 'Save this query as a "
                          f"favorite?'.{RESET}", file=sys.stderr)
                    args.command = _choose_command()
                    continue
                print("Favorites - pick one:", file=sys.stderr)
                for i, fav in enumerate(favs, 1):
                    print(f"  {i}) {fav.get('name', '?')}", file=sys.stderr)
                answer = _prompt(f"Pick 1-{len(favs)} "
                                 f"(0 = back, Enter = back to menu): ").strip()
                if not answer or answer == "0":
                    raise Back()
                try:
                    n = int(answer)
                except ValueError:
                    n = -1
                if 1 <= n <= len(favs):
                    for key, value in favs[n - 1].items():
                        if key != "name":
                            setattr(args, key, value)
                    return args
                print("Pick a number from the list.", file=sys.stderr)

            if args.command == "hosts":
                if not given("--view"):
                    args.view = _ask("View (Enter for all views)",
                                     default=CONFIG["view"] or None,
                                     back=True) or None
            elif args.command == "zones":
                if not given("--view"):
                    args.view = _ask("View (Enter for all views)",
                                     default=CONFIG["view"] or None,
                                     back=True) or None
                if not given("--kind"):
                    args.kind = _ask("Kind",
                                     default=getattr(args, "kind",
                                                     CONFIG["kind"] or "all"),
                                     choices=("all", "fwd", "rev", "ext",
                                              "other"), back=True)
            elif args.command == "search":
                if not cmd_on_cli:
                    args.query = _ask("Search query (e.g. example.com)",
                                      required=True, back=True)
                if not given("--kind"):
                    args.kind = _ask("Kind",
                                     default=getattr(args, "kind",
                                                     CONFIG["kind"] or "zones"),
                                     choices=tuple(SEARCH_KINDS) + ("all",),
                                     back=True)
            elif args.command == "network":
                if not cmd_on_cli:
                    args.target = _ask("Network (id, range, or name, "
                                       "e.g. 192.0.2.0/24)",
                                       required=True, back=True)
                if not given("--all"):
                    missing = [name for _, name in NETWORK_FLAGS
                              if not given(f"--{name}")]
                    if missing:
                        answers = _ask_network_flags(back=True)
                        for name in missing:
                            setattr(args, name, answers[name])
            elif args.command == "zone":
                if not cmd_on_cli:
                    args.zone_id = _ask("Zone (objectId or name, "
                                        "e.g. example.com)",
                                        required=True, back=True)
                if not given("--records") and not given("--all"):
                    args.records = _ask_yn("Include resource records",
                                           back=True)
            elif args.command == "records":
                if not cmd_on_cli:
                    args.zone = _ask("Zone (objectId or name; "
                                     "Enter = dump a whole view)",
                                     back=True) or None
                if not args.zone and not given("--view"):
                    args.view = _ask("View",
                                     default=CONFIG["view"] or None,
                                     back=True) or None
            elif args.command == "ip":
                if not cmd_on_cli:
                    args.address = _ask("IP address (e.g. 192.0.2.5)",
                                        required=True, back=True)

            if not (given("-f") or given("--format") or given("--json")):
                args.format = _choose_format(default=CONFIG["format"]
                                             or args.format)
            break
        except Back:
            if cmd_on_cli:
                # The command came from the CLI, so no command menu was shown
                # and there is no previous menu to return to - treat as quit.
                args.command = "quit"
                return args
            continue        # loop re-asks the command menu

    # Remember the chosen settings for 'repeat' and for next session.
    # False flags are skipped - the fresh namespace's set_defaults restores
    # them, keeping saved queries small.
    LAST_QUERY = {key: getattr(args, key, None)
                  for key in QUERY_ATTRS
                  if getattr(args, key, None) not in (None, False)}
    CONFIG["host"] = args.host
    CONFIG["format"] = args.format
    CONFIG["view"] = getattr(args, "view", "") or ""
    CONFIG["kind"] = getattr(args, "kind", "") or ""
    return args


def _run_command(client, args):
    """Run the selected command; with -o/--output FILE, the stdout payload
    (table/json/csv/yaml) is written to FILE instead, and informational notes
    are forced to stderr so the file holds only the payload."""
    fn = COMMANDS[args.command]
    output = getattr(args, "output", None)
    if not output:
        fn(client, args)
        return
    import io
    from contextlib import redirect_stdout

    global INFO
    saved_info = INFO
    buf = io.StringIO()
    try:
        INFO = sys.stderr
        with redirect_stdout(buf):
            fn(client, args)
    finally:
        INFO = saved_info
    content = _ANSI_RE.sub("", buf.getvalue())  # strip any color/table codes
    try:
        with open(output, "w", newline="", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as e:
        raise BAMError(f"Could not write {output}: {e}")
    print(f"{DIM}Saved to {output}{RESET}", file=sys.stderr)


def _warn_cli_secrets(argv):
    """Warn when a secret was passed on the command line (visible in `ps`
    and shell history) instead of via the corresponding env var."""
    if not argv:
        return
    for flag, env in (("--password", "BAM_PASSWORD"),
                      ("--token", "BAM_TOKEN")):
        if any(a == flag or a.startswith(flag + "=") for a in argv):
            print(f"{YELLOW}Warning: {flag} on the command line is visible "
                  f"in 'ps' and shell history - prefer ${env}.{RESET}",
                  file=sys.stderr)


def _reask_menu(args):
    """Re-open the command menu on the established session (host unchanged)."""
    host, fmt, psize, lim = (args.host, args.format,
                             args.page_size, args.limit)
    args = parse_args([])
    args.host, args.format = host, fmt
    args.page_size, args.limit = psize, lim
    run_wizard(args, [], ask_host=False)
    return args


def main(argv=None):
    global LAST_RESULT, LAST_COLUMNS
    load_config()
    argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(argv)
    _warn_cli_secrets(argv)
    if getattr(args, "json", False):
        args.format = "json"
    if getattr(args, "forget_credentials", False):
        had = bool(CONFIG.get("user") or CONFIG.get("password"))
        CONFIG["user"] = CONFIG["password"] = ""
        save_config()
        print(f"{DIM}{'Removed' if had else 'No'} saved credentials in "
              f"{_config_path()}.{RESET}", file=sys.stderr)
        if len(argv) == 1:          # maintenance run - nothing else to do
            return 0
    client = None
    try:
        interactive = STDIN.isatty()
        if interactive:
            print_banner()
            run_wizard(args, argv)
            if args.command == "quit":
                print_outro()
                return 0
        else:
            # Non-interactive (piped/CI): no wizard ran to ask for a host,
            # so a missing one must fail loudly here rather than silently
            # connecting nowhere.
            if not argv:
                print(f"{DIM}No command given - running 'views' "
                      f"(see --help for all commands).{RESET}", file=sys.stderr)
            if not args.host:
                raise BAMError(
                    "BAM host is required: pass -H/--host or set $BAM_HOST "
                    "(no default host is shipped).")
        auth = args.token
        if auth:
            print(f"{DIM}Using $BAM_TOKEN (skipping session creation)."
                  f"{RESET}", file=sys.stderr)
        else:
            user, password = resolve_credentials(args)
            if not VISUAL:
                print(f"{DIM}Authenticating to {args.host}…{RESET}",
                      file=sys.stderr)
            for _attempt in range(2):
                try:
                    if VISUAL:
                        with _Spinner(f"Authenticating to {args.host}…"):
                            auth = authenticate(args.host, user, password,
                                                args.verify)
                    else:
                        auth = authenticate(args.host, user, password,
                                            args.verify)
                    break
                except BAMError:
                    # Saved/stale credentials failed on a tty: ask once more,
                    # forcing fresh input instead of the saved values.
                    if not interactive or not (CREDS_PROMPTED
                                               or CREDS_USED_SAVED) \
                            or _attempt == 1:
                        raise
                    print(f"{RED}Login failed - enter credentials "
                          f"again.{RESET}", file=sys.stderr)
                    user, password = resolve_credentials(args,
                                                         force_prompt=True)
            if interactive and CREDS_PROMPTED and not CREDS_USED_SAVED:
                _maybe_save_credentials(user, password)
        client = Client(args.host, auth, args.verify)
        # Query loop: after each result, offer the main menu again or quit.
        # The session and credentials are reused - no re-login. Errors and
        # non-interactive runs still execute exactly one command.
        while True:
            set_machine_mode(args.format != "table")
            # Never offer an export of the previous round's data.
            LAST_RESULT = LAST_COLUMNS = None
            try:
                if args.command == "zone":
                    args.zone_id = resolve_zone_id(client, args.zone_id,
                                                   interactive)
                elif args.command == "records" and getattr(args, "zone", None):
                    args.zone = resolve_zone_id(client, args.zone, interactive)
                _run_command(client, args)
            except Back:
                if not interactive:
                    raise        # outer handler is non-interactive; can't go back
                args = _reask_menu(args)
                if args.command == "quit":
                    print_outro()
                    break
                continue
            except (BAMError, ReadOnlyError) as e:
                if not interactive:
                    raise        # outer handler prints + sets exit code
                print(f"{RED}{e}{RESET}", file=sys.stderr)
                if not _ask_yn("Back to the menu", default=True):
                    print_outro()
                    break
                # Re-ask the menu; the authenticated session is reused.
                args = _reask_menu(args)
                if args.command == "quit":
                    print_outro()
                    break
                continue
            if not interactive:
                break
            if LAST_RESULT is not None:
                choice = _choose_export()
                if choice != "none":
                    export_result(args.command, choice)
                if LAST_QUERY is not None \
                        and _ask_yn("Save this query as a favorite"):
                    name = _ask("Favorite name (e.g. my zones)",
                                required=True)
                    CONFIG["favorites"].append({"name": name, **LAST_QUERY})
                    save_config()
                    print(f"{DIM}Saved favorite {name!r}.{RESET}",
                          file=sys.stderr)
            if not _ask_yn("Run another query"):
                print_outro()
                break
            args = _reask_menu(args)
            if args.command == "quit":
                print_outro()
                break
    except ReadOnlyError as e:
        print(f"{RED}{e}{RESET}", file=sys.stderr)
        return 2
    except BAMError as e:
        print(f"{RED}{e}{RESET}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nClosed - goodbye!", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
