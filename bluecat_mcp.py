#!/usr/bin/env python3
"""BlueCat Discovery MCP server (stdio transport) - read-only.

Exposes the read-only BlueCat Address Manager (BAM) discovery queries from
bluecat_discover.py as Model Context Protocol tools, so AI agents can look
up views, zones, networks, and IPs on a BAM appliance. Every request is a
GET; the only POST anywhere is the session login, exactly like the CLI.

Run (stdio server - configure it as an MCP server in your client):

    BAM_HOST=bam.example.com BAM_USER=alice BAM_PASSWORD='secret' \\
        python3 bluecat_mcp.py

Credentials and host can also come from the saved config
(~/.bluecat_discover.json, written by the CLI's save option) or a
$BAM_TOKEN (base64 Basic token, skips session creation). $BAM_VERIFY=1
turns on TLS certificate verification (default off, like the CLI).

Requires the official MCP SDK:  pip install mcp
"""
import json
import os
import sys
from typing import Literal, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bluecat_discover as bd  # noqa: E402  (sys.path set just above)

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    raise SystemExit(
        "bluecat_mcp.py requires the MCP SDK:  pip install mcp")

SERVER_NAME = "bluecat-discovery"
SERVER_VERSION = "0.1.0"

_client = None       # cached authenticated Client, reused across tool calls
_client_spec = None  # (host, verify) the cached client was built for


