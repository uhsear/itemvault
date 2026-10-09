# itemvault

Restore an ArcGIS item's configuration in place, and refuse to overwrite an edit that no snapshot
holds.

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
144 assertions, 0 failed
```

The full run prints all 144 assertions. The three `...` lines show where this block is cut.

## Requirements

Python 3.9 or newer, and nothing else. No `arcgis` package, no `arcpy`, no third-party module.

The self-test prints 144 assertions on Windows (Python 3.13.2) and 144 on Ubuntu (Python 3.12.3).
The two outputs are identical line for line. The code parses as Python 3.9 syntax
(`ast.parse(..., feature_version=(3, 9))`), but no 3.9 interpreter has run it.

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

This run used the stand-in portal on `127.0.0.1`. A snapshot was taken at midnight. A second layer
was added at 12:00, and a layer URL broke at 14:00.

```
$ python itemvault.py --url http://127.0.0.1:8766 --restore vault/0123...-1767225600000-f5bf....json --apply
itemvault: restore 0123456789abcdef0123456789abcdef from the snapshot of 2026-01-01T00:00:00Z
UNSAVED: The item was modified 2026-01-01T14:00:00Z, after the snapshot of 2026-01-01T00:00:00Z, and no snapshot in the vault holds that configuration. Restoring would destroy that edit. Snapshot it first:
    python itemvault.py --url http://127.0.0.1:8766 --item 0123456789abcdef0123456789abcdef --vault DIR --apply
Refused. Nothing was written.
exit 1

$ python itemvault.py --url http://127.0.0.1:8766 --item 0123456789abcdef0123456789abcdef --vault vault --apply
0123456789abcdef0123456789abcdef: wrote vault\0123456789abcdef0123456789abcdef-1767276000000-2731ca84c906e16257c2845f66a66cd557aa352d1c0d07bbfeeaa62fe49db6be.json
exit 0

$ python itemvault.py --url http://127.0.0.1:8766 --restore vault/0123...-1767225600000-f5bf....json --apply
itemvault: restore 0123456789abcdef0123456789abcdef from the snapshot of 2026-01-01T00:00:00Z
RESTORE: The live configuration (modified 2026-01-01T14:00:00Z) is held by vault\0123456789abcdef0123456789abcdef-1767276000000-2731ca84c906e16257c2845f66a66cd557aa352d1c0d07bbfeeaa62fe49db6be.json, so this restore can be undone.
keys that change: operationalLayers
VERIFIED: the portal now holds the snapshot's configuration.
exit 0

$ python itemvault.py --url http://127.0.0.1:8766 --restore vault/0123...-1767225600000-f5bf....json --apply
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
undo is always later than the snapshot. So it gets an override flag, and the override is used every
time. The question that matters is not "is the live item newer". It is "can the configuration I am
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

None of these was found to refuse an in-place restore because a newer edit would be lost. A
`gh search repos` for the name and for "arcgis backup", "arcgis item restore" and
"webmap backup restore" found no tool that does. That is a search result, not a claim that none
exists.

## The Esri behaviour it relies on

Each item below is from Esri's documentation, as read on 2026-10-09.

- [Item](https://developers.arcgis.com/rest/users-groups-and-items/item/): `GET
  /sharing/rest/content/items/<itemID>` returns `owner` and `modified`. `modified` is "Shown in UNIX
  time in milliseconds".
- [Item Data](https://developers.arcgis.com/rest/users-groups-and-items/item-data/): `GET
  .../items/<itemID>/data` returns the JSON of a JSON item, and streams the file of a file item.
  The tool keeps only JSON objects. It refuses a file item.
- [Update Item](https://developers.arcgis.com/rest/users-groups-and-items/update-item/): `POST
  /sharing/rest/content/users/<userName>/items/<itemID>/update`, with no folder segment. The `text`
  parameter is "The JSON content for the item to be updated". The example response is
  `{"success": true, "id": ...}`. The call is "available to the user and the administrator of the
  organization". The operation has no parameter that makes the write conditional on the item's
  current state. That is why the check has to happen in the client.
- [Common parameters](https://developers.arcgis.com/rest/users-groups-and-items/common-parameters/):
  `token` is the access token parameter.

The tool reads every response body for an `error` object, whatever the HTTP status. It treats an
error object as a failure. The self-test checks this against a stand-in that answers 200 with an
error object. No Esri page was found that states the status code of such responses, so this README
does not claim one.

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
- **Numbers keep their form.** `1.0` and `1` hash differently. If a portal rewrites one into the
  other, the read-back reports `MISMATCH`. The tool never reports a false `VERIFIED`.
- **Tokens.** The read calls carry the token as the `token` query parameter. The write carries it in
  the POST body. Every error message has the token redacted. The tool sends no `Referer` header, so
  a token that is bound to a referer fails with the portal's own error, exit 2.
- **Plain http.** The tool refuses `http://` except for `127.0.0.1` and `localhost`, where the
  self-test's stand-in listens.
- **Coverage.** `coverage run --branch itemvault.py --self-test` reports 100 percent of 757
  statements and 142 branches.

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
