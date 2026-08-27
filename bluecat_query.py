#!/usr/bin/env python3
"""Shared query logic for the BlueCat Address Manager (BAM) REST v2 API.

Used by bluecat_discover.py (CLI) and bluecat_mcp.py (MCP server), which used
to each carry their own hand-copied re-derivation of the same nine queries -
views, hosts, zones, search, network, zone, records, ip, summary - and had
drifted apart in the copying. Every query_*() function here takes a client
plus plain parameters and returns data (a list of row dicts, or a dict of
sections); it never prints, never touches argparse, and knows nothing about
MCP. Rendering that data as a table/csv/yaml (bluecat_discover.emit) and
serialising it as JSON (bluecat_mcp._json) are the two adapters over it.

flatten()/ref_label() are also shared here with bluecat_export.py, which used
to carry its own copy too - see their docstrings for the resolved divergence.

Only BASE_PATH and BAMError are pulled in from bluecat_core: a URL prefix
constant and an exception class, not the HTTP transport itself. No request,
paging, or auth logic lives here - that stays bluecat_core's job, called
through the `client` each function is handed.
"""
import json
import re
import urllib.parse

from bluecat_core import BASE_PATH, BAMError


def flatten(record):
    """One row per record, aimed at being readable in a spreadsheet.

    Keys starting with `_` are dropped (`_links`, `_inheritedFields`,
    `_redactedFields` are plumbing). A nested resource reference collapses to the
    one field a human reads - its name, or its range for IP objects - because a
    cell containing `{"id":100,"type":"Configuration","name":"Example DNS", ...}` is
    unreadable and the id is already in the record's own column. Anything without
    a name falls back to compact JSON, so nothing is silently lost. Use
    --format json when the full nested object is what you want.

    A key whose value is None is kept, not dropped: the column stays present
    (blank) across every row, rather than vanishing whenever one record
    happens to have that field unset.
    """
    row = {}
    for key, value in record.items():
        if key.startswith("_"):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            row[key] = value
        elif isinstance(value, dict):
            row[key] = ref_label(value)
        elif isinstance(value, list):
            row[key] = ", ".join(
                v if isinstance(v, str) else
                ref_label(v) if isinstance(v, dict) else
                json.dumps(v, separators=(",", ":"))
                for v in value)
        else:
            row[key] = json.dumps(value, separators=(",", ":"))
    return row


def ref_label(obj):
    """The one field a human reads out of a nested BAM resource reference.

    Order matters: an IPv4Address carries an empty `name` alongside the `address`
    that is the whole point of the row, and a zone reference is more useful as its
    absoluteName than its bare label. Falls back to compact JSON so nothing is
    silently lost.
    """
    if not isinstance(obj, dict):
        return json.dumps(obj, separators=(",", ":"))
    for key in ("address", "range", "absoluteName", "name"):
        value = obj.get(key)
        if value:
            return value
    return json.dumps({k: v for k, v in obj.items() if k != "_links"},
                      separators=(",", ":"))


# --------------------------------------------------------------------------
# classification and lookup helpers (pure query logic, no CLI/MCP concerns)
# --------------------------------------------------------------------------

VIEW_FIELDS = "id,name,type"
ZONE_FIELDS = "id,name,absoluteName,type,deployable,signed"

SEARCH_KINDS = {
    "zones": ("/api/v2/zones", "name"),
    "records": ("/api/v2/resourceRecords", "absoluteName"),
    "networks": ("/api/v2/networks", "name"),
    "addresses": ("/api/v2/addresses", "name"),
    "hosts": ("/api/v2/resourceRecords", "absoluteName"),
}

REVERSE_NAME = re.compile(
    r"(\.in-addr\.arpa\.?|\.ip6\.arpa\.?)$|"
    r"^(\d{1,3})(\.\d{1,3}){0,3}(/\d{1,2})?$")

SPECIAL_ZONE_TYPES = {
    "ExternalHostsZone": "external-hosts",
    "ENUMZone": "enum",
    "InternalRootZone": "internal-root",
    "ResponsePolicyZone": "response-policy",
}


def zone_kind(zone):
    """Classify a zone object: fwd | rev | ext | other.

    BAM returns reverse zones as plain Zone objects whose absoluteName is an
    in-addr.arpa / ip6.arpa suffix or a bare CIDR/octet form. External host
    zones carry their own type.
    """
    ztype = zone.get("type", "")
    if ztype in SPECIAL_ZONE_TYPES:
        return SPECIAL_ZONE_TYPES[ztype]
    if ztype == "Zone":
        name = zone.get("absoluteName") or zone.get("name") or ""
        if REVERSE_NAME.search(name):
            return "rev"
        return "fwd"
    return "other"


