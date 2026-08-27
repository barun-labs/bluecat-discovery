#!/usr/bin/env python3
"""BlueCat BAM export - interactive browser and exporter for the REST v2 API.

Pick what you want from menus, or drive it entirely from flags. Read-only:
the only non-GET call is the login POST, and even that is skipped when
$BAM_TOKEN is set. Stdlib only.
"""
import argparse
import csv
import json
import os
import sys
import threading
import time

import bluecat_core
import bluecat_menu
from bluecat_core import BAMError, BAMTimeout, PAGE_SIZE, authenticate, _format_eta
from bluecat_menu import Back, STDIN, choose
from bluecat_query import flatten, ref_label

# Preferred leading column order. Other keys present in the data follow;
# absent ones are dropped.
CSV_FIELDS = ["id", "type", "name", "absoluteName", "ttl", "rdata", "comment", "dynamic"]
BAR_WIDTH = 30

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

RESET = "\033[0m" if COLOR else ""
BOLD = "\033[1m" if COLOR else ""
GREEN = "\033[32m" if COLOR else ""
BRIGHT_GREEN = "\033[92m" if COLOR else ""
DIM_GRAY = "\033[90m" if COLOR else ""
BRIGHT_BLUE = "\033[94m" if COLOR else ""
YELLOW = "\033[33m" if COLOR else ""


# --------------------------------------------------------------------------
# pure helpers (all unit-tested)
# --------------------------------------------------------------------------

def render_progress(fetched, total, elapsed):
    pct = fetched / total if total else 1.0
    filled = int(BAR_WIDTH * pct)
    bar = f"{GREEN}{'█' * filled}{DIM_GRAY}{'░' * (BAR_WIDTH - filled)}{RESET}"
    pct_color = BRIGHT_GREEN if fetched >= total else ""
    pct_text = f"{pct_color}{int(pct * 100):3d}%{RESET if pct_color else ''}"
    rate = fetched / elapsed if elapsed > 0 else 0
    eta = _format_eta((total - fetched) / rate) if rate > 0 else "?"
    return f"[{bar}] {pct_text}  {fetched}/{total}  •  {rate:.0f} rec/s  •  ETA {eta}"


