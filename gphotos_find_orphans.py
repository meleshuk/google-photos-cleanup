#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gphotos_find_orphans.py 6.0.0 - find Google Photos items in the library but in NO album.

WHY THIS EXISTS
---------------
The Google Photos Library API can no longer enumerate or delete your library (the
photoslibrary* OAuth scopes were removed on 2025-03-31, and the API never had a
delete-media-item method), and the Google Photos web UI has no "not in any album"
filter. A Google Takeout export is therefore the only way to IDENTIFY which items
are orphans. This script does the set difference. It does NOT delete anything.

It reads Takeout .zip archives DIRECTLY, so you never have to unzip a 250GB export.
Computing the delta needs only two things: the set of file PATHS in the export, and
the CONTENTS of the small sidecar .json files. Both come out of a zip's central
directory and its JSON entries. Media entries are never decompressed - the script
reports the bytes it actually read so you can see that for yourself.

    *** macOS /usr/bin/unzip IS UNSAFE FOR THIS EXPORT. ***
    On album folders with non-ASCII (e.g. Cyrillic) names it fails with
    "checkdir error ... Illegal byte sequence" and SILENTLY SKIPS those folders
    while continuing with everything else. The resulting tree looks fine but is
    missing whole albums - which would make every photo filed in them look like an
    orphan and get it deleted. If you must extract by hand, use
        ditto -x -k takeout-....zip ~/Downloads/takeout/
    or bsdtar (`tar -xf takeout-....zip`), both of which handle the names correctly.
    With zip mode you do not need to extract at all, which is the safest option.

THE THREE RULES THAT DECIDE WHAT AN "ITEM" IS
=============================================
Everything downstream is a set difference between library items and album items, so
the only thing that really matters is agreeing with Google Photos about what one
item is, and about what proves membership. Three rules do that, and each one was
wrong in an earlier version in a way that would have deleted filed photos.

RULE 1: ALBUM MEMBERSHIP COMES FROM SIDECARS, NOT FROM MEDIA COPIES
-------------------------------------------------------------------
Takeout does NOT reliably duplicate the media file into every album folder. Very
often an album folder contains ONLY the sidecar JSONs for its items, with zero
media files - a real export had 53 album folders holding 0 media files, and 3279
album-side sidecars with no media copy beside them.

That is normal Takeout behaviour, and it does not weaken the evidence at all: a
sidecar carries `title` and `photoTakenTime`, which is exactly - and only - what
the logical-item key needs. Whether a media copy accompanies it is IRRELEVANT to
membership.

    An album-folder sidecar registers its item as an album member whether or not
    a media file sits next to it.

Deriving membership from media+sidecar PAIRS (as versions before this one did)
throws away the membership a lone sidecar represents, and the item then falls
through into the orphan list - i.e. a photo you HAD filed in an album gets queued
for deletion. On the real export that affected 3186 of 23641 reported orphans
(13.5%), across 61 album folders.

LIBRARY-SIDE SIDECARS WITH NO MEDIA are treated the opposite way, deliberately,
because the safety asymmetry is reversed:
  * an album-side sidecar PROTECTS an item from deletion -> always honour it;
  * a library-side sidecar makes an item a deletion CANDIDATE -> honouring it
    would add a photo to the delete list on the strength of a file we never saw.
So a library/Archive folder sidecar with no media anywhere in the export counts as
a library item (it is one) but is classified UNCERTAIN, never an orphan.

RULE 2: A SIDECAR-LESS MEDIA FILE IS USUALLY ANOTHER RENDITION OF A KEYED ITEM
------------------------------------------------------------------------------
One Google Photos item can export as SEVERAL files, only one of which carries the
sidecar. Two forms of that occur, and they are handled by ONE rule, not two
special cases:

  (a) EDIT RENDITIONS. Photo edits are non-destructive: one item holds the
      original plus its edits, and Takeout writes the edited rendition as
      "<stem>-edited.<ext>" with no sidecar of its own. Verified against the UI -
      "20130831-13-51-31-edited.jpg" does not exist as an item, while
      "20130831-13-51-31.jpg" does.
  (b) LIVE PHOTO / MOTION PHOTO companions. The still half carries the sidecar and
      the motion half is exported without one, e.g.
      "Photos from 2018/IMG_5566.MP4" beside "Photos from 2018/IMG_5566.HEIC".

So: a media file with NO sidecar is FOLDED INTO an existing sidecar-keyed item in
the SAME folder when its stem matches after stripping a known rendition suffix.
A folded file never becomes its own logical item, never produces an UNCERTAIN row,
is never listed in "media_without_sidecar" as though metadata were missing, and
inherits its base item's album membership and orphan/keeper status - appearing in
that single row's file list.

The fold is deliberately conservative. It requires the SAME folder and an EXACT
stem match (no cross-folder matching: the same stem routinely recurs in a
different year), and it accepts only these shapes:
  (a) suffix stripped and the base has the SAME extension      -> edit rendition
  (b) no suffix, the sidecar-less file is a VIDEO and the base
      is a STILL IMAGE                                          -> motion half
If the stem matches ZERO keyed items in that folder, or MORE THAN ONE, the file is
left as UNCERTAIN rather than guessed at. A count of folded renditions, by form,
is printed.

    Consequences worth knowing: deleting such an item removes BOTH renditions,
    because they are one item. Ordinary photo edits therefore do NOT create extra
    orphan candidates. VIDEO "save a copy" edits are different - they create a
    genuinely separate item with its own sidecar and its own url, so they never
    appear as "-edited" and are counted as items in their own right. (Consistent
    with the real export: of 588 "-edited" files, 575 were JPG and 13 HEIC, and
    zero were videos; and all 143 sidecar-less MP4s had a same-stem still sibling
    in the same folder - HEIC 135, JPG 6, MOV 2.)

Folding is also what makes the Live Photo count truthful. Collapsing still+video
pairs by sidecar alone can only see pairs where BOTH halves have a sidecar, which
on an iPhone-heavy library reported 3 pairs where there were about 146.

RULE 3: ITEM IDENTITY IS (TITLE STEM, photoTakenTime)
-----------------------------------------------------
See MATCHING STRATEGY at the bottom. The short version: the sidecar's `title` is
the original filename before Takeout truncated or deduped it, so it is the stable
half of the key; the stem (no extension) is what collapses a Live Photo's two
halves into one item; and every fallback is biased towards UNCERTAIN.

WHAT 6.0.0 CHANGED, AND THE 5.0.0 EXPERIMENT IT REPLACES
========================================================
5.0.0 tried to rekey item identity onto the sidecar `url` permalink instead of
(title stem, photoTakenTime). It was DISCARDED. On a real 5-part export its own
cross-check caught the failure before anything was deleted:

    library sidecars    56591 -> 56591 DISTINCT urls
    album   sidecars    29119 -> 29119 DISTINCT urls
    library items in a real album   3608   (27933 under 4.x)
    items in >1 real album             0   (1081 under 4.x)
    orphans                        52983 of 56650 library items
    cross-check "4.x called it filed, url-keying calls it an orphan":
                                   expected 0, FOUND 26139

The 3608 that did match equalled EXACTLY the `sidecar-only` bucket in
resolution_methods - i.e. the only album entries whose url matched a library url
were the album folders holding NO media copy.

    *** TAKEOUT'S SIDECAR `url` IS PER-COPY, NOT PER-ITEM. ***
    When Takeout writes an album media copy, THAT COPY'S sidecar carries its own
    distinct permalink. So `url` is unique, but it is NOT SHARED between an item's
    album copy and its library copy - and being shared across copies is the one
    property identity requires. `url` is therefore unusable for membership
    matching and must never be used as a key. It remains correct and useful as a
    DELETION ADDRESS, which is the only thing it is used for here.

6.0.0 keeps 4.x's (title stem, photoTakenTime) key - it is right; it matched 27933
library items into albums - and instead narrows the one thing that was actually
broken: the SECONDARY same-stem guard.

THE SAME-STEM GUARD, AND WHY IT HAD TO BE NARROWED
--------------------------------------------------
4.x held an item back as UNCERTAIN whenever its stem appeared ANYWHERE on the album
side at a different timestamp. The guard's real purpose is narrow: to catch Takeout
writing a slightly DIFFERENT photoTakenTime for an item's album copy than for its
library copy, which is a seconds-to-minutes discrepancy. But as written it fired
6080 times (30% of the orphan list) on FALSE collisions, because iPhones recycle
IMG_NNNN filenames and the stem has the extension stripped off it:

    2025 Whistler/IMG_9247.mov          (ALBUM - a 2025 video)
    Photos from 2018/IMG_9247.HEIC      (library - withheld, because of that .mov)
    Photos from 2019/IMG_9247.HEIC      (also withheld)

Two independent narrowings, both required, both configurable:

  (a) EXTENSION-AWARE. A collision now also has to agree on extension, so a .mov
      can no longer collide with a .HEIC. Extension comes from the media file where
      there is one and from the sidecar `title` otherwise, so a sidecar-only album
      membership proof still contributes an extension. An item claims BOTH, which
      covers the transcode case from evidence rather than from a heuristic: a HEIC
      exported as JPG has a .jpg file beside a title reading ".HEIC", so it claims
      {.jpg, .heic} and still matches either side of the rewrite.

      An extension MISMATCH is treated as a WEAKER SIGNAL, not as a hard
      non-collision. Takeout really does rewrite extensions (HEIC exported as JPG;
      "-edited" renditions are JPG whatever the original was), so a mismatched pair
      can still be one item. But a genuine transcode is written in one pass with
      essentially the same metadata, so its timestamp discrepancy is SECONDS. A
      mismatched pair therefore still withholds the item, but only inside a much
      tighter window (--stem-collision-window-ext-mismatch, default 60s) and under
      its own UNCERTAIN reason so you can see and count it.

  (b) TIME-BOUNDED. An item is held back only if a same-stem album item lies within
      --stem-collision-window (default 24h) of its photoTakenTime. A collision
      years apart is filename recycling, and such an item is an ORPHAN.
      An album item with NO photoTakenTime cannot be time-bounded, so it still
      collides unconditionally - the conservative reading.

Set --stem-collision-window to 0 to switch the guard off entirely; that is a
diagnostic, not a recommendation.

PICK THE WINDOW FROM YOUR OWN EXPORT, NOT FROM THIS DEFAULT
-----------------------------------------------------------
Every run prints SAME-STEM COLLISION TIME DELTAS: for every same-stem
album/library pair it reports |delta photoTakenTime| bucketed (<1s, <1min, <1h,
<24h, <30d, <1y, >=1y), split by whether the extensions match, with examples per
bucket - plus the same distribution over only each library item's NEAREST same-stem
album item, which is what the guard actually tests. If that distribution is bimodal
(a tight cluster near zero, plus a mass years out) the window approach is validated
and the gap between the two modes is where to cut. Choose on that evidence.

EVERY RUN CROSS-CHECKS ITSELF AGAINST 4.x
-----------------------------------------
The 4.x guard is kept in the code, verbatim, and the whole classification is
replayed with it on every run. The report then states:
  * items 4.x called FILED that the new guard calls an ORPHAN  - must be 0, and a
    non-zero value is printed loudly and exits non-zero. This is the check that
    caught 5.0.0.
  * items 4.x called an ORPHAN that the new guard holds back   - must be 0; the
    change only ever loosens the guard.
  * items 4.x held UNCERTAIN that the new guard releases to ORPHAN - this is the
    INTENDED effect of 6.0.0, reported as a delta with a by-reason breakdown.

HARD GUARDRAILS (by design, please verify by reading the code)
--------------------------------------------------------------
  * STRICTLY READ-ONLY. It never deletes, moves, renames, or modifies anything in
    the export, and never writes into or re-saves an archive: every zip is opened
    mode "r". The only files written are the report files you name with -o.
  * NO NETWORK. Nothing in here opens a socket; stdlib only, no urllib use.
  * NO third-party packages, no internal tooling. Python 3.8+ standard library only,
    so it runs on a stock macOS Python 3.
  * Conservative by construction: a false "orphan" leads to a deleted photo, so
    anything the matcher is not sure about is reported as UNCERTAIN, never as an
    orphan.
  * EVERY album folder counts as a REAL album unless YOU explicitly declare it
    auto-generated. There is no built-in guess-list in effect; nothing is
    reclassified without a flag you typed.

ZIP INPUT, AND WHY ONE GLOBAL INVENTORY IS MANDATORY
----------------------------------------------------
Takeout splits one logical export across archives at ARBITRARY points, including
mid-folder. A media file can sit in part 001 while its own sidecar JSON sits in
part 003, and a single album folder's contents can straddle several archives.

Therefore this script builds ONE GLOBAL INVENTORY across every input you give it,
keyed by the path INSIDE the Takeout tree with the archive-local prefix
("Takeout/", "Takeout-2/", the archive filename) normalized away. Folder
classification, rendition folding, sidecar pairing and the set difference all run
afterwards, on that merged inventory. Pairing per-archive would strand sidecars
from their media and invent FALSE ORPHANS - the direction that makes you delete
photos you had filed.

FILENAME ENCODING (the failure that silently eats albums)
---------------------------------------------------------
Zip stores entry names either as UTF-8, flagged by general-purpose bit 11 (EFS),
or in an unspecified legacy encoding when that bit is clear. Python's zipfile
decodes flagged names as UTF-8 and UNFLAGGED names as CP437 - which turns real
UTF-8 bytes into mojibake ("Альбом" -> "╨É╨╗╤î╨▒╨╛╨╝"). Two different albums can
mojibake into names that collide, or decode into something unusable.

This script handles both cases per entry:
  * bit 11 set            -> decode UTF-8 (zipfile already did), status utf8_flagged
  * bit 11 clear          -> re-encode zipfile's cp437 string back to the original
                             bytes and decode those as UTF-8, status utf8_recovered
  * clear, not valid UTF-8-> keep the CP437 reading, status cp437_literal, and WARN
                             (that reading is a guess about a legacy encoding)
  * bytes not round-trippable at all -> status undecodable, and WARN
Counts for all four appear in the per-archive summary, so you can see at a glance
that nothing was dropped. Entry names are NEVER decoded with errors="replace",
because that can map two distinct album folders onto one name and silently merge
them. If two distinct raw names still collide after decode and normalization, the
script warns loudly and prints both.

Unicode normalization: macOS hands back directory names in NFD, while zip entries
and the sidecar `title` field are typically NFC. The same NFC normalization is
applied to zip entry paths AND to directory paths AND to titles, so zip mode and
directory mode agree on identical content.

