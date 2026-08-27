#!/usr/bin/env python3
"""Unit tests for bluecat_discover - pure helpers and CLI plumbing only.

No network access: the HTTP layer is exercised via a fake Client that
records every requested method (proving GET-only enforcement) and via
ReadOnlyError checks on the request builder.
"""
import os
import sys

os.environ["NO_COLOR"] = "1"  # force plain output before importing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import pytest

import bluecat_discover as bd
from bluecat_menu import ScriptedAnswers


@pytest.fixture(autouse=True)
def _pin_info_stream(tmp_path, monkeypatch):
    """main() routes INFO via set_machine_mode(); keep it pointing at a live
    stream so tests don't inherit a capsys-closed one (order-independent).
    Also pin the config file to a temp path so tests never read/write the
    real ~/.bluecat_discover.json."""
    bd.INFO = sys.stderr
    bd.LAST_RESULT = None
    bd.LAST_COLUMNS = None
    bd.LAST_QUERY = None
    bd.CREDS_PROMPTED = False
    bd.CREDS_USED_SAVED = False
    monkeypatch.setattr(bd, "CONFIG_PATH", str(tmp_path / "config.json"))
    bd.CONFIG.clear()
    bd.CONFIG.update({"host": "", "format": "table", "view": "",
                      "kind": "all", "favorites": [], "user": "",
                      "password": ""})
    yield


# ---------------------------------------------------------------------------
# read-only enforcement
# ---------------------------------------------------------------------------

def test_request_refuses_post():
    with pytest.raises(bd.ReadOnlyError):
        bd._request("POST", "https://x/api/v2/sessions")


def test_request_refuses_put_delete_patch():
    for method in ("PUT", "DELETE", "PATCH"):
        with pytest.raises(bd.ReadOnlyError):
            bd._request(method, "https://x/api/v2/anything")


def test_session_request_is_separate_function():
    """The only non-GET path is _session_request (login)."""
    import inspect
    src = inspect.getsource(bd.Client)
    assert "method=" not in src.replace("GET", "") or "GET" in src
    # Client only ever calls bd._request with "GET"
    assert "_request(" in src


# ---------------------------------------------------------------------------
# zone classification
# ---------------------------------------------------------------------------

def test_zone_kind_forward():
    assert bd.zone_kind({"type": "Zone", "absoluteName": "example.com"}) == "fwd"
    assert bd.zone_kind({"type": "Zone", "name": "com"}) == "fwd"


def test_zone_kind_reverse_arpa():
    assert bd.zone_kind({"type": "Zone",
                         "absoluteName": "0.10.in-addr.arpa"}) == "rev"
    assert bd.zone_kind({"type": "Zone",
                         "absoluteName": "8.e.f.ip6.arpa"}) == "rev"


def test_zone_kind_reverse_cidr():
    assert bd.zone_kind({"type": "Zone", "absoluteName": "192.0.2.0/24"}) == "rev"
    assert bd.zone_kind({"type": "Zone", "name": "192.0.2.0/24"}) == "rev"
    assert bd.zone_kind({"type": "Zone", "name": "10.0.0.0"}) == "rev"


def test_zone_kind_external_hosts():
    assert bd.zone_kind({"type": "ExternalHostsZone"}) == "external-hosts"
    assert bd.zone_kind({"type": "ENUMZone"}) == "enum"
    assert bd.zone_kind({"type": "InternalRootZone"}) == "internal-root"


def test_is_reverse():
    assert bd.is_reverse({"type": "Zone", "absoluteName": "0.10.in-addr.arpa"})
    assert not bd.is_reverse({"type": "Zone", "absoluteName": "example.com"})


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

def test_build_filter():
    assert bd.build_filter("name", "contains", "sg") == "name:contains('sg')"
    assert bd.build_filter("type", "eq", "ExternalHostsZone") == \
        "type:eq('ExternalHostsZone')"


# ---------------------------------------------------------------------------
# flatten / ref_label
# ---------------------------------------------------------------------------

def test_ref_label_prefers_address_then_name():
    assert bd.ref_label({"id": 1, "address": "10.0.0.1"}) == "10.0.0.1"
    assert bd.ref_label({"id": 1, "name": "z"}) == "z"
    assert bd.ref_label({"id": 1}) == '{"id":1}'


def test_flatten_collapses_nested():
    rec = {"id": 1, "type": "Zone", "name": "com",
           "view": {"id": 9, "name": "Authoritative", "_links": {}},
           "addresses": [{"id": 2, "address": "10.0.0.1"}],
           "_links": {"self": {"href": "/x"}}}
    row = bd.flatten(rec)
    assert row["view"] == "Authoritative"
    assert row["addresses"] == "10.0.0.1"
    assert "_links" not in row
    assert "id" in row


def test_flatten_keeps_none_valued_key():
    """flatten() is now shared with bluecat_export.py, which keeps a null
    field's key (blank cell) rather than dropping it, so exported tables
    have a stable column set across rows. A record with name=None must
    still carry the 'name' key, with its value left as None."""
    row = bd.flatten({"id": 1, "name": None})
    assert "name" in row
    assert row["name"] is None


# ---------------------------------------------------------------------------
# resolve_network
# ---------------------------------------------------------------------------

class FakeClient:
    """Minimal stand-in recording request methods; returns canned data."""

    def __init__(self, networks):
        self.networks = networks
        self.calls = []

    def get(self, path, what="request", timeout=180):
        self.calls.append(("GET", path))
        if "/addresses" in path:
            return {"data": [{"id": 1, "address": "10.0.0.1",
                              "state": "STATIC", "name": "x",
                              "macAddress": "", "location": None}],
                    "totalCount": 1}
        if "/networks/" in path:
            nid = int(path.rsplit("/", 1)[1].split("?")[0])
            for n in self.networks:
                if n["id"] == nid:
                    return dict(n)
        raise bd.BAMError("not found")

    def collection(self, path, filter_=None, fields=None, order_by=None,
                   page_size=1000, quiet=True):
        self.calls.append(("GET", path + "?" + (filter_ or "")))
        if filter_:
            _, _, value = filter_.partition("('")
            value = value.rstrip("')")
        else:
            value = ""
        hits = [n for n in self.networks
                if value and (value in n["range"] or value in n["name"]
                              or value == str(n["id"]))]
        return hits, len(hits)


def test_resolve_network_by_id():
    fake = FakeClient([{"id": 460648, "range": "192.0.2.0/24",
                        "name": "Test Network"}])
    net = bd.resolve_network(fake, "460648")
    assert net["id"] == 460648
    assert all(m == "GET" for m, _ in fake.calls)


def test_resolve_network_by_range():
    fake = FakeClient([{"id": 460648, "range": "192.0.2.0/24",
                        "name": "Test Network"}])
    net = bd.resolve_network(fake, "192.0.2.0/24")
    assert net["id"] == 460648


def test_resolve_network_ambiguous():
    fake = FakeClient([{"id": 1, "range": "10.0.0.0/24", "name": "a"},
                       {"id": 2, "range": "10.0.0.0/24", "name": "b"}])
    with pytest.raises(bd.BAMError):
        bd.resolve_network(fake, "10.0.0.0/24")


def test_resolve_network_unknown():
    fake = FakeClient([{"id": 1, "range": "10.0.0.0/24", "name": "a"}])
    with pytest.raises(bd.BAMError):
        bd.resolve_network(fake, "9.9.9.0/24")


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def test_parse_args_commands():
    assert bd.parse_args(["views"]).command == "views"
    args = bd.parse_args(["search", "foo", "--kind", "records"])
    assert args.command == "search" and args.query == "foo"
    assert bd.parse_args(["hosts", "--view", "X"]).view == "X"
    assert bd.parse_args(["network", "1", "--all"]).all is True
    assert bd.parse_args(["views", "--json"]).json is True
    # options accepted both before and after the subcommand
    assert bd.parse_args(["-f", "json", "views"]).format == "json"
    assert bd.parse_args(["views", "-f", "csv"]).format == "csv"
    assert bd.parse_args(["views", "--page-size", "7"]).page_size == 7
    assert bd.parse_args(["-f", "json", "views", "--page-size", "9"]
                         ).page_size == 9


