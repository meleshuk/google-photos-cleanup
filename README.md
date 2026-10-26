# Google Photos Orphan Cleanup

Two conservative tools for finding Google Photos items that are not in any
album and, after human review, moving those items to Trash.

1. `gphotos_find_orphans.py` analyzes a Google Takeout export entirely offline.
2. `gphotos_delete_orphans.py` consumes the reviewed report and drives your own
   authenticated Chrome session one item at a time.

## Important warnings

- This project can delete photos. Deleted items remain in Google Photos Trash
  for 60 days, which is the only undo mechanism.
- Automated browser access to Google Photos is outside Google's published APIs
  and may violate Google's Terms of Service. It may also trigger rate limits or
  account challenges.
- Google Photos has no supported deletion API. The deletion tool relies on web
  UI text and behavior that Google can change without notice.
- Always start with offline analysis, manually review samples, use the default
  dry run, and execute a five-item smoke test before a larger batch.
- Never publish Takeout archives, orphan reports, deletion journals, or real
  Google Photos permalinks. See [SECURITY.md](SECURITY.md).

## Safety properties

The finder:

- never accesses the network;
- reads Takeout ZIP files directly without decompressing media;
- honors album sidecars even when Takeout omits the album's media copy;
- treats ambiguous items as `UNCERTAIN`, never as deletion candidates;
- checks that its accounting reconciles before returning success.

The deleter:

- is dry-run by default; deletion requires `--execute`;
- acts only on exact permalinks from the reviewed input file;
- limits each run, paces requests, and stops on repeated failures;
- never automates sign-in and stores no Google credentials;
- handles stacked photos by selecting only **Current photo only**, never
  **All N photos**;
- verifies each deletion and writes an append-only resume journal;
- never opens or empties Trash.

Both scripts contain detailed design notes and guardrail rationale in their
module documentation.

## Requirements

- macOS or another Unix-like system
- Python 3.9 or newer
- A complete Google Takeout export for Google Photos
- For deletion only: Google Chrome and the Python `playwright` package

The finder uses only Python's standard library.

Clone the repository:

```bash
git clone https://github.com/meleshuk/google-photos-cleanup.git
cd google-photos-cleanup
```

## 1. Export Google Photos

Request an export from [Google Takeout](https://takeout.google.com), select
Google Photos, and download every ZIP part. Do not analyze parts separately:
Takeout can place a media file and its JSON sidecar in different archives.

Do not use macOS `/usr/bin/unzip` for this export. It can silently skip folders
whose names contain non-ASCII characters. The finder reads ZIPs directly, which
avoids that problem.

## 2. Find orphan candidates

```bash
python3 gphotos_find_orphans.py \
  ~/Downloads/takeout-*.zip \
  -o ~/Desktop/orphans.json
```

This creates:

- `orphans.json`: full machine-readable report;
- `orphans.csv`: deletion candidates with Google Photos permalinks;
- `orphans.uncertain.csv`: items intentionally withheld from the candidate set.

You may also pass a directory containing all ZIP parts or an already extracted
Takeout tree.

Read the command's summary carefully. At minimum:

1. Confirm every archive part is listed and part numbers are contiguous.
2. Check folder classification and the album count.
3. Require the reconciliation and stem-guard cross-checks to pass.
4. Open at least ten random candidate permalinks and confirm the items have no
   real album membership.
5. Pick items from several real albums and confirm they are absent from the
   orphan report.

The script's opening documentation contains the complete validation checklist
and explains generated-album flags, archived items, filename collisions, Live
Photos, edited renditions, and Takeout sidecar behavior.

## 3. Prepare browser-assisted deletion

Install the one optional dependency:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The tool attaches to your own Chrome; it does not download or launch a bundled
browser, so do not run `playwright install`.

Quit Chrome completely, then start a dedicated profile with local debugging:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-cdp-profile"
```

Sign into Google manually in that window and open
<https://photos.google.com>. Do not expose port 9222 to a network.

## 4. Dry-run and smoke-test

Offline planning performs no browser access and no deletion:

```bash
.venv/bin/python gphotos_delete_orphans.py \
  --input ~/Desktop/orphans.csv \
  --offline --limit 10
```

Then resolve a small batch through the real browser without changing anything:

```bash
.venv/bin/python gphotos_delete_orphans.py \
  --input ~/Desktop/orphans.csv
```

The first destructive run should be only five items:

```bash
.venv/bin/python gphotos_delete_orphans.py \
  --input ~/Desktop/orphans.csv \
  --smoke --execute
```

Verify those five items in Google Photos and Trash before continuing.

## 5. Continue in controlled batches

```bash
.venv/bin/python gphotos_delete_orphans.py \
  --input ~/Desktop/orphans.csv \
  --execute --limit 100 --delay 10
```

The default journal is `~/Desktop/gphotos_delete_journal.jsonl`. Keep it: it
makes runs resumable and prevents completed URLs from being acted on again.
Interrupting with Ctrl-C is safe because the journal is flushed after each item.

Use `--help` and the script's module documentation for cooldowns, quarantine,
large-run confirmation, exit codes, and recovery guidance.

## Tests

The finder has a synthetic end-to-end Takeout test. The deleter includes 99
offline guardrail and regression tests that never open a browser or network
connection.

```bash
python3 -m unittest discover -s tests -v
python3 gphotos_delete_orphans.py --self-test
```

## Project status

The finder targets the current Google Takeout layout but remains intentionally
conservative when an export is ambiguous. The deleter depends on the Google
Photos web UI and should be considered experimental. Re-run tests and perform a
new smoke test whenever Google changes that UI.

## License

[MIT](LICENSE)
