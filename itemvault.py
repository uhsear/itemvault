#!/usr/bin/env python
"""Restore an ArcGIS item's configuration in place, and refuse to overwrite an edit no snapshot holds.

A nightly job keeps a copy of every web map's JSON. A web map breaks, somebody
restores last night's copy over it, and the map works again. It also loses the
layer a colleague added at lunchtime, because that edit was newer than the copy
and nothing compared the two. The restore reported success, and it was right.

This tool keeps the copies and does the restore, and the restore is the point.
Before it writes, it reads the live item and refuses unless the configuration it
is about to overwrite is already held by a snapshot in the vault. So every
restore it performs can itself be undone. When the live item was edited after
the newest snapshot, it says so, names the time of that edit, and prints the
command that snapshots it first. After the write it reads the item back and
fails unless the portal now holds exactly what the snapshot holds.

    python itemvault.py --self-test
    python itemvault.py --url https://example.maps.arcgis.com --item ITEMID --vault vault/
    python itemvault.py --url https://example.maps.arcgis.com --item ITEMID --vault vault/ --apply
    python itemvault.py --url https://example.maps.arcgis.com --restore vault/SNAPSHOT.json
    python itemvault.py --url https://example.maps.arcgis.com --restore vault/SNAPSHOT.json --apply

The token is read from the ITEMVAULT_TOKEN environment variable, never a flag.
Nothing is written, to the vault or to the portal, without --apply.

Exit codes: 0 nothing refused, 1 refused, 2 a portal call, a file or the
post-write check failed, 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Seconds before one portal call is abandoned.
HTTP_TIMEOUT = 60

# Environment variable the token is read from. Not a flag: argv is readable by
# every process on the machine and lands in shell history and scheduler logs.
TOKEN_ENV = "ITEMVAULT_TOKEN"

# What replaces the token anywhere it could otherwise be printed.
REDACTED = "[redacted]"

# Hosts that may be reached over plain http. Only the loopback address, which
# is where the self-test's stand-in portal listens. Anywhere else the token
# would cross the network in clear.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

# Version of the snapshot file layout. A file with any other value is refused
# rather than guessed at.
SNAPSHOT_FORMAT = 1

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Restore verdicts. Exactly one per run.
CORRUPT = "CORRUPT"            # the snapshot file fails its own checks
OTHER_PORTAL = "OTHER-PORTAL"  # the snapshot was taken on a different portal
NOOP = "NOOP"                  # the live item already holds the snapshot
FUTURE = "FUTURE"              # the snapshot is later than the live item
UNSAVED = "UNSAVED"            # the live configuration is in no snapshot
RESTORE = "RESTORE"            # safe: what it overwrites is already held

# Post-write verdicts.
VERIFIED = "VERIFIED"
MISMATCH = "MISMATCH"

# An item id becomes part of a file name and a URL path, so only these
# characters are accepted from the command line.
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class PortalError(RuntimeError):
    """A portal call failed, or answered with something that is not success."""


# ----------------------------------------------------------------- pure core

def canonical(data):
    """One fixed text for a JSON value, whatever order or spacing it came in.

    The portal is free to hand back keys in another order. A byte compare would
    then call every unchanged item edited, and every restore unverifiable.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def digest(data):
    """sha256 of the canonical text. The identity of a configuration."""
    return hashlib.sha256(canonical(data).encode("ascii")).hexdigest()


