#!/usr/bin/env python3
"""Shared HTTP transport for the BlueCat Address Manager (BAM) REST v2 API.

Used by bluecat_export.py and bluecat_discover.py, which used to each carry
their own copy of this code. Read-only: Client and _request only ever issue
GET requests; _request refuses any other method with a ReadOnlyError. The
sole exception anywhere in this module is the session-login POST, made
directly by _session_request() (used by authenticate()) rather than through
_request() - so the read-only guard can never be bypassed by accident.

Stdlib only. No progress-bar rendering, printing, or colour lives here -
that is caller-specific presentation, wired in via Client.collection's
on_progress(fetched, total) callback.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_PATH = "/api/v2"
PAGE_SIZE = 5000

READ_ONLY_MSG = ("READ-ONLY enforced: only GET requests are allowed. "
                 "The only permitted non-GET is the session login in "
                 "authenticate(); set $BAM_TOKEN to skip even that.")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class BAMError(Exception):
    """Known, user-facing BAM API failure."""


class BAMTimeout(BAMError):
    """A request to the appliance timed out (server not responding).

    The appliance stalls on some collections (ExternalHostsZone records at
    limit >= 200 and at certain offsets); a timed-out page is re-fetched one
    record at a time by Client.collection().
    """


class ReadOnlyError(BAMError):
    """A non-GET request was attempted against the API."""


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def _format_eta(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def build_query(offset, limit, want_total, filter_=None, fields=None, order_by=None):
    params = {"limit": limit, "offset": offset}
    if want_total:
        params["total"] = "true"
    if filter_:
        params["filter"] = filter_
    if fields:
        params["fields"] = fields
    if order_by:
        params["orderBy"] = order_by
    return urllib.parse.urlencode(params)


# --------------------------------------------------------------------------
# HTTP layer (GET-only)
# --------------------------------------------------------------------------

def _ssl_context(verify=False):
    ctx = ssl.create_default_context()
    if not verify:
        # Self-signed internal appliance cert; --verify opts back in.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(method, url, auth=None, body=None, verify=False, timeout=180):
    if method != "GET":
        raise ReadOnlyError(READ_ONLY_MSG)
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/hal+json")
    if auth:
        req.add_header("Authorization", f"Basic {auth}")
    return urllib.request.urlopen(req, context=_ssl_context(verify), timeout=timeout)


def _session_request(host, user, password, verify=False, timeout=60):
    """The one permitted POST: create a BAM session for the bearer token."""
    url = f"https://{host}{BASE_PATH}/sessions"
    body = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Accept", "application/hal+json")
    req.add_header("Content-Type", "application/hal+json")
    try:
        with urllib.request.urlopen(req, context=_ssl_context(verify),
                                    timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        raise BAMError(f"Authentication failed for user {user} @ {host}")
    except urllib.error.URLError as e:
        raise BAMError(f"Could not reach BAM host {host}: {e.reason}")
    except TimeoutError as e:
        raise BAMError(f"Could not reach BAM host {host}: {e}")


class Client:
    def __init__(self, host, auth, verify=False):
        self.host = host
        self.auth = auth
        self.verify = verify

    def get(self, path, what="request", timeout=30):
        url = f"https://{self.host}{path}"
        try:
            with _request("GET", url, auth=self.auth, verify=self.verify,
                          timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                payload = json.loads(e.read())
                detail = (f" - {payload.get('code', '')}: "
                          f"{payload.get('message', '')}").rstrip(": ")
            except Exception:
                pass
            if e.code == 404:
                raise BAMError(f"{what}: not found{detail}")
            if e.code == 401 and os.environ.get("BAM_TOKEN"):
                # BAM sessions expire; a token that worked an hour ago will not
                # work now, and the generic 401 reads like a wrong password.
                raise BAMError(
                    f"{what}: 401 - $BAM_TOKEN is invalid or has EXPIRED. "
                    "BAM sessions time out; re-issue one with:\n"
                    f"  curl -k -X POST 'https://{self.host}/api/v2/sessions' "
                    "-H 'Content-Type: application/hal+json' "
                    "-d '{\"username\":\"<u>\",\"password\":\"<p>\"}' "
                    "| jq -r .basicAuthenticationCredentials\n"
                    "Or unset BAM_TOKEN to log in interactively.")
            if e.code in (401, 403):
                raise BAMError(f"{what}: not authorized ({e.code}){detail}")
            raise BAMError(f"BAM API error {e.code}: {e.reason}{detail}")
        except urllib.error.URLError as e:
            raise BAMError(f"Could not reach BAM host {self.host}: {e.reason}")
        except TimeoutError as e:
            raise BAMTimeout(f"BAM request timed out: {what}")

    def collection(self, path, on_progress=None, filter_=None, fields=None,
                   order_by=None, page_size=PAGE_SIZE, timeout=30):
        """Page through a GET collection; returns (records, total).

        The appliance can stall on certain pages of some collections (observed
        on ExternalHostsZone records). A timed-out page is re-fetched one
        record at a time (limit=1 always works); a record that still times out
        is skipped rather than aborting the run.

        on_progress(fetched, total), if given, is called once per page
        fetched - the caller owns all presentation (bar, spinner, colour).
        """
        records, offset, total = [], 0, None
        while True:
            query = build_query(offset, page_size, total is None, filter_,
                                fields, order_by)
            try:
                payload = self.get(f"{path}?{query}", what=f"collection {path}",
                                   timeout=timeout)
                skipped_some = False
            except BAMTimeout:
                if page_size == 1:
                    raise
                sys.stderr.write(f"page timeout at offset {offset} - "
                                 f"fetching one record at a time\n")
                sys.stderr.flush()
                payload, skipped_some = self._fetch_page_individually(
                    path, offset, page_size, fields, order_by, filter_, timeout)
            if "data" not in payload:
                raise BAMError(
                    f"{path} did not return a collection (no 'data' array) - "
                    "this looks like a single resource, not a collection")
            if total is None:
                total = payload.get("totalCount", 0)
            page = payload["data"]
            records.extend(page)
            if on_progress:
                on_progress(len(records), total)
            if len(page) < page_size and not skipped_some:
                break
            offset += page_size
        return records, total

    def _fetch_page_individually(self, path, offset, size, fields, order_by,
                                 filter_, timeout):
        """Re-fetch one page record-by-record; retries stuck records once.

        The caller's filter is re-applied on every single-record request so a
        stalled page on a filtered collection never returns unfiltered rows.
        totalCount is captured from the first successful single-record fetch
        so a stalled page never loses the real count.
        """
        by_offset = {}
        total_count = None
        for i in range(size):
            query = build_query(offset + i, 1, False, filter_, fields, order_by)
            try:
                payload = self.get(f"{path}?{query}",
                                   what=f"record at offset {offset + i}",
                                   timeout=timeout)
                if total_count is None:
                    total_count = payload.get("totalCount")
                if payload.get("data"):
                    by_offset[offset + i] = payload["data"][0]
            except BAMTimeout:
                pass
        skipped = [off for off in range(offset, offset + size)
                   if off not in by_offset]
        for off in list(skipped):  # one retry pass
            query = build_query(off, 1, False, filter_, fields, order_by)
            try:
                payload = self.get(f"{path}?{query}",
                                   what=f"record at offset {off} (retry)",
                                   timeout=timeout)
                if payload.get("data"):
                    by_offset[off] = payload["data"][0]
            except BAMTimeout:
                pass
        skipped = [off for off in range(offset, offset + size)
                   if off not in by_offset]
        if skipped:
            sys.stderr.write(
                f"skipped {len(skipped)} record(s) at offsets "
                f"{skipped[:10]}{'…' if len(skipped) > 10 else ''}\n")
            sys.stderr.flush()
        return {"data": [by_offset[off] for off in sorted(by_offset)],
                "totalCount": total_count}, bool(skipped)

    def all(self, path, **kw):
        """Small collection, no progress bar - for menu population."""
        return self.collection(path, page_size=kw.pop("page_size", 1000), **kw)[0]


def authenticate(host, user, password, verify=False):
    payload = _session_request(host, user, password, verify)
    token = payload.get("basicAuthenticationCredentials")
    if not token:
        raise BAMError(f"Authentication failed for user {user} @ {host}")
    return token