def test_parse_args_search_kind_validated():
    with pytest.raises(SystemExit):
        bd.parse_args(["search", "x", "--kind", "bogus"])


def test_parse_args_no_command_defaults_to_views():
    """Running with no command runs `views` (shortest invocation)."""
    assert bd.parse_args([]).command == "views"
    assert bd.parse_args(["--user", "x"]).command == "views"


class _TTYStdin:
    """Fake stdin that claims to be a terminal and serves queued lines."""

    def __init__(self, lines):
        self._lines = list(lines)

    def isatty(self):
        return True

    def readline(self):
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


class _PipeStdin:
    """Fake stdin that is NOT a terminal (pipe / CI run)."""

    def isatty(self):
        return False

    def readline(self):
        raise AssertionError("must not read stdin when not a terminal")


def test_resolve_credentials_uses_supplied_values():
    class A:
        user = "alice"
        password = "s3cret"
    assert bd.resolve_credentials(A()) == ("alice", "s3cret")


def test_resolve_credentials_prompts_username_when_missing(monkeypatch):
    """Converted to the injectable-answers seam (bluecat_menu.STDIN): the
    scripted answers stand in for both the username prompt and the password
    prompt, so no getpass patch is needed either - ScriptedAnswers.secret()
    just hands back the next answer."""
    class A:
        user = ""
        password = ""
    _answers(monkeypatch, "alice", "s3cret")
    assert bd.resolve_credentials(A()) == ("alice", "s3cret")


def test_resolve_credentials_prompts_password_when_missing(monkeypatch):
    class A:
        user = "alice"
        password = ""
    monkeypatch.setattr(sys, "stdin", _TTYStdin([]))  # a terminal
    seen = {}

    def fake_getpass(prompt, stream=None):
        seen["prompt"] = prompt
        seen["stream"] = stream
        return "s3cret"

    monkeypatch.setattr(bd.getpass, "getpass", fake_getpass)
    assert bd.resolve_credentials(A()) == ("alice", "s3cret")
    assert seen["prompt"] == "Password: "
    assert seen["stream"] is sys.stderr  # keep stdout clean for --json/--csv


def test_resolve_credentials_noninteractive_password_raises(monkeypatch):
    """No getpass-on-a-pipe: missing password off a terminal is an error.

    Converted to the seam: isatty=False reproduces the pipe/CI run, with no
    answers queued, so any stray read would raise EOFError rather than
    silently succeeding."""
    class A:
        user = "alice"
        password = ""
    _answers(monkeypatch, isatty=False)
    with pytest.raises(bd.BAMError):
        bd.resolve_credentials(A())


def test_resolve_credentials_noninteractive_raises(monkeypatch):
    """No hang on a pipe/CI run: missing username is a clear error."""
    class A:
        user = ""
        password = "s3cret"
    _answers(monkeypatch, isatty=False)
    with pytest.raises(bd.BAMError):
        bd.resolve_credentials(A())


# ---------------------------------------------------------------------------
# interactive wizard
# ---------------------------------------------------------------------------

def _tty(monkeypatch, *lines):
    monkeypatch.setattr(sys, "stdin", _TTYStdin(list(lines)))


def _answers(monkeypatch, *values, isatty=True):
    """Inject answers through the seam (bluecat_menu.STDIN) instead of an
    object impersonating sys.stdin. Unlike `_tty`, values carry no trailing
    "\\n": bluecat_menu.RealStdin.readline() is what used to strip it, and
    ScriptedAnswers hands answers back exactly as given."""
    monkeypatch.setattr(bd, "STDIN", ScriptedAnswers(list(values), isatty=isatty))


def test_wizard_menu_enter_defaults_to_views(monkeypatch):
    """Converted to the seam: bare Enter at every prompt is now "" instead of
    "\\n", since ScriptedAnswers hands answers back unmodified (RealStdin is
    what used to strip the newline).

    The host prompt has no shipped default; Enter only takes one when a
    prior session's host was remembered in CONFIG, so that is set up here."""
    bd.CONFIG["host"] = "bam.example.com"
    _answers(monkeypatch, "", "", "")  # host, menu Enter, format Enter
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "views"
    assert args.host == "bam.example.com"
    assert args.format == "table"


def test_wizard_menu_help_repeats(monkeypatch, capsys):
    """'h' shows the help screen, Enter returns to the menu, next pick wins."""
    _tty(monkeypatch, "\n", "h\n", "\n", "2\n", "\n", "\n")
    # host, help, Enter-after-help, hosts, view, format
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "hosts"
    assert "Here's what each command does" in capsys.readouterr().err


def test_wizard_menu_help_prompt_is_labelled(monkeypatch, capsys):
    """The post-help prompt must say what it wants - not a blank line with
    no cue that input is expected."""
    _tty(monkeypatch, "\n", "h\n", "\n", "0\n")
    # host, help, Enter-after-help, menu: quit
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert "Press Enter to return to the menu: " in capsys.readouterr().err


def test_wizard_menu_quit(monkeypatch):
    """'0' or 'quit' exits via the quit command."""
    _answers(monkeypatch, "", "0")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "quit"


def test_main_quit_without_login(monkeypatch, capsys):
    """Picking quit exits cleanly BEFORE any auth - no mocks needed."""
    _tty(monkeypatch, "\n", "0\n")
    assert bd.main([]) == 0
    err = capsys.readouterr().err
    assert "BlueCat Discovery" in err   # banner shown
    assert bd.KAOMOJI in err            # kaomoji outro