def is_reverse(zone):
    return zone_kind(zone) == "rev"


def build_filter(field, op, value):
    # Escape backslashes and quotes so a value like "d'angelo" or a path can't
    # break out of the quoted BAM filter literal or inject filter syntax.
    value = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"{field}:{op}('{value}')"


def resolve_network(client, target):
    """Resolve a network by id, exact range, or name. Returns the network."""
    target = str(target).strip()
    if target.isdigit():
        try:
            return client.get(f"{BASE_PATH}/networks/{int(target)}",
                              what=f"network {target}")
        except BAMError as e:
            raise BAMError(f"network id {target}: {e}")
    for flt in (build_filter("range", "eq", target),
                build_filter("name", "eq", target),
                build_filter("name", "contains", target)):
        hits, _ = client.collection(f"{BASE_PATH}/networks", filter_=flt,
                                    page_size=1000)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise BAMError(
                f"'{target}' matches {len(hits)} networks - pass the id or "
                f"exact range instead: " + ", ".join(
                    f"{n.get('range')} #{n.get('id')}" for n in hits[:8]))
    raise BAMError(f"no network matches {target!r}")


def walk_zones(client, zone_id, depth=0, max_depth=32):
    out = []
    if depth >= max_depth:
        return out
    kids = client.all(f"{BASE_PATH}/zones/{zone_id}/zones", fields=ZONE_FIELDS)
    for kid in kids:
        out.append(kid)
        out.extend(walk_zones(client, kid["id"], depth + 1, max_depth))
    return out


def linked_records(client, address_id):
    """Resource records linked to an address (the IP→name view)."""
    records, _ = client.collection(
        f"{BASE_PATH}/addresses/{address_id}/resourceRecords",
        fields="id,type,absoluteName,name,rdata", page_size=1000)
    return records


# --------------------------------------------------------------------------
# the nine queries - each takes a client plus plain parameters and returns
# data; none of them print, parse argv, or know about MCP.
# --------------------------------------------------------------------------

def query_views(client):
    """objectIds for configurations and their DNS views."""
    configs = client.all(f"{BASE_PATH}/configurations", fields="id,name,type")
    out = []
    for cfg in configs:
        views = client.all(f"{BASE_PATH}/configurations/{cfg['id']}/views",
                           fields=VIEW_FIELDS)
        for v in views:
            out.append({"configurationId": cfg["id"],
                        "configuration": cfg.get("name"),
                        "objectId": v["id"], "type": v.get("type"),
                        "view": v.get("name")})
    return out


def query_hosts(client, view=None, quiet=False, on_zone=None):
    """External host records per DNS view (via each view's ExternalHostsZone).

    on_zone(view, zone, records), if given, is called once per zone - the
    caller owns any progress note (the CLI prints one, MCP does not).
    """
    views = client.all(f"{BASE_PATH}/views", fields=VIEW_FIELDS)
    if view:
        views = [v for v in views if v.get("name") == view]
        if not views:
            raise BAMError(f"no view named {view!r}")
    out = []
    for v in views:
        zones = client.all(f"{BASE_PATH}/views/{v['id']}/zones",
                           fields=ZONE_FIELDS,
                           filter_=build_filter("type", "eq",
                                                "ExternalHostsZone"))
        for zone in zones:
            records, _total = client.collection(
                f"{BASE_PATH}/zones/{zone['id']}/resourceRecords",
                fields="id,type,absoluteName,ttl,comment",
                page_size=100,  # ExternalHostsZone records stall above ~100
                quiet=quiet)
            if on_zone:
                on_zone(v, zone, records)
            for r in records:
                out.append({"view": v.get("name"),
                            "viewObjectId": v["id"],
                            "zoneObjectId": zone["id"],
                            "objectId": r["id"], "type": r.get("type"),
                            "name": r.get("absoluteName"),
                            "ttl": r.get("ttl")})
    return out