def _bool_env(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _get_client():
    """Lazily build (and cache) an authenticated read-only bd.Client.

    Precedence: $BAM_HOST over the saved config host; $BAM_TOKEN over
    user/password; $BAM_USER/$BAM_PASSWORD over the saved config
    credentials. The saved config is the last resort so a plain
    `python3 bluecat_mcp.py` works when the CLI saved credentials.
    """
    global _client, _client_spec
    bd.load_config()
    host = os.environ.get("BAM_HOST") or bd.CONFIG.get("host") or ""
    if not host:
        raise ToolError("no BAM host: set $BAM_HOST or save one in the "
                        "config file")
    verify = _bool_env("BAM_VERIFY")
    # Reuse the cached session when host/verify are unchanged (credentials
    # changes require a server restart - like the CLI, the session lives
    # for the whole process).
    if _client is not None and _client_spec == (host, verify):
        return _client
    auth = os.environ.get("BAM_TOKEN")
    if not auth:
        env_user = os.environ.get("BAM_USER") or ""
        env_pass = os.environ.get("BAM_PASSWORD") or ""
        if bool(env_user) != bool(env_pass):
            raise ToolError("set both $BAM_USER and $BAM_PASSWORD (or "
                            "$BAM_TOKEN), or use the saved config")
        # user+password always come from the same source - no silent mixing
        user = env_user or bd.CONFIG.get("user") or ""
        password = env_pass or bd.CONFIG.get("password") or ""
        if not user or not password:
            raise ToolError("credentials required: set $BAM_USER and "
                            "$BAM_PASSWORD (or $BAM_TOKEN), or save "
                            "credentials with the CLI first")
        auth = bd.authenticate(host, user, password, verify)
    _client = bd.Client(host, auth, verify)
    _client_spec = (host, verify)
    return _client


def _json(fn, *args, **kwargs):
    """Run a raw tool implementation and return pretty JSON for MCP."""
    try:
        data = fn(_get_client(), *args, **kwargs)
    except ToolError:
        raise
    except bd.BAMError as e:
        raise ToolError(str(e))
    return json.dumps(data, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# raw tool implementations (take a client; unit-tested directly)
#
# Each delegates to the matching query_*() in bluecat_query.py - the same
# function bluecat_discover.py's cmd_*() call - so the query logic itself is
# not duplicated here. A tool only adds what MCP needs: resolving a
# name-or-id target up front (the CLI does this via its interactive wizard
# instead) and asking the client for quiet=True (no progress bar).
# --------------------------------------------------------------------------

def _tool_views(client):
    """objectIds for configurations and their DNS views."""
    return {"views": bd.query_views(client)}


def _tool_hosts(client, view=""):
    """External host records per DNS view (ExternalHostsZone)."""
    return {"hosts": bd.query_hosts(client, view or None, quiet=True)}


def _tool_zones(client, view="", kind="all"):
    """All zones per view, classified fwd/rev/ext/other, with objectIds.

    Kind matching mirrors the CLI: ext matches ExternalHostsZone
    ("external-hosts"); other matches the special types (enum, internal-root,
    response-policy) plus any zone that classifies as "other".
    """
    return {"zones": bd.query_zones(client, view or None, kind)}


def _tool_search(client, query, kind="all"):
    """Substring search over zones, records, networks, addresses, hosts."""
    if kind == "all":
        result = bd.query_search(client, query, "all", quiet=True)
        return {"query": query, "kind": "all", "totals": result["totals"],
                "matches": result["matches"]}
    result = bd.query_search(client, query, kind, quiet=True)
    return {"query": query, "kind": kind, "total": result["total"],
            "matches": result["matches"]}


def _tool_network(client, target, ips=False, ptrs=False, dhcp=False,
                  roles=False, limit=10000):
    """Network details: meta, DHCP ranges, deployment roles, and IPs."""
    return bd.query_network(client, target, ips=ips, ptrs=ptrs, dhcp=dhcp,
                            roles=roles, limit=limit, quiet=True)


def _tool_zone(client, target, records=False):
    """One zone by objectId or name, optionally with its records."""
    zone_id = bd.resolve_zone_id(client, target)
    return bd.query_zone(client, zone_id, records=records, quiet=True)


def _tool_records(client, zone="", view=""):
    """Resource records for one zone (objectId or name) or an entire view."""
    zone_id = bd.resolve_zone_id(client, zone) if zone else None
    return {"records": bd.query_records(client, zone_id, view or None,
                                        quiet=True)}


def _tool_ip(client, address):
    """Look up one IP address: state, name, and linked host records."""
    return bd.query_ip(client, address, quiet=True)


def _tool_summary(client):
    """Quick overview: counts of the main object types."""
    return {"items": bd.query_summary(client)}


# --------------------------------------------------------------------------
# MCP server wiring
# --------------------------------------------------------------------------

server = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions=("Read-only BlueCat Address Manager (BAM) discovery. "
                  "All tools only GET data; nothing is ever written. "
                  "Every tool returns a JSON document."),
)


@server.tool(name="bluecat_views",
             description="List BlueCat configurations and their DNS views "
                         "(objectIds for later lookups).")
def bluecat_views() -> str:
    """objectIds for configurations and their DNS views."""
    return _json(_tool_views)


@server.tool(name="bluecat_hosts",
             description="List external host records per DNS view; pass "
                         "view=<name> to filter to one view.")
def bluecat_hosts(view: Optional[str] = None) -> str:
    """External host records per DNS view (via each view's ExternalHostsZone)."""
    return _json(_tool_hosts, view or "")


@server.tool(name="bluecat_zones",
             description="List zones per DNS view, classified "
                         "forward/reverse/external, with objectIds. "
                         "kind: all|fwd|rev|ext|other; view=<name> filters.")
def bluecat_zones(view: Optional[str] = None,
                  kind: Literal["all", "fwd", "rev", "ext", "other"]
                  = "all") -> str:
    """All zones per view, classified fwd/rev/ext, with objectIds."""
    return _json(_tool_zones, view or "", kind)


@server.tool(name="bluecat_search",
             description="Substring search across zones, records, "
                         "networks, addresses and hosts. kind='all' "
                         "searches every kind at once; otherwise one of "
                         "zones|records|networks|addresses|hosts.")
def bluecat_search(query: str,
                   kind: Literal["zones", "records", "networks",
                                 "addresses", "hosts", "all"]
                   = "all") -> str:
    """Substring search over zones, records, networks, addresses, hosts."""
    return _json(_tool_search, query, kind)


@server.tool(name="bluecat_network",
             description="Inspect one network by objectId, exact range, or "
                         "name: metadata plus optional DHCP ranges, "
                         "deployment roles, and IP addresses (first "
                         "'limit' addresses, max 10000).")
def bluecat_network(target: str, ips: bool = False, ptrs: bool = False,
                    dhcp: bool = False, roles: bool = False,
                    limit: int = 10000) -> str:
    """Network details: meta, DHCP ranges, deployment roles, and IPs."""
    return _json(_tool_network, target, ips, ptrs, dhcp, roles, limit)


@server.tool(name="bluecat_zone",
             description="Inspect one zone by objectId or name; pass "
                         "records=true to include its resource records.")
def bluecat_zone(target: str, records: bool = False) -> str:
    """One zone by objectId or name, optionally with its records."""
    return _json(_tool_zone, target, records)


@server.tool(name="bluecat_records",
             description="Resource records for one zone (objectId or name) "
                         "or an entire view (every forward zone); pass "
                         "zone=<id/name> or view=<name>.")
def bluecat_records(zone: Optional[str] = None,
                    view: Optional[str] = None) -> str:
    """Resource records for one zone or an entire view, with a zone column."""
    return _json(_tool_records, zone or "", view or "")


@server.tool(name="bluecat_ip",
             description="Look up one IP address: state, name, MAC "
                         "address, and linked resource records.")
def bluecat_ip(address: str) -> str:
    """Look up one IP address: state, name, and linked host records."""
    return _json(_tool_ip, address)


@server.tool(name="bluecat_summary",
             description="Quick overview: counts of configurations, "
                         "views, zones, and networks.")
def bluecat_summary() -> str:
    """Quick overview: counts of the main object types."""
    return _json(_tool_summary)


def main():
    server.run()  # stdio transport (the default)
    return 0


if __name__ == "__main__":
    sys.exit(main())