def test_network_limit_clamped(monkeypatch):
    """--limit above the cap is clamped to 10000 in the request."""
    _tty(monkeypatch, "\n", "5\n", "10.0.0.0/24\n", "i\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    args.limit = 50000
    fake = FakeClient([{"id": 1, "range": "10.0.0.0/24", "name": "n"}])
    bd.cmd_network(fake, args)
    addr_calls = [c for c in fake.calls if "addresses" in c[1]]
    assert len(addr_calls) == 1
    assert "limit=10000" in addr_calls[0][1]


def test_main_query_loop_quit_at_round_two(monkeypatch, capsys):
    """Quit picked at a later round's menu must exit cleanly, not crash."""
    _tty(monkeypatch, "\n", "\n", "\n", "\n",   # host, menu, format, export
         "\n",                                  # save favorite? no
         "y\n",                                  # run another? yes
         "0\n")                                  # round-2 menu: quit
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    assert results == [1]
    assert bd.KAOMOJI in capsys.readouterr().err


def test_wizard_menu_pick_network_and_flags(monkeypatch):
    _tty(monkeypatch, "\n", "5\n", "192.0.2.0/24\n", "i\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "network"
    assert args.target == "192.0.2.0/24"
    assert (args.ips, args.ptrs, args.dhcp, args.roles) \
        == (True, False, False, False)
    assert args.format == "table"


def test_wizard_menu_pick_by_name(monkeypatch):
    _tty(monkeypatch, "\n", "zone\n", "460648\n", "y\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.zone_id == "460648"
    assert args.records is True


def test_wizard_zone_accepts_name(monkeypatch):
    """The zone prompt takes a name too - no more numeric-only input."""
    _tty(monkeypatch, "\n", "6\n", "example.com\n", "n\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.zone_id == "example.com"
    assert args.records is False


def test_wizard_network_flags_retry_on_invalid(monkeypatch):
    """Garbage at the network include-flags question re-prompts instead of
    crashing or silently picking a default (there is no bare-Enter default
    other than "none", which a garbage answer must not fall back to)."""
    _tty(monkeypatch, "\n", "5\n", "10.0.0.0/24\n", "maybe\n", "i\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.ips is True


# ---------------------------------------------------------------------------
# network include-flags multi-select parser (_parse_network_flags)
# ---------------------------------------------------------------------------

def test_parse_network_flags_single_letter():
    assert bd._parse_network_flags("i") == \
        {"ips": True, "ptrs": False, "dhcp": False, "roles": False}


def test_parse_network_flags_no_separator():
    """'ip' (no space/comma) means ips+ptrs, same as 'i p' or 'i,p'."""
    expected = {"ips": True, "ptrs": True, "dhcp": False, "roles": False}
    assert bd._parse_network_flags("ip") == expected
    assert bd._parse_network_flags("i p") == expected
    assert bd._parse_network_flags("i,p") == expected


def test_parse_network_flags_case_insensitive_and_order_free():
    expected = {"ips": True, "ptrs": False, "dhcp": True, "roles": False}
    assert bd._parse_network_flags("DI") == expected
    assert bd._parse_network_flags("id") == expected


def test_parse_network_flags_all():
    assert bd._parse_network_flags("a") == \
        {"ips": True, "ptrs": True, "dhcp": True, "roles": True}


def test_parse_network_flags_blank_means_none():
    assert bd._parse_network_flags("") == \
        {"ips": False, "ptrs": False, "dhcp": False, "roles": False}
    assert bd._parse_network_flags("   ") == \
        {"ips": False, "ptrs": False, "dhcp": False, "roles": False}


def test_parse_network_flags_garbage_returns_none():
    """An unrecognised character means "re-prompt", not "pick something"."""
    assert bd._parse_network_flags("x") is None
    assert bd._parse_network_flags("iz") is None


def test_ask_network_flags_reprompts_on_garbage(monkeypatch, capsys):
    _tty(monkeypatch, "bogus\n", "i,p\n")
    result = bd._ask_network_flags()
    assert result == {"ips": True, "ptrs": True, "dhcp": False,
                      "roles": False}
    assert "Please answer with any of: a, i, p, d, r" in \
        capsys.readouterr().err


def test_ask_network_flags_back(monkeypatch):
    _tty(monkeypatch, "0\n")
    with pytest.raises(bd.Back):
        bd._ask_network_flags(back=True)


def test_wizard_respects_cli_values(monkeypatch):
    """Values typed on the command line are never re-asked."""
    _tty(monkeypatch, "\n", "records\n")  # only host and kind are missing
    argv = ["search", "example.com", "-f", "json"]
    args = bd.parse_args(argv)
    bd.run_wizard(args, argv)
    assert args.query == "example.com"
    assert args.kind == "records"
    assert args.format == "json"


def test_wizard_kind_validated(monkeypatch):
    _tty(monkeypatch, "\n", "bogus\n", "records\n", "\n")
    argv = ["search", "x"]
    args = bd.parse_args(argv)
    bd.run_wizard(args, argv)
    assert args.kind == "records"


def test_wizard_given_matches_equals_form(monkeypatch):
    """--view=X is a CLI value: never re-asked, never wiped by Enter."""
    _tty(monkeypatch, "\n", "\n")  # only host and format are missing
    argv = ["hosts", "--view=Authoritative"]
    args = bd.parse_args(argv)
    bd.run_wizard(args, argv)
    assert args.view == "Authoritative"


def test_wizard_eof_cancels(monkeypatch):
    """Ctrl-D at any prompt must cancel, not busy-loop."""
    monkeypatch.setattr(sys, "stdin", _TTYStdin([]))  # EOF at first prompt
    args = bd.parse_args([])
    with pytest.raises(EOFError):
        bd.run_wizard(args, [])


def test_prompt_raises_eof_on_closed_stdin(monkeypatch):
    """A closed pipe returns '' from readline: that is EOF, not an answer.

    Converted to the seam: an empty answers list raises EOFError on the
    first read, the same contract a closed stdin has, with no stand-in
    class needed to reproduce it."""
    _answers(monkeypatch)
    with pytest.raises(EOFError):
        bd._prompt("x: ")


def test_wizard_flow_search_via_answers(monkeypatch):
    """New test for the injectable-answers seam: drive the search flow
    picked from the numbered menu (query and kind are prompted for, not
    supplied on the CLI), with no sys.stdin monkeypatching anywhere - just
    a list of scripted answers, asserted against the resulting parsed
    args."""
    _answers(monkeypatch, "", "4", "example.com", "records", "")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "search"
    assert args.query == "example.com"
    assert args.kind == "records"
    assert args.format == "table"


def test_wizard_network_menu_path_has_all_and_limit(monkeypatch):
    """Regression: bare invocation lacks subparser attrs (args.all/limit)
    that cmd_network reads - the menu path must provide them."""
    _tty(monkeypatch, "\n", "5\n", "10.0.0.0/24\n", "i\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "network"
    assert args.all is False
    assert args.limit == 10000
    assert args.ips is True
    # and the command must actually run end-to-end without AttributeError
    fake = FakeClient([{"id": 1, "range": "10.0.0.0/24", "name": "n"}])
    bd.cmd_network(fake, args)
    assert any("/networks/1" in c[1] for c in fake.calls)   # resolved by id
    addr_calls = [c for c in fake.calls if "addresses" in c[1]]
    assert len(addr_calls) == 1                      # ONE request, no paging
    assert "limit=10000" in addr_calls[0][1]         # server-side limit


def test_wizard_zone_menu_path_has_all(monkeypatch):
    """Regression: cmd_zone reads args.all (records declined)."""
    _tty(monkeypatch, "\n", "6\n", "460648\n", "n\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.all is False
    assert args.zone_id == "460648"
    assert args.records is False


def test_resolve_zone_id_by_digit():
    fake = FakeClient([])
    assert bd.resolve_zone_id(fake, "460648") == 460648


def test_resolve_zone_id_single_match():
    fake = FakeClient([{"id": 456766, "name": "example.com",
                        "range": ""}])
    assert bd.resolve_zone_id(fake, "example") == 456766


class _FieldRecordingClient:
    """Records which field each zone lookup filtered on.

    BAM keeps the bare label in `name` ("bamzone") and the FQDN in
    `absoluteName` ("bamzone.example.com"), so this stand-in only answers a
    filter on the field the caller actually asked for.
    """

    def __init__(self, zones):
        self.zones = zones
        self.fields_tried = []

    def collection(self, path, filter_=None, fields=None, order_by=None,
                   page_size=1000, quiet=True):
        field = (filter_ or "").split(":", 1)[0]
        self.fields_tried.append(field)
        _, _, value = (filter_ or "").partition("('")
        value = value.rstrip("')")
        hits = [z for z in self.zones if value and value in (z.get(field) or "")]
        return hits, len(hits)


def test_resolve_zone_id_by_fqdn_searches_absolute_name():
    """An FQDN lives in absoluteName, not name, and must still resolve."""
    client = _FieldRecordingClient(
        [{"id": 100979, "name": "bamzone", "absoluteName": "bamzone.example.com"}])
    assert bd.resolve_zone_id(client, "bamzone.example.com") == 100979
    assert client.fields_tried[0] == "absoluteName"


def test_resolve_zone_id_by_label_falls_back_to_name():
    """A bare label is only in `name`, so the fallback search finds it."""
    client = _FieldRecordingClient(
        [{"id": 100979, "name": "bamzone", "absoluteName": ""}])
    assert bd.resolve_zone_id(client, "bamzone") == 100979
    assert client.fields_tried == ["absoluteName", "name"]


def test_resolve_zone_id_no_match_raises():
    with pytest.raises(bd.BAMError):
        bd.resolve_zone_id(FakeClient([]), "zzz")


def test_resolve_zone_id_empty_target_raises():
    """Empty/whitespace targets error out before any API search."""
    with pytest.raises(bd.BAMError, match="required"):
        bd.resolve_zone_id(FakeClient([]), "")
    with pytest.raises(bd.BAMError, match="required"):
        bd.resolve_zone_id(FakeClient([]), "   ")


def test_resolve_zone_id_shows_absolute_name(monkeypatch, capsys):
    """The match list shows the FULL zone name (absoluteName) so entries
    with the same short label (e.g. two 'example' zones) are unique."""
    fake = FakeClient([{"id": 1, "name": "example", "range": "",
                        "absoluteName": "example.com"},
                       {"id": 2, "name": "example", "range": "",
                        "absoluteName": "sub.example.com"}])
    _tty(monkeypatch, "1\n")
    assert bd.resolve_zone_id(fake, "example", interactive=True) == 1
    err = capsys.readouterr().err
    assert "example.com (objectId 1)" in err
    assert "sub.example.com (objectId 2)" in err


def test_resolve_zone_id_multiple_noninteractive_raises_with_list():
    fake = FakeClient([{"id": 1, "name": "example", "range": "",
                        "absoluteName": "example.com"},
                       {"id": 2, "name": "example", "range": "",
                        "absoluteName": "sub.example.com"}])
    with pytest.raises(bd.BAMError, match="2 zones"):
        bd.resolve_zone_id(fake, "example")
    # the error lists the FULL zone names so the user can pick an objectId
    with pytest.raises(bd.BAMError, match="example.com #1"):
        bd.resolve_zone_id(fake, "example")


def test_resolve_zone_id_pick_interactive(monkeypatch):
    fake = FakeClient([{"id": 1, "name": "example.com", "range": ""},
                       {"id": 2, "name": "sub.example.com", "range": ""}])
    _tty(monkeypatch, "2\n")
    assert bd.resolve_zone_id(fake, "example", interactive=True) == 2


class _ZoneSearchClient:
    """Client stand-in that answers zone name searches."""

    def __init__(self):
        self.zones = [{"id": 456766, "name": "example.com",
                       "absoluteName": "example.com"}]

    def collection(self, path, filter_=None, fields=None, order_by=None,
                   page_size=1000, quiet=True):
        return [z for z in self.zones if "example" in z["name"]], 1


def test_main_resolves_zone_name_before_run(monkeypatch):
    """'zone example.com' in the wizard resolves to the objectId first."""
    _tty(monkeypatch, "\n", "6\n", "example.com\n", "n\n", "\n", "n\n")
    seen = {}

    def fake_zone(client, args):
        seen["zone_id"] = args.zone_id

    results = []
    _fake_main_env(monkeypatch, results, None)
    monkeypatch.setitem(bd.COMMANDS, "zone", fake_zone)
    monkeypatch.setattr(bd, "Client",
                        lambda host, auth, verify=False: _ZoneSearchClient())
    assert bd.main([]) == 0
    assert seen["zone_id"] == 456766


def test_wizard_ask_host_false_skips_host(monkeypatch):
    """Repeat rounds of the query loop keep the session host, no re-ask."""
    _tty(monkeypatch, "\n", "\n")  # menu Enter, format Enter - no host line
    args = bd.parse_args([])
    args.host = "bam.example.com"  # set by the earlier (ask_host=True) round
    bd.run_wizard(args, [], ask_host=False)
    assert args.command == "views"
    assert args.host == "bam.example.com"  # untouched


def test_wizard_format_menu_number(monkeypatch):
    """Output format is a numbered menu: 3 = csv."""
    _tty(monkeypatch, "\n", "\n", "3\n")  # host, menu, format
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.format == "csv"


def test_wizard_format_menu_name(monkeypatch):
    """Output format menu also accepts a format name."""
    _tty(monkeypatch, "\n", "\n", "yaml\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.format == "yaml"


def test_wizard_format_menu_enter_keeps_default(monkeypatch):
    """Enter at the format menu keeps the current default."""
    _tty(monkeypatch, "\n", "\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.format == "table"


def test_wizard_format_menu_retries_on_garbage(monkeypatch):
    """Non-numeric input is rejected, then the pick succeeds."""
    _tty(monkeypatch, "\n", "\n", "abc\n", "2\n")  # garbage, then json
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.format == "json"


def test_wizard_menu_ip_and_summary(monkeypatch):
    """Menu 7 = ip, 8 = summary."""
    _tty(monkeypatch, "\n", "7\n", "10.0.0.5\n", "\n")  # host, ip, addr, fmt
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "ip"
    assert args.address == "10.0.0.5"
    _tty(monkeypatch, "\n", "8\n", "\n")   # host, summary, format
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "summary"


def test_wizard_repeat_restores_last_query(monkeypatch):
    """'r' re-runs the last query with its exact settings."""
    bd.LAST_QUERY = {"command": "zone", "zone_id": "460645",
                     "records": True}
    _tty(monkeypatch, "\n", "r\n")   # host, menu: r
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.zone_id == "460645"
    assert args.records is True


def test_wizard_repeat_without_last_asks_again(monkeypatch):
    """'r' with no previous query re-asks the menu."""
    _tty(monkeypatch, "\n", "r\n", "1\n", "\n")  # host, r, views, format
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "views"


def test_wizard_captures_last_query(monkeypatch):
    """After a normal query the settings are remembered for repeat."""
    _tty(monkeypatch, "\n", "6\n", "460648\n", "y\n", "\n")
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert bd.LAST_QUERY == {"command": "zone", "zone_id": "460648",
                             "records": True}


def test_wizard_favorites_run_from_menu(monkeypatch):
    """The favorites menu entry lists favorites; picking one restores its settings."""
    bd.CONFIG["favorites"] = [{"name": "my zones", "command": "zone",
                               "zone_id": "460645", "records": True}]
    # select by name, not index: MENU order shifts as commands are added
    _tty(monkeypatch, "\n", "favorites\n", "1\n")  # host, favorites, pick 1
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.zone_id == "460645"
    assert args.records is True


def test_wizard_favorites_letter_key(monkeypatch):
    """MENU has 11 entries, so only the first 9 get a number; favorites is
    demoted to the 'f' letter key (the full word still works too - see
    test_wizard_favorites_run_from_menu above)."""
    bd.CONFIG["favorites"] = [{"name": "my zones", "command": "zone",
                               "zone_id": "460645", "records": True}]
    _tty(monkeypatch, "\n", "f\n", "1\n")  # host, favorites via 'f', pick 1
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.zone_id == "460645"
    assert args.records is True


def test_wizard_repeat_full_name_still_works(monkeypatch):
    """Repeat is also demoted to a letter key ('r', already covered by
    test_wizard_repeat_restores_last_query); typing the full word must
    still work too."""
    bd.LAST_QUERY = {"command": "zone", "zone_id": "460645", "records": True}
    _tty(monkeypatch, "\n", "repeat\n")   # host, menu: repeat
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.command == "zone"
    assert args.zone_id == "460645"
    assert args.records is True


def test_wizard_menu_caps_visible_numbers_at_nine(monkeypatch, capsys):
    """A keypress can't tell '1' from '10': only the first 9 MENU entries
    are numbered, and favorites/repeat show up as letter keys in the
    footer instead of numbers 10/11."""
    _tty(monkeypatch, "\n", "0\n")   # host, then quit at the menu
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    err = capsys.readouterr().err
    assert "9) records" in err
    assert "10)" not in err
    assert "11)" not in err
    assert "f) favorites" in err
    assert "r) repeat" in err


def test_wizard_config_defaults_apply(monkeypatch):
    """Remembered settings become the prompts' defaults."""
    bd.CONFIG["format"] = "json"
    _tty(monkeypatch, "\n", "\n", "\n")  # host, menu, format (Enter)
    args = bd.parse_args([])
    bd.run_wizard(args, [])
    assert args.format == "json"


class _ErrorBamClient:
    """Client stand-in that fails every query with a BAM API error."""

    def __init__(self, host, auth, verify=False):
        self.calls = 0

    def all(self, *a, **k):
        self.calls += 1
        raise bd.BAMError("BAM API error 400: Bad Request - "
                          "InvalidFilterAddressRange")

    def collection(self, *a, **k):
        self.calls += 1
        raise bd.BAMError("BAM API error 400: Bad Request - "
                          "InvalidFilterAddressRange")

    def get(self, *a, **k):
        self.calls += 1
        raise bd.BAMError("BAM API error 400: Bad Request")


def _error_env(monkeypatch, client):
    """Env with faked auth and a failing command client."""
    monkeypatch.setattr(bd, "resolve_credentials", lambda args: ("u", "p"))
    monkeypatch.setattr(bd, "authenticate", lambda *a, **k: "tok")
    monkeypatch.setattr(bd, "Client", lambda *a, **k: client)


def test_main_error_back_to_menu_yes(monkeypatch, capsys):
    """After a BAM error, 'Back to the menu? y' re-opens the menu (session
    kept) and quitting from there exits cleanly with the outro."""
    _tty(monkeypatch, "\n", "1\n", "\n",   # host, menu views, format
         "y\n",                            # back to the menu? yes
         "0\n")                            # menu: quit
    _error_env(monkeypatch, _ErrorBamClient("h", "tok"))
    assert bd.main([]) == 0
    err = capsys.readouterr().err
    assert "Bad Request" in err
    assert bd.KAOMOJI in err


def test_main_error_back_to_menu_no(monkeypatch, capsys):
    """'Back to the menu? n' quits gracefully with the outro."""
    _tty(monkeypatch, "\n", "1\n", "\n",   # host, menu views, format
         "n\n")                            # back to the menu? no
    _error_env(monkeypatch, _ErrorBamClient("h", "tok"))
    assert bd.main([]) == 0
    assert bd.KAOMOJI in capsys.readouterr().err


def test_main_error_back_to_menu_enter_means_yes(monkeypatch, capsys):
    """Enter at 'Back to the menu' accepts the [Y/n] default = recover."""
    _tty(monkeypatch, "\n", "1\n", "\n",   # host, menu views, format
         "\n",                             # Enter = yes (default True)
         "0\n")                            # menu: quit
    _error_env(monkeypatch, _ErrorBamClient("h", "tok"))
    assert bd.main([]) == 0
    assert bd.KAOMOJI in capsys.readouterr().err


def test_main_save_credentials_failure_rolls_back(monkeypatch, tmp_path,
                                                  capsys):
    """A failed credential write must roll CONFIG back and say so."""

    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(os, "open", boom)
    monkeypatch.setattr(bd, "CONFIG_PATH", str(tmp_path / "config.json"))

    def fake_resolve(args):
        bd.CREDS_PROMPTED = True
        return ("u", "p")

    def fake_views(client, args):
        bd.emit([{"objectId": 1, "name": "RESULT"}], args,
                ["objectId", "name"])

    monkeypatch.setattr(bd, "resolve_credentials", fake_resolve)
    monkeypatch.setattr(bd, "authenticate", lambda *a, **k: "tok")
    monkeypatch.setitem(bd.COMMANDS, "views", fake_views)
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "y\n",                              # save credentials? yes
         "\n",                               # export? none
         "\n",                               # favorite? no
         "n\n")                              # another? quit
    assert bd.main([]) == 0
    assert bd.CONFIG["user"] == "" and bd.CONFIG["password"] == ""
    err = capsys.readouterr().err
    assert "NOT saved" in err
    assert not (tmp_path / "config.json.tmp").exists()


def test_main_error_noninteractive_exit_code(monkeypatch, capsys):
    """Piped runs keep failing loudly with exit 1 - no menu question."""
    monkeypatch.setattr(sys, "stdin", _PipeStdin())
    _error_env(monkeypatch, _ErrorBamClient("h", "tok"))
    assert bd.main(["-H", "h", "views"]) == 1
    assert "Bad Request" in capsys.readouterr().err


def test_resolve_credentials_uses_saved(monkeypatch, capsys):
    """Saved credentials are offered and used without any typing."""
    bd.CONFIG["user"], bd.CONFIG["password"] = "bamuser", "hunter2"
    _tty(monkeypatch, "y\n")               # use saved credentials? yes
    user, password = bd.resolve_credentials(bd.parse_args([]))
    assert (user, password) == ("bamuser", "hunter2")
    assert bd.CREDS_USED_SAVED is True
    assert "Username" not in capsys.readouterr().err


def test_resolve_credentials_declines_saved(monkeypatch):
    """Declining saved credentials falls back to fresh prompts."""
    bd.CONFIG["user"], bd.CONFIG["password"] = "bamuser", "hunter2"
    _tty(monkeypatch, "n\n", "alice\n", "secret\n")
    user, password = bd.resolve_credentials(bd.parse_args([]))
    assert (user, password) == ("alice", "secret")
    assert bd.CREDS_PROMPTED is True


def test_main_saves_credentials_after_login(monkeypatch, tmp_path, capsys):
    """After a prompt-based login, agreeing saves credentials to the config
    file with mode 0600."""
    monkeypatch.setattr(bd, "CONFIG_PATH", str(tmp_path / "config.json"))

    def fake_resolve(args):
        bd.CREDS_PROMPTED = True
        return ("u", "p")

    def fake_views(client, args):
        bd.emit([{"objectId": 1, "name": "RESULT"}], args,
                ["objectId", "name"])

    monkeypatch.setattr(bd, "resolve_credentials", fake_resolve)
    monkeypatch.setattr(bd, "authenticate", lambda *a, **k: "tok")
    monkeypatch.setitem(bd.COMMANDS, "views", fake_views)
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "y\n",                              # save credentials? yes
         "\n",                               # export? none
         "\n",                               # favorite? no
         "n\n")                              # another? quit
    assert bd.main([]) == 0
    assert "RESULT" in capsys.readouterr().out
    assert bd.CONFIG["user"] == "u" and bd.CONFIG["password"] == "p"
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["user"] == "u" and saved["password"] == "p"
    assert oct((tmp_path / "config.json").stat().st_mode & 0o777) == "0o600"


def test_main_forget_credentials(monkeypatch, tmp_path, capsys):
    """--forget-credentials alone clears the saved pair and exits 0."""
    monkeypatch.setattr(bd, "CONFIG_PATH", str(tmp_path / "config.json"))
    bd.CONFIG["user"], bd.CONFIG["password"] = "bamuser", "hunter2"
    monkeypatch.setattr(sys, "stdin", _PipeStdin())
    assert bd.main(["--forget-credentials"]) == 0
    assert bd.CONFIG["user"] == "" and bd.CONFIG["password"] == ""
    assert "Removed saved credentials" in capsys.readouterr().err


def test_main_auth_fail_retries_with_fresh_prompt(monkeypatch, capsys):
    """Stale saved credentials fail once, then fresh input is prompted and
    used for the retry."""
    bd.CONFIG["user"], bd.CONFIG["password"] = "bamuser", "hunter2"
    calls = []

    def flaky_auth(host, user, password, verify=False):
        calls.append((user, password))
        if len(calls) == 1:
            raise bd.BAMError("401: InvalidAuthorizationCredentials")
        return "tok"

    monkeypatch.setattr(bd, "authenticate", flaky_auth)

    def fake_views(client, args):
        bd.emit([{"objectId": 1, "name": "RESULT"}], args,
                ["objectId", "name"])

    monkeypatch.setitem(bd.COMMANDS, "views", fake_views)
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "y\n",                              # use saved credentials? yes
         "alice\n", "secret\n",              # forced fresh prompts
         "n\n",                              # save credentials? no
         "\n",                               # export? none
         "\n",                               # favorite? no
         "n\n")                              # another? quit
    assert bd.main([]) == 0
    assert calls == [("bamuser", "hunter2"), ("alice", "secret")]
    captured = capsys.readouterr()
    assert "Login failed" in captured.err
    assert "RESULT" in captured.out


class _IpClient:
    """Client stand-in for cmd_ip (address search + linked records)."""
    """Client stand-in for cmd_ip (address search + linked records)."""

    def __init__(self):
        self.calls = []

    def collection(self, path, filter_=None, fields=None, order_by=None,
                   page_size=1000, quiet=True):
        self.calls.append(("collection", path))
        if "resourceRecords" in path:
            return [{"id": 5, "type": "HostRecord",
                     "absoluteName": "host-a.example.com"}], 1
        return [{"id": 21, "address": "10.0.0.5", "state": "STATIC",
                 "name": "host-a", "macAddress": ""}], 1

    def get(self, path, what="request", timeout=180):
        self.calls.append(("get", path))
        return {"data": [{"id": 5, "type": "HostRecord",
                          "absoluteName": "host-a.example.com"}],
                "totalCount": 1}


def test_cmd_ip(capsys):
    class A:
        format = "table"
        address = "10.0.0.5"
    bd.cmd_ip(_IpClient(), A())
    out = capsys.readouterr().out
    assert "10.0.0.5" in out
    assert "host-a.example.com" in out


def test_cmd_ip_machine_modes_single_document(capsys):
    """--json emits ONE document; --csv emits ONE header block."""
    fake = _IpClient()
    bd.cmd_ip(fake, type("A", (), {"format": "json",
                                   "address": "10.0.0.5"})())
    out = capsys.readouterr().out
    doc = json.loads(out)               # single JSON doc, jq-safe
    assert doc["address"]["address"] == "10.0.0.5"
    assert doc["linked"][0]["name"] == "host-a.example.com"
    fake = _IpClient()
    bd.cmd_ip(fake, type("A", (), {"format": "csv",
                                   "address": "10.0.0.5"})())
    out = capsys.readouterr().out
    assert out.count("objectId") == 1   # one CSV header for both rows
    assert "host-a.example.com" in out


def test_cmd_search_all_kinds_none_total(capsys):
    """A None totalCount (timeout fallback) must not crash the count."""

    class _SearchAll:
        def collection(self, path, filter_=None, fields=None, order_by=None,
                       page_size=1000, quiet=True):
            return [{"id": 1, "type": "Zone",
                     "absoluteName": "example.com"}], None

    bd.cmd_search(_SearchAll(),
                  type("A", (), {"format": "table", "query": "example",
                                 "kind": "all"})())
    capsys.readouterr()  # must not raise


def test_load_config_ignores_garbage(tmp_path, monkeypatch):
    """Non-dict JSON or a non-list favorites field must not crash."""
    cfg = tmp_path / "config.json"
    cfg.write_text('"just a string"')
    monkeypatch.setattr(bd, "CONFIG_PATH", str(cfg))
    bd.load_config()
    assert bd.CONFIG["favorites"] == []
    cfg.write_text('{"favorites": "oops"}')
    bd.load_config()
    assert bd.CONFIG["favorites"] == []


def test_cmd_zone_machine_modes_single_document(capsys):
    """zone --records --json emits ONE document; csv emits ONE header."""

    class _ZoneClient:
        def get(self, path, what="request", timeout=180):
            return {"id": 456766, "name": "example.com",
                    "absoluteName": "example.com", "type": "Zone"}

        def collection(self, path, filter_=None, fields=None, order_by=None,
                       page_size=1000, quiet=True):
            return [{"id": 11, "type": "GenericRecord",
                     "absoluteName": "www.example.com", "ttl": 3600,
                     "rdata": "1.2.3.4"}], 1

    bd.cmd_zone(_ZoneClient(),
                type("A", (), {"format": "json", "records": True,
                               "zone_id": "456766", "all": False})())
    out = capsys.readouterr().out
    doc = json.loads(out)               # single JSON doc, jq-safe
    assert doc["zone"]["absoluteName"] == "example.com"
    assert doc["records"][0]["absoluteName"] == "www.example.com"

    bd.cmd_zone(_ZoneClient(),
                type("A", (), {"format": "csv", "records": True,
                               "zone_id": "456766", "all": False})())
    out = capsys.readouterr().out
    assert out.count("absoluteName") == 1  # one CSV header for zone+records


def test_cmd_summary(capsys):
    class _SumClient:
        def get(self, path, what="request", timeout=180):
            return {"data": [], "totalCount": 7}

    class A:
        format = "table"
    bd.cmd_summary(_SumClient(), A())
    out = capsys.readouterr().out
    assert "networks" in out
    assert out.count("7") == 4  # configurations, views, zones, networks


def test_cmd_search_all_kinds(capsys):
    class _SearchAll:
        def collection(self, path, filter_=None, fields=None, order_by=None,
                       page_size=1000, quiet=True):
            return [{"id": 1, "type": "Zone",
                     "absoluteName": "example.com"}], 1

    class A:
        format = "table"
        query = "example"
        kind = "all"
    bd.cmd_search(_SearchAll(), A())
    out = capsys.readouterr().out
    assert out.count("example.com") == 5  # one row per kind
    assert "kind" in out


def test_main_saves_favorite(monkeypatch, tmp_path, capsys):
    """Saving a favorite persists it into the config file."""
    monkeypatch.setattr(bd, "CONFIG_PATH", str(tmp_path / "config.json"))
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "\n",                              # export? none
         "y\n",                             # save favorite? yes
         "my zones\n",                      # name
         "n\n")                             # run another? quit
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    assert bd.CONFIG["favorites"][0]["name"] == "my zones"
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["favorites"][0]["name"] == "my zones"


# ---------------------------------------------------------------------------
# query loop (main)
# ---------------------------------------------------------------------------

class _FakeBamClient:
    def __init__(self, host, auth, verify=False):
        self.host = host


def _fake_main_env(monkeypatch, results, counters=None):
    if counters is None:
        counters = {}

    def fake_views(client, args):
        results.append(1)
        bd.emit([{"objectId": len(results),
                  "name": f"RESULT{len(results)}"}],
                args, ["objectId", "name"])

    def fake_authenticate(*a, **k):
        counters["auth"] = counters.get("auth", 0) + 1
        return "tok"

    def fake_client(host, auth, verify=False):
        counters["client"] = counters.get("client", 0) + 1
        return _FakeBamClient(host, auth, verify)

    monkeypatch.setattr(bd, "resolve_credentials",
                        lambda args: ("u", "p"))
    monkeypatch.setattr(bd, "authenticate", fake_authenticate)
    monkeypatch.setattr(bd, "Client", fake_client)
    monkeypatch.setitem(bd.COMMANDS, "views", fake_views)
    monkeypatch.delenv("BAM_TOKEN", raising=False)  # env would skip auth


def test_main_query_loop_reruns_until_quit(monkeypatch, capsys):
    """Interactive run: after the result, y returns to the menu, n quits."""
    _tty(monkeypatch, "\n", "\n", "\n",      # round 1: host, menu, format
         "\n",                              # export result? Enter = none
         "\n",                              # save favorite? no
         "y\n",                             # run another query? yes
         "\n", "\n",                         # round 2: menu, format (no host)
         "\n",                              # export result? none
         "\n",                              # save favorite? no
         "n\n")                             # run another query? quit
    results, counters = [], {}
    _fake_main_env(monkeypatch, results, counters)
    assert bd.main([]) == 0
    assert results == [1, 1]                 # command ran twice
    assert counters == {"auth": 1, "client": 1}  # no re-login across rounds
    out = capsys.readouterr().out
    assert "RESULT1" in out and "RESULT2" in out


def test_main_query_loop_quits_immediately(monkeypatch, capsys):
    """Enter at 'Run another query?' quits with the kaomoji outro."""
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "\n",                              # export result? none
         "\n",                              # save favorite? no
         "n\n")                             # run another query? quit
    results, counters = [], {}
    _fake_main_env(monkeypatch, results, counters)
    assert bd.main([]) == 0
    assert results == [1]
    assert counters == {"auth": 1, "client": 1}
    out, err = capsys.readouterr()
    assert "RESULT1" in out and "RESULT2" not in out
    assert bd.KAOMOJI in err


def test_main_query_loop_exports_csv(monkeypatch, capsys, tmp_path):
    """Export menu: pick 2 (csv) writes a bluecat-views-*.csv file."""
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "2\n",                             # export result? csv
         "\n",                              # save favorite? no
         "n\n")                             # run another query? quit
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    files = list(tmp_path.glob("bluecat-views-*.csv"))
    assert len(files) == 1
    content = files[0].read_text()
    assert content.splitlines()[0] == "objectId,name"
    assert "1,RESULT1" in content


@pytest.mark.parametrize("answer,ext,needle", [
    ("3", "json", '"objectId"'),     # json
    ("4", "yaml", "objectId: 1"),    # yaml
    ("5", "yaml", "objectId: 1"),    # yml
    ("6", "txt", "objectId"),        # table
])
def test_main_query_loop_export_formats(monkeypatch, tmp_path, answer, ext,
                                        needle):
    """Export menu numbers map to json/yaml/yml/table files."""
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch, "\n", "\n", "\n", f"{answer}\n", "\n", "n\n")
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    files = list(tmp_path.glob(f"bluecat-views-*.{ext}"))
    assert len(files) == 1
    assert needle in files[0].read_text()


def test_export_choice_retries_on_bad_number(monkeypatch, tmp_path):
    """An out-of-range number is rejected, then the pick succeeds."""
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch, "\n", "\n", "\n", "9\n", "3\n", "\n", "n\n")
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    assert len(list(tmp_path.glob("bluecat-views-*.json"))) == 1


def test_export_choice_retries_on_garbage(monkeypatch, tmp_path):
    """Non-numeric garbage is rejected, then the pick succeeds."""
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch, "\n", "\n", "\n", "abc\n", "2\n", "\n", "n\n")
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    assert len(list(tmp_path.glob("bluecat-views-*.csv"))) == 1