def query_zones(client, view=None, kind="all"):
    """All zones per view, classified fwd/rev/ext/other, with objectIds."""
    views = client.all(f"{BASE_PATH}/views", fields=VIEW_FIELDS)
    if view:
        views = [v for v in views if v.get("name") == view]
        if not views:
            raise BAMError(f"no view named {view!r}")
    kinds = {"all": None, "fwd": {"fwd"}, "rev": {"rev"},
             "ext": {"external-hosts"},
             "other": {"enum", "internal-root", "response-policy",
                       "other"}}[kind]
    out = []
    for v in views:
        tops = client.all(f"{BASE_PATH}/views/{v['id']}/zones",
                          fields=ZONE_FIELDS)
        zones = list(tops)
        for top in tops:
            if zone_kind(top) == "fwd":
                zones.extend(walk_zones(client, top["id"]))
        for z in zones:
            zkind = zone_kind(z)
            if kinds and zkind not in kinds:
                continue
            out.append({"view": v.get("name"), "viewObjectId": v["id"],
                        "objectId": z["id"], "type": z.get("type"),
                        "kind": zkind, "name": z.get("absoluteName")
                        or z.get("name"),
                        "deployable": z.get("deployable"),
                        "signed": z.get("signed")})
    return out


def query_search(client, query, kind="all", quiet=False):
    """Substring search over zones, records, networks, addresses, hosts.

    kind="all" returns {"totals": {kind: count}, "matches": [...]}; a single
    kind returns {"total": count, "matches": [...]}.
    """
    if kind == "all":
        out = []
        totals = {}
        for k, (path, field) in SEARCH_KINDS.items():
            flt = build_filter(field, "contains", query)
            if k == "hosts":
                flt += (f" and "
                        f"{build_filter('type', 'eq', 'ExternalHostRecord')}")
            records, total = client.collection(
                path, filter_=flt, page_size=100,
                fields="id,type,absoluteName,name,range,address,state,ttl",
                quiet=quiet)
            totals[k] = total
            for r in records:
                out.append({"kind": k, "objectId": r.get("id"),
                            "type": r.get("type"),
                            "name": r.get("absoluteName") or r.get("name")
                            or r.get("address") or r.get("range")})
        return {"totals": totals, "matches": out}
    if kind not in SEARCH_KINDS:
        raise BAMError(f"--kind must be one of {', '.join(SEARCH_KINDS)}")
    path, field = SEARCH_KINDS[kind]
    filter_ = build_filter(field, "contains", query)
    if kind == "hosts":
        filter_ += f" and {build_filter('type', 'eq', 'ExternalHostRecord')}"
    records, total = client.collection(path, filter_=filter_,
        fields="id,type,absoluteName,name,range,address,state,ttl,"
               "rdata,comment", quiet=quiet)
    out = []
    for r in records:
        row = {"objectId": r.get("id"), "type": r.get("type"),
               "name": r.get("absoluteName") or r.get("name")
               or r.get("address") or r.get("range"),
               "ttl": r.get("ttl")}
        if kind in ("networks", "addresses"):
            row["detail"] = r.get("range") or r.get("address")
        if kind == "addresses":
            row["state"] = r.get("state")
        out.append(row)
    return {"total": total, "matches": out}


def query_network(client, target, ips=False, ptrs=False, dhcp=False,
                  roles=False, limit=10000, quiet=False):
    """Network details: meta, DHCP ranges, deployment roles, and IPs.

    `target` is an objectId, exact range, or name (resolved via
    resolve_network). Returns a "sections" dict with a "network" row always
    present, plus "dhcpRanges"/"deploymentRoles"/"addresses"+"addressesTotal"
    when the matching flag is set.
    """
    net = resolve_network(client, target)
    meta = client.get(f"{BASE_PATH}/networks/{net['id']}",
                      what=f"network #{net['id']}")
    row = {"objectId": meta["id"], "type": meta.get("type"),
           "name": meta.get("name"), "range": meta.get("range"),
           "gateway": meta.get("gateway"),
           "location": ref_label(meta.get("location")) if meta.get(
               "location") else "",
           "defaultView": ref_label(meta.get("defaultView"))
           if meta.get("defaultView") else ""}
    sections = {"network": row}

    if dhcp:
        ranges, _total = client.collection(
            f"{BASE_PATH}/networks/{meta['id']}/ranges",
            fields="id,type,name,range", quiet=quiet)
        sections["dhcpRanges"] = ranges
    if roles:
        deployment_roles, _total = client.collection(
            f"{BASE_PATH}/networks/{meta['id']}/deploymentRoles",
            fields="id,type,name,absoluteName", quiet=quiet)
        sections["deploymentRoles"] = deployment_roles
    if ips:
        # Ask for only the first `limit` addresses in ONE request instead of
        # paging the whole collection and slicing afterwards - big speedup on
        # large networks. The request limit is clamped to a safe page size.
        limit = min(max(int(limit), 1), 10000)
        params = {"limit": limit, "total": "true",
                  "fields": "id,address,state,name,macAddress,location"}
        qs = urllib.parse.urlencode(params)
        payload = client.get(
            f"{BASE_PATH}/networks/{meta['id']}/addresses?{qs}",
            what=f"addresses in network #{meta['id']}")
        addrs = payload.get("data", [])
        total = payload.get("totalCount", len(addrs))
        out = []
        for a in addrs:
            r = {"objectId": a["id"], "address": a.get("address"),
                 "state": a.get("state"), "name": a.get("name") or "",
                 "macAddress": a.get("macAddress") or "",
                 "location": ref_label(a.get("location"))
                 if a.get("location") else ""}
            if ptrs:
                linked = linked_records(client, a["id"])
                r["ptr"] = ", ".join(
                    l["absoluteName"] or l["name"]
                    for l in linked
                    if l.get("type") in ("GenericRecord", "PTRRecord")) or ""
                r["linked"] = ", ".join(
                    l.get("absoluteName") or l.get("name") or l.get("type")
                    for l in linked) or ""
            out.append(r)
        sections["addresses"] = out
        sections["addressesTotal"] = total
    return sections