PER-ITEM PERMALINKS
-------------------
Takeout sidecars carry a `url` field (present on 100% of 82107 sidecars in a real
export, e.g. https://photos.google.com/photo/AF1Qip...). That URL is carried
through onto every orphan AND every uncertain row, in both the JSON and the CSV,
because addressing an item by its own permalink is what makes a later deletion
pass tractable instead of fighting the virtualized infinite-scroll grid. Rows for
items that have no sidecar at all have no URL, and the output says so plainly:
those cannot take part in an automated, permalink-driven pass. A companion tool has
already used these urls to trash items successfully, so as an ADDRESS the field is
sound.

    It is NOT an identity. See "WHAT 6.0.0 CHANGED" above: the url is per-COPY, so
    an item's album copy and its library copy carry DIFFERENT urls. Nothing in this
    script may key, group, or match on `url`, and 5.0.0 was discarded for doing
    exactly that.

AUTO-GENERATED ALBUMS (the risky knob - read before using it)
-------------------------------------------------------------
Google creates albums you did not make: "Spotlight", face/people clusters named
after people, auto-created trips/memories, generated movies/animations/collages.
Membership in one of those is not a reason to keep a photo, so declaring an album
generated makes its items ORPHANS unless some *real* album also holds them.

    *** THIS IS THE DANGEROUS DIRECTION. ***
    Declaring an album generated MOVES ITS ITEMS INTO THE DELETE LIST. If you
    declare a real album by mistake, you will delete photos you had filed. The
    opposite mistake (leaving a generated album undeclared) only leaves a few
    items out of the orphan list, which costs you nothing but a second pass.

So reclassification is explicit, exact, and auditable:

    --generated-album "Spotlight"       repeatable; matches the folder name EXACTLY
                                        (Unicode-normalized, case-insensitive)
    --generated-albums-file FILE        one folder name per line; '#' starts a
                                        comment; blank lines ignored
    --generated-album-regex RE          repeatable; case-insensitive FULL match
                                        against the folder name. Powerful and
                                        correspondingly easy to get wrong - every
                                        folder it matched is printed back to you.

A name you declare that matches NO folder in the export is reported as a warning,
so typos cannot silently fail to apply.

The script NEVER infers "this folder is named after a person, therefore it is a
face cluster". A user-made album named after a person is completely normal and is
indistinguishable from a people cluster by name alone.

WHAT THE EXPORT ACTUALLY TELLS US ABOUT ALBUM PROVENANCE (investigated, see the
SIGNAL SURVEY section of the output)
-----------------------------------------------------------------------------
Takeout has no documented "this album was auto-generated" flag. An album folder
may contain a "metadata.json" (older exports wrap the same fields in an
"albumData" object) holding title/description/access/date/geoData/enrichments -
none of which is specified to mark provenance. So rather than guess, this script
SURVEYS your export: it prints which metadata shapes and field values actually
occur across your album folders and whether any of them partition the set at all.
If every album looks identical, it says so plainly and relies solely on what you
declare. Where a data-driven hint does exist - e.g. a folder whose items are
almost entirely Google-generated media (-ANIMATION/-COLLAGE/-EFFECTS/MOVIE/...) -
it is printed as a SUGGESTION for you to confirm. Suggestions never change the
orphan math on their own; only your flags do.

HOW TO RUN
----------
  1. Request a Takeout of Google Photos (https://takeout.google.com), choose
     "Google Photos", and download all the .zip parts. DO NOT UNZIP THEM.
  2. Point the script at the archives. Any of these work:
         # every part of the export, globbed (recommended)
         python3 gphotos_find_orphans.py ~/Downloads/takeout-*.zip -o ~/Desktop/orphans.json

         # a directory that CONTAINS the zips - they are found for you
         python3 gphotos_find_orphans.py ~/Downloads -o ~/Desktop/orphans.json

         # an already-unzipped tree (original behaviour, still supported)
         python3 gphotos_find_orphans.py ~/Downloads/takeout -o ~/Desktop/orphans.json

         # a mix of zips and directories is fine; duplicate content is de-duplicated
         python3 gphotos_find_orphans.py ~/Downloads/takeout-*.zip ~/old-export -o out.json
     PASS EVERY PART AT ONCE. One logical export split over 5 archives must be read
     as 5 inputs in one run - a media file's sidecar often lives in a different
     part, and running the parts separately would invent false orphans.
  3. Read ARCHIVE / SOURCE SUMMARY first: entry counts, media vs json counts, the
     encoding tallies, and the bytes actually read (media is never decompressed).
     Then read the warnings about part numbering and export timestamps.
  4. First analysis pass - declare nothing:
         python3 gphotos_find_orphans.py ~/Downloads/takeout-*.zip -o ~/Desktop/orphans.json
     Read the full album list in FOLDER CLASSIFICATION, the SIGNAL SURVEY, and the
     SUGGESTIONS, and decide which folders are not your albums.
  5. Second pass - declare them, exactly:
         python3 gphotos_find_orphans.py ~/Downloads/takeout-*.zip -o ~/Desktop/orphans.json \
             --generated-album "Spotlight" \
             --generated-albums-file ~/Desktop/generated-albums.txt
     Then read GENERATED-ALBUM RECLASSIFICATION COST: it shows, per album, exactly
     how many items those flags pushed into the delete list.
  6. Outputs (derived from -o):
         orphans.json            full machine-readable report
         orphans.csv             one row per orphan logical item, with its url
         orphans.uncertain.csv   items we refused to classify, with reason and url
     Plus a human-readable summary on stdout, including CONTIGUOUS DATE RANGES
     intended for shift-click range selection in the web UI.
  7. Other flags:
         --include-archived      also treat Archive-only items as orphans (default: no)
         --library-dir-regex     override which top-level folders are "the library"
         --sample-sidecars N     how many sidecar JSONs to dump key sets for
         --max-samples-per-run N filenames to list per contiguous run
         --no-fold-renditions    disable Rule 2 (diagnostic only; makes every
                                 sidecar-less rendition its own UNCERTAIN row)
         --verbose               per-folder pairing detail

HOW TO SANITY-CHECK BEFORE YOU TRUST IT (do this, it takes 5 minutes)
---------------------------------------------------------------------
  a. Read "ARCHIVE / SOURCE SUMMARY". Every part of your export must be listed. If
     you have 5 zips, 5 rows must appear - a missing part means every photo that
     lived in it is unaccounted for. Check the part-number and timestamp warnings:
     a non-contiguous set (001,002,004) or two different export timestamps means
     you are mixing exports, and the delta will be wrong.
  b. In that table, check "undecodable" and "cp437_literal" are 0. Any non-zero
     value means at least one entry name could not be read as UTF-8, so a folder
     may be misnamed; the names are printed in the warnings. Also check the
     bytes-read figure is tiny relative to the archive size - that is the proof
     media was never decompressed.
  c. Read "HOW THE THREE ITEM-IDENTITY RULES CHANGED THE ANSWER". It states, on
     your own data, how many photos would have been mis-reported as orphans
     without Rule 1, and how many UNCERTAIN rows Rule 2 removed. The Rule 1 number
     is the count of photos this version saves from deletion.
  d. Read the "FOLDER CLASSIFICATION" table. Every folder must be in the bucket you
     expect: library / album / generated_album / archive / excluded. Non-ASCII album
     names must appear correctly spelled - if any looks like mojibake, STOP. If a
     real album got classified as "library" (or vice versa) every number below it is
     wrong; fix with --library-dir-regex and re-run. Album folders marked
     "sidecar-only" are normal Takeout output, not breakage.
  e. Count your albums in the Google Photos album picker and compare with the album
     folder count printed. They should match. A short count here is exactly the
     macOS-unzip failure mode, and it means some albums were never seen.
  f. Read the "generated_album" bucket line by line. Every folder in it is one you
     typed, and every item in it is now deletable. If you do not recognise a folder
     there as something Google made for you, take it out of your flags and re-run.
  g. Read "GENERATED-ALBUM RECLASSIFICATION COST". For each generated album, open
     the matching entry in the Google Photos album picker and confirm it is the
     Google-made collection you meant - not a same-named album of your own.
  h. Pick ~10 random rows from orphans.csv (e.g.
         (head -1 orphans.csv; tail -n +2 orphans.csv | sort -R | head -10) | column -s, -t
     ) and open each row's `url` directly - that is the item's own permalink - then
     check the info panel ("i") shows an empty album list, OR only albums you
     declared generated. The generated_albums column says which to expect. 10/10
     must agree. Prefer rows whose file_count is 2 or more, so you also confirm
     that a folded rendition really is the same item in the UI.
  i. Do the reverse: open two or three of your REAL albums, pick a photo from each,
     and grep its filename in orphans.csv. It must NOT appear. Do this for an album
     the classification table marked "sidecar-only", since that is the case Rule 1
     fixes.
  j. Check the counts add up: library_logical_items == in_album + orphans +
     uncertain (+ archived_excluded). The script prints this and exits non-zero if
     it does not balance.
  k. Read "STEM-COLLISION GUARD CROSS-CHECK". "items 4.x called FILED that this run
     calls an ORPHAN" must be 0. If it is not, STOP - do not delete anything, and
     treat the run as broken. The run also exits non-zero in that case.
  l. Read "SAME-STEM COLLISION TIME DELTAS" and confirm the distribution justifies
     the window you ran with. If deltas cluster near zero and then jump to years,
     the default 24h is comfortably inside the gap. If you instead see a dense band
     of same-extension collisions at hours or days, lower the window and re-run -
     those are plausibly real album/library copies of one item. Then spot-check a
     handful of items the guard RELEASED to the orphan list (they are listed in the
     cross-check section) by opening their urls, exactly as in (h).
  m. Only then delete, and delete in the ranges printed in the "CONTIGUOUS RUNS"
     section, newest-first, verifying each selection count against the printed count
     before confirming the delete.

EXIT CODES
----------
  0  reconciled, and the cross-check against 4.x is clean
  2  the reconciliation line does not balance - do not trust the run (unchanged)
  3  the same-stem guard cross-check found a violation: an item 4.x protected is
     in this run's delete list, or the new guard withheld something 4.x released

MATCHING STRATEGY (explicit, because this is where a wrong answer costs photos)
-------------------------------------------------------------------------------
The same logical photo may appear as a SEPARATE FILE COPY in an album folder and in
the chronological folder - or as a sidecar only - and Takeout mangles filenames
differently in each place (dedup suffixes like "IMG_1234(1).jpg", truncation of
long names, extension rewrites). Filename equality alone is therefore unreliable.

  1. Media files are paired with their sidecar JSON across the WHOLE merged
     inventory, so a sidecar in another archive still pairs. Sidecar naming patterns
     are DISCOVERED from the export, not hardcoded: exact "<file>.json",
     "<file>.supplemental-metadata.json", any truncation of that token, dedup-suffix
     relocation ("IMG_1234.jpg(1).json" vs "IMG_1234(1).jpg"), sidecar `title`
     equality, and longest-unambiguous-prefix for truncated names. The distinct
     patterns actually found are printed. Pairing exists to attach metadata to a
     media file - NOT to decide album membership (Rule 1).
  2. Sidecar-less media files are then folded into a keyed item in the same folder
     per Rule 2, so they never reach the keying stage as separate things.
  3. A logical item key is (normalized title stem, photoTakenTime epoch), where the
     title comes from the sidecar's `title` field - which is the ORIGINAL filename,
     before Takeout truncated or deduped it - and is Unicode-NFC normalized and
     lowercased. This is stable across album/library copies, and a sidecar alone
     supplies both halves of it.
  4. Keying on the stem (no extension) also collapses Live Photo still+video pairs
     that BOTH carry sidecars (IMG_1234.HEIC + IMG_1234.MOV at the same timestamp)
     into ONE logical item, which is what the UI treats them as. Pairs where only
     the still has a sidecar are handled by Rule 2 instead.
  5. Fallbacks, in order, and all of them downgrade confidence:
       - a sidecar-less rendition whose base is absent or ambiguous -> UNCERTAIN
       - any other media file with no sidecar -> UNCERTAIN, own bucket
       - a library-side sidecar with no media in the export -> UNCERTAIN
       - sidecar present but no photoTakenTime -> UNCERTAIN
       - sidecar that is not valid UTF-8 or not parseable -> UNCERTAIN
       - stem AND extension match an album item at a different timestamp, within
         --stem-collision-window -> UNCERTAIN
       - stem matches an album item at a DIFFERENT extension, within the tighter
         --stem-collision-window-ext-mismatch -> UNCERTAIN
       - stem matches an album item whose photoTakenTime is missing, so the
         collision cannot be time-bounded at all -> UNCERTAIN
       - stem is a prefix of (or prefixed by) an album stem, i.e. plausible
         truncation collision -> UNCERTAIN
     Only an item with a strong key, no name collision, and no prefix collision is
     reported as an orphan. A same-stem album item that is OUTSIDE the window (and,
     for a mismatched extension, outside the tighter window) is filename recycling
     rather than a collision, and no longer withholds the item.
  6. "Album" in steps 1-5 means REAL album. Folders you declared generated confer
     no membership and are excluded from the collision guards too, which is exactly
     why declaring one is the dangerous direction and requires an explicit flag.
"""

import argparse
import bisect
import contextlib
import csv
import fnmatch
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

TOOL_VERSION = "6.0.0"

# ---------------------------------------------------------------------------
# Folder classification
# ---------------------------------------------------------------------------

# Chronological "library" folders. Covers "Photos from 2024" and the older
# date-named variants. Override with --library-dir-regex if your locale differs.
DEFAULT_LIBRARY_DIR_RE = r"^(photos\s+from\s+\d{4}|\d{4}(-\d{2}(-\d{2})?)?)$"

# Folders that are not albums and not library: their contents are already deleted
# or unusable, so they are excluded from both sides of the set difference.
EXCLUDED_DIR_NAMES = {
    "trash", "bin", "recently deleted", "papelera", "corbeille", "papierkorb",
    "failed videos", "failed video",
}

# Archive is a library state, not an album. Archived items are usually ALSO present
# in "Photos from YYYY"; an Archive-only item is still not in an album, but users
# rarely want to bulk-delete archived things, so by default these are excluded from
# the orphan list and reported separately. --include-archived flips that.
ARCHIVE_DIR_NAMES = {"archive", "archivo", "archiv"}

NON_MEDIA_EXTS = {
    ".json", ".html", ".htm", ".txt", ".csv", ".ini", ".db", ".pdf", ".xml",
}

# JSON files at the top level or inside album folders that describe the album /
# account rather than a media item.
KNOWN_NON_SIDECAR_JSON = {
    "metadata.json", "album metadata.json", "print-subscriptions.json",
    "shared_album_comments.json", "user-generated-memory-titles.json",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tif",
              ".tiff", ".dng", ".cr2", ".nef", ".arw", ".raf", ".orf", ".rw2"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".mpg",
              ".mpeg", ".webm"}

# Tokens a sidecar may insert before ".json"; we accept any prefix of these,
# because Takeout truncates them to fit filesystem name limits.
SIDECAR_TOKENS = ("supplemental-metadata", "supplementary-metadata", "suplemento-metadatos")

DEDUP_RE = re.compile(r"\((\d+)\)$")

# Google Photos "save a copy" photo-edit outputs, in the locales Google ships.
# A PHOTO edit produces one of these with NO sidecar, and it is a second rendition
# of the same item rather than an item of its own (video edits make a real new item
# with its own sidecar, so they never look like this).
EDITED_SUFFIX_RE = re.compile(
    r"[-_ ](?:edited|edit|bearbeitet|editado|editada|modifie|modifié|modificato|"
    r"bewerkt|redigerad|muokattu|edytowane|editat|duzenlendi|düzenlendi|"
    r"изменено|отредактировано|編集済み|已编辑|수정됨)$", re.I)

# The two shapes Rule 2 accepts when folding a sidecar-less file into a keyed item.
FOLD_EDIT_RENDITION = "edit-rendition"      # <stem>-edited.<ext> -> <stem>.<ext>
FOLD_LIVE_MOTION = "live-photo-motion"      # <stem>.mp4 -> <stem>.heic (still+motion)
FOLD_KINDS = (FOLD_EDIT_RENDITION, FOLD_LIVE_MOTION)

# Fields that would give us a per-item permalink or stable id for later automation.
PERMALINK_KEYS = ("url", "id", "photoId", "mediaItemId", "productUrl", "sourceUrl",
                  "shareUrl", "permalink")
# Looks relevant but is not: it only records how the item was uploaded.
NOT_A_PERMALINK_KEYS = ("googlePhotosOrigin",)
# The sidecar field carried onto every output row.
ITEM_URL_KEY = "url"

# Suffixes Google puts on media IT generated from your photos. Unlike album names,
# this IS a reliable marker - but of an ITEM, not of an album. Used only to
# SUGGEST that a folder full of such items might be a generated collection.
GENERATED_MEDIA_RE = re.compile(
    r"(?:^|[-_ ])(?:ANIMATION|COLLAGE|EFFECTS|SMILE|MIX|PANO|BOUNCE|MOTION|"
    r"MOVIE|CINEMATIC|STYLIZED)$", re.I)

# Album-metadata fields worth testing as provenance discriminators in the survey.
# Presence/value of these is reported; none of them is documented to mean
# "generated", which is the whole point of surveying instead of assuming.
SURVEY_FIELDS = ("access", "description", "date", "geoData", "enrichments",
                 "sharedAlbumComments", "location", "albumType", "type", "source")

# Names Google is known to use for collections it creates. SUGGESTION ONLY - never
# applied, never on by default. Deliberately excludes anything person-name-shaped.
KNOWN_GENERATED_NAME_HINTS = {
    "spotlight", "memories", "recent highlights", "highlights", "trips",
    "animations", "collages", "movies", "stylized photos", "creations",
    "auto awesome", "rediscover this day",
}

# The folder inside a Takeout tree that holds the photo library. Everything above
# it (the "Takeout/" or "Takeout-2/" wrapper, the archive name) is prefix we strip
# so that the same logical path from any archive or directory compares equal.
PHOTOS_ROOT_PREFIX = "google photos"

# General purpose bit 11: entry name (and comment) are UTF-8. See APPNOTE 4.4.4.
ZIP_EFS_FLAG = 0x800

# takeout-20261023T213506Z-1-001.zip
ZIP_PART_RE = re.compile(
    r"^(?P<stem>.*?)-?(?P<ts>\d{8}T\d{6}Z)-(?P<group>\d+)-(?P<part>\d+)\.zip$", re.I)

# Why an item was held back instead of being called an orphan. Each is its own
# bucket in the summary, so a population like the sidecar-less leftovers can be
# adjudicated on its own instead of hiding inside a bigger number.
REASON_RENDITION_NO_BASE = (
    "sidecar-less file that looks like a rendition, but no single sidecar-keyed "
    "base item with the same stem exists in its folder")
REASON_MEDIA_NO_SIDECAR = (
    "media file with no sidecar anywhere in the export - cannot key or address")
REASON_LIB_SIDECAR_ONLY = (
    "sidecar in a library/Archive folder with no media file anywhere in the export")
REASON_NO_TAKEN_TIME = "sidecar has no photoTakenTime; cannot key reliably"
# 4.x's same-stem guard. Kept verbatim because the whole classification is replayed
# with it on every run as a cross-check; it is never used to classify a reported row.
REASON_STEM_DIFF_TS = (
    "same filename stem exists in an album but with a different timestamp")
# The three buckets 6.0.0 splits that one guard into.
REASON_STEM_NEAR_TS = (
    "same filename stem AND extension exists in an album at a different timestamp, "
    "within --stem-collision-window")
REASON_STEM_NEAR_TS_EXT = (
    "same filename stem exists in an album at a DIFFERENT extension, within the "
    "tighter --stem-collision-window-ext-mismatch (possible Takeout transcode)")
REASON_STEM_ALBUM_NO_TS = (
    "same filename stem exists in an album item that has no photoTakenTime, so the "
    "collision cannot be time-bounded")
REASON_PREFIX_COLLISION = (
    "filename stem is a prefix of / prefixed by an album item's stem "
    "(possible Takeout truncation collision)")

# Buckets whose items have no sidecar at all, so they have no photoTakenTime to
# key on and no url to navigate to.
UNADDRESSABLE_REASONS = (REASON_RENDITION_NO_BASE, REASON_MEDIA_NO_SIDECAR)

# |delta photoTakenTime| buckets for the same-stem collision survey. Integer epoch
# seconds mean "<1s" is exactly delta == 0, i.e. an identical timestamp - which is
# an exact key match and already counted as filed, never a guard case.
COLLISION_BUCKETS = (
    ("<1s", 1), ("<1min", 60), ("<1h", 3600), ("<24h", 86400),
    ("<30d", 30 * 86400), ("<1y", 365 * 86400), (">=1y", None),
)
# How the two sides' extension evidence compares.
EXT_MATCH, EXT_MISMATCH, EXT_UNKNOWN = "ext_match", "ext_mismatch", "ext_unknown"
EXT_CLASSES = (EXT_MATCH, EXT_MISMATCH, EXT_UNKNOWN)

DURATION_UNITS = {"": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
                  "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                  "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                  "d": 86400, "day": 86400, "days": 86400,
                  "w": 7 * 86400, "week": 7 * 86400, "weeks": 7 * 86400}
DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$", re.I)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def nfc(s):
    """Canonical composition.

    Applied to EVERY name the script handles, whatever its origin: zip entry
    paths, on-disk directory and file names (macOS hands those back as NFD), and
    sidecar `title` values. Without a single normal form, the same album read from
    a zip and from an unzipped copy would compare unequal.
    """
    return unicodedata.normalize("NFC", s)


def norm_folder(name):
    """Folder name -> canonical form used for --generated-album matching."""
    return nfc(name).strip().casefold()


def split_dedup(stem):
    """'IMG_1234(1)' -> ('IMG_1234', 1); 'IMG_1234' -> ('IMG_1234', None)."""
    m = DEDUP_RE.search(stem)
    if m:
        return stem[: m.start()], int(m.group(1))
    return stem, None


def norm_name(name):
    """Filename -> (normalized stem, lowercase ext, dedup index)."""
    name = nfc(name)
    stem, ext = os.path.splitext(name)
    stem, dup = split_dedup(stem)
    return stem.strip().lower(), ext.lower(), dup


def is_media_filename(name):
    if name.startswith("."):
        return False
    ext = os.path.splitext(name)[1].lower()
    return ext not in NON_MEDIA_EXTS


def is_json_filename(name):
    return name.lower().endswith(".json")


def is_generated_media_name(name):
    stem = os.path.splitext(nfc(name))[0]
    stem, _dup = split_dedup(stem)
    return bool(GENERATED_MEDIA_RE.search(stem.strip()))


def strip_rendition_suffix(stem):
    """'img_1-edited' -> ('img_1', True); 'img_1' -> ('img_1', False).

    `stem` is a normalized filename stem (see norm_name). The boolean says whether
    a known edit-rendition suffix was removed, which decides which of Rule 2's two
    shapes may apply to the file.
    """
    m = EDITED_SUFFIX_RE.search(stem)
    if m:
        return stem[: m.start()].strip(), True
    return stem, False


def banner(lines, indent="  "):
    """Print >>> ... <<< lines padded to a common width (counts vary in length)."""
    width = max(len(l) for l in lines)
    for l in lines:
        print("%s>>> %s <<<" % (indent, l.ljust(width)))


def iso(ts):
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def day(ts):
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def parse_duration(text):
    """'24h' / '90s' / '0' / '1800' -> seconds as an int. Raises ValueError."""
    m = DURATION_RE.match(text or "")
    if not m:
        raise ValueError("expected a number with an optional unit, e.g. 24h, 90s, 0")
    unit = m.group(2).lower()
    if unit not in DURATION_UNITS:
        raise ValueError("unknown unit %r; use s, m, h, d or w" % m.group(2))
    return int(round(float(m.group(1)) * DURATION_UNITS[unit]))


def human_duration(secs):
    """Seconds -> the shortest exact unit, for echoing a parsed flag back."""
    for unit, size in (("w", 7 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs and secs % size == 0:
            return "%d%s (%ds)" % (secs // size, unit, secs)
    return "%ds" % secs


def approx_duration(secs):
    """Seconds -> a short, readable approximation, for deltas in example rows."""
    for unit, size in (("y", 365 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%.1f%s" % (secs / float(size), unit)
    return "%ds" % secs


def delta_bucket(delta):
    """|seconds| -> the label of its COLLISION_BUCKETS bucket."""
    for label, upper in COLLISION_BUCKETS:
        if upper is None or delta < upper:
            return label
    return COLLISION_BUCKETS[-1][0]


def human_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f TiB" % n


def extract_url(data):
    """The item's own permalink from its sidecar, if the export carries one."""
    if not isinstance(data, dict):
        return None
    v = data.get(ITEM_URL_KEY)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def extract_ts(data):
    if not isinstance(data, dict):
        return None
    node = data.get("photoTakenTime")
    if isinstance(node, dict):
        v = node.get("timestamp")
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    return None


def sidecar_title(data):
    if isinstance(data, dict):
        t = data.get("title")
        if isinstance(t, str) and t.strip():
            return nfc(t.strip())
    return None


# ---------------------------------------------------------------------------
# Zip entry name decoding
# ---------------------------------------------------------------------------

# Status values, in order of trustworthiness. Only the first two are safe.
ENC_UTF8_FLAGGED = "utf8_flagged"      # bit 11 set; zipfile decoded UTF-8 for us
ENC_UTF8_RECOVERED = "utf8_recovered"  # bit 11 clear, but bytes really were UTF-8
ENC_CP437_LITERAL = "cp437_literal"    # bit 11 clear and not UTF-8; a guess
ENC_UNDECODABLE = "undecodable"        # cannot even recover the original bytes

ENC_STATUSES = (ENC_UTF8_FLAGGED, ENC_UTF8_RECOVERED, ENC_CP437_LITERAL,
                ENC_UNDECODABLE)


def decode_zip_entry_name(zi):
    """Decode one zip entry name honestly. -> (unnormalized_name, status).

    The name is returned WITHOUT Unicode normalization. Callers normalize it to
    NFC for identity, but keep this raw form too: two entries whose NFC forms are
    equal while their stored forms differ are a genuine merge of two distinct
    files, and the only way to notice is to compare the pre-normalization text.

    zipfile has already decoded the stored bytes for us, but it had to guess: if
    general purpose bit 11 is set it used UTF-8 (correct), and otherwise it used
    CP437 (per the spec's legacy default), which mangles archives that store UTF-8
    without setting the flag - and Takeout cannot be assumed to set it. So for
    unflagged entries we reverse zipfile's CP437 decode to get the original bytes
    back, then try UTF-8 on those.

    Nothing here uses errors="replace". A lossy decode can map two different album
    folders onto one name, which would silently merge two albums and corrupt the
    delta; an honest status plus a loud warning is the only safe behaviour.
    """
    # orig_filename is pre-separator-rewrite, so it round-trips to the stored bytes.
    decoded = getattr(zi, "orig_filename", None) or zi.filename

    if zi.flag_bits & ZIP_EFS_FLAG:
        return decoded.replace("\\", "/"), ENC_UTF8_FLAGGED

    try:
        raw = decoded.encode("cp437")
    except UnicodeEncodeError:
        # zipfile did not actually decode this as cp437 (it can fall back to UTF-8
        # internally), so we cannot recover the stored bytes. Keep what we have.
        return decoded.replace("\\", "/"), ENC_UNDECODABLE

    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Genuinely not UTF-8. The CP437 reading stands, but it is a guess about an
        # unspecified legacy encoding, so the caller must warn.
        return decoded.replace("\\", "/"), ENC_CP437_LITERAL
    return name.replace("\\", "/"), ENC_UTF8_RECOVERED


# ---------------------------------------------------------------------------
# Sources: a directory tree or a .zip archive
# ---------------------------------------------------------------------------

class Entry(object):
    """One file in the merged inventory.

    `parts` is the path INSIDE the photo library, as NFC components, with the
    archive-local prefix already stripped - that is what makes an entry from
    part 001 and an entry from part 003 land in the same logical folder.
    """

    __slots__ = ("parts", "source", "size", "enc_status", "unnorm", "locator")

    def __init__(self, parts, source, size, enc_status, unnorm, locator):
        self.parts = parts
        self.source = source
        self.size = size
        self.enc_status = enc_status
        # The same path BEFORE NFC normalization. Equal `parts` with different
        # `unnorm` inside one source means two distinct files were merged.
        self.unnorm = unnorm
        self.locator = locator      # zip: ZipInfo; dir: absolute path

    @property
    def folder(self):
        return "/".join(self.parts[:-1])

    @property
    def name(self):
        return self.parts[-1]

    @property
    def relpath(self):
        return "/".join(self.parts)


class SourceBase(object):
    def __init__(self, path):
        self.path = path
        self.label = os.path.basename(path.rstrip(os.sep)) or path
        self.kind = "?"
        self.entries = 0
        self.media = 0
        self.jsons = 0
        self.other = 0
        self.skipped_outside = 0
        self.enc_counts = Counter()
        self.bytes_read = 0
        self.total_uncompressed = 0
        self.dup_skipped = 0
        self.warnings = []

    def close(self):
        pass


def strip_photos_prefix(parts):
    """Drop everything up to and including the "Google Photos" component.

    Returns None for entries that are not under a photo-library root (other
    Takeout products, the wrapper directory itself, archive_browser.html at the
    very top, ...). Keeping the suffix only is what lets "Takeout/Google Photos/X"
    from one archive and "Takeout-2/Google Photos/X" from another be recognised as
    the same logical folder X.
    """
    for i, p in enumerate(parts):
        if p.strip().lower().startswith(PHOTOS_ROOT_PREFIX):
            tail = parts[i + 1:]
            return tail or None
    return None


class ZipSource(SourceBase):
    """Reads a Takeout .zip without extracting it.

    Entries come from the central directory, which gives every media filename for
    free. Only .json entries are ever decompressed - `read_json` refuses anything
    else, so the 250GB of media cannot be touched even by accident.
    """

    def __init__(self, path):
        SourceBase.__init__(self, path)
        self.kind = "zip"
        self.label = os.path.basename(path)
        try:
            self.zf = zipfile.ZipFile(path, "r")        # read-only, never written
        except (zipfile.BadZipFile, OSError) as exc:
            raise SourceError("cannot open zip %s: %s" % (path, exc))
        try:
            self.archive_bytes = os.path.getsize(path)
        except OSError:
            self.archive_bytes = 0
        self.part_info = parse_zip_part(self.label)

    def close(self):
        try:
            self.zf.close()
        except Exception:
            pass

    def iter_entries(self):
        for zi in self.zf.infolist():
            name, status = decode_zip_entry_name(zi)
            self.enc_counts[status] += 1
            if name.endswith("/") or zi.is_dir():
                continue
            self.entries += 1
            self.total_uncompressed += zi.file_size
            raw_parts = tuple(p for p in name.split("/") if p not in ("", ".", ".."))
            if not raw_parts:
                continue
            tail = strip_photos_prefix(raw_parts)
            if tail is None:
                self.skipped_outside += 1
                continue
            if status in (ENC_CP437_LITERAL, ENC_UNDECODABLE):
                self.warnings.append(
                    "%s: entry name is not valid UTF-8 (%s), so this path may be "
                    "misspelled: %r" % (self.label, status, name))
            yield Entry(tuple(nfc(p) for p in tail), self, zi.file_size, status,
                        "/".join(tail), zi)

    def read_json(self, entry):
        if not is_json_filename(entry.name):
            raise AssertionError(
                "refusing to decompress a non-JSON entry: %s" % entry.relpath)
        with self.zf.open(entry.locator, "r") as fh:
            data = fh.read()
        self.bytes_read += len(data)
        return data


class DirSource(SourceBase):
    """Reads an already-unzipped tree (the original input mode)."""

    def __init__(self, path, roots):
        SourceBase.__init__(self, path)
        self.kind = "dir"
        self.roots = roots

    def iter_entries(self):
        for root in self.roots:
            root_parent = os.path.dirname(os.path.abspath(root))
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                rel = os.path.relpath(dirpath, root_parent)
                # Kept un-normalized here; macOS hands back NFD, and the NFC form
                # is derived below so both modes agree on identity while a real
                # composition difference is still detectable.
                base_parts = tuple(
                    p for p in rel.split(os.sep) if p not in ("", ".", ".."))
                tail_base = strip_photos_prefix(base_parts)
                for fn in sorted(filenames):
                    self.entries += 1
                    full = os.path.join(dirpath, fn)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    self.total_uncompressed += size
                    if tail_base is None:
                        # Sits directly in "Google Photos/" (archive_browser.html,
                        # print-subscriptions.json) rather than in a folder, so it
                        # belongs to no album and no library month.
                        self.skipped_outside += 1
                        continue
                    tail = tail_base + (fn,)
                    self.enc_counts["filesystem"] += 1
                    yield Entry(tuple(nfc(p) for p in tail), self, size,
                                "filesystem", "/".join(tail), full)

    def read_json(self, entry):
        if not is_json_filename(entry.name):
            raise AssertionError(
                "refusing to read a non-JSON file: %s" % entry.relpath)
        with open(entry.locator, "rb") as fh:
            data = fh.read()
        self.bytes_read += len(data)
        return data


class SourceError(Exception):
    pass


def parse_zip_part(filename):
    """-> dict(ts, group, part) or None, from takeout-<ts>-<group>-<part>.zip."""
    m = ZIP_PART_RE.match(filename)
    if not m:
        return None
    return {"timestamp": m.group("ts"),
            "group": int(m.group("group")),
            "part": int(m.group("part")),
            "stem": m.group("stem")}


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------

def find_photos_roots_in_dir(path, max_depth=3):
    """Every "Google Photos" directory at or under `path` (shallow search)."""
    roots = []
    if os.path.basename(path.rstrip(os.sep)).lower().startswith(PHOTOS_ROOT_PREFIX):
        return [path]
    stack = [(path, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            if e.name.lower().startswith(PHOTOS_ROOT_PREFIX):
                roots.append(e.path)
            elif depth < max_depth:
                stack.append((e.path, depth + 1))
    return sorted(set(roots))


def resolve_inputs(raw_paths):
    """Expand the command line into concrete zip files and library directories.

    Accepts: a .zip, several .zips, a glob the shell did not expand, a directory
    holding .zips, an unzipped tree, or any mix. Returns (zip_paths, dir_paths).
    """
    zips, dirs, problems = [], [], []
    for raw in raw_paths:
        expanded = [raw]
        if any(ch in raw for ch in "*?["):
            # The shell usually does this, but quote it or run on a shell that
            # does not and we still behave.
            pattern = os.path.abspath(os.path.expanduser(raw))
            base, pat = os.path.dirname(pattern), os.path.basename(pattern)
            try:
                names = sorted(os.listdir(base))
            except OSError:
                names = []
            expanded = [os.path.join(base, n) for n in names
                        if fnmatch.fnmatch(n, pat)]
            if not expanded:
                problems.append("glob matched nothing: %s" % raw)
                continue
        for p in expanded:
            p = os.path.abspath(os.path.expanduser(p))
            if os.path.isfile(p):
                if p.lower().endswith(".zip"):
                    zips.append(p)
                else:
                    problems.append("not a .zip or directory: %s" % p)
            elif os.path.isdir(p):
                roots = find_photos_roots_in_dir(p)
                found_zips = []
                try:
                    found_zips = sorted(
                        os.path.join(p, n) for n in os.listdir(p)
                        if n.lower().endswith(".zip")
                        and os.path.isfile(os.path.join(p, n)))
                except OSError:
                    pass
                if roots:
                    dirs.append((p, roots))
                if found_zips:
                    zips.extend(found_zips)
                if not roots and not found_zips:
                    problems.append(
                        "no 'Google Photos' directory and no .zip files under: %s" % p)
            else:
                problems.append("does not exist: %s" % p)
    # Stable, de-duplicated order.
    seen = set()
    zips = [z for z in zips if not (z in seen or seen.add(z))]
    seen = set()
    dirs = [d for d in dirs if not (d[0] in seen or seen.add(d[0]))]
    return zips, dirs, problems


# ---------------------------------------------------------------------------
# The ONE GLOBAL inventory
# ---------------------------------------------------------------------------

def build_inventory(sources):
    """Merge every source into a single path-keyed inventory, then group by folder.

    This function is the whole reason zip mode is correct. Takeout splits one
    export across archives at arbitrary points, including mid-folder, so a media
    file in part 001 may have its sidecar in part 003 and an album's contents may
    straddle several archives. Everything downstream - classification, rendition
    folding, sidecar pairing, the set difference - runs on this merged view, never
    per-archive.

    De-duplication: if the same library-relative path arrives from more than one
    source (you passed a zip and an unzipped copy of it), the first source wins and
    the rest are counted as duplicates, so content is never double-counted.
    Directories are offered first, since an extracted copy needs no decompression.

    Returns (folders, info) where folders maps folder path -> {name: Entry}.
    """
    by_path = {}                       # relpath -> Entry
    unnorm_seen = defaultdict(set)     # (source label, relpath) -> {unnorm paths}
    dup_sources = Counter()            # (winner label, loser label) -> count
    warnings = []

    for src in sources:
        for entry in src.iter_entries():
            rel = entry.relpath
            # Scoped to one source on purpose: the SAME logical path arriving from
            # two sources is the de-duplication case below, whereas two different
            # stored spellings inside ONE source really are two files collapsing
            # into one - which would silently merge them.
            unnorm_seen[(src.label, rel)].add(entry.unnorm)
            prev = by_path.get(rel)
            if prev is None:
                by_path[rel] = entry
                if is_json_filename(entry.name):
                    src.jsons += 1
                elif is_media_filename(entry.name):
                    src.media += 1
                else:
                    src.other += 1
            else:
                src.dup_skipped += 1
                dup_sources[(prev.source.label, src.label)] += 1

    # A composition difference, or a guessed decode, can land two genuinely
    # different files or album folders on one name. Merging two albums would
    # corrupt the delta, so this is never silent.
    collisions = []
    for (label, rel), spellings in sorted(unnorm_seen.items()):
        if len(spellings) > 1:
            collisions.append((label, rel, sorted(spellings)))
    for label, rel, spellings in collisions:
        warnings.append(
            "NAME COLLISION in %s: %d differently-stored paths collapse to %r "
            "after decode+NFC %s - two distinct items or album folders have been "
            "merged, so counts for them are wrong. Inspect the archive before "
            "deleting anything."
            % (label, len(spellings), rel,
               [s.encode("unicode_escape").decode("ascii") for s in spellings]))

    folders = defaultdict(dict)
    for rel, entry in by_path.items():
        folders[entry.folder][entry.name] = entry

    info = {
        "total_paths": len(by_path),
        "duplicate_paths_skipped": sum(dup_sources.values()),
        "duplicate_source_pairs": {"%s <- %s" % (w, l): n
                                   for (w, l), n in sorted(dup_sources.items())},
        "name_collisions": [{"source": label, "path": rel,
                             "stored_spellings": spellings}
                            for label, rel, spellings in collisions],
        "warnings": warnings,
    }
    return folders, info


def check_archive_set(zip_sources):
    """Warn about a part set that looks wrong. Never refuses - only the user knows
    how many parts the export was supposed to have."""
    warnings = []
    parsed = [(s, s.part_info) for s in zip_sources]
    unparsed = [s.label for s, p in parsed if not p]
    if unparsed:
        warnings.append(
            "could not parse a Takeout part number from %d archive name(s): %s. "
            "Part contiguity could not be checked for them - make sure you passed "
            "EVERY part of the export in this one run."
            % (len(unparsed), ", ".join(sorted(unparsed))))

    known = [(s, p) for s, p in parsed if p]
    stamps = sorted({p["timestamp"] for _s, p in known})
    if len(stamps) > 1:
        warnings.append(
            "MIXED EXPORTS: these archives carry %d different export timestamps "
            "(%s). Archives from different Takeout runs describe different library "
            "states; merging them WILL produce a wrong delta. Keep one export's "
            "parts only." % (len(stamps), ", ".join(stamps)))

    by_group = defaultdict(list)
    for s, p in known:
        by_group[(p["timestamp"], p["group"])].append(p["part"])
    for (ts, group), nums in sorted(by_group.items()):
        nums_sorted = sorted(nums)
        dupes = [n for n, c in Counter(nums).items() if c > 1]
        if dupes:
            warnings.append(
                "export %s group %d: part number(s) %s appear more than once"
                % (ts, group, sorted(dupes)))
        expected = list(range(1, max(nums_sorted) + 1))
        missing = [n for n in expected if n not in set(nums_sorted)]
        if missing:
            warnings.append(
                "NON-CONTIGUOUS PART SET for export %s group %d: found %s but "
                "%s missing. Every photo that lived in a missing part is absent "
                "from this run, and anything whose sidecar lived there will be "
                "reported UNCERTAIN rather than classified. Pass the missing "
                "archive(s) too."
                % (ts, group, nums_sorted, missing))
        if min(nums_sorted) != 1:
            warnings.append(
                "export %s group %d: lowest part number is %d, not 1 - part 1 "
                "looks absent" % (ts, group, min(nums_sorted)))
    return warnings


# ---------------------------------------------------------------------------
# Sidecar loading / pairing / rendition folding
# ---------------------------------------------------------------------------

def load_sidecar(entry):
    """Read and parse one JSON entry. -> (data, error_or_None).

    Content is decoded STRICTLY as UTF-8 (after an optional BOM). A sidecar whose
    bytes are not valid UTF-8 is reported rather than patched up with
    errors="replace": its `title` is the identity key for the item, and a
    silently mangled title produces a wrong key, which produces a false orphan.
    """
    try:
        data = entry.source.read_json(entry)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return None, "unreadable (%s: %s)" % (type(exc).__name__, exc)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, "content is not valid UTF-8 (%s)" % exc
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, "unparseable JSON (%s)" % exc


def looks_like_sidecar(data):
    if not isinstance(data, dict):
        return False
    if "photoTakenTime" in data:
        return True
    if "title" in data and ("creationTime" in data or "googlePhotosOrigin" in data
                            or "imageViews" in data):
        return True
    return False


def unwrap_album_metadata(data):
    """Older exports nest the album fields under 'albumData'."""
    if isinstance(data, dict) and isinstance(data.get("albumData"), dict):
        return data["albumData"], True
    return data, False


def strip_sidecar_token(stem):
    """'IMG_1234.JPG.supplemental-me' -> ('IMG_1234.JPG', True).

    `stem` is the json filename with '.json' and any trailing '(N)' removed.
    Returns (media_name_candidate, token_was_stripped).
    """
    idx = stem.rfind(".")
    if idx <= 0:
        return stem, False
    token = stem[idx + 1:].lower()
    if not token:
        return stem, False
    for full in SIDECAR_TOKENS:
        # accept any non-trivial prefix of the token, Takeout truncates it
        if len(token) >= 3 and full.startswith(token):
            return stem[:idx], True
    return stem, False


def fold_renditions(folder_label, pairs, leftover_media, errors):
    """RULE 2: fold each sidecar-less media file into a keyed item in this folder.

    `pairs` are the (media_entry, json_name, data, method) tuples that DID pair, so
    their stems are sidecar-keyed. For every sidecar-less file we strip a known
    rendition suffix and look for exactly one keyed base in the SAME folder:

      (a) suffix stripped + base has the SAME extension  -> edit rendition
          IMG_1.jpg + IMG_1-edited.jpg are one item
      (b) no suffix + this file is a VIDEO + base is a STILL IMAGE -> motion half
          IMG_5566.HEIC + IMG_5566.MP4 are one Live Photo

    Zero or multiple candidates means we do not know, so the file is left for the
    UNCERTAIN path rather than attached to a guess. Returns
    (folded, still_unpaired) where folded is a list of
    (media_entry, base_media_name, base_json_name, base_data, fold_kind).
    """
    keyed = defaultdict(list)          # normalized base stem -> [(name, ext, jn, data)]
    for me, jn, data, _method in pairs:
        s, e, _d = norm_name(me.name)
        keyed[s].append((me.name, e, jn, data))

    folded, still = [], []
    for entry in leftover_media:
        s, e, _d = norm_name(entry.name)
        base_stem, stripped = strip_rendition_suffix(s)
        cands = keyed.get(base_stem, ())
        if stripped:
            shape = FOLD_EDIT_RENDITION
            fits = [c for c in cands if c[1] == e]
        elif e in VIDEO_EXTS:
            shape = FOLD_LIVE_MOTION
            fits = [c for c in cands if c[1] in IMAGE_EXTS]
        else:
            shape, fits = None, []
        # A rendition can never be its own base.
        fits = [c for c in fits if c[0] != entry.name]
        if shape is not None and len(fits) == 1:
            base_name, _be, base_jn, base_data = fits[0]
            folded.append((entry, base_name, base_jn, base_data, shape))
            continue
        if shape is not None and len(fits) > 1:
            errors.append(
                "%s/%s looks like a %s of %r but %d sidecar-keyed files in that "
                "folder could be its base, so it was left UNCERTAIN instead of "
                "being attached to a guess"
                % (folder_label, entry.name, shape, base_stem, len(fits)))
        still.append((entry, shape is not None))
    return folded, still


def pair_folder(folder_label, entries, fold_enabled=True):
    """Resolve ONE LOGICAL FOLDER of the merged inventory (possibly assembled from
    several archives) into pairs, folded renditions, leftovers and album metadata.

    Returns a dict; see the keys built at the end. Note that `sidecar_only` is NOT
    an error condition - for an album folder it is the normal Takeout shape and it
    is what proves membership (Rule 1).
    """
    errors = []
    media_names = sorted(n for n in entries if is_media_filename(n))
    json_names = sorted(n for n in entries if is_json_filename(n))

    sidecars = {}         # json name -> data
    album_metadata = None
    for jn in json_names:
        data, err = load_sidecar(entries[jn])
        if err is not None:
            errors.append("sidecar %s/%s: %s" % (folder_label, jn, err))
            continue
        if jn.lower() in KNOWN_NON_SIDECAR_JSON or not looks_like_sidecar(data):
            if jn.lower() in ("metadata.json", "album metadata.json"):
                album_metadata = data
            continue
        sidecars[jn] = data

    # Precompute, for each sidecar, the media-name candidate it points at.
    cand = {}     # json name -> (media_candidate, dup_index, token_stripped)
    for jn in sidecars:
        base = jn[: -len(".json")]
        base, dup = split_dedup(base)
        media_cand, stripped = strip_sidecar_token(base)
        cand[jn] = (media_cand, dup, stripped)

    pairs = []
    used_json = set()
    remaining = list(media_names)

    def pattern_for(media_name, json_name):
        """Human-readable discovered pattern, e.g. '{FILE}.supplemental-metadata.json'."""
        base = json_name[: -len(".json")]
        base, dup = split_dedup(base)
        core, stripped = strip_sidecar_token(base)
        if core.lower() == media_name.lower():
            shape = "{FILE}"
        elif norm_name(core)[:2] == norm_name(media_name)[:2]:
            shape = "{FILE-WITHOUT-DEDUP-SUFFIX}"
        elif media_name.lower().startswith(core.lower()):
            shape = "{FILE-TRUNCATED-TO-%d-CHARS}" % len(core)
        else:
            shape = "{FILE-MATCHED-VIA-SIDECAR-TITLE}"
        if stripped:
            token = base[len(core) + 1:]
            shape += ".%s" % (token if token.lower() in SIDECAR_TOKENS
                              else "{TRUNCATED-TOKEN:%s}" % token)
        if dup is not None:
            shape += "({N})"
        return shape + ".json"

    def take(media_name, json_name, method):
        used_json.add(json_name)
        pairs.append((entries[media_name], json_name, sidecars[json_name], method))

    # Pass 1: exact "<media>.json" / "<media>.<token>.json", same dedup index.
    by_key = defaultdict(list)
    for jn, (mc, dup, _st) in cand.items():
        s, e, d = norm_name(mc)
        by_key[(s, e, dup if dup is not None else d)].append(jn)
    still = []
    for mn in remaining:
        s, e, d = norm_name(mn)
        cands = [j for j in by_key.get((s, e, d), []) if j not in used_json]
        if len(cands) == 1:
            take(mn, cands[0], "name+dedup")
        else:
            still.append(mn)
    remaining = still

    # Pass 2: ignore the dedup index entirely (Takeout sometimes drops or moves it).
    by_key2 = defaultdict(list)
    for jn, (mc, _dup, _st) in cand.items():
        s, e, _d = norm_name(mc)
        by_key2[(s, e)].append(jn)
    still = []
    for mn in remaining:
        s, e, _d = norm_name(mn)
        cands = [j for j in by_key2.get((s, e), []) if j not in used_json]
        if len(cands) == 1:
            take(mn, cands[0], "name-ignoring-dedup")
        else:
            still.append(mn)
    remaining = still

    # Pass 3: sidecar `title` equality (title is the pre-mangling original name).
    by_title = defaultdict(list)
    for jn, data in sidecars.items():
        t = data.get("title")
        if isinstance(t, str) and t:
            s, e, _d = norm_name(t)
            by_title[(s, e)].append(jn)
    still = []
    for mn in remaining:
        s, e, _d = norm_name(mn)
        cands = [j for j in by_title.get((s, e), []) if j not in used_json]
        if len(cands) == 1:
            take(mn, cands[0], "sidecar-title")
        else:
            still.append(mn)
    remaining = still

    # Pass 4: truncated sidecar name is a prefix of the media name. Longest
    # unambiguous wins; ties are left unpaired rather than guessed.
    free = [(cand[j][0].lower(), j) for j in sidecars if j not in used_json]
    still = []
    for mn in remaining:
        low = mn.lower()
        hits = [(len(p), j) for p, j in free if len(p) >= 6 and low.startswith(p)
                and j not in used_json]
        if hits:
            hits.sort(reverse=True)
            best_len = hits[0][0]
            top = [j for ln, j in hits if ln == best_len]
            if len(top) == 1:
                take(mn, top[0], "truncated-prefix")
                continue
            errors.append("ambiguous sidecar prefix match for %s/%s (%d candidates)"
                          % (folder_label, mn, len(top)))
        still.append(mn)
    remaining = still

    # RULE 2: the files left over are sidecar-less. Most are extra renditions of an
    # item that IS keyed here, so fold them in rather than inventing items.
    leftover_media = [entries[mn] for mn in remaining]
    if fold_enabled:
        folded, unpaired = fold_renditions(folder_label, pairs, leftover_media, errors)
    else:
        folded = []
        unpaired = [(entry, False) for entry in leftover_media]

    # RULE 1: sidecars that matched no media file. In an album folder each one is a
    # membership proof; in a library folder it is an item we never saw the file for.
    leftover_json = sorted(j for j in sidecars if j not in used_json)

    patterns = Counter(pattern_for(me.name, jn) for me, jn, _d, _m in pairs)
    return {
        "pairs": pairs,
        "folded": folded,
        "unpaired": unpaired,
        "sidecar_only": [(jn, sidecars[jn]) for jn in leftover_json],
        "album_metadata": album_metadata,
        "patterns": patterns,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

class FileRec(object):
    """One export file (or one sidecar-only membership proof) attached to an item."""

    __slots__ = ("relpath", "name", "scope", "folder", "sidecar_name", "data",
                 "method", "stem", "ext", "title_ext", "ts", "title", "url",
                 "name_may_be_truncated", "source", "sidecar_only", "no_sidecar",
                 "rendition_shaped", "rendition_of", "fold_kind")

    def __init__(self, relpath, name, scope, folder, sidecar_name, data, method,
                 source, sidecar_only=False, no_sidecar=False,
                 rendition_shaped=False, rendition_of=None, fold_kind=None,
                 ext_override=None):
        self.relpath = relpath
        self.name = name
        self.scope = scope          # library | album | generated | archive
        self.folder = folder
        self.sidecar_name = sidecar_name
        self.data = data
        self.method = method
        self.source = source
        self.sidecar_only = sidecar_only
        self.no_sidecar = no_sidecar
        self.rendition_shaped = rendition_shaped
        self.rendition_of = rendition_of
        self.fold_kind = fold_kind
        self.title = sidecar_title(data)
        self.ts = extract_ts(data)
        self.url = extract_url(data)
        # The identity key comes from the sidecar `title` (the original filename,
        # before Takeout truncated or deduped it), so a folded rendition and a
        # sidecar-only proof land on exactly the same key as their item.
        self.stem = norm_name(self.title if self.title else name)[0]
        # Extension comes from the actual file, so a Live Photo's .MOV is not
        # mistaken for its .HEIC; a sidecar-only proof contributes none.
        self.ext = ext_override if ext_override is not None else norm_name(name)[1]
        # The extension the sidecar CLAIMS, which is the only extension evidence a
        # sidecar-only membership proof has. Used by the same-stem collision guard,
        # never by `exts` / is_live_pair - a proof still owns no file.
        self.title_ext = norm_name(self.title)[1] if self.title else ""
        # Only an UNKEYED on-disk name can be a truncation we must guard against.
        # A folded rendition and a sidecar-only proof both have a known title, and
        # their base record already carries the flag if it applies.
        self.name_may_be_truncated = (
            not sidecar_only and fold_kind is None
            and (self.title is None or len(os.path.splitext(name)[0]) >= 30))


SCOPE_BY_KIND = {
    "library": "library",
    "archive": "archive",
    "album": "album",
    "generated_album": "generated",
}


def classify_dir(name, library_re, generated):
    """-> excluded | archive | library | generated_album | album.

    The generated test runs last, so declaring "Trash" or "Photos from 2024"
    generated cannot change how those folders are handled.
    """
    low = nfc(name).strip().lower()
    if low in EXCLUDED_DIR_NAMES:
        return "excluded"
    if low in ARCHIVE_DIR_NAMES:
        return "archive"
    if library_re.match(low):
        return "library"
    if generated.matches(name):
        return "generated_album"
    return "album"


def describe_value(v):
    """Collapse a metadata value to something comparable across albums."""
    if v is None:
        return "null"
    if isinstance(v, str):
        return "<empty string>" if not v.strip() else repr(v) if len(v) <= 40 \
            else "<string len %d>" % len(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return "<number>"
    if isinstance(v, list):
        return "<empty list>" if not v else "<list of %d>" % len(v)
    if isinstance(v, dict):
        return "<empty object>" if not v else "<object keys: %s>" % ",".join(sorted(v))
    return "<%s>" % type(v).__name__


def scan(folders, library_re, generated, fold_enabled=True, verbose=False):
    """Classify folders and resolve their contents over the MERGED inventory."""
    files = []
    classification = []        # (folder, kind, media_count)
    patterns = Counter()
    pair_methods = Counter()
    unpaired = []              # relpaths of sidecar-less media we could not fold
    sidecar_only_album = []    # relpaths: membership proofs (normal)
    sidecar_only_library = []  # relpaths: item whose media we never saw
    folded_counts = Counter()
    folded_examples = []
    errors = []
    albums = {}                # album folder -> info (real AND generated)
    excluded_counts = Counter()
    sidecar_samples = []       # (relpath, data)
    sidecar_key_counts = Counter()
    sidecar_total = 0
    url_present = 0
    split_folders = []         # folders assembled from more than one source

    for folder in sorted(folders):
        entries = folders[folder]
        if not folder:
            continue
        # Classification keys on the FIRST component under "Google Photos"; a
        # nested subfolder (Takeout does not normally make them) is treated as its
        # own album, which is the conservative default since album membership
        # protects items from deletion.
        kind = classify_dir(folder if "/" in folder else folder.split("/")[0],
                            library_re, generated)
        res = pair_folder(folder, entries, fold_enabled=fold_enabled)
        patterns.update(res["patterns"])
        errors.extend(res["errors"])
        pairs, folded = res["pairs"], res["folded"]
        unpaired_here, sconly = res["unpaired"], res["sidecar_only"]
        media_count = len(pairs) + len(folded) + len(unpaired_here)
        srcs = sorted({e.source.label for e in entries.values()})
        if len(srcs) > 1:
            split_folders.append({"folder": folder, "sources": srcs,
                                  "media_files": media_count})
        if verbose:
            print("  [%-15s] %-40s %4d paired, %4d folded, %4d unpaired, "
                  "%4d sidecar-only  %s"
                  % (kind, folder[:40], len(pairs), len(folded),
                     len(unpaired_here), len(sconly), "+".join(srcs)))
            for me, bn, _jn, _d, fk in folded:
                print("        FOLD %-34s -> %-34s (%s)"
                      % (me.name[:34], bn[:34], fk))

        if kind == "excluded":
            excluded_counts[folder] += media_count
            classification.append((folder, kind, media_count))
            continue

        scope = SCOPE_BY_KIND[kind]

        def note_sidecar(data):
            sidecar_key_counts.update(data.keys())
            if extract_url(data):
                return 1
            return 0

        for me, jn, data, method in pairs:
            files.append(FileRec(me.relpath, me.name, scope, folder, jn, data,
                                 method, me.source.label))
            pair_methods[method] += 1
            sidecar_total += 1
            url_present += note_sidecar(data)
            if len(sidecar_samples) < 64:
                sidecar_samples.append(("%s/%s" % (folder, jn), data))

        for me, base_name, base_jn, base_data, fold_kind in folded:
            # Same sidecar data as the base, so the same item key - this file is
            # another rendition of that one item, not an item of its own.
            files.append(FileRec(me.relpath, me.name, scope, folder, base_jn,
                                 base_data, "folded:" + fold_kind, me.source.label,
                                 rendition_of=base_name, fold_kind=fold_kind))
            pair_methods["folded:" + fold_kind] += 1
            folded_counts[fold_kind] += 1
            if len(folded_examples) < 40:
                folded_examples.append({
                    "folder": folder, "rendition": me.name, "base": base_name,
                    "kind": fold_kind})

        for me, shaped in unpaired_here:
            files.append(FileRec(me.relpath, me.name, scope, folder, None, None,
                                 "none", me.source.label, no_sidecar=True,
                                 rendition_shaped=shaped))
            pair_methods["none"] += 1
            unpaired.append(me.relpath)

        for jn, data in sconly:
            entry = entries[jn]
            implied = sidecar_title(data) or jn[: -len(".json")]
            # ext_override="" so a membership proof with no file of its own cannot
            # make an item look like a still+video pair.
            files.append(FileRec(entry.relpath, implied, scope, folder, jn, data,
                                 "sidecar-only", entry.source.label,
                                 sidecar_only=True, ext_override=""))
            pair_methods["sidecar-only"] += 1
            sidecar_total += 1
            url_present += note_sidecar(data)
            if len(sidecar_samples) < 64:
                sidecar_samples.append(("%s/%s" % (folder, jn), data))
            if scope in ("album", "generated"):
                sidecar_only_album.append(entry.relpath)
            else:
                sidecar_only_library.append(entry.relpath)

        if scope in ("album", "generated"):
            meta, wrapped = unwrap_album_metadata(res["album_metadata"])
            shared = False
            title = folder
            meta_keys = None
            if isinstance(meta, dict):
                meta_keys = sorted(meta.keys())
                shared = bool(meta.get("access") not in (None, "protected")) or \
                    "sharedAlbumComments" in meta
                if isinstance(meta.get("title"), str):
                    title = nfc(meta["title"])
            gen_media = sum(1 for me, _j, _d, _m in pairs
                            if is_generated_media_name(me.name))
            gen_media += sum(1 for me, _s in unpaired_here
                             if is_generated_media_name(me.name))
            albums[folder] = {
                "folder": folder,
                "name": folder,
                "title": title,
                "kind": kind,
                "media_files": media_count,
                "sidecar_only_members": len(sconly),
                "shared": shared,
                "sources": srcs,
                "has_album_metadata": meta_keys is not None,
                "album_metadata_wrapped_in_albumData": wrapped,
                "album_metadata_keys": meta_keys,
                "album_metadata_fields": {
                    k: describe_value(meta.get(k))
                    for k in SURVEY_FIELDS
                    if isinstance(meta, dict) and k in meta
                } if isinstance(meta, dict) else {},
                "generated_media_items": gen_media,
                "generated_media_fraction": (
                    round(gen_media / float(media_count), 3) if media_count else 0.0),
            }
        classification.append((folder, kind, media_count))

    return {
        "files": files, "classification": classification, "patterns": patterns,
        "pair_methods": pair_methods, "unpaired": unpaired,
        "sidecar_only_album": sidecar_only_album,
        "sidecar_only_library": sidecar_only_library,
        "folded_counts": folded_counts, "folded_examples": folded_examples,
        "errors": errors, "albums": albums,
        "excluded_counts": excluded_counts, "sidecar_samples": sidecar_samples,
        "sidecar_key_counts": sidecar_key_counts, "sidecar_total": sidecar_total,
        "url_present": url_present, "split_folders": split_folders,
    }


# ---------------------------------------------------------------------------
# Generated-album declaration
# ---------------------------------------------------------------------------

class GeneratedSpec(object):
    """What the user explicitly declared auto-generated, plus an audit trail.

    Nothing here is inferred. `names` come from --generated-album and
    --generated-albums-file, `regexes` from --generated-album-regex. Every folder
    each one matched is recorded so the report can show the user what their own
    flags did.
    """

    def __init__(self, names, regexes):
        self.names = set()
        for n in names:
            n = nfc(n).strip()
            if n:
                self.names.add(norm_folder(n))
        self.regexes = regexes
        self.matched_by_name = defaultdict(set)   # norm name -> folder names
        self.matched_by_regex = defaultdict(set)  # pattern  -> folder names

    @property
    def active(self):
        return bool(self.names or self.regexes)

    def matches(self, folder_name):
        norm = norm_folder(folder_name)
        hit = False
        if norm in self.names:
            self.matched_by_name[norm].add(folder_name)
            hit = True
        for rx in self.regexes:
            if rx.fullmatch(nfc(folder_name).strip()):
                self.matched_by_regex[rx.pattern].add(folder_name)
                hit = True
        return hit

    def unmatched_names(self):
        return sorted(self.names - set(self.matched_by_name))

    def unmatched_regexes(self):
        return [rx.pattern for rx in self.regexes
                if rx.pattern not in self.matched_by_regex]


def read_generated_albums_file(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        sys.exit("error: --generated-albums-file not found: %s" % path)
    names = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    names.append(line)
    except UnicodeDecodeError as exc:
        sys.exit("error: --generated-albums-file %s is not valid UTF-8: %s"
                 % (path, exc))
    except OSError as exc:
        sys.exit("error: cannot read --generated-albums-file %s: %s" % (path, exc))
    return names


# ---------------------------------------------------------------------------
# Logical items + set difference
# ---------------------------------------------------------------------------

class Item(object):
    """One Google Photos item: every file and membership proof that keys to it."""

    __slots__ = ("stem", "ts", "files", "folders")

    def __init__(self, stem, ts):
        self.stem = stem
        self.ts = ts
        self.files = []
        self.folders = set()

    def add(self, rec):
        self.files.append(rec)
        self.folders.add(rec.folder)

    @property
    def real_files(self):
        """Files that actually exist in the export (not sidecar-only proofs)."""
        return [f for f in self.files if not f.sidecar_only]

    @property
    def exts(self):
        return {f.ext for f in self.real_files if f.ext}

    @property
    def name_exts(self):
        """Every extension this item's names claim, from files AND from sidecars.

        Wider than `exts` on purpose: the same-stem collision guard has to be able
        to compare an album item that is a sidecar-only membership proof (no file,
        so no `exts` at all) against a library item, and the sidecar `title` is the
        only extension evidence such a proof carries.
        """
        out = set()
        for f in self.files:
            if f.ext:
                out.add(f.ext)
            if f.title_ext:
                out.add(f.title_ext)
        return out

    @property
    def titles(self):
        return {f.title for f in self.files if f.title}

    @property
    def may_be_truncated(self):
        return any(f.name_may_be_truncated for f in self.files)

    @property
    def all_no_sidecar(self):
        return bool(self.files) and all(f.no_sidecar for f in self.files)

    @property
    def all_sidecar_only(self):
        return bool(self.files) and all(f.sidecar_only for f in self.files)

    @property
    def rendition_shaped(self):
        return any(f.rendition_shaped for f in self.files)

    @property
    def is_live_pair(self):
        exts = self.exts
        return bool(exts & IMAGE_EXTS) and bool(exts & VIDEO_EXTS)

    @property
    def url(self):
        for f in self.files:
            if f.url:
                return f.url
        return None

    @property
    def where(self):
        """folder/filename of one representative file, for examples in the report."""
        f = (self.real_files or self.files)[0]
        return "%s/%s" % (f.folder, f.name)

    @property
    def display_name(self):
        titles = self.titles
        if titles:
            return sorted(titles)[0]
        real = self.real_files
        return (real or self.files)[0].name


def group_items(recs):
    items = {}
    for r in recs:
        key = (r.stem, r.ts)
        it = items.get(key)
        if it is None:
            it = items[key] = Item(r.stem, r.ts)
        it.add(r)
    return items


def prefix_collides(stem, sorted_stems, min_len=12, window=500):
    """True if `stem` is a strict prefix of, or strictly prefixed by, an album stem.

    Guards against Takeout's name truncation making one logical item look like two
    distinct ones. Only applied to items whose name might actually be truncated
    (see FileRec.name_may_be_truncated) and only for stems long enough that a
    prefix relationship is evidence of truncation rather than coincidence.
    """
    if len(stem) < min_len:
        return False
    i = bisect.bisect_left(sorted_stems, stem)
    # album stems that start with `stem` sort at or just after it
    for j in range(i, min(i + window, len(sorted_stems))):
        other = sorted_stems[j]
        if other == stem:
            continue
        if other.startswith(stem):
            return True
        if not other.startswith(stem[:min_len]):
            break
    # album stems that `stem` starts with sort before it
    for j in range(i - 1, max(i - window - 1, -1), -1):
        other = sorted_stems[j]
        if len(other) >= min_len and stem.startswith(other):
            return True
        if other[:min_len] != stem[:min_len]:
            break
    return False


def ext_class(lib_exts, album_exts):
    """How two items' extension evidence compares -> one of EXT_CLASSES.

    "Unknown" when either side claims no extension at all, which is weaker than a
    mismatch and must not be read as one.
    """
    if not lib_exts or not album_exts:
        return EXT_UNKNOWN
    return EXT_MATCH if (lib_exts & album_exts) else EXT_MISMATCH


class LegacyStemGuard(object):
    """4.x's same-stem guard, verbatim: ANY album item with this stem withholds.

    Retained only so every run can replay the whole classification with the old
    rule and prove the new one never turns a filed photo into an orphan. It is the
    check that caught 5.0.0; see the module docstring.
    """

    label = "4.x (any same-stem album item, any timestamp, any extension)"

    def __init__(self, album_items):
        self.album_stems = {k[0] for k in album_items}

    def verdict(self, _it, stem, _ts):
        return REASON_STEM_DIFF_TS if stem in self.album_stems else None


class StemCollisionGuard(object):
    """6.0.0's same-stem guard: extension-aware AND time-bounded.

    The guard exists to catch Takeout writing a slightly different photoTakenTime
    for an item's album copy than for its library copy - a seconds-to-minutes
    discrepancy. Anything further out is iPhone recycling IMG_NNNN filenames, so
    two narrowings apply, and an item survives the guard only if BOTH let it:

      * a same-extension collision withholds the item within `window`;
      * a DIFFERENT-extension collision is a weaker signal, not a non-collision -
        Takeout does rewrite extensions - so it still withholds, but only within
        `ext_window`, which is clamped never to reach further than `window`;
      * an album item with no photoTakenTime cannot be time-bounded, so it
        withholds unconditionally.

    A window of 0 disables the guard, which is a diagnostic only.
    """

    def __init__(self, album_items, window, ext_window):
        self.window = window
        # A weaker signal must never out-reach the strong one.
        self.ext_window = min(window, ext_window)
        self.label = ("6.0.0 (same-stem within %s; different extension within %s)"
                      % (human_duration(window), human_duration(self.ext_window)))
        self.by_stem = defaultdict(list)
        for (stem, ts), it in album_items.items():
            self.by_stem[stem].append((ts, frozenset(it.name_exts)))

    def verdict(self, it, stem, ts):
        candidates = self.by_stem.get(stem)
        if not candidates or self.window <= 0:
            return None
        lib_exts = frozenset(it.name_exts)
        weak = None
        for album_ts, album_exts in candidates:
            if album_ts is None or ts is None:
                return REASON_STEM_ALBUM_NO_TS
            delta = abs(album_ts - ts)
            if ext_class(lib_exts, album_exts) == EXT_MISMATCH:
                if delta <= self.ext_window:
                    weak = REASON_STEM_NEAR_TS_EXT
            elif delta <= self.window:
                return REASON_STEM_NEAR_TS
        return weak


def stem_collision_deltas(library_items, album_items, max_pairs_per_stem,
                          examples_per_cell):
    """The empirical case for a window: |delta| over every same-stem lib/album pair.

    Reported two ways, because they answer different questions. The PAIR
    distribution shows the shape of the whole collision population; the NEAREST
    distribution (each library item against only its closest same-stem album item)
    is what the guard actually tests, so it is the one to read a cut point off.
    Both are split by whether the two sides' extensions agree.

    Nothing here classifies anything - it is evidence, printed so the window is
    chosen from the export rather than guessed.
    """
    album_by_stem = defaultdict(list)
    for (stem, ts), it in album_items.items():
        album_by_stem[stem].append((ts, it))
    lib_by_stem = defaultdict(list)
    for (stem, ts), it in library_items.items():
        if stem in album_by_stem:
            lib_by_stem[stem].append((ts, it))

    pair_counts = {lbl: Counter() for lbl, _u in COLLISION_BUCKETS}
    near_counts = {lbl: Counter() for lbl, _u in COLLISION_BUCKETS}
    examples = defaultdict(list)       # (bucket, ext_class) -> [str, ...]
    pairs_total = 0
    truncated_stems = []
    undated = Counter()                # pairs we cannot measure at all

    for stem in sorted(lib_by_stem):
        lib_list = sorted(lib_by_stem[stem], key=lambda p: (p[0] is None, p[0]))
        alb_list = sorted(album_by_stem[stem], key=lambda p: (p[0] is None, p[0]))
        if len(lib_list) * len(alb_list) > max_pairs_per_stem:
            truncated_stems.append((stem, len(lib_list), len(alb_list)))
        budget = max_pairs_per_stem
        for lib_ts, lib_it in lib_list:
            lib_exts = frozenset(lib_it.name_exts)
            best = None
            for alb_ts, alb_it in alb_list:
                cls = ext_class(lib_exts, frozenset(alb_it.name_exts))
                if lib_ts is None or alb_ts is None:
                    undated[cls] += 1
                    continue
                delta = abs(alb_ts - lib_ts)
                if budget > 0:
                    budget -= 1
                    pairs_total += 1
                    bucket = delta_bucket(delta)
                    pair_counts[bucket][cls] += 1
                    cell = examples[(bucket, cls)]
                    if len(cell) < examples_per_cell:
                        cell.append({"stem": stem, "delta_seconds": delta,
                                     "delta": approx_duration(delta),
                                     "album": alb_it.where,
                                     "library": lib_it.where})
                if best is None or delta < best[0]:
                    best = (delta, cls)
            if best is not None:
                near_counts[delta_bucket(best[0])][best[1]] += 1

    return {
        "pair_buckets": {lbl: dict(pair_counts[lbl])
                         for lbl, _u in COLLISION_BUCKETS},
        "nearest_buckets": {lbl: dict(near_counts[lbl])
                            for lbl, _u in COLLISION_BUCKETS},
        "examples": {"%s|%s" % k: v for k, v in sorted(examples.items())},
        "pairs_measured": pairs_total,
        "pairs_unmeasurable_missing_timestamp": dict(undated),
        "colliding_stems": len(lib_by_stem),
        "library_items_with_a_same_stem_album_item": sum(
            len(v) for v in lib_by_stem.values()),
        "stems_truncated_at_pair_cap": [
            {"stem": s, "library_items": a, "album_items": b}
            for s, a, b in truncated_stems[:50]],
        "stems_truncated_at_pair_cap_total": len(truncated_stems),
        "max_pairs_per_stem": max_pairs_per_stem,
    }


def crosscheck_stem_guard(legacy, new):
    """Compare the new classification against the UNMODIFIED 4.x one.

    Two of the three outcomes MUST be empty. `legacy_filed_now_orphan` is the
    safety net: it is the check that caught 5.0.0's url rekey (26139 violations).
    `legacy_uncertain_now_orphan` is 6.0.0's whole point and is expected to be
    large, so it is reported as an intended delta rather than as a violation.
    """
    def keys(items):
        return {(it.stem, it.ts) for it in items}

    legacy_filed = keys(legacy["in_album"])
    legacy_orphan = keys(legacy["orphans"])
    legacy_uncertain = {(it.stem, it.ts) for it, _r in legacy["uncertain"]}
    new_orphan_items = {(it.stem, it.ts): it for it in new["orphans"]}
    new_orphan = set(new_orphan_items)
    new_uncertain = {(it.stem, it.ts): r for it, r in new["uncertain"]}

    filed_now_orphan = sorted(legacy_filed & new_orphan)
    orphan_now_held = sorted(legacy_orphan & set(new_uncertain))
    released = sorted(legacy_uncertain & new_orphan,
                      key=lambda k: (-(k[1] or 0), k[0]))
    legacy_reason = {(it.stem, it.ts): r for it, r in legacy["uncertain"]}
    return {
        "legacy_guard": LegacyStemGuard.label,
        "legacy_filed_now_orphan": len(filed_now_orphan),
        "legacy_filed_now_orphan_examples": [
            new_orphan_items[k].where for k in filed_now_orphan[:20]],
        "legacy_orphan_now_uncertain": len(orphan_now_held),
        "legacy_orphan_now_uncertain_examples": [
            "%s  ->  %s" % (k[0], new_uncertain[k]) for k in orphan_now_held[:20]],
        "legacy_uncertain_now_orphan": len(released),
        "legacy_uncertain_now_orphan_by_legacy_reason": dict(
            Counter(legacy_reason[k] for k in released)),
        "legacy_uncertain_now_orphan_examples": [
            new_orphan_items[k].where for k in released[:20]],
        "violations": len(filed_now_orphan) + len(orphan_now_held),
    }


def classify_library(library_items, album_keys, archive_keys, include_archived,
                     gen_folders_by_key, stem_guard):
    """The funnel: every library item becomes in_album / orphan / uncertain /
    archived_excluded, biased so that anything doubtful is never an orphan.

    Factored out so the caller can run it again with a DIFFERENT album-key set, or a
    DIFFERENT same-stem guard, and measure what an earlier rule would have produced.
    `stem_guard` must therefore be built from the same album items as `album_keys`.
    """
    sorted_album_stems = sorted({k[0] for k in album_keys})

    orphans, uncertain, in_album, archived_excluded = [], [], [], []
    generated_only = []

    for key, it in library_items.items():
        stem, ts = key
        if key in album_keys:
            in_album.append(it)
            continue
        if it.all_no_sidecar:
            # No sidecar anywhere: no photoTakenTime to key on, no url to open.
            uncertain.append((it, REASON_RENDITION_NO_BASE if it.rendition_shaped
                              else REASON_MEDIA_NO_SIDECAR))
            continue
        if it.all_sidecar_only:
            # Rule 1's deliberate asymmetry: on the library side a lone sidecar is
            # evidence of an item, but not licence to queue a file we never saw.
            uncertain.append((it, REASON_LIB_SIDECAR_ONLY))
            continue
        if ts is None:
            uncertain.append((it, REASON_NO_TAKEN_TIME))
            continue
        collision = stem_guard.verdict(it, stem, ts)
        if collision is not None:
            uncertain.append((it, collision))
            continue
        if it.may_be_truncated and prefix_collides(stem, sorted_album_stems):
            uncertain.append((it, REASON_PREFIX_COLLISION))
            continue
        if key in archive_keys and not include_archived:
            archived_excluded.append(it)
            continue
        orphans.append(it)
        if key in gen_folders_by_key:
            generated_only.append(it)

    orphans.sort(key=lambda i: (-(i.ts or 0), i.stem))
    uncertain.sort(key=lambda p: (-(p[0].ts or 0), p[0].stem))
    generated_only.sort(key=lambda i: (-(i.ts or 0), i.stem))
    return {"orphans": orphans, "uncertain": uncertain, "in_album": in_album,
            "archived_excluded": archived_excluded,
            "generated_only": generated_only}


def assemble_items(files):
    """Group file records into logical items, per scope.

    Archive-only items are not in "Photos from YYYY" but are still library items,
    so they are merged into the library view.
    """
    album_items = group_items([f for f in files if f.scope == "album"])
    gen_items = group_items([f for f in files if f.scope == "generated"])
    archive_items = group_items([f for f in files if f.scope == "archive"])
    library_items = group_items([f for f in files if f.scope == "library"])
    for key, it in archive_items.items():
        if key not in library_items:
            library_items[key] = it
    return album_items, gen_items, archive_items, library_items


def unfold(files):
    """The same records as if Rule 2 had never folded anything.

    Each folded rendition becomes an unkeyed, sidecar-less record again, which is
    what it would have been on its own. Used only to measure Rule 2's effect; it
    re-reads nothing, so the 'before' column is an exact replay rather than an
    arithmetic guess (several folded files share a synthetic key, and album-side
    renditions never produce UNCERTAIN rows at all, so arithmetic over-counts).
    """
    out = []
    for f in files:
        if f.fold_kind:
            out.append(FileRec(f.relpath, f.name, f.scope, f.folder, None, None,
                               "none", f.source, no_sidecar=True,
                               rendition_shaped=True))
        else:
            out.append(f)
    return out


def compute(scanres, include_archived, stem_window, stem_ext_window,
            max_pairs_per_stem=64, examples_per_cell=3):
    """Set difference against REAL albums only, plus the before/after diagnostics.

    Album membership comes from every album-side record - paired media AND lone
    sidecars (Rule 1). The funnel is then replayed three more times - with
    pair-only membership, with folding undone, and with 4.x's unmodified same-stem
    guard - so the report can state exactly what each rule changed on this export
    and prove the new guard never turns a filed photo into an orphan.
    """
    files = scanres["files"]
    gen_recs = [f for f in files if f.scope == "generated"]
    album_items, gen_items, archive_items, library_items = assemble_items(files)

    album_keys = set(album_items.keys())
    archive_keys = set(archive_items.keys())

    # Membership an earlier, pairs-only rule would have seen.
    album_items_pairs_only = {k: it for k, it in album_items.items()
                              if it.real_files}
    album_keys_pairs_only = set(album_items_pairs_only)
    sidecar_only_album_keys = album_keys - album_keys_pairs_only

    gen_folders_by_key = defaultdict(set)
    for r in gen_recs:
        gen_folders_by_key[(r.stem, r.ts)].add(r.folder)

    guard = StemCollisionGuard(album_items, stem_window, stem_ext_window)
    res = classify_library(library_items, album_keys, archive_keys,
                           include_archived, gen_folders_by_key, guard)
    # Rule 1 replay: membership from media+sidecar pairs only.
    cf_rule1 = classify_library(library_items, album_keys_pairs_only,
                                archive_keys, include_archived,
                                gen_folders_by_key,
                                StemCollisionGuard(album_items_pairs_only,
                                                   stem_window, stem_ext_window))
    # 4.x replay: identical in every respect EXCEPT the same-stem guard, so the
    # difference is attributable to the guard alone and nothing else.
    legacy = classify_library(library_items, album_keys, archive_keys,
                              include_archived, gen_folders_by_key,
                              LegacyStemGuard(album_items))

    # Which library items are protected ONLY by a sidecar-only membership proof,
    # and which of those the old rule would actually have queued for deletion.
    cf_orphan_keys = {(it.stem, it.ts) for it in cf_rule1["orphans"]}
    saved = [it for it in res["in_album"]
             if (it.stem, it.ts) in sidecar_only_album_keys
             and (it.stem, it.ts) in cf_orphan_keys]
    saved.sort(key=lambda i: (-(i.ts or 0), i.stem))

    folded_total = sum(scanres["folded_counts"].values())
    live_now = sum(1 for it in library_items.values() if it.is_live_pair)

    # Rule 2 replay: folding undone, so each rendition stands alone again.
    if folded_total:
        uf_album, _ug, uf_arch, uf_lib = assemble_items(unfold(files))
        cf_rule2 = classify_library(uf_lib, set(uf_album.keys()),
                                    set(uf_arch.keys()), include_archived,
                                    gen_folders_by_key,
                                    StemCollisionGuard(uf_album, stem_window,
                                                       stem_ext_window))
        live_before = sum(1 for it in uf_lib.values() if it.is_live_pair)
        unc_before = len(cf_rule2["uncertain"])
        orph_before = len(cf_rule2["orphans"])
    else:
        live_before, unc_before = live_now, len(res["uncertain"])
        orph_before = len(res["orphans"])

    res.update({
        "stem_guard_label": guard.label,
        "stem_guard_window": stem_window,
        "stem_guard_ext_window": guard.ext_window,
        "stem_guard_crosscheck": crosscheck_stem_guard(legacy, res),
        "legacy_orphans": len(legacy["orphans"]),
        "legacy_orphan_keys": {(it.stem, it.ts) for it in legacy["orphans"]},
        "legacy_uncertain": len(legacy["uncertain"]),
        "legacy_uncertain_by_reason": dict(
            Counter(r for _i, r in legacy["uncertain"])),
        "collision_deltas": stem_collision_deltas(
            library_items, album_items, max_pairs_per_stem, examples_per_cell),
        "library_items": library_items, "album_items": album_items,
        "generated_items": gen_items, "archive_items": archive_items,
        "gen_folders_by_key": gen_folders_by_key,
        "sidecar_only_album_keys": sidecar_only_album_keys,
        "album_keys_pairs_only": album_keys_pairs_only,
        "rule1_saved_from_deletion": saved,
        "live_pairs": live_now,
        "before_after": {
            "orphans_now": len(res["orphans"]),
            "orphans_without_rule1_sidecar_membership": len(cf_rule1["orphans"]),
            "items_in_album_only_via_sidecar_proof": len(
                [it for it in res["in_album"]
                 if (it.stem, it.ts) in sidecar_only_album_keys]),
            "photos_rule1_saves_from_deletion": len(saved),
            "uncertain_now": len(res["uncertain"]),
            "uncertain_without_rule2_folding": unc_before,
            "orphans_without_rule2_folding": orph_before,
            "renditions_folded": folded_total,
            "renditions_folded_by_kind": dict(scanres["folded_counts"]),
            "media_without_sidecar_now": len(scanres["unpaired"]),
            "media_without_sidecar_without_rule2_folding":
                len(scanres["unpaired"]) + folded_total,
            "live_photo_pairs_now": live_now,
            "live_photo_pairs_without_rule2_folding": live_before,
        },
    })
    return res


def contiguous_runs(library_items, orphan_keys):
    """Maximal runs of orphans that are ADJACENT in the library's chronological order.

    This is what shift-click selection in the Google Photos grid actually selects:
    newest-first order, no non-orphan in between.
    """
    ordered = sorted((it for it in library_items.values() if it.ts is not None),
                     key=lambda i: (-i.ts, i.stem))
    runs, cur = [], []
    for it in ordered:
        if (it.stem, it.ts) in orphan_keys:
            cur.append(it)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def full_orphan_day_ranges(library_items, orphan_keys):
    """Calendar days where EVERY library item is an orphan, collapsed to ranges."""
    per_day_total = Counter()
    per_day_orphan = Counter()
    for key, it in library_items.items():
        if it.ts is None:
            continue
        d = day(it.ts)
        per_day_total[d] += 1
        if key in orphan_keys:
            per_day_orphan[d] += 1
    full_days = sorted(d for d in per_day_total
                       if per_day_total[d] == per_day_orphan[d] and per_day_total[d] > 0)
    ranges = []
    for d in full_days:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        if ranges and (dt - ranges[-1][1]).days == 1:
            ranges[-1] = (ranges[-1][0], dt, ranges[-1][2] + per_day_orphan[d])
        else:
            ranges.append((dt, dt, per_day_orphan[d]))
    return [(a.isoformat(), b.isoformat(), n) for a, b, n in ranges]


# ---------------------------------------------------------------------------
# Album-provenance signal survey (the investigation)
# ---------------------------------------------------------------------------

def survey_album_signals(scanres):
    """Does THIS export carry anything that distinguishes generated albums?

    Reports the metadata shapes and field values that actually occur across album
    folders, and whether any of them partition the set. Nothing here is treated as
    proof of provenance; Takeout documents no such flag.
    """
    albums = list(scanres["albums"].values())
    shapes = defaultdict(list)
    for a in albums:
        keyshape = ("<no metadata.json>" if not a["has_album_metadata"]
                    else ", ".join(a["album_metadata_keys"]) or "<empty object>")
        shapes[keyshape].append(a["folder"])

    field_values = defaultdict(lambda: defaultdict(list))
    for a in albums:
        for k, v in a["album_metadata_fields"].items():
            field_values[k][v].append(a["folder"])

    discriminating = {k: {val: folders for val, folders in vals.items()}
                      for k, vals in field_values.items() if len(vals) > 1}
    missing_meta = [a["folder"] for a in albums if not a["has_album_metadata"]]

    if not albums:
        verdict = "NO_ALBUMS"
    elif len(shapes) == 1 and not discriminating:
        verdict = "NO_SIGNAL"
    else:
        verdict = "DIFFERENCES_FOUND"
    return {
        "albums_examined": len(albums),
        "metadata_shapes": {k: sorted(v) for k, v in shapes.items()},
        "fields_with_more_than_one_value": discriminating,
        "albums_without_metadata_json": sorted(missing_meta),
        "verdict": verdict,
    }


def suggest_generated_albums(scanres):
    """Data-driven candidates for the user to CONFIRM. Never applied automatically.

    Two signals only, both auditable:
      * the folder is dominated by media Google itself generated (-ANIMATION,
        -COLLAGE, -EFFECTS, MOVIE, ... suffixes, which are a real Takeout marker);
      * the folder name is one Google is known to use for its own collections.
    Person-name shape is deliberately NOT a signal.
    """
    out = []
    for a in sorted(scanres["albums"].values(), key=lambda x: x["folder"]):
        if a["kind"] == "generated_album":
            continue
        reasons = []
        if a["media_files"] >= 3 and a["generated_media_fraction"] >= 0.8:
            reasons.append(
                "%d of %d items (%.0f%%) are Google-generated media by filename marker"
                % (a["generated_media_items"], a["media_files"],
                   100.0 * a["generated_media_fraction"]))
        if norm_folder(a["name"]) in KNOWN_GENERATED_NAME_HINTS:
            reasons.append("folder name is one Google uses for its own collections")
        if reasons:
            out.append({"folder": a["folder"], "name": a["name"],
                        "media_files": a["media_files"], "reasons": reasons})
    return out


# ---------------------------------------------------------------------------
# Permalink feasibility
# ---------------------------------------------------------------------------

def permalink_finding(scanres, sample_n):
    keys = scanres["sidecar_key_counts"]
    total = max(scanres["sidecar_total"], 1)
    found = {}
    for k in PERMALINK_KEYS:
        if keys.get(k):
            found[k] = keys[k]
    # Also look for any key whose name smells like a url/id we did not anticipate.
    extra = {k: c for k, c in keys.items()
             if k not in found and k not in NOT_A_PERMALINK_KEYS
             and re.search(r"(url|uri|^id$|id$)", k, re.I)}
    samples = []
    for relpath, data in scanres["sidecar_samples"][:sample_n]:
        vals = {}
        for k in list(found) + list(extra):
            if k in data:
                vals[k] = data[k]
        samples.append({"path": relpath, "keys": sorted(data.keys()),
                        "permalink_values": vals})
    return {
        "sidecars_inspected": scanres["sidecar_total"],
        "permalink_or_id_fields_present": found,
        "other_url_or_id_like_fields": extra,
        "coverage": {k: round(100.0 * c / total, 1) for k, c in found.items()},
        "samples": samples,
        "verdict": ("PRESENT" if found else "ABSENT"),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_csv(path, rows, header):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def item_row(it, gen_folders_by_key, extra=()):
    gen = sorted(gen_folders_by_key.get((it.stem, it.ts), ()))
    folded = [f for f in it.files if f.fold_kind]
    return [
        it.display_name,
        day(it.ts),
        iso(it.ts),
        it.ts if it.ts is not None else "",
        it.url or "",
        "yes" if it.is_live_pair else "no",
        len(it.real_files),
        "|".join(sorted(it.exts)),
        "|".join(sorted({f.fold_kind for f in folded})),
        "|".join(gen),
        "|".join(f.relpath for f in sorted(it.files, key=lambda f: f.relpath)),
        "|".join(sorted({f.source for f in it.files})),
    ] + list(extra)


ITEM_HEADER = ["title", "date", "taken_time_utc", "taken_time_epoch", "url",
               "live_photo_pair", "file_count", "extensions", "folded_renditions",
               "generated_albums", "takeout_paths", "source_archives"]


def item_dict(it, gen_folders_by_key, extra=None):
    folded = [f for f in it.files if f.fold_kind]
    d = {
        "title": it.display_name,
        "date": day(it.ts),
        "taken_time_utc": iso(it.ts),
        "taken_time_epoch": it.ts,
        "url": it.url,
        "live_photo_pair": it.is_live_pair,
        "file_count": len(it.real_files),
        "extensions": sorted(it.exts),
        "folded_renditions": [
            {"file": f.name, "base": f.rendition_of, "kind": f.fold_kind}
            for f in folded
        ],
        "generated_albums": sorted(gen_folders_by_key.get((it.stem, it.ts), ())),
        "orphan_solely_due_to_generated_album":
            bool(gen_folders_by_key.get((it.stem, it.ts))),
        "takeout_paths": sorted(f.relpath for f in it.files),
        "source_archives": sorted({f.source for f in it.files}),
    }
    if extra:
        d.update(extra)
    return d


def print_source_summary(sources, inv_info, part_warnings, elapsed):
    """Per-archive accounting, including the proof that media was never read."""
    print("=" * 78)
    print("ARCHIVE / SOURCE SUMMARY  <-- CHECK EVERY PART OF YOUR EXPORT IS LISTED.")
    print("                              A missing part means every photo in it is")
    print("                              invisible to this run.")
    print("=" * 78)
    hdr = ("%-34s %-5s %8s %8s %7s %7s  %s"
           % ("source", "kind", "entries", "media", "json", "other", "in-library"))
    print("\n  " + hdr)
    print("  " + "-" * (len(hdr) + 2))
    tot = Counter()
    for s in sources:
        in_lib = s.media + s.jsons + s.other
        print("  %-34s %-5s %8d %8d %7d %7d  %d"
              % (s.label[:34], s.kind, s.entries, s.media, s.jsons, s.other, in_lib))
        for key in ("entries", "media", "jsons", "other", "dup_skipped",
                    "skipped_outside", "bytes_read", "total_uncompressed"):
            tot[key] += getattr(s, key)
    print("  %-34s %-5s %8d %8d %7d %7d"
          % ("TOTAL", "", tot["entries"], tot["media"], tot["jsons"], tot["other"]))

    print("\n  Entry-name decoding (zip stores names as UTF-8 only when general")
    print("  purpose bit 11 is set; otherwise Python's zipfile would read real")
    print("  UTF-8 bytes as CP437 mojibake, so unflagged names are recovered):")
    enc_hdr = ("%-34s %13s %14s %13s %12s %10s"
               % ("source", "utf8_flagged", "utf8_recovered", "cp437_literal",
                  "undecodable", "filesystem"))
    print("  " + enc_hdr)
    print("  " + "-" * (len(enc_hdr) + 2))
    enc_tot = Counter()
    for s in sources:
        c = s.enc_counts
        print("  %-34s %13d %14d %13d %12d %10d"
              % (s.label[:34], c[ENC_UTF8_FLAGGED], c[ENC_UTF8_RECOVERED],
                 c[ENC_CP437_LITERAL], c[ENC_UNDECODABLE], c["filesystem"]))
        enc_tot.update(c)
    print("  %-34s %13d %14d %13d %12d %10d"
          % ("TOTAL", enc_tot[ENC_UTF8_FLAGGED], enc_tot[ENC_UTF8_RECOVERED],
             enc_tot[ENC_CP437_LITERAL], enc_tot[ENC_UNDECODABLE],
             enc_tot["filesystem"]))
    bad = enc_tot[ENC_CP437_LITERAL] + enc_tot[ENC_UNDECODABLE]
    if bad:
        print("")
        banner(["%d entry name(s) could NOT be read as UTF-8. Those paths may be" % bad,
                "misspelled, so an album may be mis-identified. The names are in",
                "the WARNINGS section - check them before trusting the delta."])
    else:
        print("\n  All entry names decoded cleanly as UTF-8 (flagged or recovered);")
        print("  nothing was dropped and no name needed a lossy decode.")

    print("\n  Work actually done:")
    print("    library-relative paths in the merged inventory : %d"
          % inv_info["total_paths"])
    print("    duplicate paths skipped (same content twice)   : %d"
          % inv_info["duplicate_paths_skipped"])
    for pair, n in sorted(inv_info["duplicate_source_pairs"].items()):
        print("        %-50s %d path(s) -- kept the first named" % (pair, n))
    print("    entries outside the photo library (ignored)    : %d"
          % tot["skipped_outside"])
    print("    bytes READ (JSON sidecars only)                : %s"
          % human_bytes(tot["bytes_read"]))
    print("    bytes NOT read (media left compressed)         : %s"
          % human_bytes(max(tot["total_uncompressed"] - tot["bytes_read"], 0)))
    zbytes = sum(getattr(s, "archive_bytes", 0) for s in sources)
    if zbytes:
        print("    archive size on disk                           : %s"
              % human_bytes(zbytes))
    print("    elapsed                                        : %.1fs" % elapsed)
    print("    => media entries are never decompressed; that is why this runs in")
    print("       minutes against a 250GB export and needs no extra disk.")

    if part_warnings:
        print("")
        banner(["ARCHIVE SET WARNINGS - read these before trusting any count"])
        for w in part_warnings:
            print("    !! %s" % w)
    elif any(s.kind == "zip" for s in sources):
        print("\n  Part numbering and export timestamps look consistent.")


def print_rule_impact(res, scanres, fold_enabled):
    """What the item-identity rules did to the answer, in before/after terms."""
    ba = res["before_after"]
    print("\n" + "=" * 78)
    print("HOW THE THREE ITEM-IDENTITY RULES CHANGED THE ANSWER")
    print("=" * 78)

    print("\n  RULE 1 - album membership from sidecars, not from media copies")
    print("  %-56s %7d" % ("album folders holding ZERO media files:",
                           sum(1 for a in scanres["albums"].values()
                               if a["media_files"] == 0)))
    print("  %-56s %7d" % ("album-side sidecars with no media copy beside them:",
                           len(scanres["sidecar_only_album"])))
    print("  %-56s %7d" % ("library items that are album members ONLY via those:",
                           ba["items_in_album_only_via_sidecar_proof"]))
    print("  %-56s %7d" % ("orphans reported now:", ba["orphans_now"]))
    print("  %-56s %7d" % ("orphans a pairs-only membership rule would report:",
                           ba["orphans_without_rule1_sidecar_membership"]))
    n_saved = ba["photos_rule1_saves_from_deletion"]
    if n_saved:
        print("")
        banner(["%d photo(s) ARE filed in a real album and would have been" % n_saved,
                "queued for deletion by the old pairs-only rule. They are now",
                "correctly excluded from the orphan list."])
    else:
        print("\n  No item's membership depended on a sidecar-only proof in this")
        print("  export, so Rule 1 changed nothing here.")

    print("\n  RULE 2 - a sidecar-less file is another rendition of a keyed item")
    if not fold_enabled:
        print("  DISABLED by --no-fold-renditions: every sidecar-less rendition is")
        print("  its own UNCERTAIN row. This is a diagnostic mode; the numbers below")
        print("  are the 'before' column.")
    print("  %-56s %7d" % ("renditions folded into their base item:",
                           ba["renditions_folded"]))
    for kind in FOLD_KINDS:
        n = ba["renditions_folded_by_kind"].get(kind, 0)
        label = ("  ... %s (<stem>-edited.<ext> -> <stem>.<ext>)" % kind
                 if kind == FOLD_EDIT_RENDITION
                 else "  ... %s (<stem>.mp4 -> <stem>.heic/.jpg)" % kind)
        print("  %-56s %7d" % (label, n))
    print("  %-56s %7d" % ("UNCERTAIN rows now:", ba["uncertain_now"]))
    print("  %-56s %7d" % ("UNCERTAIN rows without folding:",
                           ba["uncertain_without_rule2_folding"]))
    print("  %-56s %7d" % ("orphans without folding (must equal orphans now):",
                           ba["orphans_without_rule2_folding"]))
    print("  %-56s %7d" % ("media files with no sidecar now:",
                           ba["media_without_sidecar_now"]))
    print("  %-56s %7d" % ("media files with no sidecar without folding:",
                           ba["media_without_sidecar_without_rule2_folding"]))
    print("  %-56s %7d" % ("Live Photo still+motion pairs now:",
                           ba["live_photo_pairs_now"]))
    print("  %-56s %7d" % ("Live Photo pairs counted without folding:",
                           ba["live_photo_pairs_without_rule2_folding"]))
    if ba["renditions_folded"]:
        print("\n  Folding does NOT move items between orphan and keeper: a base item")
        print("  is keyed by its own sidecar either way, so the orphan COUNT is")
        print("  unchanged and the affected orphan rows simply gained a file.")
        print("  Deleting such an item removes BOTH renditions - they are one item.")
        print("  Ordinary photo edits therefore add no orphan candidates; only VIDEO")
        print("  'save a copy' edits make a genuinely separate item (own sidecar,")
        print("  own url), and those are counted as items in their own right.")
    if scanres["folded_examples"]:
        print("\n  Examples of what was folded:")
        for ex in scanres["folded_examples"][:8]:
            print("    %-28s + %-28s  (%s)"
                  % (ex["base"][:28], ex["rendition"][:28], ex["kind"]))

    print("\n  RULE 3 - identity is (sidecar title stem, photoTakenTime)")
    print("  %-56s %7d" % ("library logical items:", len(res["library_items"])))
    print("  %-56s %7d" % ("distinct REAL album logical items:",
                           len(res["album_items"])))


def print_collision_deltas(res, examples_per_cell):
    """The case for the window, printed so it can be judged rather than trusted."""
    cd = res["collision_deltas"]
    print("\n" + "=" * 78)
    print("SAME-STEM COLLISION TIME DELTAS  <-- the evidence for the window")
    print("=" * 78)
    print("  For every library item sharing a filename stem with an album item,")
    print("  |delta photoTakenTime|, split by whether the two sides' extensions")
    print("  agree. This classifies nothing; it exists so --stem-collision-window")
    print("  is chosen from YOUR export instead of from this script's default.")
    print("")
    print("  colliding stems                                   : %7d"
          % cd["colliding_stems"])
    print("  library items with a same-stem album item         : %7d"
          % cd["library_items_with_a_same_stem_album_item"])
    print("  pairs measured                                    : %7d"
          % cd["pairs_measured"])
    if cd["stems_truncated_at_pair_cap_total"]:
        print("  stems truncated at the %d-pair cap                : %7d"
              % (cd["max_pairs_per_stem"], cd["stems_truncated_at_pair_cap_total"]))
        for row in cd["stems_truncated_at_pair_cap"][:5]:
            print("      %-24s %d library x %d album items"
                  % (row["stem"][:24], row["library_items"], row["album_items"]))
    unmeasurable = sum(cd["pairs_unmeasurable_missing_timestamp"].values())
    if unmeasurable:
        print("  pairs with a missing photoTakenTime (unmeasurable) : %7d"
              % unmeasurable)
        print("      Those collisions are NOT time-bounded by the guard; every one")
        print("      of them still holds its library item back as UNCERTAIN.")

    for title, key, note in (
        ("ALL PAIRS - the shape of the whole collision population",
         "pair_buckets", None),
        ("NEAREST ALBUM ITEM PER LIBRARY ITEM - what the guard actually tests",
         "nearest_buckets",
         ("Read the cut point off THIS table: the guard only ever compares",
          "an item against its closest same-stem album item.")),
    ):
        buckets = cd[key]
        total = sum(sum(v.values()) for v in buckets.values())
        print("\n  %s" % title)
        for line in note or ():
            print("  %s" % line)
        print("  %-10s %10s %13s %12s %10s"
              % ("|delta|", "ext match", "ext mismatch", "ext unknown", "total"))
        for label, _upper in COLLISION_BUCKETS:
            row = buckets[label]
            rt = sum(row.values())
            print("  %-10s %10d %13d %12d %10d%s"
                  % (label, row.get(EXT_MATCH, 0), row.get(EXT_MISMATCH, 0),
                     row.get(EXT_UNKNOWN, 0), rt,
                     "   %5.1f%%" % (100.0 * rt / total) if total else ""))
        print("  %-10s %10d %13d %12d %10d"
              % ("TOTAL",
                 sum(v.get(EXT_MATCH, 0) for v in buckets.values()),
                 sum(v.get(EXT_MISMATCH, 0) for v in buckets.values()),
                 sum(v.get(EXT_UNKNOWN, 0) for v in buckets.values()), total))

    # Bimodality is the specific thing that would justify a window at all, so say
    # outright whether this export shows it rather than leaving it to the eye.
    near = cd["nearest_buckets"]
    tight = sum(sum(near[l].values()) for l in ("<1s", "<1min", "<1h"))
    far = sum(sum(near[l].values()) for l in ("<30d", "<1y", ">=1y"))
    middle = sum(sum(near[l].values()) for l in ("<24h",))
    total = tight + middle + far
    print("\n  Shape of the NEAREST distribution:")
    print("    within 1h of each other : %7d" % tight)
    print("    1h .. 24h apart         : %7d" % middle)
    print("    30 days or more apart   : %7d" % far)
    if not total:
        print("    No same-stem collisions at all on this export - the guard is")
        print("    inert here and the window value cannot change the answer.")
    elif tight and far and middle * 20 < total:
        banner(["BIMODAL: a tight cluster at %d and a far mass at %d, with only"
                % (tight, far),
                "%d in between. A time window separates them cleanly, which"
                % middle,
                "is exactly the case the --stem-collision-window narrowing assumes."])
    elif middle * 20 >= total and total:
        banner(["NOT cleanly bimodal: %d of %d nearest collisions fall in the"
                % (middle, total),
                "1h..24h band, so a window of %s is cutting through a dense"
                % human_duration(res["stem_guard_window"]),
                "region. Lower the window and re-run before trusting the result."])
    else:
        print("    One mode only (tight %d, far %d). Nothing to separate."
              % (tight, far))

    if examples_per_cell and cd["examples"]:
        print("\n  Examples, up to %d per bucket per extension class:"
              % examples_per_cell)
        for label, _upper in COLLISION_BUCKETS:
            for cls in EXT_CLASSES:
                rows = cd["examples"].get("%s|%s" % (label, cls))
                if not rows:
                    continue
                print("    [%s / %s]" % (label, cls))
                for r in rows:
                    print("      delta=%-8s %s" % (r["delta"], r["stem"]))
                    print("        [ALBUM]   %s" % r["album"])
                    print("        [library] %s" % r["library"])


def print_stem_guard_crosscheck(res, out_unc):
    """The safety net, printed whether or not it fires. It caught 5.0.0."""
    cc = res["stem_guard_crosscheck"]
    print("\n" + "=" * 78)
    print("STEM-COLLISION GUARD CROSS-CHECK  (this run vs UNMODIFIED 4.x)")
    print("=" * 78)
    print("  Both classifications are computed on this same export, differing in")
    print("  NOTHING but the same-stem guard, so every difference below is the")
    print("  guard's and the guard's alone.")
    print("  Be clear about what the two MUST-BE-ZERO lines are worth here: by")
    print("  construction 6.0.0 only ever RELAXES a condition 4.x already applied,")
    print("  and it does not touch album membership, so both should be zero")
    print("  structurally rather than luckily. They are computed and printed")
    print("  anyway, as a tripwire: 5.0.0 changed membership, believed it was")
    print("  safe, and this check is what reported 26139 filed photos in its")
    print("  delete list.")
    print("    4.x guard  : %s" % cc["legacy_guard"])
    print("    this run   : %s" % res["stem_guard_label"])
    print("")
    print("  %-58s %7d" % ("orphans under 4.x:", res["legacy_orphans"]))
    print("  %-58s %7d" % ("orphans now:", len(res["orphans"])))
    print("  %-58s %7d" % ("UNCERTAIN under 4.x:", res["legacy_uncertain"]))
    print("  %-58s %7d" % ("UNCERTAIN now:", len(res["uncertain"])))
    print("")
    print("  MUST BE ZERO - items 4.x called FILED that this run calls an ORPHAN")
    print("  %-58s %7d" % ("", cc["legacy_filed_now_orphan"]))
    if cc["legacy_filed_now_orphan"]:
        banner(["*** SAFETY CHECK FAILED: %d item(s) that 4.x classified as"
                % cc["legacy_filed_now_orphan"],
                "FILED IN AN ALBUM are in this run's DELETE list. DO NOT DELETE",
                "ANYTHING. This run is not trustworthy; it exits non-zero."])
        for w in cc["legacy_filed_now_orphan_examples"]:
            print("      %s" % w)
    else:
        print("      OK - no item 4.x protected has become a deletion candidate.")
    print("\n  MUST BE ZERO - items 4.x called an ORPHAN that this run holds back")
    print("  %-58s %7d" % ("", cc["legacy_orphan_now_uncertain"]))
    if cc["legacy_orphan_now_uncertain"]:
        print("      !! UNEXPECTED: the new guard is strictly LOOSER than 4.x's, so")
        print("      !! it cannot withhold something 4.x released. Treat this")
        print("      !! run as broken; it exits non-zero.")
        for w in cc["legacy_orphan_now_uncertain_examples"]:
            print("      %s" % w)
    else:
        print("      OK - the guard only ever loosened, as intended.")
    print("\n  INTENDED DELTA - items 4.x held UNCERTAIN that are now ORPHANS")
    print("  %-58s %7d" % ("", cc["legacy_uncertain_now_orphan"]))
    print("      This is the whole point of 6.0.0: 4.x withheld these on a")
    print("      same-stem collision that is actually filename recycling. By 4.x's")
    print("      reason:")
    released_by_reason = cc["legacy_uncertain_now_orphan_by_legacy_reason"]
    for reason, n in sorted(released_by_reason.items(),
                            key=lambda kv: (-kv[1], kv[0])):
        print("      %7d  %s" % (n, reason))
    if cc["legacy_uncertain_now_orphan_examples"]:
        print("      Examples (spot-check these by url before deleting):")
        for w in cc["legacy_uncertain_now_orphan_examples"][:10]:
            print("        %s" % w)
    print("\n  The items still held back are in %s with their reason." % out_unc)


def main(argv=None):
    started = time.time()
    ap = argparse.ArgumentParser(
        description="Find Google Photos items that are in the library but in no album "
                    "(READ-ONLY, offline). Reads Takeout .zip archives directly.")
    ap.add_argument("inputs", nargs="+", metavar="PATH",
                    help="Takeout .zip archive(s), a directory containing them, an "
                         "already-unzipped export, or any mix. PASS EVERY PART OF THE "
                         "EXPORT IN ONE RUN: a media file's sidecar often lives in a "
                         "different part, and reading parts separately invents false "
                         "orphans.")
    ap.add_argument("-o", "--output", required=True,
                    help="report path, e.g. ~/Desktop/orphans.json "
                         "(a .csv and .uncertain.csv are written alongside)")
    ap.add_argument("--generated-album", action="append", default=[],
                    metavar="NAME",
                    help="declare an album folder auto-generated by Google, so it "
                         "confers NO album membership. Exact folder-name match "
                         "(Unicode-normalized, case-insensitive). Repeatable. "
                         "WARNING: this moves that album's items into the orphan "
                         "(delete) list.")
    ap.add_argument("--generated-albums-file", metavar="FILE",
                    help="file of album folder names to treat as auto-generated, one "
                         "per line; '#' starts a comment")
    ap.add_argument("--generated-album-regex", action="append", default=[],
                    metavar="RE",
                    help="declare auto-generated every album folder whose name FULLY "
                         "matches this case-insensitive regex. Repeatable. Every "
                         "folder matched is printed back for review.")
    ap.add_argument("--include-archived", action="store_true",
                    help="treat Archive-only items as orphans too (default: exclude "
                         "and report separately)")
    ap.add_argument("--library-dir-regex", default=DEFAULT_LIBRARY_DIR_RE,
                    help="regex (matched case-insensitively against the folder name) "
                         "identifying chronological library folders. Default: %(default)s")
    ap.add_argument("--stem-collision-window", default="24h", metavar="DURATION",
                    help="hold a library item back as UNCERTAIN only if an album "
                         "item with the SAME filename stem lies within this much of "
                         "its photoTakenTime. Collisions further out are iPhone "
                         "filename recycling, not the same item, and the item is an "
                         "ORPHAN. Accepts a unit (s/m/h/d/w); 0 disables the guard "
                         "entirely. Default: %(default)s")
    ap.add_argument("--stem-collision-window-ext-mismatch", default="60s",
                    metavar="DURATION",
                    help="the tighter window used when the two sides' extensions "
                         "DISAGREE. Takeout does rewrite extensions (HEIC->JPG), so "
                         "a mismatch is a weaker signal rather than proof of two "
                         "different items - but a real transcode is written in one "
                         "pass, so its timestamps differ by seconds. Clamped never "
                         "to exceed --stem-collision-window. Default: %(default)s")
    ap.add_argument("--collision-pairs-per-stem", type=int, default=64,
                    metavar="N",
                    help="cap on same-stem pairs measured per stem in the collision "
                         "delta survey, so one heavily recycled stem cannot dominate "
                         "it. Truncated stems are reported. Default: %(default)s")
    ap.add_argument("--collision-examples", type=int, default=3, metavar="N",
                    help="example collisions to print per delta bucket per "
                         "extension class (default %(default)s)")
    ap.add_argument("--sample-sidecars", type=int, default=5,
                    help="how many sidecar JSONs to dump full key sets for (default 5)")
    ap.add_argument("--max-samples-per-run", type=int, default=3,
                    help="filenames to print per contiguous run (default 3)")
    ap.add_argument("--no-fold-renditions", action="store_true",
                    help="DIAGNOSTIC: disable Rule 2, so every sidecar-less edit or "
                         "motion rendition becomes its own UNCERTAIN row instead of "
                         "joining its base item")
    ap.add_argument("--verbose", action="store_true",
                    help="print per-folder pairing and folding detail as the scan runs")
    args = ap.parse_args(argv)

    fold_enabled = not args.no_fold_renditions

    try:
        stem_window = parse_duration(args.stem_collision_window)
        stem_ext_window = parse_duration(args.stem_collision_window_ext_mismatch)
    except ValueError as exc:
        sys.exit("error: bad --stem-collision-window value: %s" % exc)
    if args.collision_pairs_per_stem < 1:
        sys.exit("error: --collision-pairs-per-stem must be at least 1")

    try:
        library_re = re.compile(args.library_dir_regex, re.I)
    except re.error as exc:
        sys.exit("error: bad --library-dir-regex: %s" % exc)

    gen_names = list(args.generated_album)
    if args.generated_albums_file:
        gen_names += read_generated_albums_file(args.generated_albums_file)
    gen_regexes = []
    for pat in args.generated_album_regex:
        try:
            gen_regexes.append(re.compile(pat, re.I))
        except re.error as exc:
            sys.exit("error: bad --generated-album-regex %r: %s" % (pat, exc))
    generated = GeneratedSpec(gen_names, gen_regexes)

    out_json = os.path.abspath(os.path.expanduser(args.output))
    if out_json.lower().endswith(".json"):
        base = out_json[:-5]
    else:
        base, out_json = out_json, out_json + ".json"
    out_csv = base + ".csv"
    out_unc = base + ".uncertain.csv"
    out_dir = os.path.dirname(out_json) or "."
    if not os.path.isdir(out_dir):
        sys.exit("error: output directory does not exist: %s" % out_dir)

    zip_paths, dir_specs, problems = resolve_inputs(args.inputs)
    if not zip_paths and not dir_specs:
        msg = ["error: nothing readable in the inputs you gave."]
        msg += ["       %s" % p for p in problems]
        msg.append("       Pass Takeout .zip archives, a directory containing them,")
        msg.append("       or an unzipped tree containing a 'Google Photos' folder.")
        sys.exit("\n".join(msg))

    print("gphotos_find_orphans.py %s" % TOOL_VERSION)
    print("READ-ONLY scan. Nothing in the export is modified; archives are opened")
    print('mode "r" and never rewritten. Only .json entries are ever decompressed.')
    print("No network access.\n")
    print("same-stem collision window           : %s%s"
          % (human_duration(stem_window),
             "   (0 = GUARD DISABLED)" if stem_window <= 0 else ""))
    print("  ... when extensions disagree       : %s"
          % human_duration(min(stem_window, stem_ext_window)))
    if stem_ext_window > stem_window:
        print("  (clamped down from %s: a weaker signal must not reach further)"
              % human_duration(stem_ext_window))
    print("")
    for p in problems:
        print("  note: %s" % p)
    print("Inputs: %d zip archive(s), %d directory tree(s)"
          % (len(zip_paths), len(dir_specs)))
    for p in zip_paths:
        print("  [zip] %s" % p)
    for p, roots in dir_specs:
        print("  [dir] %s" % p)
        for r in roots:
            print("          library root: %s" % r)
    print("")

    with contextlib.ExitStack() as stack:
        sources = []
        # Directories first: they win de-duplication ties, since an extracted copy
        # costs no decompression to read.
        for p, roots in dir_specs:
            sources.append(DirSource(p, roots))
        for p in zip_paths:
            try:
                zs = ZipSource(p)
            except SourceError as exc:
                sys.exit("error: %s" % exc)
            stack.callback(zs.close)
            sources.append(zs)

        zip_sources = [s for s in sources if s.kind == "zip"]
        part_warnings = check_archive_set(zip_sources)

        folders, inv_info = build_inventory(sources)
        scanres = scan(folders, library_re, generated, fold_enabled=fold_enabled,
                       verbose=args.verbose)
        res = compute(scanres, args.include_archived, stem_window, stem_ext_window,
                      max_pairs_per_stem=args.collision_pairs_per_stem,
                      examples_per_cell=args.collision_examples)

        source_warnings = []
        for s in sources:
            source_warnings.extend(s.warnings)
        all_warnings = (part_warnings + inv_info["warnings"] + source_warnings
                        + scanres["errors"])

        elapsed = time.time() - started
        print_source_summary(sources, inv_info, part_warnings, elapsed)

        source_rows = [{
            "label": s.label, "path": s.path, "kind": s.kind,
            "entries": s.entries, "media": s.media, "json": s.jsons,
            "other": s.other, "duplicate_paths_skipped": s.dup_skipped,
            "entries_outside_library": s.skipped_outside,
            "bytes_read": s.bytes_read,
            "bytes_uncompressed_total": s.total_uncompressed,
            "archive_bytes": getattr(s, "archive_bytes", None),
            "part_info": getattr(s, "part_info", None),
            "encoding_counts": {k: s.enc_counts[k] for k in
                                list(ENC_STATUSES) + ["filesystem"]},
        } for s in sources]

        lib = res["library_items"]
        orphans = res["orphans"]
        orphan_keys = set((it.stem, it.ts) for it in orphans)
        gen_map = res["gen_folders_by_key"]

        print_rule_impact(res, scanres, fold_enabled)

        # ---- folders assembled from several archives ---------------------
        print("\n" + "=" * 78)
        print("FOLDERS SPLIT ACROSS ARCHIVES")
        print("=" * 78)
        if scanres["split_folders"]:
            print("  Takeout split these folders mid-way, so their contents (and in")
            print("  some cases a media file and its own sidecar) live in different")
            print("  archives. They were merged into one logical folder BEFORE")
            print("  pairing and before the set difference - which is the only way to")
            print("  avoid inventing false orphans:")
            for sf in sorted(scanres["split_folders"],
                             key=lambda d: (-d["media_files"], d["folder"]))[:40]:
                print("    %6d items  %-40s  %s"
                      % (sf["media_files"], sf["folder"][:40],
                         " + ".join(sf["sources"])))
            if len(scanres["split_folders"]) > 40:
                print("    ... %d more (full list in the JSON report)"
                      % (len(scanres["split_folders"]) - 40))
        else:
            print("  None - every folder came from a single source. (With a")
            print("  multi-part export this is unusual; double-check you passed all")
            print("  the parts.)")

        # ---- folder classification table ---------------------------------
        print("\n" + "=" * 78)
        print("FOLDER CLASSIFICATION  <-- CHECK THIS FIRST. A misclassified folder")
        print("                           invalidates every number below it.")
        print("=" * 78)
        buckets = defaultdict(list)
        for folder, kind, n in scanres["classification"]:
            buckets[kind].append((folder, n))
        for kind in ("library", "album", "generated_album", "archive", "excluded"):
            rows = sorted(buckets.get(kind, []))
            print("\n[%s] %d folder(s), %d media files"
                  % (kind.upper(), len(rows), sum(n for _f, n in rows)))
            if kind == "generated_album":
                if rows:
                    print("    These confer NO album membership: their items are ORPHANS")
                    print("    unless a REAL album also holds them. You declared each one.")
                else:
                    print("    (none declared - every album folder is being treated as a")
                    print("     REAL, user-made album, which is the conservative default)")
            if kind == "album" and rows:
                print("    Full list, so you can decide what is actually yours. Compare")
                print("    the count above with your album count in the Google Photos")
                print("    album picker - a short count means albums were never seen.")
                print("    'sidecar-only' is NORMAL Takeout output, not breakage: those")
                print("    members still count as filed (Rule 1).")
            for folder, n in rows:
                marks = []
                info = scanres["albums"].get(folder)
                if info:
                    if info["shared"]:
                        marks.append("shared album")
                    if info["sidecar_only_members"]:
                        marks.append("%d sidecar-only member(s)"
                                     % info["sidecar_only_members"])
                    if info["generated_media_items"]:
                        marks.append("%d/%d items are Google-generated media"
                                     % (info["generated_media_items"],
                                        info["media_files"]))
                    if not info["has_album_metadata"]:
                        marks.append("no metadata.json")
                    if len(info["sources"]) > 1:
                        marks.append("split across %d archives" % len(info["sources"]))
                mark = ("  [%s]" % "; ".join(marks)) if marks else ""
                print("    %6d  %s%s" % (n, folder, mark))

        # ---- what the user declared --------------------------------------
        if generated.active:
            print("\n" + "=" * 78)
            print("YOUR GENERATED-ALBUM DECLARATIONS (audit trail)")
            print("=" * 78)
            for norm in sorted(generated.names):
                hits = sorted(generated.matched_by_name.get(norm, ()))
                print("  --generated-album %-28s -> %s"
                      % (norm, ", ".join(hits) if hits else "NO FOLDER MATCHED"))
            for rx in generated.regexes:
                hits = sorted(generated.matched_by_regex.get(rx.pattern, ()))
                print("  --generated-album-regex %r matched %d folder(s):"
                      % (rx.pattern, len(hits)))
                for h in hits:
                    print("        %s" % h)
                if not hits:
                    print("        NO FOLDER MATCHED")
            bad_names = generated.unmatched_names()
            bad_res = generated.unmatched_regexes()
            if bad_names or bad_res:
                print("\n  !! WARNING: these declarations matched nothing and had NO effect")
                print("     (typo? wrong spelling? album not in this export?):")
                for n in bad_names:
                    print("        --generated-album %s" % n)
                for p in bad_res:
                    print("        --generated-album-regex %r" % p)

        # ---- the investigation: does the export tell us? -----------------
        survey = survey_album_signals(scanres)
        print("\n" + "=" * 78)
        print("SIGNAL SURVEY: can the export itself identify auto-generated albums?")
        print("=" * 78)
        print("  Takeout documents NO 'auto-generated album' flag. Rather than guess,")
        print("  here is what your %d album folder(s) actually contain:"
              % survey["albums_examined"])
        print("\n  album metadata.json shapes present:")
        for shape, folders_ in sorted(survey["metadata_shapes"].items(),
                                      key=lambda kv: (-len(kv[1]), kv[0])):
            print("    %4d folder(s): %s" % (len(folders_), shape))
            for f in folders_[:6]:
                print("            %s" % f)
            if len(folders_) > 6:
                print("            ... %d more" % (len(folders_) - 6))
        if survey["fields_with_more_than_one_value"]:
            print("\n  fields that take MORE THAN ONE value across your albums")
            print("  (i.e. they do partition the set - a hint, NOT proof of provenance):")
            for field, vals in sorted(survey["fields_with_more_than_one_value"].items()):
                print("    %s:" % field)
                for val, folders_ in sorted(vals.items(), key=lambda kv: -len(kv[1])):
                    print("        %-28s %d folder(s): %s"
                          % (val, len(folders_), ", ".join(sorted(folders_)[:4])
                             + (", ..." if len(folders_) > 4 else "")))
        if survey["verdict"] == "NO_ALBUMS":
            print("\n  >>> No album folders in this export, so nothing to survey. <<<")
        elif survey["verdict"] == "NO_SIGNAL":
            print("")
            banner(["VERDICT: NO DEPENDABLE SIGNAL. Every album folder in your",
                    "export looks structurally identical, so the export cannot",
                    "tell a Google-made album from one you made. Classification",
                    "rests ENTIRELY on what you declare with --generated-album."])
        else:
            print("")
            banner(["VERDICT: differences exist (shown above), but NONE of these",
                    "fields is specified to mean 'auto-generated'. Treat them as",
                    "hints to investigate, not as a classifier. Nothing above has",
                    "changed the orphan math; only your flags do that."])

        # ---- suggestions (opt-in only) -----------------------------------
        suggestions = suggest_generated_albums(scanres)
        print("\n  SUGGESTED CANDIDATES to review (not applied - re-run with the flag if")
        print("  you agree). Based on item contents and known Google collection names.")
        print("  Folders are NEVER suggested for having a person-shaped name: an album")
        print("  you named after someone is normal and looks identical from here.")
        if suggestions:
            for s in suggestions:
                print("    %-40s (%d items)" % (s["name"], s["media_files"]))
                for r in s["reasons"]:
                    print("        - %s" % r)
                print('        to apply: --generated-album "%s"' % s["name"])
        else:
            print("    (no candidates - nothing in your albums matched either signal)")

        # ---- permalink feasibility (prominent) ---------------------------
        pl = permalink_finding(scanres, args.sample_sidecars)
        print("\n" + "#" * 78)
        print("#  PERMALINK FEASIBILITY FINDING  --  decides the design of any later")
        print("#  browser-automation deletion step (per-item URL vs. scrolling the grid)")
        print("#" * 78)
        if pl["verdict"] == "PRESENT":
            print("\n  >>> YES: the sidecar JSON DOES expose a per-item id/URL field. <<<")
            print("      Fields found (count / %% of %d sidecars):"
                  % pl["sidecars_inspected"])
            for k, c in sorted(pl["permalink_or_id_fields_present"].items()):
                print("        %-22s %7d  (%.1f%%)" % (k, c, pl["coverage"][k]))
            print("      => Every orphan and uncertain row carries its item's `url`,")
            print("         in both the JSON and the CSV, so a later deletion pass can")
            print("         navigate straight to each item instead of wrestling with")
            print("         the virtualized grid. Verify one URL by hand first.")
            if max(pl["coverage"].values()) < 95.0:
                print("      !! CAUTION: the best-covered field appears in only %.1f%% of"
                      % max(pl["coverage"].values()))
                print("         sidecars, so a permalink-driven pass would miss the rest.")
                print("         Treat this as PARTIALLY available, not available.")
        else:
            print("\n  >>> NO: the sidecar JSON does NOT expose any per-item id or URL. <<<")
            print("      None of %s were present in any of the %d sidecars inspected."
                  % (", ".join(PERMALINK_KEYS), pl["sidecars_inspected"]))
            print("      => A later deletion pass CANNOT address items by permalink from")
            print("         Takeout data alone. It must work the grid (search by date,")
            print("         then shift-click ranges), which is exactly what the")
            print("         CONTIGUOUS RUNS section below is for.")
            for k in NOT_A_PERMALINK_KEYS:
                if scanres["sidecar_key_counts"].get(k):
                    print("      (`%s` is present in %d sidecars but only records HOW the"
                          % (k, scanres["sidecar_key_counts"][k]))
                    print("       item was uploaded - it is NOT an item id or URL.)")
        if pl["other_url_or_id_like_fields"]:
            print("      Other url/id-ish keys seen: %s"
                  % ", ".join("%s(%d)" % (k, v) for k, v in
                              sorted(pl["other_url_or_id_like_fields"].items())))
        print("\n  Complete key set of %d sample sidecar(s):" % len(pl["samples"]))
        for s in pl["samples"]:
            print("    %s" % s["path"])
            print("      keys: %s" % ", ".join(s["keys"]))
            if s["permalink_values"]:
                print("      id/url values: %s" % json.dumps(s["permalink_values"])[:400])
        print("\n  Union of all sidecar keys seen (key: occurrences):")
        for k, c in scanres["sidecar_key_counts"].most_common():
            print("      %-28s %d" % (k, c))

        # ---- counts ------------------------------------------------------
        ba = res["before_after"]
        n_lib = len(lib)
        n_alb = len(res["album_items"])
        n_gen = len(res["generated_items"])
        n_orp = len(orphans)
        n_unc = len(res["uncertain"])
        n_inalb = len(res["in_album"])
        n_arch = len(res["archived_excluded"])
        n_genonly = len(res["generated_only"])
        live = res["live_pairs"]
        print("\n" + "=" * 78)
        print("COUNTS (logical items; a Live Photo still+motion pair counts as ONE,")
        print("        and an edited rendition counts as part of its base item)")
        print("=" * 78)
        print("  library logical items (chronological folders + archive) : %7d" % n_lib)
        print("  REAL album logical items (distinct, across all albums)  : %7d" % n_alb)
        print("  generated-album logical items (confer no membership)    : %7d" % n_gen)
        print("  library items matched into a REAL album                 : %7d" % n_inalb)
        print("    of those, matched only via a sidecar-only proof       : %7d"
              % ba["items_in_album_only_via_sidecar_proof"])
        print("  ORPHANS (library, in no REAL album)                     : %7d" % n_orp)
        print("    of those, orphans ONLY because of generated albums    : %7d" % n_genonly)
        print("  UNCERTAIN (deliberately NOT classified as orphan)       : %7d" % n_unc)
        print("  archived-only, excluded from orphans%s   : %7d"
              % ("" if not args.include_archived else " (INCLUDED)", n_arch))
        print("  media files in Trash/Bin/Failed folders (ignored)       : %7d"
              % sum(scanres["excluded_counts"].values()))
        total_check = n_inalb + n_orp + n_unc + n_arch
        print("  ---- reconciliation: %d + %d + %d + %d = %d vs library %d  -> %s"
              % (n_inalb, n_orp, n_unc, n_arch, total_check, n_lib,
                 "OK" if total_check == n_lib else "MISMATCH (do not trust this run)"))
        multi = sum(1 for it in res["album_items"].values() if len(it.folders) > 1)
        print("  items appearing in more than one REAL album             : %7d" % multi)
        print("  library items that are Live Photo still+motion pairs    : %7d" % live)
        print("  renditions folded into a base item (Rule 2)             : %7d"
              % ba["renditions_folded"])
        n_url = sum(1 for it in orphans if it.url)
        print("  orphan rows carrying a per-item url                    : %7d of %d"
              % (n_url, n_orp))
        if n_orp and n_url < n_orp:
            print("    (%d orphan(s) have no sidecar, so no permalink exists for them;"
                  % (n_orp - n_url))
            print("     they cannot be included in an automated deletion pass.)")
        print("  ---- the same two numbers under 4.x's wider same-stem guard:")
        print("  orphans under 4.x                                       : %7d"
              % res["legacy_orphans"])
        print("  UNCERTAIN under 4.x                                     : %7d"
              % res["legacy_uncertain"])

        print_collision_deltas(res, args.collision_examples)
        print_stem_guard_crosscheck(res, out_unc)

        # ---- what the generated flags cost -------------------------------
        print("\n" + "!" * 78)
        print("!  GENERATED-ALBUM RECLASSIFICATION COST")
        print("!  What your --generated-album flags added to the DELETE list")
        print("!" * 78)
        gen_cost = Counter()
        for it in res["generated_only"]:
            for folder in gen_map[(it.stem, it.ts)]:
                gen_cost[folder] += 1
        if not generated.active:
            print("\n  You declared nothing generated, so this run cost you nothing: all")
            print("  %d album folder(s) were treated as REAL, user-made albums and every"
                  % len(buckets.get("album", [])))
            print("  item in them is excluded from the orphan list. This is the safe")
            print("  default. Review the album list above and the SUGGESTED CANDIDATES,")
            print("  then re-run with --generated-album for the ones Google made.")
        elif n_genonly == 0:
            print("\n  You declared %d folder(s) generated, but NO item became an orphan"
                  % len(buckets.get("generated_album", [])))
            print("  because of it - every item in them is also in a real album (or was")
            print("  already an orphan, or was held back as UNCERTAIN).")
        else:
            print("")
            banner(["%d item(s) are in the orphan list SOLELY because you" % n_genonly,
                    "declared an album auto-generated. Without those flags they",
                    'would count as "in an album" and would NOT be deleted.'])
            print("\n  Per generated album (an item in two of them is counted in both, so")
            print("  these can sum above %d):" % n_genonly)
            print("  %8s  %s" % ("items", "generated album"))
            for folder, n in sorted(gen_cost.items(), key=lambda kv: (-kv[1], kv[0])):
                print("  %8d  %s" % (n, folder))
            print("\n  Before deleting, open each album above in the Google Photos album")
            print("  picker and confirm it is the Google-made collection you meant.")
            print("  Their items are listed with a non-empty generated_albums column in")
            print("  %s." % out_csv)

        # ---- contiguous runs ---------------------------------------------
        runs = contiguous_runs(lib, orphan_keys)
        legacy_runs = contiguous_runs(lib, res["legacy_orphan_keys"])
        day_ranges = full_orphan_day_ranges(lib, orphan_keys)
        print("\n" + "=" * 78)
        print("CONTIGUOUS RUNS OF ORPHANS  (newest first = Google Photos grid order)")
        print("Each run is a block of orphans with NO keeper between them, so one")
        print("shift-click from the first to the last item selects exactly the run.")
        print("ALWAYS compare the selection count the UI shows against 'items' here.")
        print("=" * 78)
        big = sorted(runs, key=lambda r: -len(r))
        print("\n  Releasing a recycled-filename collision adds an orphan, which")
        print("  either extends a run, bridges two runs into one, or starts a new")
        print("  one - so the run count can move either way. 4.x -> this run:")
        print("  %-40s %7d -> %7d"
              % ("runs (4.x guard -> this run):", len(legacy_runs), len(runs)))
        print("  %-40s %7d -> %7d"
              % ("longest run:",
                 max((len(r) for r in legacy_runs), default=0),
                 max((len(r) for r in runs), default=0)))
        print("  %-40s %7d -> %7d"
              % ("items inside runs of >=10:",
                 sum(len(r) for r in legacy_runs if len(r) >= 10),
                 sum(len(r) for r in runs if len(r) >= 10)))
        print("  %-40s %7d -> %7d"
              % ("runs of exactly 1 item:",
                 sum(1 for r in legacy_runs if len(r) == 1),
                 sum(1 for r in runs if len(r) == 1)))
        print("\n  %d run(s). Largest runs first:" % len(runs))
        print("  %6s  %-12s  %-12s  %s" % ("items", "newest", "oldest", "samples"))
        for r in big[:60]:
            samples = ", ".join(it.display_name for it in r[: args.max_samples_per_run])
            if len(r) > args.max_samples_per_run:
                samples += ", ..."
            print("  %6d  %-12s  %-12s  %s"
                  % (len(r), day(r[0].ts), day(r[-1].ts), samples))
        if len(big) > 60:
            print("  ... %d more runs (full list in the JSON report)" % (len(big) - 60))
        if runs:
            singles = sum(1 for r in runs if len(r) == 1)
            print("\n  runs of 1 item (must be clicked individually): %d" % singles)
            print("  items inside runs of >=10: %d"
                  % sum(len(r) for r in runs if len(r) >= 10))
        print("\n  Calendar days where EVERY library item is an orphan (safe to select")
        print("  by date range in the UI), collapsed into contiguous ranges:")
        if day_ranges:
            for a, b, n in day_ranges[:60]:
                print("      %s .. %s   %d item(s)" % (a, b, n))
            if len(day_ranges) > 60:
                print("      ... %d more ranges (full list in the JSON report)"
                      % (len(day_ranges) - 60))
        else:
            print("      (none - every day containing an orphan also contains a keeper)")

        # ---- sidecar pattern discovery -----------------------------------
        print("\n" + "=" * 78)
        print("SIDECAR NAMING PATTERNS DISCOVERED (not hardcoded)")
        print("=" * 78)
        for pat, n in scanres["patterns"].most_common():
            print("  %8d  %s" % (n, pat))
        print("\n  how each media file / membership proof was resolved:")
        for m, n in scanres["pair_methods"].most_common():
            print("  %8d  %s" % (n, m))

        print("\n  SIDECARS WITH NO MEDIA FILE BESIDE THEM")
        print("    in ALBUM folders  : %6d  <- NORMAL. Takeout often writes an album"
              % len(scanres["sidecar_only_album"]))
        print("                                as sidecars only, with no media copies.")
        print("                                Each one still proves membership and")
        print("                                protects its item from deletion (Rule 1).")
        print("                                Nothing is missing from your export.")
        print("    in LIBRARY folders: %6d  <- worth a look. The item exists but its"
              % len(scanres["sidecar_only_library"]))
        print("                                file was not in any archive you passed,")
        print("                                so it is held back as UNCERTAIN rather")
        print("                                than queued for deletion. A large number")
        print("                                here can mean a missing archive part.")
        for p in scanres["sidecar_only_library"][:10]:
            print("        %s" % p)
        if len(scanres["sidecar_only_library"]) > 10:
            print("        ... %d more (full list in the JSON report)"
                  % (len(scanres["sidecar_only_library"]) - 10))

        if scanres["unpaired"]:
            print("\n  %d media file(s) have no sidecar AND could not be folded into a"
                  % len(scanres["unpaired"]))
            print("  keyed item in their folder (all UNCERTAIN, never orphans):")
            for p in scanres["unpaired"][:15]:
                print("      %s" % p)
            if len(scanres["unpaired"]) > 15:
                print("      ... %d more (full list in the JSON report)"
                      % (len(scanres["unpaired"]) - 15))
        else:
            print("\n  Every media file either paired with a sidecar or folded into a")
            print("  keyed item in its own folder - nothing was left unexplained.")

        # ---- uncertain summary -------------------------------------------
        if res["uncertain"] or res["legacy_uncertain_by_reason"]:
            print("\n" + "=" * 78)
            print("UNCERTAIN ITEMS - NOT in the orphan list, by reason")
            print("=" * 78)
            by_reason = Counter(r for _it, r in res["uncertain"])
            if not by_reason:
                print("  None. Every library item was classified.")
            for reason, n in by_reason.most_common():
                print("  %7d  %s" % (n, reason))
            print("\n  The same breakdown under 4.x's wider same-stem guard, for")
            print("  comparison (every other reason bucket is unaffected by the")
            print("  guard and should be identical):")
            for reason, n in sorted(res["legacy_uncertain_by_reason"].items(),
                                    key=lambda kv: (-kv[1], kv[0])):
                print("  %7d  %s" % (n, reason))
            unaddressable = sum(n for r, n in by_reason.items()
                                if r in UNADDRESSABLE_REASONS)
            if unaddressable:
                print("\n  %d of these have no sidecar at all, so they have neither a"
                      % unaddressable)
                print("  photoTakenTime to key on nor a url to open. They CANNOT take")
                print("  part in an automated, permalink-driven deletion pass - finding")
                print("  them means looking by eye.")
            if by_reason:
                print("  Review %s if you want to adjudicate these by hand; it"
                      % out_unc)
                print("  carries the reason and, where one exists, the item's url.")

        if all_warnings:
            print("\n" + "=" * 78)
            print("WARNINGS (%d)" % len(all_warnings))
            print("=" * 78)
            for w in all_warnings[:40]:
                print("  !! %s" % w)
            if len(all_warnings) > 40:
                print("  ... %d more (full list in the JSON report)"
                      % (len(all_warnings) - 40))

        # ---- write reports -----------------------------------------------
        write_csv(out_csv, [item_row(it, gen_map) for it in orphans], ITEM_HEADER)
        write_csv(out_unc,
                  [item_row(it, gen_map, [reason]) for it, reason in res["uncertain"]],
                  ITEM_HEADER + ["reason_not_classified"])

        elapsed = time.time() - started
        enc_tot = Counter()
        for s in sources:
            enc_tot.update(s.enc_counts)

        report = {
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": "gphotos_find_orphans.py",
            "version": TOOL_VERSION,
            "read_only": True,
            "inputs": list(args.inputs),
            "sources": source_rows,
            "source_totals": {
                "entries": sum(s.entries for s in sources),
                "media": sum(s.media for s in sources),
                "json": sum(s.jsons for s in sources),
                "other": sum(s.other for s in sources),
                "bytes_read": sum(s.bytes_read for s in sources),
                "bytes_uncompressed_total": sum(s.total_uncompressed for s in sources),
                "entries_outside_library": sum(s.skipped_outside for s in sources),
                "encoding_counts": {k: enc_tot[k] for k in
                                    list(ENC_STATUSES) + ["filesystem"]},
                "elapsed_seconds": round(elapsed, 2),
            },
            "inventory": {
                "total_paths": inv_info["total_paths"],
                "duplicate_paths_skipped": inv_info["duplicate_paths_skipped"],
                "duplicate_source_pairs": inv_info["duplicate_source_pairs"],
                "name_collisions": inv_info["name_collisions"],
            },
            "folders_split_across_archives": scanres["split_folders"],
            "options": {
                "include_archived": args.include_archived,
                "library_dir_regex": args.library_dir_regex,
                "fold_renditions": fold_enabled,
                "stem_collision_window_seconds": stem_window,
                "stem_collision_window_ext_mismatch_seconds":
                    res["stem_guard_ext_window"],
                "stem_collision_guard": res["stem_guard_label"],
                "collision_pairs_per_stem": args.collision_pairs_per_stem,
                "generated_albums_declared": sorted(generated.names),
                "generated_album_regexes": [rx.pattern for rx in generated.regexes],
                "generated_declarations_that_matched_nothing": {
                    "names": generated.unmatched_names(),
                    "regexes": generated.unmatched_regexes(),
                },
            },
            "counts": {
                "library_logical_items": n_lib,
                "real_album_logical_items": n_alb,
                "generated_album_logical_items": n_gen,
                "library_items_in_real_album": n_inalb,
                "orphans": n_orp,
                "orphans_solely_due_to_generated_albums": n_genonly,
                "orphans_with_url": n_url,
                "uncertain": n_unc,
                "archived_excluded": n_arch,
                "trash_or_failed_media_files_ignored":
                    sum(scanres["excluded_counts"].values()),
                "items_in_multiple_real_albums": multi,
                "library_live_photo_pairs": live,
                "renditions_folded": ba["renditions_folded"],
                "sidecars_without_media_in_albums":
                    len(scanres["sidecar_only_album"]),
                "sidecars_without_media_in_library":
                    len(scanres["sidecar_only_library"]),
                "media_without_sidecar": len(scanres["unpaired"]),
                "reconciles": total_check == n_lib,
                "orphans_under_legacy_stem_guard": res["legacy_orphans"],
                "uncertain_under_legacy_stem_guard": res["legacy_uncertain"],
                "contiguous_runs": len(runs),
                "contiguous_runs_under_legacy_stem_guard": len(legacy_runs),
                "stem_guard_crosscheck_violations":
                    res["stem_guard_crosscheck"]["violations"],
            },
            "rule_impact_before_after": ba,
            "stem_collision_delta_distribution": res["collision_deltas"],
            "stem_guard_crosscheck": res["stem_guard_crosscheck"],
            "uncertain_by_reason": dict(Counter(r for _i, r in res["uncertain"])),
            "uncertain_by_reason_under_legacy_stem_guard":
                res["legacy_uncertain_by_reason"],
            "rule1_photos_saved_from_deletion": [
                item_dict(it, gen_map) for it in res["rule1_saved_from_deletion"][:500]
            ],
            "generated_album_cost": [
                {"folder": f, "items_made_orphan": n}
                for f, n in sorted(gen_cost.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "generated_album_matches": {
                "by_name": {k: sorted(v) for k, v in generated.matched_by_name.items()},
                "by_regex": {k: sorted(v) for k, v in generated.matched_by_regex.items()},
            },
            "album_provenance_signal_survey": survey,
            "generated_album_suggestions": suggestions,
            "folder_classification": [
                {"folder": f, "kind": k, "media_files": n}
                for f, k, n in sorted(scanres["classification"])
            ],
            "albums": sorted(scanres["albums"].values(), key=lambda a: a["folder"]),
            "sidecar_patterns": dict(scanres["patterns"]),
            "resolution_methods": dict(scanres["pair_methods"]),
            "folded_rendition_examples": scanres["folded_examples"],
            "media_without_sidecar": scanres["unpaired"],
            "sidecars_without_media_in_albums": scanres["sidecar_only_album"],
            "sidecars_without_media_in_library": scanres["sidecar_only_library"],
            "permalink_finding": pl,
            "orphans": [item_dict(it, gen_map) for it in orphans],
            "uncertain": [item_dict(it, gen_map, {"reason": reason})
                          for it, reason in res["uncertain"]],
            "contiguous_runs": [
                {
                    "items": len(r), "newest_date": day(r[0].ts),
                    "oldest_date": day(r[-1].ts),
                    "newest_title": r[0].display_name,
                    "oldest_title": r[-1].display_name,
                    "titles": [it.display_name for it in r],
                    "urls": [it.url for it in r],
                } for r in runs
            ],
            "fully_orphan_day_ranges": [
                {"start": a, "end": b, "items": n} for a, b, n in day_ranges
            ],
            "warnings": all_warnings,
        }
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False, sort_keys=False)

        print("\nWrote:")
        print("  %s   (full report)" % out_json)
        print("  %s   (%d orphan rows, %d with a url, %d only because of "
              "generated albums)" % (out_csv, n_orp, n_url, n_genonly))
        print("  %s   (%d uncertain rows)" % (out_unc, n_unc))
        print("\nRead %s from %d source(s) in %.1fs; media was never decompressed."
              % (human_bytes(sum(s.bytes_read for s in sources)), len(sources),
                 elapsed))
        print("\nDO NOT DELETE ANYTHING YET. Run the sanity checks in the header comment")
        print("of this script (section 'HOW TO SANITY-CHECK') first - especially")
        print("confirming every archive part is listed, that the album count matches")
        print("the Google Photos album picker, the STEM-COLLISION GUARD CROSS-CHECK")
        print("reading zero, and the 10-random-orphans spot check using each")
        print("row's url.")
        violations = res["stem_guard_crosscheck"]["violations"]
        if total_check != n_lib:
            return 2
        if violations:
            print("\nExiting 3: the same-stem guard cross-check found %d"
                  " violation(s)." % violations)
            return 3
        return 0


if __name__ == "__main__":
    sys.exit(main())
