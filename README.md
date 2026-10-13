# itemvault

Restore an ArcGIS item's configuration in place, and refuse to overwrite an edit that no snapshot
holds. A live item that is newer than the snapshot is still restored when a snapshot already holds
its configuration. The tool judges by content, not by the time alone.

A nightly job keeps a copy of every web map's JSON. One afternoon a web map breaks: a layer URL
points at a server that no longer exists. Somebody restores last night's copy over the item, and
the map works again. The restore reports success.

The next morning a colleague asks where her layer went. She added it at lunchtime, hours after the
nightly copy and hours before the break. The restore put back last night's configuration, and her
edit was part of what it overwrote. No backup holds it, because it happened between two backups.
Nothing compared the live item with the copy before the write. The restore did exactly what it was
told.

itemvault makes that restore refuse. Before it writes, it reads the live item. It writes only when
the configuration it is about to overwrite is already in a snapshot in the vault. When it is not,
the tool names the time of the edit that would be lost, and prints the command that snapshots it
first. So every restore that itemvault performs can itself be undone. After the write, it reads the
item back and fails unless the portal now holds exactly what the snapshot holds.

```
$ python itemvault.py --self-test
itemvault self-test: a stand-in portal on 127.0.0.1, no credentials
--------------------------------------------------------------------
PASS  two key orders of one configuration have one digest  <-- pinned defect
...
PASS  restoring last night over an edit no snapshot holds is REFUSED  <-- pinned defect
PASS  and the refusal names the time of that edit and the command that snapshots it first
PASS  a vault holding a different edit does not hold this one  <-- pinned defect
PASS  once the live configuration is in the vault, the restore is allowed
PASS  and the plan names the snapshot that can undo it
PASS  the live configuration is held by content, whatever time the portal stamped on it
PASS  another item's snapshot of the same JSON does not hold this item  <-- pinned defect
PASS  a live item that already holds the snapshot is a no-op
PASS  the portal returning the keys in another order is still a no-op  <-- pinned defect
PASS  a snapshot later than the live item is refused as not from its history  <-- pinned defect
PASS  an item whose configuration was wiped to {} is restored without a snapshot of the wipe  <-- pinned defect
PASS  and so is one with no data at all
PASS  a snapshot from another portal is refused even when the content is held  <-- pinned defect
...
PASS  restoring last night over a newer unsaved edit is REFUSED, exit 1  <-- pinned defect
PASS  and nothing at all is written to the portal  <-- pinned defect
PASS  and the refusal prints the snapshot command to run first
PASS  snapshotting the live item adds it to the vault
PASS  now the restore is allowed, and without --apply writes nothing
PASS  the plan names the top-level keys that will change
PASS  with --apply it writes, reads back and reports VERIFIED
PASS  the write carries the token in the POST body, never the url  <-- pinned defect
PASS  the write sends the configuration and nothing else about the item
PASS  the owner's name is quoted into the update url
PASS  the token never reaches stdout or stderr
PASS  restoring it again is a no-op and writes nothing
PASS  a restore never writes to the vault
PASS  a write the portal confirms but never made is a MISMATCH, exit 2  <-- pinned defect
PASS  a 200 carrying an error object is a failure, exit 2  <-- pinned defect
PASS  and the token the portal echoed back is redacted  <-- pinned defect
...
PASS  importing the file defines the tool and prints nothing
PASS  a run with a failure says so in its footer and exits 1
--------------------------------------------------------------------
146 assertions, 0 failed
```

The full run prints all 146 assertions. The three `...` lines show where this block is cut.

## Requirements

Python 3.9 or newer, and nothing else. No `arcgis` package, no `arcpy`, no third-party module.

The self-test prints 146 assertions on Windows (Python 3.13.2), on Ubuntu (Python 3.12.3) and on
CPython 3.9.25. The Windows and Ubuntu outputs are identical line for line.

The self-test needs no portal and no credentials. It starts a stand-in portal on `127.0.0.1` and
runs every network path against it.

```
git clone https://github.com/uhsear/itemvault.git
```

## Usage

Put the token in the environment. It is never a flag, because a command line is visible to every
process on the machine and lands in shell history.

```
set ITEMVAULT_TOKEN=<token>        # Windows
export ITEMVAULT_TOKEN=<token>     # POSIX
```

Take snapshots, for example from a nightly scheduled task. Repeat `--item` for more items.

```
python itemvault.py --url https://example.maps.arcgis.com --item ITEMID --vault vault/
python itemvault.py --url https://example.maps.arcgis.com --item ITEMID --vault vault/ --apply
```

Restore one snapshot over its item. Run without `--apply` first to see the verdict.