def normalize_collection(value):
    """Accept 'zones', '/api/v2/zones' or 'views/100900/zones' - return the API path.

    Absolute URLs are rejected: the host comes from --host, and silently talking
    to a different appliance than the one authenticated against would be worse
    than an error message.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("collection is empty")
    if value.startswith(("http://", "https://")):
        raise ValueError("pass a path such as 'zones' or '/api/v2/zones', not a full URL")
    value = value.lstrip("/")
    if not value.startswith("api/v2"):
        value = f"api/v2/{value}"
    return "/" + value


# Readable default column sets per scenario. Missing columns are skipped, so one
# preset can safely cover a polymorphic collection.
PRESETS = {
    "records": ["zone", "type", "absoluteName", "name", "ttl", "rdata", "linkedRecord",
                "addresses", "comment", "dynamic", "id"],
    "zones": ["absoluteName", "type", "name", "deployable", "id"],
    "servers": ["name", "type", "profile", "state", "connected", "serverGroup",
                "version", "privateAddress", "id"],
    "ipam": ["range", "type", "name", "gateway", "state", "location", "id"],
}


def select_fields(records, preset):
    """Preset columns that actually exist in the data, in preset order."""
    present = derive_fields(records)
    chosen = [f for f in PRESETS.get(preset, []) if f in present]
    return chosen or None


def derive_fields(records):
    """Union of keys across all records, CSV_FIELDS first, then first-seen order.

    Scans every record, not just the first: BAM collections are polymorphic, so a
    later HostRecord can carry keys an earlier ExternalHostRecord lacks.
    """
    seen = []
    for record in records:
        for key in record:
            if key not in seen:
                seen.append(key)
    ordered = [f for f in CSV_FIELDS if f in seen]
    ordered += [f for f in seen if f not in ordered]
    return ordered


def default_out_name(label, fmt):
    slug = (label or "export").replace("/api/v2/", "").strip("/").replace("/", "-")
    slug = "".join(c if c.isalnum() or c in "-_." else "-" for c in slug).strip("-") or "export"
    return f"{slug}_{time.strftime('%Y-%m-%d_%H%M')}.{fmt}"


def write_csv(records, path, fields=None):
    rows = [flatten(r) for r in records]
    fields = fields or derive_fields(rows) or CSV_FIELDS
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(records, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
        f.write("\n")


def write_output(records, path, fmt):
    if fmt == "json":
        write_json(records, path)
    else:
        write_csv(records, path)


# --------------------------------------------------------------------------
# terminal chrome
# --------------------------------------------------------------------------

def print_banner():
    lines = [
        r"     __    __",
        r"    (  \  /  )",
        r"     \_ )( _/",
        "",
        "▛▀▖▌  ▌ ▌▛▀▘▞▀▘▞▀▖▀▛▘",
        "▙▄▘▌  ▌ ▌▙▄ ▌  ▙▄▌ ▌ ",
        "▌ ▌▌  ▌ ▌▌  ▌  ▌ ▌ ▌ ",
        "▀▀ ▀▀ ▝▀ ▀▀▘▝▀▘▘ ▘ ▘ ",
        "",
        "   Address Manager REST v2 - browse & export",
    ]
    for line in lines:
        print(f"{BRIGHT_BLUE}{line}{RESET}")
    print()


def prompt(label, default=""):
    """Ask one question; Enter takes `default`.

    A bool `default` switches this to yes/no mode: the suffix becomes
    "[Y/n]"/"[y/N]" (matching bluecat_discover.py's _ask_yn), one keystroke
    answers it (no Enter needed - Enter still works too, taking the
    default), junk answers reprompt, and the return value is a bool instead
    of a string. Any other `default` keeps the original behaviour
    untouched: "[default]" shown verbatim, a full line read, and the typed
    (or default) string returned as-is.
    """
    if isinstance(default, bool):
        suffix = " [Y/n]" if default else " [y/N]"
        while True:
            value = STDIN.keypress(f"{label}{suffix}: ").strip().lower()
            if not value:
                return default
            if value in ("y", "yes"):
                return True
            if value in ("n", "no"):
                return False
            print(f"{YELLOW}Please answer y or n.{RESET}")
    suffix = f" [{default}]" if default else ""
    value = STDIN.input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_secret(label):
    return STDIN.secret(f"{label}: ")


def _keypress_prompt(prompt_text):
    """input_fn for choose(): reads one keystroke via the STDIN seam
    instead of a full line - for the fixed, single-screen SCENARIOS menu,
    where every choice is a digit 1-6 (STDIN.keypress() falls back to a
    full line read on its own whenever a keystroke can't safely be read)."""
    return STDIN.keypress(prompt_text)


# --------------------------------------------------------------------------
# API - Client, BAMError, BAMTimeout, and authenticate() now live in
# bluecat_core.py, and Back/choose() now live in bluecat_menu.py (both
# imported at the top of this file), so this module and bluecat_discover.py
# share one HTTP transport and one numbered-menu chooser.
# --------------------------------------------------------------------------

Client = bluecat_core.Client


# --------------------------------------------------------------------------
# domain walks
# --------------------------------------------------------------------------

ZONE_FIELDS = "id,name,absoluteName,type,deployable,signed"


def walk_zones(client, zone_id, depth=0, max_depth=32, on_zone=None):
    """Depth-first over a zone and every subzone beneath it.

    BAM models DNS as a label tree - 'com' is a Zone with 'example' beneath it -
    so 'all zones in this view' means walking, not one listing. max_depth is a
    loop guard, not a real limit; DNS bottoms out long before 32.
    """
    out = []
    if depth >= max_depth:
        return out
    children = client.all(f"/api/v2/zones/{zone_id}/zones", fields=ZONE_FIELDS)
    for child in children:
        out.append(child)
        if on_zone:
            on_zone(child, len(out))
        out.extend(walk_zones(client, child["id"], depth + 1, max_depth, on_zone))
    return out


def zone_label(z):
    name = z.get("absoluteName") or z.get("name") or "(unnamed)"
    kind = z.get("type", "")
    tail = "" if kind == "Zone" else f"  {DIM_GRAY}[{kind}]{RESET}"
    return f"{name}{tail}  {DIM_GRAY}#{z.get('id')}{RESET}"


def named(o):
    return f"{o.get('name') or o.get('range') or o.get('absoluteName') or '(unnamed)'}" \
           f"  {DIM_GRAY}#{o.get('id')}{RESET}"


def block_label(b):
    return f"{b.get('range', '?'):<24}{b.get('name') or ''}  {DIM_GRAY}#{b.get('id')} {b.get('type','')}{RESET}"


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def pick_configuration(client):
    configs = client.all("/api/v2/configurations", fields="id,name,type")
    return choose(configs, named, "Configuration")


def pick_view(client, config_id):
    views = client.all(f"/api/v2/configurations/{config_id}/views", fields="id,name,type")
    return choose(views, named, "View")


def scenario_zone_records(client):
    """Drill the zone tree, then export that zone's records (optionally recursive)."""
    config = pick_configuration(client)
    view = pick_view(client, config["id"])
    zones = client.all(f"/api/v2/views/{view['id']}/zones", fields=ZONE_FIELDS)

    zone = choose(zones, zone_label, f"Zones in view '{view['name']}'")
    # keep drilling while the chosen zone has children and the user wants to go deeper
    while True:
        children = client.all(f"/api/v2/zones/{zone['id']}/zones", fields=ZONE_FIELDS)
        if not children:
            break
        label = zone.get("absoluteName") or zone.get("name")
        print(f"\n{DIM_GRAY}'{label}' has {len(children)} subzone(s).{RESET}")
        if not prompt("Drill into a subzone?", False):
            break
        try:
            zone = choose(children, zone_label, f"Subzones of {label}")
        except Back:
            break

    label = zone.get("absoluteName") or zone.get("name")
    recursive = prompt(f"Include records of every subzone under '{label}'?",
                       False)
    targets = [zone]
    if recursive:
        print("Walking the zone tree...")
        targets += walk_zones(client, zone["id"])
        print(f"  {len(targets)} zone(s) to read.")
    return collect_records(client, targets), f"records-{label}", "records"


def scenario_all_zones(client):
    """Every zone in a view, flattened - the 'forward zone list' export."""
    config = pick_configuration(client)
    view = pick_view(client, config["id"])
    tops = client.all(f"/api/v2/views/{view['id']}/zones", fields=ZONE_FIELDS)
    found = list(tops)
    print(f"Walking the zone tree under view '{view['name']}'...")

    def tick(_z, _n):
        print(f"\r  {len(found)} zone(s) found...\033[K", end="", flush=True)

    for top in tops:
        found.extend(walk_zones(client, top["id"], on_zone=tick))
    print()
    return found, f"zones-{view['name']}", "zones"


def scenario_external_hosts(client):
    config = pick_configuration(client)
    view = pick_view(client, config["id"])
    zones = client.all(f"/api/v2/views/{view['id']}/zones",
                       fields=ZONE_FIELDS, filter_="type:eq('ExternalHostsZone')")
    if not zones:
        raise BAMError(f"View '{view['name']}' has no External Hosts zone")
    zone = choose(zones, zone_label, "External Hosts zone")
    return collect_records(client, [zone]), f"external-hosts-{view['name']}", "records"


def scenario_blocks_networks(client):
    """IPAM drill-down: block → nested blocks → networks → addresses."""
    config = pick_configuration(client)
    node = choose(client.all(f"/api/v2/configurations/{config['id']}/blocks",
                             fields="id,name,type,range"),
                  block_label, "IP blocks")
    while True:
        kids = client.all(f"/api/v2/blocks/{node['id']}/blocks",
                          fields="id,name,type,range")
        if not kids or not prompt(
                f"Drill into one of {len(kids)} nested block(s)?", False):
            break
        try:
            node = choose(kids, block_label, f"Blocks under {node.get('range')}")
        except Back:
            break

    what = choose(
        [("networks", "Networks in this block"),
         ("addresses", "Addresses in this block"),
         ("availableNetworks", "Available (free) networks"),
         ("availableAddresses", "Available (free) addresses"),
         ("deploymentRoles", "Deployment roles - this is where reverse DNS lives"),
         ("blocks", "Nested blocks")],
        lambda t: t[1], f"What to export from {node.get('range')}")
    sub, _ = what
    records, _ = client.collection(f"/api/v2/blocks/{node['id']}/{sub}",
                                   on_progress=_progress_printer())
    print()
    return records, f"{sub}-{node.get('range')}", ("zones" if sub == "deploymentRoles" else "ipam")


def scenario_servers(client):
    config = pick_configuration(client)
    records, _ = client.collection(f"/api/v2/configurations/{config['id']}/servers",
                                   on_progress=_progress_printer())
    print()
    return records, f"servers-{config['name']}", "servers"


def scenario_raw(client):
    raw = prompt("Collection path", "configurations")
    path = normalize_collection(raw)
    filt = prompt("Filter (blank for none)", "")
    fields = prompt("Fields (blank for all)", "")
    records, _ = client.collection(path, on_progress=_progress_printer(),
                                   filter_=filt or None, fields=fields or None)
    print()
    return records, path, None


def _progress_printer():
    start = time.monotonic()

    def on_progress(fetched, total):
        elapsed = time.monotonic() - start
        print(f"\r{render_progress(fetched, total, elapsed)}\033[K", end="", flush=True)

    return on_progress


def collect_records(client, zones):
    """Resource records for a list of zones, annotated with their zone name.

    The zone column is added here because BAM does not return it on the record,
    and a merged multi-zone CSV without it is unusable.
    """
    out = []
    for i, zone in enumerate(zones, 1):
        label = zone.get("absoluteName") or zone.get("name")
        print(f"\r  [{i}/{len(zones)}] {label}\033[K", end="", flush=True)
        try:
            records, _ = client.collection(f"/api/v2/zones/{zone['id']}/resourceRecords")
        except BAMError as e:
            print(f"\r  {YELLOW}skipped {label}: {e}{RESET}\033[K")
            continue
        for r in records:
            r["zone"] = label
        out.extend(records)
    print(f"\r  {len(out)} record(s) from {len(zones)} zone(s).\033[K")
    return out


SCENARIOS = [
    ("Zone records - pick a zone, optionally include every subzone", scenario_zone_records),
    ("Zone list - every zone in a view, flattened", scenario_all_zones),
    ("External hosts - the ExternalHostsZone records", scenario_external_hosts),
    ("IP blocks & networks - incl. addresses, free space, reverse DNS roles",
     scenario_blocks_networks),
    ("Servers", scenario_servers),
    ("Raw collection path - any of the 454 GET endpoints", scenario_raw),
]


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Browse and export any BlueCat Address Manager REST v2 collection.",
        epilog="Interactive:\n"
               "  %(prog)s\n"
               "Unattended:\n"
               "  %(prog)s -c zones/100/resourceRecords -o hosts.csv -y\n"
               "  %(prog)s -c configurations/200/servers "
               "--filter \"name:contains('Cache')\" --fields id,name -y\n"
               "  %(prog)s --list\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-H", "--host", default=os.environ.get("BAM_HOST", ""))
    p.add_argument("-u", "--user", default=os.environ.get("BAM_USER", ""))
    p.add_argument("-c", "--collection",
                   help="skip the menus and export this collection directly")
    p.add_argument("-o", "--out", help="output file (default: <what>_<stamp>.<ext>)")
    p.add_argument("--format", choices=("csv", "json"), default="csv")
    p.add_argument("--filter", dest="filter_",
                   help="BAM filter, e.g. \"type:eq('ExternalHostRecord')\"")
    p.add_argument("--fields", help="comma list of fields to request server-side")
    p.add_argument("--order-by")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--verify", action="store_true",
                   help="verify the TLS certificate (off by default: self-signed appliance)")
    p.add_argument("--list", action="store_true", help="list root collections and exit")
    p.add_argument("-y", "--yes", action="store_true", help="skip prompts, use defaults")
    p.add_argument("-B", "--no-banner", action="store_true")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# saved credentials - one config file shared with bluecat_discover.py