def query_zone(client, zone_id, records=False, quiet=False):
    """One zone by (already-resolved) objectId, optionally with its records."""
    zone = client.get(f"{BASE_PATH}/zones/{zone_id}", what=f"zone {zone_id}")
    out = {"zone": zone}
    if records:
        recs, total = client.collection(
            f"{BASE_PATH}/zones/{zone['id']}/resourceRecords",
            fields="id,type,absoluteName,ttl,rdata,addresses,comment",
            quiet=quiet)
        out["records"] = recs
        out["recordCount"] = total
    return out


def query_records(client, zone=None, view=None, quiet=False):
    """Resource records for one zone (already-resolved objectId) or an
    entire view (every forward zone), with a zone column."""
    if zone:
        zones = [client.get(f"{BASE_PATH}/zones/{zone}", what=f"zone {zone}")]
    else:
        views = client.all(f"{BASE_PATH}/views", fields=VIEW_FIELDS)
        if view:
            views = [v for v in views if v.get("name") == view]
            if not views:
                raise BAMError(f"no view named {view!r}")
        zones = []
        for v in views:
            tops = client.all(f"{BASE_PATH}/views/{v['id']}/zones",
                              fields=ZONE_FIELDS)
            for top in tops:
                if zone_kind(top) == "fwd":
                    zones.append(top)
                    zones.extend(walk_zones(client, top["id"]))
        if not zones:
            raise BAMError("no forward zones found - pass a zone id/name "
                           "or --view NAME")
    out = []
    for z in zones:
        label = z.get("absoluteName") or z.get("name")
        records, _total = client.collection(
            f"{BASE_PATH}/zones/{z['id']}/resourceRecords",
            fields="id,type,absoluteName,ttl,rdata,addresses,comment",
            page_size=100, quiet=quiet)
        for r in records:
            out.append({"zone": label, "objectId": r.get("id"),
                        "type": r.get("type"),
                        "name": r.get("absoluteName"),
                        "ttl": r.get("ttl"), "rdata": r.get("rdata"),
                        "addresses": r.get("addresses"),
                        "comment": r.get("comment")})
    return out


def query_ip(client, address, quiet=False):
    """Look up one IP address: state, name, and linked host records."""
    hits, _total = client.collection(f"{BASE_PATH}/addresses",
        filter_=build_filter("address", "eq", address), page_size=10,
        fields="id,address,state,name,macAddress", quiet=quiet)
    if not hits:
        raise BAMError(f"no address matches {address!r}")
    addr = hits[0]
    row = {"objectId": addr["id"], "address": addr.get("address"),
           "state": addr.get("state"), "name": addr.get("name") or "",
           "macAddress": addr.get("macAddress") or ""}
    linked = linked_records(client, addr["id"])
    linked_rows = [{"objectId": l.get("id"), "type": l.get("type"),
                    "name": l.get("absoluteName") or l.get("name") or ""}
                   for l in linked]
    return {"address": row, "linked": linked_rows}


def query_summary(client):
    """Quick overview: counts of the main object types."""
    def count(path):
        payload = client.get(f"{BASE_PATH}{path}?limit=1&total=true",
                             what=f"count {path.strip('/')}")
        data = payload.get("data", [])
        return payload.get("totalCount", len(data) if data else 0)

    return [{"item": "configurations", "count": count("/configurations")},
            {"item": "views", "count": count("/views")},
            {"item": "zones", "count": count("/zones")},
            {"item": "networks", "count": count("/networks")}]
