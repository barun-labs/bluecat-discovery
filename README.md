# bluecat-discovery

Read-only discovery and export for a BlueCat Address Manager (BAM) REST v2
appliance: DNS views, zones, external hosts, networks and IP details,
substring search, and a quick summary. Every request is a GET. The one
exception anywhere in the code is the session-login POST used to turn a
username and password into a bearer token, and even that is skipped when
you supply an already-issued token instead. There is no create, update, or
delete path: the shared HTTP client refuses any non-GET method outright.

Three ways to use it: two standalone scripts, and a launcher in front of
both. `bluecat_discover.py` prints results to the screen (or writes them to
a file with `--json`/`--csv`/`--yaml`); `bluecat_export.py` is a menu-driven
browser that always writes a CSV or JSON file. `bluecat_mcp.py` exposes the
same read-only queries as an MCP server, so an AI agent can look things up
on your BAM.

## Requirements

Python 3.9 or later, standard library only, for `bluecat_discover.py`,
`bluecat_export.py`, and `bluecat.py`. The MCP server additionally needs
the official SDK:

```
pip install mcp
```

## Running it

```
python3 bluecat.py                 # asks which tool you want, then hands off
python3 bluecat.py discover ARGS   # forwards ARGS to bluecat_discover.py
python3 bluecat.py export ARGS     # forwards ARGS to bluecat_export.py

python3 bluecat_discover.py        # run it directly, same as above
python3 bluecat_export.py
```

Run any of them with no arguments on a real terminal and you get a guided,
menu-driven session. Pipe input into one, or pass flags, and it runs
without prompting instead.

## Authentication

The appliance host is never built in. Provide it with `-H`/`--host` or
`$BAM_HOST`. On a terminal with neither set, you are asked for it (`BAM's
Management IP or host:`); a host remembered from an earlier session is
offered as the default, but nothing ships with one. Off a terminal (piped
input, `-y`, CI) with no host given, the tool fails immediately with a
message naming `-H` and `$BAM_HOST`, instead of guessing.

Credentials work the same way:

- `$BAM_USER` and `$BAM_PASSWORD` (or `-u`/`--user` and a prompt for the
  password) log in and obtain a session token.
- `$BAM_TOKEN` skips the login step entirely: set it to an
  already-issued `basicAuthenticationCredentials` value and every request
  uses it directly.
- On a terminal, after a successful password login you can opt in to
  saving the username and password to `~/.bluecat_discover.json` (mode
  600) so the next run offers them back to you. `--forget-credentials`
  (discover) clears them again. Nothing is saved unless you say yes.

Do not pass `--password` on the command line: it stays visible in your
shell history and in `ps` output for as long as the process runs. Use
`$BAM_PASSWORD` instead; both tools warn you if you use `--password` or
`--token` directly.

TLS verification is off by default, since these appliances typically carry
a self-signed certificate; pass `--verify` to turn it on.

## bluecat_discover.py

Nine commands:

- `views` - configuration and DNS view objectIds.
- `hosts [--view NAME]` - external host records per view.
- `zones [--view NAME] [--kind fwd|rev|ext|all]` - the zone tree, classified.
- `search QUERY --kind zones|records|networks|addresses|hosts` - substring
  search by name.
- `network TARGET [--ips] [--ptrs] [--dhcp] [--roles] [--all]` - one
  network's metadata, DHCP ranges, deployment roles, and IP list.
- `zone ID|NAME [--records]` - one zone, optionally with its records; a
  name is searched, and several matches let you pick one.
- `records [ZONE] [--view NAME]` - resource records for one zone or an
  entire view.
- `ip ADDRESS` - state, name, and linked records for one address.
- `summary` - counts of configurations, views, zones, and networks.

Every command supports `--json`, `-f csv`, and `-f yaml` (also `yml`); add
`-o FILE` to write the result to a file instead of stdout.

## bluecat_export.py

A menu of six scenarios, each ending in a CSV or JSON file:

- zone records, with an option to include every subzone underneath
- every zone in a view, flattened into one list
- external host records
- IP blocks and networks, down to addresses, free space, and reverse-DNS
  roles
- servers
- a raw collection path, for anything not covered by the scenarios above

Pass `-c PATH` to skip the menus and export one collection directly
(`bluecat_export.py -c zones/100/resourceRecords -o out.csv -y`), or
`--list` to see the root collections available.

## MCP server

`bluecat_mcp.py` exposes the same queries `bluecat_discover.py` uses, as
nine MCP tools: `bluecat_views`, `bluecat_hosts`, `bluecat_zones`,
`bluecat_search`, `bluecat_network`, `bluecat_zone`, `bluecat_records`,
`bluecat_ip`, and `bluecat_summary`. Each returns one JSON document; BAM
errors come back as MCP tool errors with the same message the CLI would
print.

Run it as a stdio server:

```
BAM_HOST=bam.example.com BAM_USER=alice BAM_PASSWORD='secret' \
    python3 bluecat_mcp.py
```

Host and credentials can also come from the saved config file or
`$BAM_TOKEN`; `$BAM_VERIFY=1` turns TLS verification on. Register
`python3 /path/to/bluecat_mcp.py` as a stdio MCP server in your client
(Claude Desktop, or an `mcp` entry in an agent config), with the `BAM_*`
environment variables set on the server process. The server authenticates
once and reuses that session for its whole lifetime.

## Interactive features

Both CLI tools guide you through a numbered menu when run on a terminal
with no arguments. Short yes/no and digit menus answer on a single
keystroke, no Enter required; longer lists (zones, networks) still take a
typed number or a substring to filter by. Every menu offers `0` to go back
a step instead of forcing you through the rest of the questions.

`bluecat_discover.py`'s wizard remembers your last host, output format,
view, and search kind between sessions, and lets you save a query as a
favorite to re-run later with one pick from a list. `r` re-runs the last
query with its exact settings again.

## Tests

```
python3 -m pytest tests/
```

No network access is required; the HTTP layer is exercised through fake
clients and a read-only enforcement check on the request builder itself.
The MCP SDK is not required to run the test suite.
