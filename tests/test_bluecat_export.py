#!/usr/bin/env python3
"""Tests for bluecat_export.py - the stall-fallback behaviour only.

The rest of the old script is covered by its own suite in Desktop/bluecat.
Here we import the patched file by path and verify that a timed-out page is
re-fetched one record at a time, and that truly stuck records are skipped
without aborting the export.
"""
import argparse
import importlib.util
import json
import os

import pytest

from bluecat_menu import ScriptedAnswers

os.environ["NO_COLOR"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_TARGET = os.path.join(os.path.dirname(_HERE), "bluecat_export.py")

_spec = importlib.util.spec_from_file_location("bluecat_export_patched",
                                               _TARGET)
be = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(be)


@pytest.fixture(autouse=True)
def _reset_config(tmp_path, monkeypatch):
    """Pin CONFIG_PATH to a fresh temp file and CONFIG to defaults for every
    test, so no test reads/writes the real ~/.bluecat_discover.json or
    leaks saved credentials into a later test."""
    monkeypatch.setattr(be, "CONFIG_PATH", str(tmp_path / "config.json"))
    be.CONFIG.clear()
    be.CONFIG.update(be.bluecat_menu.default_config())
    monkeypatch.delenv("BAM_TOKEN", raising=False)
    monkeypatch.delenv("BAM_PASSWORD", raising=False)
    yield


class RecordingAnswers(ScriptedAnswers):
    """ScriptedAnswers that also records every prompt string it was asked,
    so a test can assert on the exact rendered text (e.g. the "(y/n)"
    doubling bug, or a suffix like "[y/N]") without sys.stdin involved.
    keypress() is recorded too, since bool-mode prompt() now reads through
    it instead of input()."""

    def __init__(self, answers, isatty=True):
        super().__init__(answers, isatty=isatty)
        self.prompts = []

    def input(self, prompt_text):
        self.prompts.append(prompt_text)
        return super().input(prompt_text)

    def keypress(self, prompt_text, stream=None):
        self.prompts.append(prompt_text)
        return super().keypress(prompt_text, stream=stream)


class FlakyClient(be.Client):
    """Real Client with a scripted get(): stalls where we say so.

    `records` is the flat list of record ids; the fake serves `records[off:off+limit]`
    like the real paged API.
    """

    def __init__(self, stall_markers, records):
        super().__init__("x", "y")
        self.stall_markers = stall_markers
        self.records = records
        self.requests = []

    def get(self, path, what="request", timeout=30):
        self.requests.append(path)
        for marker in self.stall_markers:
            if marker in path:
                raise be.BAMTimeout("boom")
        off = int(path.split("offset=")[1].split("&")[0])
        limit = int(path.split("limit=")[1].split("&")[0])
        return {"totalCount": len(self.records),
                "data": [{"id": i} for i in self.records[off:off + limit]]}


def _ids(records):
    return [r["id"] for r in records]


def test_fallback_fetches_stuck_page_record_by_record():
    """Page 1 (limit=2) stalls; its records are fetched one at a time."""
    client = FlakyClient(["limit=2&offset=0"], [0, 1, 2, 3])
    records, total = be.Client.collection(client, "/api/v2/whatever",
                                          page_size=2)
    assert total == 4
    assert _ids(records) == [0, 1, 2, 3]
    assert "limit=1&offset=0" in client.requests[1]
    assert "limit=1&offset=1" in client.requests[2]


def test_fallback_skips_truly_stuck_record():
    """A record that stalls even at limit=1 is skipped, not fatal."""
    client = FlakyClient(["limit=2&offset=0", "limit=1&offset=1"],
                         [0, 1, 2, 3])
    records, total = be.Client.collection(client, "/api/v2/whatever",
                                          page_size=2)
    assert total == 4
    assert _ids(records) == [0, 2, 3]  # offset 1 skipped


def test_plain_pages_untouched():
    """No stalls -> collection behaves exactly as before."""
    client = FlakyClient([], [0, 1, 2, 3, 4])
    records, total = be.Client.collection(client, "/api/v2/whatever",
                                          page_size=2)
    assert total == 5
    assert _ids(records) == [0, 1, 2, 3, 4]
    assert all("limit=1" not in p for p in client.requests)


def test_requests_are_all_get():
    """Every request the shared Client makes is a GET (no POST/PUT/DELETE).

    The Client moved to bluecat_core, so scan there: that is where the
    read-only invariant now lives for both bluecat_export and
    bluecat_discover.
    """
    import re
    core = os.path.join(os.path.dirname(_HERE), "bluecat_core.py")
    src = open(core).read()
    client_src = src.split("class Client:")[1].split("\n\n\ndef ")[0]
    method_calls = re.findall(r'_request\("([A-Z]+)"', client_src)
    assert method_calls and set(method_calls) <= {"GET"}


def test_scenario_raw_via_scripted_answers(monkeypatch):
    """New test for the injectable-answers seam (bluecat_menu.STDIN): drive
    scenario_raw's three prompts (collection path, filter, fields) with a
    scripted list of answers and check the records it returns - no
    sys.stdin monkeypatching anywhere, unlike a wizard test built on an
    object impersonating sys.stdin."""
    class FakeClient:
        def collection(self, path, on_progress=None, filter_=None, fields=None):
            self.seen = (path, filter_, fields)
            return [{"id": 1}, {"id": 2}], 2

    monkeypatch.setattr(be, "STDIN",
                        ScriptedAnswers(["hosts", "name:contains('x')", "id,name"]))
    client = FakeClient()
    records, path, preset = be.scenario_raw(client)
    assert records == [{"id": 1}, {"id": 2}]
    assert path == "/api/v2/hosts"
    assert preset is None
    assert client.seen == ("/api/v2/hosts", "name:contains('x')", "id,name")


def test_prompt_and_prompt_secret_via_scripted_answers(monkeypatch):
    """prompt()/prompt_secret() now read through the same seam: a password
    answer comes back with no echo and no real getpass/tty involved."""
    monkeypatch.setattr(be, "STDIN", ScriptedAnswers(["", "s3cret"]))
    assert be.prompt("Output filename", "default.csv") == "default.csv"
    assert be.prompt_secret("Password") == "s3cret"


def test_non_get_is_refused_at_runtime():
    """_request refuses any non-GET method, not merely by source inspection."""
    import bluecat_core
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            bluecat_core._request(method, "https://example.invalid/api/v2/x")
        except bluecat_core.ReadOnlyError:
            continue
        raise AssertionError(f"{method} was not refused")


# ---------------------------------------------------------------------------
# prompt() yes/no mode (FIX 3: dropped "(y/n)" text; suffix carries it)
# ---------------------------------------------------------------------------

def test_prompt_bool_default_true_renders_upper_y(monkeypatch):
    spy = RecordingAnswers([""])
    monkeypatch.setattr(be, "STDIN", spy)
    assert be.prompt("Use saved credentials for alice", True) is True
    assert spy.prompts == ["Use saved credentials for alice [Y/n]: "]


def test_prompt_bool_default_false_renders_upper_n(monkeypatch):
    spy = RecordingAnswers([""])
    monkeypatch.setattr(be, "STDIN", spy)
    assert be.prompt("Drill into a subzone?", False) is False
    assert spy.prompts == ["Drill into a subzone? [y/N]: "]


def test_prompt_bool_accepts_y_and_n(monkeypatch):
    monkeypatch.setattr(be, "STDIN", ScriptedAnswers(["y", "N", "yes", "no"]))
    assert be.prompt("q", False) is True
    assert be.prompt("q", True) is False
    assert be.prompt("q", False) is True
    assert be.prompt("q", True) is False


def test_prompt_bool_retries_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr(be, "STDIN", ScriptedAnswers(["bogus", "y"]))
    assert be.prompt("q", False) is True
    assert "Please answer y or n." in capsys.readouterr().out


def test_prompt_string_default_rendering_unchanged(monkeypatch):
    """Non-boolean defaults must still render as "[default]", untouched."""
    spy = RecordingAnswers([""])
    monkeypatch.setattr(be, "STDIN", spy)
    assert be.prompt("Output filename", "out.csv") == "out.csv"
    assert spy.prompts == ["Output filename [out.csv]: "]


# ---------------------------------------------------------------------------
# scenario_zone_records: the "Drill into a subzone?" default flipped to No
# ---------------------------------------------------------------------------

class _OneChildZoneClient:
    """Fake client for a config/view/zone tree that is one item deep at
    every level (so bluecat_menu.choose() auto-selects without ever calling
    the real builtin input()), with exactly one subzone below the top zone
    so the drill prompt actually fires."""

    def all(self, path, fields=None, filter_=None):
        if path == "/api/v2/configurations":
            return [{"id": 1, "name": "Config1", "type": "Configuration"}]
        if path == "/api/v2/configurations/1/views":
            return [{"id": 2, "name": "View1", "type": "View"}]
        if path == "/api/v2/views/2/zones":
            return [{"id": 100, "name": "top", "absoluteName": "top.com",
                     "type": "Zone"}]
        if path == "/api/v2/zones/100/zones":
            return [{"id": 101, "name": "child",
                     "absoluteName": "child.top.com", "type": "Zone"}]
        if path == "/api/v2/zones/101/zones":
            return []  # child is a leaf: the drill loop stops here
        raise AssertionError(f"unexpected path {path!r}")

    def collection(self, path, on_progress=None):
        return [], 0


def test_scenario_zone_records_subzone_default_is_no(monkeypatch):
    """Enter at 'Drill into a subzone?' must now stop at the top zone
    instead of walking down into the child (the old default was 'y')."""
    spy = RecordingAnswers(["", ""])  # drill? Enter; recursive? Enter
    monkeypatch.setattr(be, "STDIN", spy)
    records, label, preset = be.scenario_zone_records(_OneChildZoneClient())
    assert label == "records-top.com"        # stayed on the top zone
    assert preset == "records"
    assert records == []
    assert spy.prompts[0] == "Drill into a subzone? [y/N]: "
    assert "(y/n)" not in spy.prompts[0]


def test_scenario_zone_records_y_still_drills_down(monkeypatch):
    """'y' still works - only the bare-Enter default changed."""
    spy = RecordingAnswers(["y", "n"])  # drill? yes; recursive? no
    monkeypatch.setattr(be, "STDIN", spy)
    records, label, preset = be.scenario_zone_records(_OneChildZoneClient())
    assert label == "records-child.top.com"  # drilled into the child


# ---------------------------------------------------------------------------
# saved credentials (FIX 1): connect() reads/writes the shared config file
# ---------------------------------------------------------------------------

def _connect_args(**overrides):
    args = argparse.Namespace(host="bam.example.com", user="", yes=False,
                              collection=None, list=False, verify=False)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_connect_offers_and_uses_saved_credentials(monkeypatch):
    be.CONFIG["user"], be.CONFIG["password"] = "bamuser", "hunter2"
    # host (Enter = default), use saved credentials? yes
    spy = RecordingAnswers(["", "y"])
    monkeypatch.setattr(be, "STDIN", spy)
    calls = []
    monkeypatch.setattr(be, "authenticate",
                        lambda host, user, password, verify:
                        calls.append((host, user, password)) or "tok")
    client = be.connect(_connect_args())
    assert calls == [("bam.example.com", "bamuser", "hunter2")]
    assert isinstance(client, be.Client)
    assert spy.prompts[-1] == "Use saved credentials for bamuser [Y/n]: "


def test_connect_declines_saved_prompts_fresh_and_offers_to_save(monkeypatch):
    be.CONFIG["user"], be.CONFIG["password"] = "bamuser", "hunter2"
    # host, decline saved, username, password (secret), decline save-offer
    monkeypatch.setattr(be, "STDIN", ScriptedAnswers(
        ["", "n", "alice", "s3cret", "n"]))
    calls = []
    monkeypatch.setattr(be, "authenticate",
                        lambda host, user, password, verify:
                        calls.append((user, password)) or "tok")
    be.connect(_connect_args())
    assert calls == [("alice", "s3cret")]
    assert be.CONFIG["user"] == "bamuser"    # untouched: declined to save


def test_connect_saves_credentials_after_fresh_login(monkeypatch):
    # no saved creds yet -> host, username, password (secret), agree to save
    monkeypatch.setattr(be, "STDIN",
                        ScriptedAnswers(["", "alice", "s3cret", "y"]))
    monkeypatch.setattr(be, "authenticate", lambda *a, **k: "tok")
    be.connect(_connect_args())
    assert be.CONFIG["user"] == "alice"
    assert be.CONFIG["password"] == "s3cret"
    saved = json.loads(open(be._config_path()).read())
    assert saved["user"] == "alice" and saved["password"] == "s3cret"
    assert oct(os.stat(be._config_path()).st_mode & 0o777) == "0o600"


def test_connect_explicit_user_skips_saved_credentials_offer(monkeypatch):
    """--user (or $BAM_USER) on the CLI always wins - no saved-creds
    question, exactly like bluecat_discover.py's resolve_credentials()."""
    be.CONFIG["user"], be.CONFIG["password"] = "bamuser", "hunter2"
    # host, password (secret), decline save-offer
    monkeypatch.setattr(be, "STDIN", ScriptedAnswers(["", "s3cret", "n"]))
    calls = []
    monkeypatch.setattr(be, "authenticate",
                        lambda host, user, password, verify:
                        calls.append((user, password)) or "tok")
    be.connect(_connect_args(user="alice"))
    assert calls == [("alice", "s3cret")]


def test_connect_yes_flag_skips_saved_credentials_offer(monkeypatch):
    """-y/--yes means "no prompts": the saved-creds question (and the
    offer-to-save question) must not appear even when saved credentials
    exist. $BAM_PASSWORD is unset here, so the password is still fetched
    via prompt_secret() - a pre-existing quirk of -y, unrelated to this
    fix - hence the one queued answer."""
    be.CONFIG["user"], be.CONFIG["password"] = "bamuser", "hunter2"
    spy = RecordingAnswers(["s3cret"])
    monkeypatch.setattr(be, "STDIN", spy)
    monkeypatch.setattr(be, "authenticate", lambda *a, **k: "tok")
    be.connect(_connect_args(user="alice", yes=True))
    assert spy.prompts == []      # no y/n question rendered via .input()