```
python itemvault.py --url https://example.maps.arcgis.com --restore vault/SNAPSHOT.json
python itemvault.py --url https://example.maps.arcgis.com --restore vault/SNAPSHOT.json --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--url` | none | The portal, such as `https://example.maps.arcgis.com` or `https://gis.example.org/portal`. Required. |
| `--item` | none | An item to snapshot. Repeat it for more items. |
| `--vault` | see text | The snapshot directory. Required with `--item`. With `--restore` it defaults to the directory of the snapshot file. |
| `--restore` | none | A snapshot file to restore over its item. |
| `--apply` | off | Write the snapshot files, or do the restore. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Give `--item` or `--restore`, not both. The parser refuses a shortened flag, so `--ap` is an error
and is never read as `--apply`.

Exit codes: 0 nothing was refused, 1 refused, 2 a portal call, a file, or the check after the write
failed, 64 usage error.

## What it decides

A restore gets exactly one verdict. The tool checks them in this order.

| Verdict | Exit | When |
|---|---|---|
| `CORRUPT` | 1 | The snapshot file fails its own checks. Its data does not match its sha256, it holds `{}`, or a field is missing. No portal call is made. |
| `OTHER-PORTAL` | 1 | The snapshot was taken on a different portal. An item id that two portals share does not make them one item. No portal call is made. |
| `NOOP` | 0 | The live item already holds the snapshot's configuration. Nothing is written. |
| `FUTURE` | 1 | The snapshot says the item was modified later than the live item says. The snapshot is not from this item's history. |
| `UNSAVED` | 1 | The live configuration is in no snapshot in the vault. A restore would destroy it. |
| `RESTORE` | 0 | The live configuration is in the vault, or the live item has no configuration. The restore can be undone. |

After a write under `--apply`, the tool reads the item's data back:

| Verdict | Exit | When |
|---|---|---|
| `VERIFIED` | 0 | The portal now holds exactly the snapshot's configuration. |
| `MISMATCH` | 2 | The portal said `success: true`, but the data it returns is not the snapshot's. |

## A real run

This run used a scratch copy of the self-test's stand-in portal on `127.0.0.1`. The stand-in lives
only inside `--self-test`, so you cannot repeat these four transcripts as printed. A snapshot was
taken at midnight. A second layer was added at 12:00, and a layer URL broke at 14:00.

```
$ python itemvault.py --url http://127.0.0.1:8766 --restore vault/0123...-1767225600000-f645....json --apply
itemvault: restore 0123456789abcdef0123456789abcdef from the snapshot of 2026-01-01T00:00:00Z
UNSAVED: The item was modified 2026-01-01T14:00:00Z, after the snapshot of 2026-01-01T00:00:00Z, and no snapshot in the vault holds that configuration. Restoring would destroy that edit. Snapshot it first:
    python itemvault.py --url http://127.0.0.1:8766 --item 0123456789abcdef0123456789abcdef --vault DIR --apply
Refused. Nothing was written.
exit 1

$ python itemvault.py --url http://127.0.0.1:8766 --item 0123456789abcdef0123456789abcdef --vault vault --apply
0123456789abcdef0123456789abcdef: wrote vault\0123456789abcdef0123456789abcdef-1767276000000-b18508feebc16acd0ef9620374ef45b3a267306062d48593405230c6c500982b.json
exit 0

$ python itemvault.py --url http://127.0.0.1:8766 --restore vault/0123...-1767225600000-f645....json --apply
itemvault: restore 0123456789abcdef0123456789abcdef from the snapshot of 2026-01-01T00:00:00Z
RESTORE: The live configuration (modified 2026-01-01T14:00:00Z) is held by vault\0123456789abcdef0123456789abcdef-1767276000000-b18508feebc16acd0ef9620374ef45b3a267306062d48593405230c6c500982b.json, so this restore can be undone.
keys that change: operationalLayers
VERIFIED: the portal now holds the snapshot's configuration.
exit 0

$ python itemvault.py --url http://127.0.0.1:8766 --restore vault/0123...-1767225600000-f645....json --apply
itemvault: restore 0123456789abcdef0123456789abcdef from the snapshot of 2026-01-01T00:00:00Z
NOOP: The live configuration already matches the snapshot. Nothing to restore.
exit 0
```

The snapshot file names are shortened with `...` in the commands above. The output is unchanged.
The colleague's 12:00 layer is not lost. The 14:00 snapshot holds it, together with the broken URL,
and she can copy the layer back from that file.

## Why the obvious version is wrong

The first guard people write compares times: refuse when the live item's `modified` is later than
the snapshot's. That guard refuses every restore anyone would want, because the edit you want to
undo is always later than the snapshot. A guard that always refuses invites an override flag, and
a flag that is always needed stops guarding. The question that matters is not "is the live item newer". It is "can the configuration I am
about to overwrite be put back". This tool answers that question by content. It hashes the live
configuration and looks for that hash in the vault.

Three details follow from that:

