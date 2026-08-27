#!/usr/bin/env python3
"""Unit tests for bluecat_query - the shared record-shaping and query logic
used by both bluecat_export.py, bluecat_discover.py (CLI), and
bluecat_mcp.py (MCP server).

Pins the resolved divergence between the two callers' former separate
copies of flatten(): a null-valued field keeps its key (export's old
behaviour), it does not vanish from the row (discover's old behaviour).

The query_*() tests below call the shared query functions directly with a
fake client and assert on the returned data - no argparse, no tty, no MCP.
That is the point of moving them out of bluecat_discover.cmd_*() and
bluecat_mcp._tool_*(): the query-shaping logic is now testable on its own.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluecat_query as bq


def test_flatten_keeps_null_valued_key_with_none_value():
    row = bq.flatten({"id": 1, "name": None, "ttl": 300})
    assert "name" in row, "a None-valued field must keep its key, not vanish"
    assert row["name"] is None
    assert row == {"id": 1, "name": None, "ttl": 300}


def test_ref_label_precedence_address_range_absoluteName_name():
    """address beats range beats absoluteName beats name - an IPv4Address
    carries an empty name alongside its address, so name must lose."""
    assert bq.ref_label({"address": "10.0.0.1", "range": "10.0.0.0/24",
                         "absoluteName": "a.example.com", "name": "a"}) \
        == "10.0.0.1"
    assert bq.ref_label({"range": "10.0.0.0/24",
                         "absoluteName": "a.example.com", "name": "a"}) \
        == "10.0.0.0/24"
    assert bq.ref_label({"absoluteName": "a.example.com", "name": "a"}) \
        == "a.example.com"
    assert bq.ref_label({"name": "a"}) == "a"
    assert bq.ref_label({"id": 1}) == '{"id":1}'


# ---------------------------------------------------------------------------
# fake client for the query_*() tests
# ---------------------------------------------------------------------------

class FakeClient:
    """BAM client stand-in with canned responses keyed by path.

    get() ignores the querystring when matching (query_network builds one
    with a variable `limit`); collection() and all() match on the exact path
    used by the query - a missing all() key returns [] (no children/rows),
    matching a real empty BAM collection.
    """

    def __init__(self, get=None, collection=None, all_=None):
        self._get = dict(get or {})
        self._collection = dict(collection or {})
        self._all = dict(all_ or {})
        self.calls = []

    def get(self, path, what="request", timeout=30):
        self.calls.append(("get", path))
        base = path.split("?", 1)[0]
        if base not in self._get:
            raise AssertionError(f"no canned GET for {path}")
        return self._get[base]

    def collection(self, path, filter_=None, fields=None, order_by=None,
                   page_size=1000, quiet=False):
        self.calls.append(("collection", path))
        if path not in self._collection:
            raise AssertionError(f"no canned collection for {path}")
        return self._collection[path]

    def all(self, path, **kw):
        self.calls.append(("all", path))
        return self._all.get(path, [])


# ---------------------------------------------------------------------------
# query_views
# ---------------------------------------------------------------------------

def test_query_views_returns_one_row_per_view():
    fake = FakeClient(all_={
        f"{bq.BASE_PATH}/configurations": [
            {"id": 1, "name": "cfg1", "type": "Configuration"}],
        f"{bq.BASE_PATH}/configurations/1/views": [
            {"id": 10, "name": "Authoritative", "type": "View"}],
    })
    assert bq.query_views(fake) == [
        {"configurationId": 1, "configuration": "cfg1", "objectId": 10,
         "type": "View", "view": "Authoritative"}]


# ---------------------------------------------------------------------------
# query_hosts
# ---------------------------------------------------------------------------

def test_query_hosts_returns_rows_and_calls_on_zone_once_per_zone():
    fake = FakeClient(
        all_={
            f"{bq.BASE_PATH}/views": [
                {"id": 10, "name": "Authoritative", "type": "View"}],
            f"{bq.BASE_PATH}/views/10/zones": [
                {"id": 100, "name": "ExternalHostsZone",
                 "type": "ExternalHostsZone"}],
        },
        collection={
            f"{bq.BASE_PATH}/zones/100/resourceRecords": (
                [{"id": 5, "type": "ExternalHostRecord",
                  "absoluteName": "ext.example.com", "ttl": 300}], 1),
        },
    )
    seen = []
    rows = bq.query_hosts(
        fake, on_zone=lambda v, z, recs: seen.append(
            (v["name"], z["id"], len(recs))))
    assert rows == [{"view": "Authoritative", "viewObjectId": 10,
                     "zoneObjectId": 100, "objectId": 5,
                     "type": "ExternalHostRecord",
                     "name": "ext.example.com", "ttl": 300}]
    assert seen == [("Authoritative", 100, 1)]


def test_query_hosts_unknown_view_raises():
    fake = FakeClient(all_={
        f"{bq.BASE_PATH}/views": [{"id": 10, "name": "Authoritative"}]})
    with pytest.raises(bq.BAMError):
        bq.query_hosts(fake, view="Nope")


# ---------------------------------------------------------------------------
# query_zones
# ---------------------------------------------------------------------------

def test_query_zones_classifies_and_walks_children():
    fake = FakeClient(all_={
        f"{bq.BASE_PATH}/views": [{"id": 10, "name": "Authoritative"}],
        f"{bq.BASE_PATH}/views/10/zones": [
            {"id": 100, "name": "example.com",
             "absoluteName": "example.com", "type": "Zone",
             "deployable": True, "signed": False}],
        f"{bq.BASE_PATH}/zones/100/zones": [],
    })
    assert bq.query_zones(fake) == [
        {"view": "Authoritative", "viewObjectId": 10, "objectId": 100,
         "type": "Zone", "kind": "fwd", "name": "example.com",
         "deployable": True, "signed": False}]


def test_query_zones_kind_filter_excludes_non_matching():
    fake = FakeClient(all_={
        f"{bq.BASE_PATH}/views": [{"id": 10, "name": "Authoritative"}],
        f"{bq.BASE_PATH}/views/10/zones": [
            {"id": 100, "name": "example.com",
             "absoluteName": "example.com", "type": "Zone"}],
        f"{bq.BASE_PATH}/zones/100/zones": [],
    })
    assert bq.query_zones(fake, kind="rev") == []


# ---------------------------------------------------------------------------
# query_search
# ---------------------------------------------------------------------------

def test_query_search_single_kind():
    fake = FakeClient(collection={
        "/api/v2/zones": ([{"id": 1, "type": "Zone",
                            "absoluteName": "example.com"}], 1),
    })
    result = bq.query_search(fake, "time", kind="zones")
    assert result == {"total": 1, "matches": [
        {"objectId": 1, "type": "Zone", "name": "example.com", "ttl": None}]}


def test_query_search_all_kinds_one_row_per_kind():
    paths = {path for path, _field in bq.SEARCH_KINDS.values()}
    hit = [{"id": 1, "type": "Zone", "absoluteName": "example.com"}], 1
    fake = FakeClient(collection={path: hit for path in paths})
    result = bq.query_search(fake, "time", kind="all")
    assert set(result["totals"]) == set(bq.SEARCH_KINDS)
    assert len(result["matches"]) == len(bq.SEARCH_KINDS)


def test_query_search_bad_kind_raises():
    with pytest.raises(bq.BAMError):
        bq.query_search(FakeClient(), "x", kind="bogus")


# ---------------------------------------------------------------------------
# query_network
# ---------------------------------------------------------------------------

def test_query_network_sections_by_flag():
    fake = FakeClient(
        get={
            f"{bq.BASE_PATH}/networks/1": {
                "id": 1, "type": "IPv4Network", "name": "n",
                "range": "10.0.0.0/24", "gateway": "10.0.0.1",
                "location": None, "defaultView": None},
            f"{bq.BASE_PATH}/networks/1/addresses": {
                "data": [{"id": 21, "address": "10.0.0.5",
                          "state": "STATIC", "name": "a",
                          "macAddress": "", "location": None}],
                "totalCount": 1},
        },
        collection={
            f"{bq.BASE_PATH}/networks/1/ranges": (
                [{"id": 2, "type": "DHCP4Range", "name": "r",
                  "range": "10.0.0.10-10.0.0.20"}], 1),
            f"{bq.BASE_PATH}/networks/1/deploymentRoles": ([], 0),
        },
    )
    sections = bq.query_network(fake, "1", ips=True, dhcp=True, roles=True)
    assert sections["network"]["objectId"] == 1
    assert sections["network"]["range"] == "10.0.0.0/24"
    assert sections["dhcpRanges"][0]["id"] == 2
    assert sections["deploymentRoles"] == []
    assert sections["addresses"] == [
        {"objectId": 21, "address": "10.0.0.5", "state": "STATIC",
         "name": "a", "macAddress": "", "location": ""}]
    assert sections["addressesTotal"] == 1


def test_query_network_no_flags_returns_only_network_section():
    fake = FakeClient(get={
        f"{bq.BASE_PATH}/networks/1": {
            "id": 1, "type": "IPv4Network", "name": "n",
            "range": "10.0.0.0/24", "gateway": None,
            "location": None, "defaultView": None}})
    assert list(bq.query_network(fake, "1")) == ["network"]


# ---------------------------------------------------------------------------
# query_zone
# ---------------------------------------------------------------------------

def test_query_zone_with_records():
    fake = FakeClient(
        get={f"{bq.BASE_PATH}/zones/100": {
            "id": 100, "name": "example.com",
            "absoluteName": "example.com", "type": "Zone"}},
        collection={f"{bq.BASE_PATH}/zones/100/resourceRecords": (
            [{"id": 5, "type": "GenericRecord",
              "absoluteName": "www.example.com", "ttl": 3600,
              "rdata": "1.2.3.4"}], 1)},
    )
    result = bq.query_zone(fake, 100, records=True)
    assert result["zone"]["absoluteName"] == "example.com"
    assert result["records"][0]["absoluteName"] == "www.example.com"
    assert result["recordCount"] == 1


def test_query_zone_without_records_omits_records_key():
    fake = FakeClient(get={f"{bq.BASE_PATH}/zones/100": {
        "id": 100, "name": "example.com"}})
    result = bq.query_zone(fake, 100)
    assert "records" not in result


# ---------------------------------------------------------------------------
# query_records
# ---------------------------------------------------------------------------

def test_query_records_by_zone():
    fake = FakeClient(
        get={f"{bq.BASE_PATH}/zones/100": {
            "id": 100, "name": "example.com",
            "absoluteName": "example.com"}},
        collection={f"{bq.BASE_PATH}/zones/100/resourceRecords": (
            [{"id": 5, "type": "HostRecord",
              "absoluteName": "a.example.com", "ttl": 300, "rdata": None,
              "addresses": None, "comment": None}], 1)},
    )
    assert bq.query_records(fake, zone=100) == [
        {"zone": "example.com", "objectId": 5, "type": "HostRecord",
         "name": "a.example.com", "ttl": 300, "rdata": None,
         "addresses": None, "comment": None}]


def test_query_records_by_view_walks_forward_zones():
    fake = FakeClient(
        all_={
            f"{bq.BASE_PATH}/views": [{"id": 10, "name": "Authoritative"}],
            f"{bq.BASE_PATH}/views/10/zones": [
                {"id": 100, "name": "example.com",
                 "absoluteName": "example.com", "type": "Zone"}],
            f"{bq.BASE_PATH}/zones/100/zones": [],
        },
        collection={f"{bq.BASE_PATH}/zones/100/resourceRecords": (
            [{"id": 5, "type": "HostRecord",
              "absoluteName": "a.example.com", "ttl": 300, "rdata": None,
              "addresses": None, "comment": None}], 1)},
    )
    rows = bq.query_records(fake, view="Authoritative")
    assert rows[0]["zone"] == "example.com"


def test_query_records_no_forward_zones_raises():
    fake = FakeClient(all_={
        f"{bq.BASE_PATH}/views": [{"id": 10, "name": "Authoritative"}],
        f"{bq.BASE_PATH}/views/10/zones": [],
    })
    with pytest.raises(bq.BAMError, match="no forward zones"):
        bq.query_records(fake)


# ---------------------------------------------------------------------------
# query_ip
# ---------------------------------------------------------------------------

def test_query_ip_found_with_linked_records():
    fake = FakeClient(collection={
        f"{bq.BASE_PATH}/addresses": (
            [{"id": 21, "address": "10.0.0.5", "state": "STATIC",
              "name": "a", "macAddress": ""}], 1),
        f"{bq.BASE_PATH}/addresses/21/resourceRecords": (
            [{"id": 5, "type": "HostRecord",
              "absoluteName": "a.example.com"}], 1),
    })
    result = bq.query_ip(fake, "10.0.0.5")
    assert result["address"]["address"] == "10.0.0.5"
    assert result["linked"] == [
        {"objectId": 5, "type": "HostRecord", "name": "a.example.com"}]


def test_query_ip_not_found_raises():
    fake = FakeClient(collection={f"{bq.BASE_PATH}/addresses": ([], 0)})
    with pytest.raises(bq.BAMError):
        bq.query_ip(fake, "9.9.9.9")


# ---------------------------------------------------------------------------
# query_summary
# ---------------------------------------------------------------------------

def test_query_summary_counts_each_object_type():
    fake = FakeClient(get={
        f"{bq.BASE_PATH}/configurations": {"data": [], "totalCount": 2},
        f"{bq.BASE_PATH}/views": {"data": [], "totalCount": 3},
        f"{bq.BASE_PATH}/zones": {"data": [], "totalCount": 40},
        f"{bq.BASE_PATH}/networks": {"data": [], "totalCount": 5},
    })
    assert bq.query_summary(fake) == [
        {"item": "configurations", "count": 2},
        {"item": "views", "count": 3},
        {"item": "zones", "count": 40},
        {"item": "networks", "count": 5}]