def is_ms(value):
    """True for a non-negative integer time in milliseconds.

    bool is refused because True is an int in Python and would read as 1 ms.
    A string is refused because "999" sorts after "1700000000000".
    """
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def iso_ms(value):
    """A millisecond time as UTC text, or the raw number if it cannot be one."""
    try:
        moment = datetime.datetime.fromtimestamp(value / 1000.0,
                                                 datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return "%d ms" % value
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(url):
    """The portal root as one comparable string, or ValueError.

    Scheme and host are lowercased, a trailing slash and a trailing
    /sharing/rest are dropped, so two spellings of one portal compare equal.
    The query is refused without being echoed, because the one thing people
    paste into a query string is a token.
    """
    parts = urllib.parse.urlsplit(url.strip())
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("--url must not carry a query string, a fragment or "
                         "a user name. The token belongs in %s." % TOKEN_ENV)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise ValueError("--url must be an https url with a host, not %r"
                         % url)
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ValueError("--url must be https. Over http the token would "
                         "cross the network in clear.")
    netloc = host if parts.port is None else "%s:%d" % (host, parts.port)
    path = parts.path.rstrip("/")
    if path.lower().endswith("/sharing/rest"):
        path = path[:-len("/sharing/rest")]
    return "%s://%s%s" % (scheme, netloc, path)


def valid_id(item_id):
    return bool(ITEM_ID_RE.match(item_id or ""))


def has_config(data):
    """True when data is a non-empty JSON object, the shape a web map, an app
    or a dashboard keeps its configuration in."""
    return isinstance(data, dict) and bool(data)


def build_snapshot(portal, item_id, item, data, taken):
    """The snapshot document for one item. ValueError when there is nothing
    worth keeping, because an empty snapshot restored later blanks the item."""
    modified = item.get("modified")
    if not is_ms(modified):
        raise ValueError("item %s: the portal gave no modified time, so a "
                         "later restore could not tell what is newer"
                         % item_id)
    if not has_config(data):
        raise ValueError("item %s holds no JSON configuration to keep"
                         % item_id)
    return {
        "tool": "itemvault",
        "format": SNAPSHOT_FORMAT,
        "portal": portal,
        "item": item_id,
        "type": item.get("type"),
        "title": item.get("title"),
        "owner": item.get("owner"),
        "modified": modified,
        "taken": taken,
        "sha256": digest(data),
        "data": data,
    }


def snapshot_name(snap):
    """File name: item, edit time, full digest. Two different configurations
    can never share a name, so writing one never replaces another."""
    return "%s-%d-%s.json" % (snap["item"], snap["modified"], snap["sha256"])


def check_snapshot(doc):
    """None when doc is a whole, untampered snapshot, else the reason it is not."""
    if not isinstance(doc, dict) or doc.get("tool") != "itemvault":
        return "it is not an itemvault snapshot"
    if doc.get("format") != SNAPSHOT_FORMAT:
        return "its format %r is not %d" % (doc.get("format"),
                                            SNAPSHOT_FORMAT)
    for key in ("portal", "item", "sha256"):
        value = doc.get(key)
        if not isinstance(value, str) or not value:
            return "it has no %s" % key
    if not is_ms(doc.get("modified")):
        return "its modified time is not a time in milliseconds"
    if not has_config(doc.get("data")):
        return "it holds an empty configuration, and restoring that would " \
               "blank the item"
    if digest(doc["data"]) != doc["sha256"]:
        return "its data does not match its own sha256, so the file was " \
               "edited or damaged after it was written"
    return None


def held_by(vault, item_id, sha):
    """The path of the vault snapshot holding this configuration, or None.

    vault is a list of (path, doc) already passed through check_snapshot. Only
    the content counts: a configuration is safe to overwrite when it can be
    put back, whatever time the portal stamped on it.
    """
    for path, doc in vault:
        if doc["item"] == item_id and doc["sha256"] == sha:
            return path
    return None


def changed_keys(before, after):
    """Top-level keys whose value differs, so a plan says what moves."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["(the whole configuration)"]
    keys = set(before) | set(after)
    return sorted(k for k in keys
                  if k not in before or k not in after
                  or canonical(before[k]) != canonical(after[k]))


def preflight(target, portal):
    """The refusal that needs no portal call, or None."""
    problem = check_snapshot(target)
    if problem:
        return CORRUPT, "The snapshot is refused: %s." % problem
    if target["portal"] != portal:
        return OTHER_PORTAL, (
            "The snapshot was taken on %s, not on %s. An item id that two "
            "portals share does not make them the same item."
            % (target["portal"], portal))
    return None


def decide_restore(target, portal, live_item, live_data, vault):
    """(verdict, reason) for restoring target over the live item.

    The order matters. A matching configuration is a no-op before anything is
    judged newer. A live item that is older than the snapshot means the
    snapshot is from somewhere else. Only then is the live configuration
    looked for in the vault, and a live item with no configuration at all has
    nothing in it to lose.
    """
    early = preflight(target, portal)
    if early:
        return early
    live_mod = live_item.get("modified")
    if not is_ms(live_mod):
        raise ValueError("the portal gave no modified time for item %s"
                         % target["item"])
    live_sha = digest(live_data)
    if live_sha == target["sha256"]:
        return NOOP, ("The live configuration already matches the snapshot. "
                      "Nothing to restore.")
    if live_mod < target["modified"]:
        return FUTURE, (
            "The snapshot says the item was modified %s, but the live item "
            "says %s, which is earlier. The snapshot is not from this item's "
            "history." % (iso_ms(target["modified"]), iso_ms(live_mod)))
    if not has_config(live_data):
        return RESTORE, ("The live item holds no configuration, so the "
                         "restore overwrites nothing.")
    holder = held_by(vault, target["item"], live_sha)
    if holder is None:
        return UNSAVED, (
            "The item was modified %s, after the snapshot of %s, and no "
            "snapshot in the vault holds that configuration. Restoring would "
            "destroy that edit. Snapshot it first:\n"
            "    python itemvault.py --url %s --item %s --vault DIR --apply"
            % (iso_ms(live_mod), iso_ms(target["modified"]), portal,
               target["item"]))
    return RESTORE, ("The live configuration (modified %s) is held by %s, so "
                     "this restore can be undone." % (iso_ms(live_mod),
                                                      holder))


def verify(target, after_data):
    """VERIFIED only when the portal now holds exactly the snapshot."""
    return VERIFIED if digest(after_data) == target["sha256"] else MISMATCH


def redact(text, token):
    """text with the token, raw or url-quoted, replaced."""
    if token:
        for form in (token, urllib.parse.quote_plus(token),
                     urllib.parse.quote(token)):
            text = text.replace(form, REDACTED)
    return text


# ----------------------------------------------------------------- self-test

def self_test():
    """Assertions over the core and the CLI, against a stand-in portal on the
    loopback address. No credentials and no real network."""
    import http.server
    import threading

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("itemvault self-test: a stand-in portal on 127.0.0.1, no "
          "credentials")
    print("-" * 68)

    portal = "https://example.maps.arcgis.com"
    iid = "0123456789abcdef0123456789abcdef"
    night = {"version": "2.31", "operationalLayers": [
        {"id": "layer-1", "title": "Hydrants",
         "url": "https://example.com/arcgis/rest/services/Hydrants/"
                "FeatureServer/0"}],
        "baseMap": {"title": "Topographic"}}
    lunch = json.loads(json.dumps(night))
    lunch["operationalLayers"].append(
        {"id": "layer-2", "title": "Valves",
         "url": "https://example.com/arcgis/rest/services/Valves/"
                "FeatureServer/0"})
    broken = json.loads(json.dumps(lunch))
    broken["operationalLayers"][0]["url"] = "https://gone.example.com/x"

    def snap(data, modified, item_id=iid, where=portal):
        return build_snapshot(where, item_id, {"modified": modified,
                                               "type": "Web Map",
                                               "title": "Sample map",
                                               "owner": "owner1"},
                              data, "2026-01-01T00:00:00Z")

    # ---- the identity of a configuration
    check(digest({"b": 1, "a": 2}) == digest({"a": 2, "b": 1}),
          "two key orders of one configuration have one digest"
          "  <-- pinned defect")
    check(canonical({"a": [1, 2]}) == '{"a":[1,2]}',
          "the canonical text has no spacing, so whitespace never differs")
    check(digest({"a": True}) != digest({"a": 1}),
          "true and 1 are different configurations")
    check(digest({"a": 1.0}) != digest({"a": 1}),
          "1.0 and 1 are different configurations, never silently equal")
    check(canonical({"t": "caf\u00e9"}) == '{"t":"caf\\u00e9"}',
          "non-ascii text is escaped, so the snapshot file stays ascii")

    # ---- times
    check(is_ms(1700000000000), "an integer millisecond time is a time")
    check(is_ms(0), "zero is a time")
    check(not is_ms(True), "True is not a time, though Python calls it 1"
          "  <-- pinned defect")
    check(not is_ms("1700000000000"),
          "a time in a string is refused, because strings sort wrongly"
          "  <-- pinned defect")
    check(not is_ms(-1), "a negative time is refused")
    check(not is_ms(1.5e12), "a float time is refused")
    check(not is_ms(None), "a missing time is refused")
    check(iso_ms(0) == "1970-01-01T00:00:00Z",
          "zero ms prints as the epoch in UTC")
    check(iso_ms(1700000000000) == "2023-11-14T22:13:20Z",
          "a millisecond time prints as UTC, not local time")
    check(iso_ms(10 ** 20) == "%d ms" % 10 ** 20,
          "a time past year 9999 prints as its number instead of crashing")

    # ---- the portal url
    check(normalize_url("https://Example.Maps.ArcGIS.com/") == portal,
          "scheme and host case and a trailing slash do not make a second "
          "portal  <-- pinned defect")
    check(normalize_url(" https://example.maps.arcgis.com/sharing/rest ")
          == portal, "a pasted /sharing/rest is dropped")
    check(normalize_url("https://gis.example.org/portal/sharing/rest/")
          == "https://gis.example.org/portal",
          "an Enterprise web adaptor path is kept")
    check(normalize_url("https://gis.example.org:7443/arcgis")
          == "https://gis.example.org:7443/arcgis", "a port is kept")
    check(normalize_url("http://127.0.0.1:8080")
          == "http://127.0.0.1:8080",
          "plain http is accepted on the loopback address")
    raises(lambda: normalize_url("http://gis.example.org/portal"),
           "plain http anywhere else is refused  <-- pinned defect")
    def error_text(fn, kind=ValueError):
        """The message fn raises, or "" when it raises nothing."""
        try:
            fn()
        except kind as exc:
            return str(exc)
        return ""

    check(error_text(lambda: normalize_url(portal)) == "",
          "a clean portal url raises nothing")
    leaked = error_text(lambda: normalize_url(
        "https://example.maps.arcgis.com?token=SECRET99"))
    check("SECRET99" not in leaked and "query" in leaked,
          "a url with a token in its query is refused without echoing it"
          "  <-- pinned defect")
    raises(lambda: normalize_url("https://u:pw@example.com"),
           "a url carrying a user name and password is refused")
    raises(lambda: normalize_url("https://example.com#frag"),
           "a url with a fragment is refused")
    raises(lambda: normalize_url("ftp://example.com"),
           "a scheme other than http or https is refused")
    raises(lambda: normalize_url("example.maps.arcgis.com"),
           "a url with no scheme is refused, not read as a path")
    raises(lambda: normalize_url("https://example.com:notaport"),
           "a port that is not a number is refused")

    # ---- item ids
    check(valid_id(iid), "a 32 character hex id is a valid id")
    check(not valid_id("../../etc"), "an id with a path in it is refused"
          "  <-- pinned defect")
    check(not valid_id(""), "an empty id is refused")
    check(not valid_id(None), "a missing id is refused")

    # ---- building a snapshot
    s_night = snap(night, 1000)
    check(s_night["sha256"] == digest(night) and s_night["modified"] == 1000
          and s_night["item"] == iid and s_night["portal"] == portal,
          "a snapshot records the portal, the item, the edit time and the "
          "digest")
    check(check_snapshot(s_night) is None, "a fresh snapshot passes its own "
          "checks")
    check(snapshot_name(s_night) == "%s-1000-%s.json" % (iid, digest(night)),
          "the file name carries the item, the edit time and the full digest")
    check(snapshot_name(s_night) != snapshot_name(snap(lunch, 1000)),
          "two configurations stamped with one time get two file names"
          "  <-- pinned defect")
    raises(lambda: snap({}, 1000),
           "an empty configuration is not kept, it would blank the item "
           "later  <-- pinned defect")
    raises(lambda: snap(None, 1000), "an item with no data is not kept")
    raises(lambda: snap([1, 2], 1000), "a JSON list is not a configuration")
    raises(lambda: snap(night, None), "an item with no modified time is not "
           "kept")
    raises(lambda: snap(night, "1000"),
           "a modified time in a string is not kept  <-- pinned defect")

    # ---- checking a snapshot file
    def edited(**changes):
        doc = json.loads(json.dumps(s_night))
        doc.update(changes)
        return doc

    check(check_snapshot(None) is not None, "nothing at all is not a snapshot")
    check(check_snapshot({"tool": "other"}) is not None,
          "another tool's JSON is not a snapshot")
    check("format" in check_snapshot(edited(format=2)),
          "a snapshot of an unknown format is refused, not guessed at")
    check("portal" in check_snapshot(edited(portal="")),
          "a snapshot with no portal is refused")
    check("item" in check_snapshot(edited(item=None)),
          "a snapshot with no item is refused")
    check("modified" in check_snapshot(edited(modified="1000")),
          "a snapshot whose time is a string is refused")
    check("empty" in check_snapshot(edited(data={}, sha256=digest({}))),
          "a truncated snapshot holding {} is refused even with a matching "
          "sha256  <-- pinned defect")
    tampered = edited()
    tampered["data"]["baseMap"]["title"] = "Imagery"
    check("sha256" in (check_snapshot(tampered) or ""),
          "a snapshot edited by hand fails its own sha256  <-- pinned defect")

    # ---- what a plan says moves
    check(changed_keys(night, lunch) == ["operationalLayers"],
          "a plan names the top-level keys that change")
    check(changed_keys({"a": 1}, {"b": 1}) == ["a", "b"],
          "a key added or removed is named too")
    check(changed_keys({"a": {"x": 1, "y": 2}}, {"a": {"y": 2, "x": 1}}) == [],
          "a key whose value only changed order is not named")
    check(changed_keys(None, night) == ["(the whole configuration)"],
          "a live item with no configuration changes the whole of it")

    # ---- the restore decision
    s_lunch = snap(lunch, 2000)
    held = [("vault/lunch.json", s_lunch)]

    def decide(target, live_mod, live_data, vault=(), where=portal):
        return decide_restore(target, where, {"modified": live_mod},
                              live_data, list(vault))

    verdict, reason = decide(s_night, 3000, broken)
    check(verdict == UNSAVED,
          "restoring last night over an edit no snapshot holds is REFUSED"
          "  <-- pinned defect")
    check("1970-01-01T00:00:03Z" in reason and "--apply" in reason,
          "and the refusal names the time of that edit and the command that "
          "snapshots it first")
    check(decide(s_night, 3000, broken, held)[0] == UNSAVED,
          "a vault holding a different edit does not hold this one"
          "  <-- pinned defect")
    check(decide(s_night, 2000, lunch, held)[0] == RESTORE,
          "once the live configuration is in the vault, the restore is "
          "allowed")
    check("vault/lunch.json" in decide(s_night, 2000, lunch, held)[1],
          "and the plan names the snapshot that can undo it")
    check(decide(s_night, 9000, lunch, held)[0] == RESTORE,
          "the live configuration is held by content, whatever time the "
          "portal stamped on it")
    other_item = [("vault/x.json", snap(lunch, 2000, item_id="f" * 32))]
    check(decide(s_night, 2000, lunch, other_item)[0] == UNSAVED,
          "another item's snapshot of the same JSON does not hold this item"
          "  <-- pinned defect")
    check(decide(s_night, 1000, json.loads(canonical(night)))[0] == NOOP,
          "a live item that already holds the snapshot is a no-op")
    reordered = json.loads(json.dumps(night, sort_keys=True))
    check(decide(s_night, 5000, reordered)[0] == NOOP,
          "the portal returning the keys in another order is still a no-op"
          "  <-- pinned defect")
    check(decide(s_lunch, 1500, night)[0] == FUTURE,
          "a snapshot later than the live item is refused as not from its "
          "history  <-- pinned defect")
    check(decide(s_night, 4000, {})[0] == RESTORE,
          "an item whose configuration was wiped to {} is restored without "
          "a snapshot of the wipe  <-- pinned defect")
    check(decide(s_night, 4000, None)[0] == RESTORE,
          "and so is one with no data at all")
    check(decide(s_night, 2000, lunch, held,
                 where="https://other.example.com")[0] == OTHER_PORTAL,
          "a snapshot from another portal is refused even when the content "
          "is held  <-- pinned defect")
    check(decide(tampered, 2000, lunch, held)[0] == CORRUPT,
          "a tampered snapshot is refused before anything else is judged")
    raises(lambda: decide(s_night, None, lunch, held),
           "a live item with no modified time cannot be judged")
    raises(lambda: decide(s_night, True, lunch, held),
           "nor one whose modified time is True")
    check(preflight(s_night, portal) is None,
          "a whole snapshot for this portal needs no early refusal")
    check(verify(s_night, json.loads(json.dumps(night, sort_keys=True)))
          == VERIFIED, "a read-back holding the snapshot in any key order is "
          "VERIFIED")
    check(verify(s_night, lunch) == MISMATCH,
          "a read-back holding anything else is a MISMATCH")

    # ---- the token stays out of every message
    check(redact("a tok+en/x b", "tok+en/x") == "a [redacted] b",
          "a raw token is redacted")
    check(redact("u?token=tok%2Ben%2Fx", "tok+en/x") ==
          "u?token=[redacted]", "a url-quoted token is redacted"
          "  <-- pinned defect")
    check(redact("u?token=tok%2Ben/x", "tok+en/x") ==
          "u?token=[redacted]", "a token quoted with a bare slash is "
          "redacted")
    check(redact("nothing here", None) == "nothing here",
          "with no token nothing is changed")

    # ---- the parser
    def refused_by_parser(argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                _parse(argv)
            except SystemExit as exc:
                return (exc.code == 64
                        and "unrecognized arguments" in err.getvalue())
        return False

    check(refused_by_parser(["--restore", "x.json", "--url", portal, "--ap"]),
          "a unique prefix --ap of --apply is refused with exit 64, not read "
          "as --apply  <-- pinned defect")
    check(refused_by_parser(["--rest", "x.json"]),
          "a prefix --rest of --restore is refused too")
    check(not refused_by_parser(["--item", "x", "--apply"]),
          "--apply spelled in full is accepted")
    check(not _parse(["--self-test"]).apply, "--apply is off by default")

    # ---- the CLI against a stand-in portal
    token = "tok-SECRET-4f9a+/="
    other = "f" * 32
    blank = "a" * 32
    filed = "b" * 32
    gone = "c" * 32
    state = {}
    seen = []
    mode = {"write": "apply", "get": "ok"}
    rest = "/sharing/rest/content/items/"

    def put(item_id, data, modified, owner="gis.owner@example.com"):
        raw = data if isinstance(data, bytes) else (
            b"" if data is None else json.dumps(data).encode("utf-8"))
        state[item_id] = {"item": {"id": item_id, "modified": modified,
                                   "owner": owner, "type": "Web Map",
                                   "title": "Sample map",
                                   "ownerFolder": None},
                          "raw": raw}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        def reply(self, code, body):
            raw = body if isinstance(body, bytes) else \
                json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(parts.query))
            seen.append(("GET", self.headers.get("Host"), self.path, query))
            if mode["get"] == "500":
                return self.reply(500, b"boom")
            key = parts.path[len(rest):]
            want_data = key.endswith("/data")
            entry = state.get(key[:-5] if want_data else key)
            if not parts.path.startswith(rest) or entry is None:
                # The portal answers a missing item with 200 and an error.
                return self.reply(200, {"error": {
                    "code": 400, "message": "Item does not exist or is "
                    "inaccessible. token=%s" % query.get("token")}})
            if want_data:
                return self.reply(200, entry["raw"])
            if mode["get"] == "html":
                return self.reply(200, b"<html>sign in</html>")
            if mode["get"] == "noowner":
                return self.reply(200, dict(entry["item"], owner=None))
            return self.reply(200, entry["item"])

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = dict(urllib.parse.parse_qsl(
                self.rfile.read(length).decode("ascii")))
            path = urllib.parse.unquote(self.path)
            seen.append(("POST", self.headers.get("Host"), self.path, form))
            match = re.match(r"^/sharing/rest/content/users/([^/]+)/items/"
                             r"([^/]+)/update$", path)
            entry = state.get(match.group(2)) if match else None
            if entry is None or match.group(1) != entry["item"]["owner"]:
                return self.reply(200, {"error": {"code": 403,
                                                  "message": "not owner"}})
            if mode["write"] == "error":
                return self.reply(200, {"error": {
                    "code": 500, "message": "Unable to update item. "
                    "token=%s" % form.get("token")}})
            if mode["write"] == "refuse":
                return self.reply(200, {"success": False})
            if mode["write"] == "garbage":
                return self.reply(200, b"<html>gateway</html>")
            if mode["write"] == "apply":
                entry["raw"] = form["text"].encode("utf-8")
                entry["item"]["modified"] += 1000
            # "ignore" claims success and changes nothing.
            return self.reply(200, {"success": True, "id": match.group(2)})

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    tmp = tempfile.mkdtemp(prefix="itemvault-")
    saved_token = os.environ.pop(TOKEN_ENV, None)
    try:
        port = server.server_address[1]
        live = "http://127.0.0.1:%d" % port
        vault = os.path.join(tmp, "vault")

        def run_cli(argv, with_token=True):
            """main() with its output captured, so the self-test stays
            readable."""
            if with_token:
                os.environ[TOKEN_ENV] = token
            else:
                os.environ.pop(TOKEN_ENV, None)
            del seen[:]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = main(argv)
            return code, out.getvalue(), err.getvalue()

        def files():
            return sorted(os.listdir(vault)) if os.path.isdir(vault) else []

        def live_data(item_id=iid):
            return json.loads(state[item_id]["raw"].decode("utf-8"))

        def posts():
            return [s for s in seen if s[0] == "POST"]

        check(server.server_address[0] == "127.0.0.1",
              "the stand-in portal listens on the loopback address only")

        # The story. Last night's snapshot, a good edit at lunch that no
        # snapshot holds, then a break, then a restore of last night.
        put(iid, night, 1000)
        snap_args = ["--url", live, "--item", iid, "--vault", vault]
        code, out, err = run_cli(snap_args)
        check(code == 0 and "Check only" in out and files() == [],
              "a snapshot without --apply writes nothing at all"
              "  <-- pinned defect")
        code, out, err = run_cli(snap_args + ["--apply"])
        night_file = os.path.join(vault, files()[0]) if files() else ""
        check(code == 0 and len(files()) == 1 and "wrote" in out,
              "a snapshot with --apply writes one file")
        with io.open(night_file, encoding="ascii") as fh:
            on_disk = json.load(fh)
        check(check_snapshot(on_disk) is None and on_disk["data"] == night,
              "the file on disk is a whole snapshot of the live configuration")
        check(not [f for f in os.listdir(vault) if f.endswith(".part")],
              "no partial file is left beside it")
        check(all(s[3].get("token") == token for s in seen),
              "every read carries the token")
        code, out, err = run_cli(snap_args + ["--apply"])
        check(code == 0 and "already held" in out and len(files()) == 1,
              "a second snapshot of an unchanged item writes no second file")
        code, out, err = run_cli(snap_args + ["--item", iid, "--apply"],
                                 with_token=False)
        check(code == 0 and out.count("already held") == 1,
              "one item named twice is read and kept once")
        check(not [s for s in seen if "token" in s[3]],
              "with no token in the environment no token is sent")

        put(iid, lunch, 2000)          # a colleague's edit, never snapshotted
        state[iid]["raw"] = json.dumps(broken).encode("utf-8")
        state[iid]["item"]["modified"] = 3000   # and then it broke
        restore_args = ["--url", live, "--restore", night_file]
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 1 and UNSAVED in out,
              "restoring last night over a newer unsaved edit is REFUSED, "
              "exit 1  <-- pinned defect")
        check(posts() == [] and live_data() == broken,
              "and nothing at all is written to the portal  <-- pinned defect")
        check("--item %s --vault DIR --apply" % iid in out,
              "and the refusal prints the snapshot command to run first")

        code, out, err = run_cli(snap_args + ["--apply"])
        check(code == 0 and len(files()) == 2,
              "snapshotting the live item adds it to the vault")
        code, out, err = run_cli(restore_args)
        check(code == 0 and RESTORE in out and "Check only" in out
              and posts() == [],
              "now the restore is allowed, and without --apply writes nothing")
        check("operationalLayers" in out,
              "the plan names the top-level keys that will change")
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 0 and VERIFIED in out and live_data() == night,
              "with --apply it writes, reads back and reports VERIFIED")
        post = posts()[0] if posts() else ("", "", "", {})
        check(len(posts()) == 1 and "token" not in post[2]
              and post[3].get("token") == token,
              "the write carries the token in the POST body, never the url"
              "  <-- pinned defect")
        check(sorted(post[3]) == ["f", "text", "token"],
              "the write sends the configuration and nothing else about the "
              "item")
        check("%40" in post[2] and "gis.owner@example.com" not in post[2],
              "the owner's name is quoted into the update url")
        check(token not in out and token not in err,
              "the token never reaches stdout or stderr")
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 0 and NOOP in out and posts() == [],
              "restoring it again is a no-op and writes nothing")
        check(len(files()) == 2, "a restore never writes to the vault")

        # The write that says success and did nothing, and its relatives.
        state[iid]["raw"] = json.dumps(broken).encode("utf-8")
        state[iid]["item"]["modified"] = 9000
        mode["write"] = "ignore"
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 2 and MISMATCH in out and "restored" not in out.lower(),
              "a write the portal confirms but never made is a MISMATCH, "
              "exit 2  <-- pinned defect")
        mode["write"] = "error"
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 2 and "Unable to update item" in err,
              "a 200 carrying an error object is a failure, exit 2"
              "  <-- pinned defect")
        check(token not in err and REDACTED in err,
              "and the token the portal echoed back is redacted"
              "  <-- pinned defect")
        mode["write"] = "refuse"
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 2 and "not confirmed" in err,
              "an update answering success false is a failure, exit 2")
        mode["write"] = "garbage"
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 2 and "not confirmed" in err,
              "an update answering with HTML is a failure, exit 2")
        mode["write"] = "apply"
        code, out, err = run_cli(restore_args + ["--apply"], with_token=False)
        check(code == 64 and posts() == [] and seen == [],
              "a restore with --apply and no token is a usage error before "
              "any call")

        # Refusals that need no portal call at all.
        bad = os.path.join(tmp, "tampered.json")
        doc = json.loads(json.dumps(on_disk))
        doc["data"]["baseMap"]["title"] = "Imagery"
        with io.open(bad, "w", encoding="ascii") as fh:
            json.dump(doc, fh)
        code, out, err = run_cli(["--url", live, "--restore", bad, "--vault",
                                  vault, "--apply"])
        check(code == 1 and CORRUPT in out and seen == [],
              "a tampered snapshot is refused before any portal call"
              "  <-- pinned defect")
        notjson = os.path.join(tmp, "notjson.json")
        with io.open(notjson, "w", encoding="ascii") as fh:
            fh.write("{ half a file")
        code, out, err = run_cli(["--url", live, "--restore", notjson])
        check(code == 1 and CORRUPT in out,
              "a snapshot that is not JSON is refused, exit 1")
        code, out, err = run_cli(["--url", "http://localhost:%d" % port,
                                  "--restore", night_file, "--apply"])
        check(code == 1 and OTHER_PORTAL in out and seen == [],
              "the same item id on another host name is refused as another "
              "portal")
        code, out, err = run_cli(["--url", live, "--restore",
                                  os.path.join(tmp, "missing.json")])
        check(code == 2, "a snapshot file that cannot be read exits 2")

        # A damaged copy of the live configuration in the vault holds nothing.
        broken2 = dict(broken, version="3.0")
        put(iid, broken2, 12000)
        held_copy = snap(broken2, 12000, where=live)
        held_copy["data"]["version"] = "9.99"
        with io.open(os.path.join(vault, snapshot_name(held_copy)), "w",
                     encoding="ascii") as fh:
            json.dump(held_copy, fh)
        with io.open(os.path.join(vault, "junk.json"), "w",
                     encoding="ascii") as fh:
            fh.write("not json")
        with io.open(os.path.join(vault, "notes.txt"), "w",
                     encoding="ascii") as fh:
            fh.write("ignored")
        with io.open(os.path.join(vault, "elsewhere.json"), "w",
                     encoding="ascii") as fh:
            json.dump(snap(broken2, 12000, where="https://other.example.com"),
                      fh)
        code, out, err = run_cli(restore_args + ["--apply"])
        check(code == 1 and UNSAVED in out and posts() == [],
              "a damaged vault file that claims the live configuration does "
              "not hold it  <-- pinned defect")
        check("elsewhere.json" not in out,
              "nor does a whole snapshot of it taken on another portal")
        check("2 file(s) in the vault were skipped" in out,
              "and the skipped vault files are counted")

        # The rescue: a configuration wiped to nothing needs no snapshot.
        put(blank, {}, 5000)
        s_blank = snap(night, 4000, item_id=blank, where=live)
        blank_file = os.path.join(tmp, snapshot_name(s_blank))
        with io.open(blank_file, "w", encoding="ascii") as fh:
            json.dump(s_blank, fh)
        code, out, err = run_cli(["--url", live, "--restore", blank_file,
                                  "--apply"])
        check(code == 0 and VERIFIED in out and live_data(blank) == night,
              "a wiped item is restored and verified with no snapshot of the "
              "wipe  <-- pinned defect")
        state[blank]["raw"] = b"%PDF-1.7 not json"
        code, out, err = run_cli(["--url", live, "--restore", blank_file])
        check(code == 1 and "not JSON" in out,
              "a live item whose data is not JSON is refused, exit 1")
        state[blank]["raw"] = json.dumps(lunch).encode("utf-8")
        state[blank]["item"]["modified"] = 1
        code, out, err = run_cli(["--url", live, "--restore", blank_file])
        check(code == 1 and FUTURE in out,
              "a snapshot later than the live item exits 1")
        mode["get"] = "noowner"
        state[blank]["raw"] = b""
        state[blank]["item"]["modified"] = 6000
        code, out, err = run_cli(["--url", live, "--restore", blank_file,
                                  "--apply"])
        check(code == 2 and "owner" in err and posts() == [],
              "a live item with no owner cannot be written, exit 2")
        mode["get"] = "ok"

        s_gone = snap(night, 1000, item_id=gone, where=live)
        gone_file = os.path.join(tmp, snapshot_name(s_gone))
        with io.open(gone_file, "w", encoding="ascii") as fh:
            json.dump(s_gone, fh)
        code, out, err = run_cli(["--url", live, "--restore", gone_file])
        check(code == 2 and "does not exist" in err and token not in err,
              "restoring an item the portal no longer has exits 2, with the "
              "echoed token redacted")

        # Snapshot refusals and failures, one item at a time.
        put(filed, b"%PDF-1.7 binary", 1000)
        put(other, {}, 1000)
        code, out, err = run_cli(["--url", live, "--item", filed, "--item",
                                  other, "--item", gone, "--item", iid,
                                  "--vault", vault, "--apply"])
        check(code == 2 and "not JSON" in out and "no JSON configuration" in out
              and "does not exist" in err,
              "a file item, an empty item and a missing item are each named")
        check(not [f for f in files() if f.startswith((filed, other, gone))],
              "and none of them gets a snapshot file")
        check("%s: wrote" % iid in out,
              "and the healthy item in the same run is still kept")
        with io.open(os.path.join(vault, snapshot_name(held_copy)),
                     encoding="ascii") as fh:
            check(check_snapshot(json.load(fh)) is None,
                  "and its file replaced the damaged one of the same name")
        code, out, err = run_cli(["--url", live, "--item", filed, "--vault",
                                  vault])
        check(code == 1, "a refused snapshot with nothing else wrong exits 1")
        mode["get"] = "500"
        code, out, err = run_cli(snap_args)
        check(code == 2 and "500" in err and token not in err,
              "an HTTP 500 is a failed call, exit 2, with the token redacted")
        mode["get"] = "html"
        code, out, err = run_cli(snap_args)
        check(code == 2 and "did not answer with JSON" in err,
              "an item answer that is an HTML page is a failed call, exit 2")
        mode["get"] = "ok"
        wrong_owner = error_text(lambda: update_data(
            live, "someone.else", iid, night, token), PortalError)
        check("not owner" in wrong_owner,
              "an update the portal refuses with a 200 and an error raises")
        put(other, json.loads('{"x": 1}'), "1000")
        code, out, err = run_cli(["--url", live, "--item", other, "--vault",
                                  vault])
        check(code == 1 and "modified time" in out,
              "an item whose modified time is a string is not kept")
        state[other]["item"] = {"no": "id"}
        code, out, err = run_cli(["--url", live, "--item", other, "--vault",
                                  vault])
        check(code == 2 and "no item" in err,
              "an item answer with no id is a failed call")
        state[other]["item"] = None
        code, out, err = run_cli(["--url", live, "--item", other, "--vault",
                                  vault])
        check(code == 2 and "no item" in err,
              "an item answer that is not an object is a failed call")
        blocker = os.path.join(tmp, "blocker")
        with io.open(blocker, "w", encoding="ascii") as fh:
            fh.write("a file where the vault should be")
        code, out, err = run_cli(["--url", live, "--item", iid, "--vault",
                                  os.path.join(blocker, "v"), "--apply"])
        check(code == 2 and "could not write" in err,
              "a vault that cannot be created exits 2")
        code, out, err = run_cli(["--url", live, "--item", iid, "--vault",
                                  blocker])
        check(code == 0 and "would write" in out,
              "a vault path that is not a directory reads as an empty vault")

        # Usage errors.
        for argv, label in (
                ([], "no mode at all"),
                (["--url", live], "a url and no mode"),
                (["--url", live, "--item", iid], "--item without --vault"),
                (["--item", iid, "--vault", vault], "no --url"),
                (["--url", live, "--item", iid, "--restore", night_file,
                  "--vault", vault], "--item and --restore together"),
                (["--url", "http://gis.example.org", "--item", iid,
                  "--vault", vault], "an http url off the loopback address"),
                (["--url", live, "--item", "../x", "--vault", vault],
                 "an item id with a path in it")):
            code, out, err = run_cli(argv)
            check(code == 64 and seen == [],
                  "usage: %s exits 64 with no portal call" % label)

        argv_before = sys.argv
        try:
            sys.argv = ["itemvault.py", "--url", live, "--item", iid,
                        "--vault", vault]
            check(run_cli(None)[0] == 0,
                  "main with no argv reads the arguments after the program "
                  "name")
        finally:
            sys.argv = argv_before
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop(TOKEN_ENV, None)
        os.environ.update({TOKEN_ENV: saved_token} if saved_token else {})

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass. The probe's output is swallowed so
    # a green run prints no FAIL line.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")

    # Importing the file must define the tool and run nothing, so another
    # script can call decide_restore() without starting a restore.
    import importlib.util
    spec = importlib.util.spec_from_file_location("itemvault_import_probe",
                                                  os.path.abspath(__file__))
    module = importlib.util.module_from_spec(spec)
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    try:
        spec.loader.exec_module(module)
        printed = sys.stdout.getvalue()
    finally:
        sys.stdout = quiet
    check(callable(module.decide_restore) and printed == "",
          "importing the file defines the tool and prints nothing")

    def summary(total, failures):
        """The footer lines and the exit code of a run."""
        lines = ["%d assertions, %d failed" % (total, len(failures))]
        lines.extend("  FAILED: %s" % f for f in failures)
        return lines, 1 if failures else 0

    check(summary(5, ["x"]) == (["5 assertions, 1 failed", "  FAILED: x"], 1),
          "a run with a failure says so in its footer and exits 1")

    print("-" * 68)
    lines, code = summary(passed[0] + len(failed), failed)
    for line in lines:
        print(line)
    return code


# ----------------------------------------------------------------------- I/O

def _request(method, url, params, token):
    """One HTTP call returning the raw body. Every failure is a PortalError
    with the token redacted, because urllib quotes the url back in its
    errors and a GET url carries the token."""
    params = dict(params)
    if token:
        params["token"] = token
    query = urllib.parse.urlencode(params)
    if method == "POST":
        req = urllib.request.Request(url, data=query.encode("ascii"),
                                     method="POST")
    else:
        req = urllib.request.Request(url + ("?" + query if query else ""))
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        raise PortalError(redact("%s %s: %s" % (method, url, exc), token))


def _json(raw, where, token):
    """Parsed JSON, None for an empty body, ValueError for anything else.

    The body is read for an error object whatever the status code, because
    the portal answers many failures with HTTP 200.
    """
    if not raw.strip():
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ValueError("%s did not answer with JSON" % where)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        raise PortalError(redact("%s: error %s: %s" % (
            where, err.get("code"), err.get("message")), token))
    return body


def fetch_item(portal, item_id, token):
    url = "%s/sharing/rest/content/items/%s" % (
        portal, urllib.parse.quote(item_id, safe=""))
    try:
        body = _json(_request("GET", url, {"f": "json"}, token),
                     "item %s" % item_id, token)
    except ValueError as exc:
        raise PortalError(str(exc))
    if not isinstance(body, dict) or "id" not in body:
        raise PortalError("item %s: the portal answered with no item"
                          % item_id)
    return body


def fetch_data(portal, item_id, token):
    """The item's configuration, None when it has none. ValueError when the
    data is not JSON, which makes it a file item and out of scope."""
    url = "%s/sharing/rest/content/items/%s/data" % (
        portal, urllib.parse.quote(item_id, safe=""))
    try:
        return _json(_request("GET", url, {}, token),
                     "item %s data" % item_id, token)
    except ValueError:
        raise ValueError("item %s: its data is not JSON, so it is a file "
                         "item, which this tool does not keep" % item_id)


def update_data(portal, owner, item_id, data, token):
    """POST the configuration as the item's text. Success means the portal
    said success: true, nothing less."""
    url = "%s/sharing/rest/content/users/%s/items/%s/update" % (
        portal, urllib.parse.quote(owner, safe=""),
        urllib.parse.quote(item_id, safe=""))
    raw = _request("POST", url, {"f": "json", "text": canonical(data)}, token)
    try:
        body = _json(raw, "update of item %s" % item_id, token)
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("success") is not True:
        raise PortalError("update of item %s was not confirmed: the portal "
                          "answered %s" % (item_id, redact(
                              repr(body if body is not None else raw[:80]),
                              token)))


def load_vault(vault_dir, portal):
    """(path, doc) for every whole snapshot of this portal in the directory,
    and how many .json files were skipped as unreadable or damaged. A damaged
    file holds nothing, so it can never make a restore look safe."""
    found, skipped = [], 0
    if not os.path.isdir(vault_dir):
        return found, skipped
    for name in sorted(os.listdir(vault_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(vault_dir, name)
        try:
            with io.open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (IOError, OSError, ValueError):
            skipped += 1
            continue
        if check_snapshot(doc) is not None:
            skipped += 1
            continue
        if doc["portal"] == portal:
            found.append((path, doc))
    return found, skipped


def write_snapshot(vault_dir, snap):
    """Write through a .part file, so a run killed mid-write never leaves a
    file that looks like a snapshot."""
    if not os.path.isdir(vault_dir):
        os.makedirs(vault_dir)
    path = os.path.join(vault_dir, snapshot_name(snap))
    part = path + ".part"
    with io.open(part, "w", encoding="ascii", newline="\n") as fh:
        fh.write(json.dumps(snap, indent=1, sort_keys=True,
                            ensure_ascii=True))
        fh.write("\n")
    os.replace(part, path)
    return path


# ----------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, and 2 here means a portal call failed.
    A scheduler reading the code must see a usage error as 64."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="itemvault.py",
        description="Snapshot ArcGIS item configurations, and restore one in "
                    "place only when what it overwrites is already held.",
        epilog="The token is read from %s. Nothing is written without "
               "--apply." % TOKEN_ENV,
        allow_abbrev=False,
    )
    ap.add_argument("--url", help="the portal, for example "
                    "https://example.maps.arcgis.com or "
                    "https://gis.example.org/portal")
    ap.add_argument("--item", action="append",
                    help="an item to snapshot. Repeat for more items.")
    ap.add_argument("--vault", help="the snapshot directory. For --restore "
                    "it defaults to the snapshot file's own directory.")
    ap.add_argument("--restore", metavar="SNAPSHOT",
                    help="restore this snapshot file over its item")
    ap.add_argument("--apply", action="store_true",
                    help="write the snapshot files, or perform the restore. "
                         "Without it nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage(message):
    print("error: %s" % message, file=sys.stderr)
    return 64


def run_snapshot(args, portal, token):
    vault, _skipped = load_vault(args.vault, portal)
    taken = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    worst, pending = 0, 0
    for item_id in args.item:
        try:
            item = fetch_item(portal, item_id, token)
            data = fetch_data(portal, item_id, token)
            snap = build_snapshot(portal, item_id, item, data, taken)
        except PortalError as exc:
            print("error: %s" % exc, file=sys.stderr)
            worst = 2
            continue
        except ValueError as exc:
            print("refused: %s" % exc)
            worst = max(worst, 1)
            continue
        holder = held_by(vault, item_id, snap["sha256"])
        if holder:
            print("%s: already held by %s" % (item_id, holder))
            continue
        if not args.apply:
            print("%s: would write %s (modified %s)" % (
                item_id, snapshot_name(snap), iso_ms(snap["modified"])))
            pending += 1
            continue
        try:
            path = write_snapshot(args.vault, snap)
        except (IOError, OSError) as exc:
            print("error: could not write a snapshot of %s: %s"
                  % (item_id, exc), file=sys.stderr)
            worst = 2
            continue
        vault.append((path, snap))
        print("%s: wrote %s" % (item_id, path))
    if pending:
        print("\nCheck only. Nothing was written. Re-run with --apply.")
    return worst


def run_restore(args, portal, token):
    try:
        with io.open(args.restore, encoding="utf-8") as fh:
            target = json.load(fh)
    except ValueError:
        target = None
    except (IOError, OSError) as exc:
        print("error: could not read %s: %s" % (args.restore, exc),
              file=sys.stderr)
        return 2
    early = preflight(target, portal)
    if early:
        print("%s: %s\nRefused. Nothing was written." % early)
        return 1
    item_id = target["item"]
    vault_dir = args.vault or os.path.dirname(args.restore) or "."
    print("itemvault: restore %s from the snapshot of %s"
          % (item_id, iso_ms(target["modified"])))
    try:
        live_item = fetch_item(portal, item_id, token)
        live_data = fetch_data(portal, item_id, token)
        vault, skipped = load_vault(vault_dir, portal)
        verdict, reason = decide_restore(target, portal, live_item,
                                         live_data, vault)
    except PortalError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("refused: %s\nNothing was written." % exc)
        return 1
    if skipped:
        print("%d file(s) in the vault were skipped as unreadable or "
              "damaged." % skipped)
    print("%s: %s" % (verdict, reason))
    if verdict == NOOP:
        return 0
    if verdict != RESTORE:
        print("Refused. Nothing was written.")
        return 1
    print("keys that change: %s" % ", ".join(changed_keys(live_data,
                                                          target["data"])))
    if not args.apply:
        print("\nCheck only. Nothing was written. Re-run with --apply.")
        return 0
    try:
        owner = live_item.get("owner")
        if not isinstance(owner, str) or not owner:
            raise PortalError("item %s: the portal named no owner, so there "
                              "is no update url to write to" % item_id)
        update_data(portal, owner, item_id, target["data"], token)
        after = fetch_data(portal, item_id, token)
    except (PortalError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    outcome = verify(target, after)
    if outcome == VERIFIED:
        print("%s: the portal now holds the snapshot's configuration."
              % outcome)
        return 0
    print("%s: the portal confirmed the update, but reading it back does not "
          "give the snapshot's configuration." % outcome)
    return 2


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()
    if bool(args.item) == bool(args.restore):
        return _usage("give --item (to snapshot) or --restore (to restore), "
                      "exactly one of them. Use --self-test to verify the "
                      "tool without a portal.")
    if not args.url:
        return _usage("--url is required.")
    try:
        portal = normalize_url(args.url)
    except ValueError as exc:
        return _usage(str(exc))
    token = os.environ.get(TOKEN_ENV) or None
    if args.item:
        if not args.vault:
            return _usage("--item needs --vault.")
        bad = [i for i in args.item if not valid_id(i)]
        if bad:
            return _usage("not an item id: %s" % ", ".join(bad))
        # Order kept, repeats dropped.
        args.item = list(dict.fromkeys(args.item))
        return run_snapshot(args, portal, token)
    if args.apply and not token:
        return _usage("a restore with --apply needs a token in %s."
                      % TOKEN_ENV)
    return run_restore(args, portal, token)


if __name__ == "__main__":
    sys.exit(main())