- **Order of keys.** The portal can return the same JSON with its keys in another order. Before
  hashing, the tool sorts the keys and removes whitespace. Otherwise every unchanged item looks
  edited, and every restore looks unverifiable.
- **A wiped item.** When an item's configuration is wiped to `{}`, no snapshot can hold the wipe,
  because the tool refuses to keep an empty configuration. A content-only rule would then refuse
  the one restore that matters most. An item with no configuration has nothing to lose, so that
  restore is allowed.
- **Success that did nothing.** `success: true` from the update call is not proof. The tool reads
  the data back and compares hashes. A write that the portal confirmed but did not make is
  `MISMATCH`, exit 2.

## Existing alternatives

The backup half of this tool is a commodity. Use one of these if a backup is all you need.

- [A-Charvin/AGOL-Backup-Utility](https://github.com/A-Charvin/AGOL-Backup-Utility) has a GUI, scans
  an organization, backs up items in several formats, and restores them. Its README says the ZIP
  restore "Creates new item with GIS.content.add()". A new item gets a new id, so every app and
  dashboard that refers to the old id still refers to the old id. Its other restore path has an
  `overwrite` option. The README does not describe a check against newer edits.
- [skeenp/agol_backup](https://github.com/skeenp/agol_backup) schedules backups of ArcGIS Online
  content. It is archived.
- The ArcGIS Online [recycle bin](https://doc.arcgis.com/en/arcgis-online/manage-data/restore-items.htm)
  restores deleted items for 14 days. It holds deleted items. The page says nothing about earlier
  versions of an item that was edited.
- The ArcGIS Enterprise WebGISDR utility
  ([overview](https://enterprise.arcgis.com/en/portal/11.3/administer/linux/overview-backup-restore-web-gis.htm))
  backs up and restores a whole deployment. Its
  [restore page](https://enterprise.arcgis.com/en/portal/11.0/administer/windows/restore-web-gis.htm)
  says: "Any items or services created since the last export will be lost." It works at the level
  of the organization, not one item.

- [palavido-dev/agobackup](https://github.com/palavido-dev/agobackup) (AGPL-3.0) is the closest
  match. It is open source, self-hosted and scheduled. Its README says a restore can go "into the
  existing layer (keeping its item ID and service URL ...)" or "over a web map or app whose
  configuration got wrecked", and that "Dry run is the default". The README describes no refusal
  when the live item has a newer edit than the backup. This was checked by reading its README
  only. Its code was not read.
- [agrc/project-moonwalk](https://github.com/agrc/project-moonwalk) (MIT) backs up and restores
  ArcGIS Online items from a website. Its README describes no check of the live item either.

None of these was found, from its README, to refuse an in-place restore because a newer edit would
be lost. On 2026-10-13 an unquoted `gh search repos` for "arcgis online backup restore" found the
three GitHub tools above. The same search for "arcgis item restore" and "webmap backup restore"
found nothing. That is a search result, not a claim that no such tool exists.

Other public repositories are also called `itemvault`. They are unrelated: a Dungeons & Dragons item
vault, a MERN web application and a community showcase app.

## The Esri behaviour it relies on

Each item below is from Esri's documentation, as read on 2026-10-09.

- [Item](https://developers.arcgis.com/rest/users-groups-and-items/item/): `GET
  /sharing/rest/content/items/<itemID>` returns `owner` and `modified`. `modified` is "Shown in UNIX
  time in milliseconds".
- [Item Data](https://developers.arcgis.com/rest/users-groups-and-items/item-data/): `GET
  .../items/<itemID>/data` returns the JSON of a JSON item, and streams the file of a file item.
  The tool keeps only JSON objects. It refuses a file item whose file is not JSON.
- [Update Item](https://developers.arcgis.com/rest/users-groups-and-items/update-item/): `POST
  /sharing/rest/content/users/<userName>/items/<itemID>/update`, with no folder segment. The `text`
  parameter is "The JSON content for the item to be updated". The example response is
  `{"success": true, "id": ...}`. The call is "available to the user and the administrator of the
  organization". The operation has no parameter that makes the write conditional on the item's
  current state. That is why the check has to happen in the client.
- [Common parameters](https://developers.arcgis.com/rest/users-groups-and-items/common-parameters/):
  `token` is the access token parameter.

The tool reads every HTTP 200 response body for an `error` object and treats one as a failure. The
self-test checks this against a stand-in that answers 200 with an error object. No Esri page was
found that states the status code of such responses, so this README does not claim one. A 4xx or
5xx status is a failed call too. The tool reports the status line and does not read the body, so
the portal's own message is not shown.

## Limits

- **Not run against a real portal.** Every network path ran against the stand-in portal on
  `127.0.0.1`. That stand-in follows the documented URLs and responses above. A real portal can
  differ in ways the documentation does not state.
- **A race of one request.** Update Item has no conditional parameter. An edit that lands between
  the tool's read and its write is overwritten, and nothing reports it. An edit that lands between
  the write and the read-back makes the read-back report `MISMATCH`. Run restores when nobody is
  editing the item.
- **The configuration only.** The tool keeps and restores the item's data, the JSON that `/data`
  returns. It does not restore the title, description, tags, thumbnail, sharing, metadata or
  resources.
- **No organization-wide enumeration.** Name each item with `--item`. To list every item in an
  organization, use [itemcensus](https://github.com/uhsear/itemcensus).
- **Rewrites keep their form.** `1.0` and `1` hash differently. If a portal rewrites a number, a key
  or a value when it saves, the read-back reports `MISMATCH`, and that item cannot be restored. The
  tool never reports a false `VERIFIED`.
- **Tokens.** The read calls carry the token as the `token` query parameter. The write carries it in
  the POST body. Every error message has the token redacted. The tool sends no `Referer` header, and
  a token bound to a referer was not tried.
- **Plain http.** The tool refuses `http://` except for `127.0.0.1` and `localhost`, where the
  self-test's stand-in listens.
- **Coverage.** `coverage run --branch itemvault.py --self-test` reports 100 percent of 763
  statements and 144 branches.

### Smaller edge cases

Each one below is a known edge case. None of them writes without `--apply`.

- **Items in a folder.** The update call uses the root URL and omits the folder segment. Esri
  documents a folder form, and this was not run against a real portal.
- **Key order on write.** The write sends the configuration with sorted keys and no spaces, so a
  restored web map's JSON has its keys in a different order.
- **Not an object.** A live item whose data is a non-empty JSON list, string or number counts as
  having no configuration, so a restore overwrites it without a snapshot.
- **A key named error.** A configuration with a top-level `error` object is read as a portal error,
  and the run exits 2.
- **Item type.** The tool never checks the item type. A file item whose file is a JSON object passes
  as a configuration, and that restore was not tried on a real portal.
- **An HTML answer.** If `/data` answers with an HTML page, the tool reports a file item and exits
  1, not 2.
- **Redirects.** The tool follows HTTP redirects on read calls, to another host or to plain http,
  and reads the body it is sent to as the item's data. The read calls carry the token in the query,
  so a redirecting portal or proxy can receive it.
- **IPv6 and ports.** An IPv6 literal host gives an invalid URL, and `:443` on an https URL is not
  dropped, so `https://host:443` and `https://host` count as two portals.
- **Item id spelling.** The snapshot keeps the id as typed on the command line, so upper-case and
  lower-case spellings of one id make separate entries, and a restore can be refused as `UNSAVED`.
- **Portal in the file name.** The file name carries no portal. Two portals that share an item id,
  an edit time and a configuration write one file name, and the second write replaces the first.
- **Snapshot checks.** Only `data`, `item`, `modified` and `portal` are checked, and the sha256
  covers `data` alone. A hand-edited `modified` or `item` field is not detected.
- **Trusted item id.** A restore trusts the item id inside the snapshot file, so restore only files
  you made or reviewed.
- **Byte order mark.** A snapshot file that starts with a UTF-8 byte order mark is refused as
  `CORRUPT` with the text "it is not an itemvault snapshot". A vault file with one is counted as
  skipped.
- **NaN.** NaN or Infinity in item data is written to the snapshot as those bare tokens. A real
  portal would likely reject the later update with exit 2.
- **Undo outside the vault.** A restore from a snapshot file outside `--vault` says it can be
  undone, but the undo is refused as `UNSAVED` until that configuration is snapshotted into the
  vault. The refusal prints the command.
- **Secrets inside items.** A snapshot is a copy of the item's JSON, including any token or password
  embedded in a layer URL. Protect the vault like the item and do not commit it.
- **Token typed as a flag.** A token typed on the command line by mistake is echoed in the usage
  error, so keep it in `ITEMVAULT_TOKEN` only.
- **Cut-off token.** If the portal answers an update with a non-JSON page that echoes the token,
  the first 80 bytes are printed, and a token cut at that boundary is shown in part.
- **Console encoding.** Output is not forced to UTF-8. A vault path outside the Windows code page
  can raise `UnicodeEncodeError` when stdout is redirected to a file.
- **Unreadable vault.** A vault directory that cannot be listed ends the run with a Python
  traceback and exit 1, which this tool otherwise uses for "refused".

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [sharewatch](https://github.com/uhsear/sharewatch) - snapshots sharing, where this snapshots configuration
- [whobreaks](https://github.com/uhsear/whobreaks) - lists what refers to an item before you change it
- [itemcensus](https://github.com/uhsear/itemcensus) - lists every item in an organization
- [safe-republish](https://github.com/uhsear/safe-republish) - the same refusal before a destructive write, for a feature class