# (~/.bluecat_discover.json), so a password saved by either tool works in
# both and is never typed (or drifts) twice. Everything but user/password
# in that file (host/format/view/kind/favorites) belongs to
# bluecat_discover.py; this script loads it all so a save here never drops
# those fields, but only ever changes user/password itself.
# --------------------------------------------------------------------------

CONFIG_PATH = ""  # overridable; resolved lazily so a changed $HOME wins


def _config_path():
    """Config file location; tests may override via CONFIG_PATH."""
    return bluecat_menu.config_path(CONFIG_PATH)


CONFIG = bluecat_menu.default_config()


def load_config():
    """Read the shared config file's user/password (and everything else, so
    a later save doesn't drop bluecat_discover.py's own settings)."""
    bluecat_menu.load_config(_config_path(), CONFIG)


def save_config():
    """Write CONFIG back to the shared config file, 0600 perms."""
    return bluecat_menu.save_config(_config_path(), CONFIG)


def _maybe_save_credentials(user, password):
    """Offer to persist the just-entered credentials (plain text!)."""
    if not prompt(f"Save username/password to {_config_path()} "
                  f"(plain text, chmod 600) for next time", False):
        return
    CONFIG["user"], CONFIG["password"] = user, password
    if save_config():
        print(f"{DIM_GRAY}Credentials saved to {_config_path()} "
              f"(chmod 600).{RESET}")
        return
    CONFIG["user"] = CONFIG["password"] = ""
    print(f"{YELLOW}Could not save credentials to {_config_path()} "
          f"(read-only? - they were NOT saved).{RESET}", file=sys.stderr)


