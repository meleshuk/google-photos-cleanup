# Finder design and safety notes

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