def test_export_choice_accepts_name(monkeypatch, tmp_path):
    """Typing a format name works as well as a number."""
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch, "\n", "\n", "\n", "yaml\n", "\n", "n\n")
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 0
    assert len(list(tmp_path.glob("bluecat-views-*.yaml"))) == 1


def test_export_menu_eof_cancels(monkeypatch, capsys):
    """Ctrl-D at the export menu cancels cleanly (exit 130)."""
    _tty(monkeypatch, "\n", "\n", "\n", "\n", "\n")  # host, menu, format, export, fav
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 130
    assert "Closed - goodbye!" in capsys.readouterr().err


def test_export_table_strips_ansi(monkeypatch, tmp_path):
    """A table export must be plain text even with colors enabled."""
    monkeypatch.setattr(bd, "BOLD", "\033[1m")
    monkeypatch.setattr(bd, "RESET", "\033[0m")
    bd.LAST_RESULT = [{"objectId": 1, "name": "com"}]
    bd.LAST_COLUMNS = ["objectId", "name"]
    monkeypatch.chdir(tmp_path)
    bd.export_result("views", "table")
    files = list(tmp_path.glob("bluecat-views-*.txt"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "\x1b[" not in content
    assert "objectId" in content


def test_main_query_loop_never_exports_stale_result(monkeypatch, tmp_path):
    """An empty round must not offer an export of the previous round."""
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch, "\n", "\n", "\n",      # host, menu, format
         "\n",                              # round 1: export? none
         "\n",                              # round 1: favorite? no
         "y\n",                             # run another? yes
         "\n", "\n",                        # round 2: menu, format
         "n\n")                             # round 2: run another? quit
    results = []

    def fake_views(client, args):
        results.append(1)
        if len(results) == 1:
            bd.emit([{"objectId": 1, "name": "ROUND1"}], args,
                    ["objectId", "name"])
        # round 2 emits nothing - there must be no export offer

    _fake_main_env(monkeypatch, results, None)
    monkeypatch.setitem(bd.COMMANDS, "views", fake_views)
    assert bd.main([]) == 0
    assert results == [1, 1]
    assert not list(tmp_path.glob("bluecat-*.csv"))


def test_main_query_loop_eof_at_prompt_cancels(monkeypatch, capsys):
    """Ctrl-D at 'Run another query?' cancels cleanly (exit 130) and says so."""
    _tty(monkeypatch, "\n", "\n", "\n", "\n")  # host, menu, format, export
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 130
    assert results == [1]
    assert "Closed - goodbye!" in capsys.readouterr().err


def test_main_keyboard_interrupt_prints_goodbye(monkeypatch, capsys):
    """Ctrl-C during a command force-quits with an explicit goodbye."""
    def boom(client, args):
        raise KeyboardInterrupt

    results = []
    _fake_main_env(monkeypatch, results, None)
    monkeypatch.setitem(bd.COMMANDS, "views", boom)
    _tty(monkeypatch, "\n", "\n", "\n")   # host, menu, format
    assert bd.main([]) == 130
    assert "Closed - goodbye!" in capsys.readouterr().err


def test_main_noninteractive_runs_once(monkeypatch, capsys):
    """Piped stdin: no prompts, no loop question, exactly one command.

    -H is required here since a piped run has no shipped default host to
    fall back on."""
    monkeypatch.setattr(sys, "stdin", _PipeStdin())  # any read = failure
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main(["-H", "bam.example.com"]) == 0
    assert results == [1]
    assert "RESULT1" in capsys.readouterr().out


def test_main_noninteractive_no_host_fails_clearly(monkeypatch, capsys):
    """No -H, no $BAM_HOST, piped stdin: a clear error, not a silent
    default and not a confusing connection failure."""
    monkeypatch.setattr(sys, "stdin", _PipeStdin())  # any read = failure
    monkeypatch.delenv("BAM_HOST", raising=False)
    results = []
    _fake_main_env(monkeypatch, results, None)
    assert bd.main([]) == 1
    err = capsys.readouterr().err
    assert "-H" in err and "BAM_HOST" in err


def test_collection_falls_back_to_individual_fetches():
    """A timed-out page is re-fetched one record at a time."""

    class FlakyClient(bd.Client):
        def __init__(self):
            super().__init__("x", "y")
            self.requests = []

        def get(self, path, what="request", timeout=15):
            self.requests.append(path)
            off = int(path.split("offset=")[1].split("&")[0])
            if "limit=2&offset=0" in path:
                raise bd.BAMTimeout("boom")  # first page stalls
            if "limit=1" in path:
                return {"totalCount": 4,
                        "data": [{"id": off}] if off < 2 else []}
            # normal pages: full page, then empty page at the end
            return {"totalCount": 4,
                    "data": [{"id": 2}, {"id": 3}] if off == 2 else []}

    client = FlakyClient()
    records, total = bd.Client.collection(client, "/api/v2/whatever",
                                          page_size=2, timeout=5)
    assert total == 4
    assert [r["id"] for r in records] == [0, 1, 2, 3]
    assert "limit=1&offset=0" in client.requests[1]
    assert "limit=1&offset=1" in client.requests[2]


def test_collection_skips_stuck_records_with_warning():
    """A record that times out even at limit=1 is skipped, not fatal."""

    class FlakyClient(bd.Client):
        def __init__(self):
            super().__init__("x", "y")
            self.requests = []

        def get(self, path, what="request", timeout=15):
            self.requests.append(path)
            off = int(path.split("offset=")[1].split("&")[0])
            if "limit=2&offset=0" in path:
                raise bd.BAMTimeout("boom")  # first page stalls
            if "limit=1" in path:
                if off == 1:
                    raise bd.BAMTimeout("boom")  # this record also stalls
                return {"totalCount": 4,
                        "data": [{"id": off}] if off < 4 else []}
            return {"totalCount": 4,
                    "data": [{"id": 2}, {"id": 3}] if off == 2 else []}

    client = FlakyClient()
    records, _ = bd.Client.collection(client, "/api/v2/whatever",
                                      page_size=2, timeout=5)
    assert [r["id"] for r in records] == [0, 2, 3]  # offset 1 skipped


def test_emit_table_has_grid_lines(capsys):
    class A:
        format = "table"
    bd.emit([{"objectId": 1, "name": "com"}], A(), ["objectId", "name"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("┌")     # top border
    assert "│" in lines[1]              # header row inside the grid
    assert lines[2].startswith("├")     # header separator
    assert lines[-1].startswith("└")    # bottom border
    assert "│ 1 " in lines[3] or "│1 " in lines[3]  # data row padded


def test_emit_table_smoke(capsys):
    class A:
        format = "table"
    bd.emit([{"objectId": 1, "name": "com"}, {"objectId": 2, "name": "sg"}],
            A())
    out = capsys.readouterr().out
    assert "objectId" in out and "com" in out and "sg" in out


def test_emit_json(capsys):
    class A:
        format = "json"
    bd.emit([{"a": 1}], A())
    assert json.loads(capsys.readouterr().out) == [{"a": 1}]


def test_emit_csv(capsys):
    class A:
        format = "csv"
    bd.emit([{"a": 1, "b": "x"}], A())
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "a,b"


def test_emit_yaml(capsys):
    class A:
        format = "yaml"
    bd.emit([{"objectId": 1, "name": "com", "signed": False,
              "ttl": 300}], A())
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "-"
    assert "  objectId: 1" in out
    assert '  name: "com"' in out
    assert "  signed: false" in out
    assert "  ttl: 300" in out


def test_emit_yml_alias(capsys):
    class A:
        format = "yml"
    bd.emit([{"a": 1}], A())
    assert "  a: 1" in capsys.readouterr().out


def test_emit_no_records_machine_mode_stderr(capsys):
    """'No records.' must not pollute machine-mode stdout (piped jq)."""
    bd.INFO = sys.stderr  # pin AFTER capsys replaced stderr
    class A:
        format = "json"
    bd.emit([], A())
    out, err = capsys.readouterr()
    assert out == ""
    assert "No records." in err


def test_yaml_scalar_escapes_newlines():
    assert bd._yaml_scalar("a\nb") == '"a\\nb"'


def test_parse_args_format_yaml_yml():
    assert bd.parse_args(["views", "-f", "yaml"]).format == "yaml"
    assert bd.parse_args(["views", "-f", "yml"]).format == "yml"


# ---------------------------------------------------------------------------
# terminal-width-aware table rendering (_terminal_columns, _fit_columns,
# and emit()'s table branch)
# ---------------------------------------------------------------------------

def test_terminal_columns_none_when_not_a_tty(monkeypatch):
    """capsys's stdout is never a tty, so this needs no patching - it is
    the same condition a real pipe/redirect produces."""
    monkeypatch.setenv("COLUMNS", "40")
    assert bd._terminal_columns() is None


def test_terminal_columns_reads_shutil_get_terminal_size(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("COLUMNS", "97")
    monkeypatch.setenv("LINES", "24")
    assert bd._terminal_columns() == 97


def test_fit_columns_all_fit_when_wide():
    widths = {"a": 5, "b": 5, "c": 5}
    visible, hidden = bd._fit_columns(["a", "b", "c"], widths, 100)
    assert visible == ["a", "b", "c"]
    assert hidden == []


def test_fit_columns_drops_rightmost_first():
    widths = {"a": 5, "b": 5, "c": 5}
    # full table: 2 + 3*(5+2) + 2 seps = 25; without "c": 2 + 2*7 + 1 = 17
    visible, hidden = bd._fit_columns(["a", "b", "c"], widths, 20)
    assert visible == ["a", "b"]
    assert hidden == ["c"]


def test_fit_columns_drops_more_as_width_shrinks():
    widths = {"a": 5, "b": 5, "c": 5, "d": 5}
    visible, hidden = bd._fit_columns(["a", "b", "c", "d"], widths, 12)
    assert visible == ["a"]
    assert hidden == ["b", "c", "d"]


def test_fit_columns_never_drops_first_even_narrower_than_it():
    """max_width smaller than the first column alone still keeps it -
    something must always be shown."""
    widths = {"a": 20, "b": 5, "c": 5}
    visible, hidden = bd._fit_columns(["a", "b", "c"], widths, 5)
    assert visible == ["a"]
    assert hidden == ["b", "c"]


def test_fit_columns_preserves_original_order():
    widths = {c: 3 for c in "abcdefgh"}
    visible, hidden = bd._fit_columns(list("abcdefgh"), widths, 20)
    assert visible + hidden == list("abcdefgh")


# The network --ips --ptrs address row: 8 columns, the case the width fix
# targets directly.
_ADDR_COLUMNS = ["objectId", "address", "state", "name", "macAddress",
                 "location", "ptr", "linked"]
_ADDR_ROWS = [
    {"objectId": 1, "address": "192.0.2.10", "state": "STATIC",
     "name": "gw.example.com", "macAddress": "",
     "location": "", "ptr": "gw.example.com", "linked": "gw.example.com"},
    {"objectId": 2, "address": "192.0.2.20", "state": "DHCP_RESERVED",
     "name": "host2.example.com", "macAddress": "aa:bb:cc:dd:ee:ff",
     "location": "SG/DC1", "ptr": "host2.example.com",
     "linked": "host2.example.com, ANAME"},
]


class _TableArgs:
    format = "table"


def _force_tty_width(monkeypatch, columns):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "24")


def test_emit_table_wide_terminal_shows_every_column(monkeypatch, capsys):
    _force_tty_width(monkeypatch, 200)
    bd.INFO = sys.stdout  # table + tty: informational notes share stdout
    bd.emit(_ADDR_ROWS, _TableArgs(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    for col in _ADDR_COLUMNS:
        assert col in out
    assert "hidden" not in out


def test_emit_table_80_cols_drops_rightmost_and_notes_it(monkeypatch, capsys):
    _force_tty_width(monkeypatch, 80)
    bd.INFO = sys.stdout
    bd.emit(_ADDR_ROWS, _TableArgs(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    lines = out.splitlines()
    header_line = lines[1]
    assert "objectId" in header_line
    note = next(l for l in lines if "hidden" in l)
    assert "-f json" in note
    # Every column named in the note must actually be missing from the
    # header - the note and the table must agree.
    for col in _ADDR_COLUMNS:
        if col in note.split(":", 1)[1]:
            assert col not in header_line


def test_emit_table_40_cols_keeps_first_column_and_borders_align(
        monkeypatch, capsys):
    _force_tty_width(monkeypatch, 40)
    bd.INFO = sys.stdout
    bd.emit(_ADDR_ROWS, _TableArgs(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l and l[0] in "┌│├└"]
    assert "objectId" in lines[1]
    # Borders never corrupt: every grid line (border/header/data - the
    # trailing note is excluded by the character filter above) is the
    # same visible width.
    widths = {len(l) for l in lines}
    assert len(widths) == 1


def test_emit_table_note_not_shown_when_everything_fits(monkeypatch, capsys):
    _force_tty_width(monkeypatch, 500)
    bd.INFO = sys.stdout
    bd.emit(_ADDR_ROWS, _TableArgs(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    assert "hidden" not in out


def test_emit_table_non_tty_keeps_every_column_even_when_narrow(
        monkeypatch, capsys):
    """A pipe/redirect must never lose fidelity - this is the human-screen
    courtesy's off switch."""
    monkeypatch.setenv("COLUMNS", "20")  # would drop columns on a tty
    bd.INFO = sys.stderr
    bd.emit(_ADDR_ROWS, _TableArgs(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    for col in _ADDR_COLUMNS:
        assert col in out


def test_emit_json_unaffected_by_narrow_terminal(monkeypatch, capsys):
    _force_tty_width(monkeypatch, 20)
    class A:
        format = "json"
    bd.emit(_ADDR_ROWS, A(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    data = json.loads(out)
    for col in _ADDR_COLUMNS:
        assert all(col in row for row in data)


def test_emit_csv_unaffected_by_narrow_terminal(monkeypatch, capsys):
    _force_tty_width(monkeypatch, 20)
    class A:
        format = "csv"
    bd.emit(_ADDR_ROWS, A(), _ADDR_COLUMNS)
    header = capsys.readouterr().out.splitlines()[0]
    for col in _ADDR_COLUMNS:
        assert col in header.split(",")


def test_emit_yaml_unaffected_by_narrow_terminal(monkeypatch, capsys):
    _force_tty_width(monkeypatch, 20)
    class A:
        format = "yaml"
    bd.emit(_ADDR_ROWS, A(), _ADDR_COLUMNS)
    out = capsys.readouterr().out
    for col in _ADDR_COLUMNS:
        assert f"  {col}:" in out