def connect(args):
    # -H/--host or $BAM_HOST (already in args.host from parse_args) always
    # wins. Otherwise offer the remembered host from a previous session as
    # the prompt's default - that is the operator's own saved value, not a
    # shipped one, so there is no built-in fallback below it.
    host_default = args.host or CONFIG.get("host") or ""
    non_interactive = args.yes or args.collection or args.list \
        or not STDIN.isatty()
    if non_interactive:
        host = host_default
    else:
        host = prompt("BAM's Management IP or host", host_default)
    if not host:
        raise BAMError(
            "BAM host is required: pass -H/--host or set $BAM_HOST.")

    # $BAM_TOKEN is an already-issued basicAuthenticationCredentials blob. Set it
    # and the login POST is skipped entirely - the only way to run this where
    # creating a session is not the operator's to do. Env var, never a flag:
    # argv shows up in `ps` and shell history.
    auth = os.environ.get("BAM_TOKEN")
    if auth:
        print(f"{DIM_GRAY}Using $BAM_TOKEN (skipping session creation).{RESET}")
        client = Client(host, auth, args.verify)
        # Probe now. With a password the login POST proves the host; with a token
        # nothing is sent until the first export, so a wrong host or a dead token
        # would surface three menus deep as a confusing timeout.
        with _Ticker(f"Checking {host}..."):
            # short timeout: this is a reachability probe, not a real fetch
            root = client.get("/api/v2", what=f"connecting to {host}", timeout=15)
        print(f"Connected to {root.get('name', 'Address Manager')} "
              f"{root.get('version', '')}".rstrip())
        return client

    env_password = os.environ.get("BAM_PASSWORD")
    saved_user = CONFIG.get("user") or ""
    saved_password = CONFIG.get("password") or ""
    used_saved = False
    if not args.yes and not args.user and not env_password \
            and saved_user and saved_password and STDIN.isatty():
        if prompt(f"Use saved credentials for {saved_user}", True):
            user, password = saved_user, saved_password
            used_saved = True
    if not used_saved:
        user = args.user if (args.user or args.yes) else prompt("Username", args.user)
        if not user:
            raise BAMError("Username is required (--user or $BAM_USER).")
        password = env_password or prompt_secret("Password")
    with _Ticker("Authenticating..."):
        auth = authenticate(host, user, password, args.verify)
    print("Authenticated.")
    if not used_saved and not args.yes and STDIN.isatty():
        _maybe_save_credentials(user, password)
    return Client(host, auth, args.verify)


class _Ticker:
    """Prints '<label> (Ns)' via \r every second while a blocking call runs,
    so a slow call still looks alive instead of frozen.
    """

    def __init__(self, label, handoff=None):
        self.label = label
        self.handoff = handoff or threading.Event()
        self._stop = threading.Event()
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(1):
            # ponytail: tiny window where this and a real renderer's own print
            # could land on the same tick; self-heals on the next \r redraw
            if not self.handoff.is_set():
                elapsed = time.monotonic() - self._start
                print(f"\r{self.label} ({elapsed:.0f}s)\033[K", end="", flush=True)

    def __enter__(self):
        print(f"\r{self.label}\033[K", end="", flush=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=1.5)
        if not self.handoff.is_set():
            print("\r\033[K", end="", flush=True)


def save(records, label, args, preset=None):
    if not records:
        print(f"{YELLOW}Nothing to export.{RESET}")
        return
    fmt = args.format
    rows = [flatten(r) for r in records]
    fields = None
    if fmt == "csv" and not args.fields:
        fields = select_fields(rows, preset) if preset else None
        if fields and not args.yes:
            wide = derive_fields(rows)
            print(f"\n{DIM_GRAY}Columns: {', '.join(fields)}{RESET}")
            if prompt(f"Use all {len(wide)} columns instead?", False):
                fields = None
    default = default_out_name(label, fmt)
    out_path = args.out or (default if args.yes else prompt("Output filename", default))
    if fmt == "json":
        write_json(records, out_path)
    else:
        write_csv(records, out_path, fields=fields)
    print(f"{BRIGHT_GREEN}Done.{RESET} {len(records)} record(s) → {out_path}")


def main(argv=None):
    args = parse_args(argv)
    load_config()
    if not args.no_banner:
        print_banner()

    try:
        client = connect(args)
    except BAMError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        if args.list:
            payload = client.get("/api/v2", what="root")
            for name in sorted(k for k in payload.get("_links", {})
                               if k not in ("self", "service-doc", "service-desc")):
                print(f"  /api/v2/{name}")
            return 0

        if args.collection:
            path = normalize_collection(args.collection)
            records, _ = client.collection(
                path, on_progress=_progress_printer(), filter_=args.filter_,
                fields=args.fields, order_by=args.order_by, page_size=args.page_size)
            print()
            save(records, path, args, None)
            return 0

        while True:
            try:
                _, fn = choose(
                    SCENARIOS, lambda s: s[0], "What do you want to export?",
                    exact=True, show_count=False, index_width=1,
                    footer_lines=["  0) back"],
                    prompt_text=f"Pick 1-{len(SCENARIOS)} (0 = back): ",
                    invalid_msg=f"Pick a number 1-{len(SCENARIOS)}.",
                    input_fn=_keypress_prompt,
                )
            except Back:
                return 0
            try:
                records, label, preset = fn(client)
            except Back:
                continue
            save(records, label, args, preset)
            if not prompt("\nAnother export?", False):
                return 0

    except BAMError as e:
        print()
        print(str(e), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
