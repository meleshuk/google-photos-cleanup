"""Finder surveys, reports, and command-line orchestration."""

from .finder_inventory import *
from .finder_matching import *

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
        it.upload_mechanism or "unknown",
        (json.dumps(it.membership_resolution, sort_keys=True)
         if it.membership_resolution else ""),
    ] + list(extra)


ITEM_HEADER = ["title", "date", "taken_time_utc", "taken_time_epoch", "url",
               "live_photo_pair", "file_count", "extensions", "folded_renditions",
               "generated_albums", "takeout_paths", "source_archives",
               "upload_mechanism", "duplicate_membership_resolution"]


def item_dict(it, gen_folders_by_key, extra=None):
    folded = [f for f in it.files if f.fold_kind]
    d = {
        "title": it.display_name,
        "date": day(it.ts),
        "taken_time_utc": iso(it.ts),
        "taken_time_epoch": it.ts,
        "url": it.url,
        "upload_mechanism": it.upload_mechanism,
        "duplicate_membership_resolution": it.membership_resolution,
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
                    "skipped_outside", "bytes_read", "total_uncompressed",
                    "media_bytes_read", "media_files_hashed"):
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
          % human_bytes(max(tot["total_uncompressed"] - tot["bytes_read"]
                            - tot["media_bytes_read"], 0)))
    print("    media bytes READ for duplicate hashes           : %s"
          % human_bytes(tot["media_bytes_read"]))
    print("    media files hashed                              : %d"
          % tot["media_files_hashed"])
    zbytes = sum(getattr(s, "archive_bytes", 0) for s in sources)
    if zbytes:
        print("    archive size on disk                           : %s"
              % human_bytes(zbytes))
    print("    elapsed                                        : %.1fs" % elapsed)
    if not tot["media_bytes_read"]:
        print("    => media entries were not decompressed; no extra disk is needed.")
    else:
        print("    => selected duplicate media were streamed for hashing; no extraction.")

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
    ap.add_argument("--hash-duplicate-media", action="store_true",
                    help="stream selected ambiguous library/album media for SHA-256 "
                         "comparison; default is metadata-only. Identical or missing "
                         "media remain ambiguous unless upload mechanism resolves them")
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
    print('mode "r" and never rewritten.')
    print("Duplicate media hashing: %s" % ("enabled" if args.hash_duplicate_media
                                           else "disabled (JSON-only scan)"))
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
                      examples_per_cell=args.collision_examples,
                      hash_duplicate_media=args.hash_duplicate_media)

        source_warnings = []
        for s in sources:
            source_warnings.extend(s.warnings)
        all_warnings = (part_warnings + inv_info["warnings"] + source_warnings
                        + scanres["errors"] + res["evidence_warnings"])

        elapsed = time.time() - started
        print_source_summary(sources, inv_info, part_warnings, elapsed)

        source_rows = [{
            "label": s.label, "path": s.path, "kind": s.kind,
            "entries": s.entries, "media": s.media, "json": s.jsons,
            "other": s.other, "duplicate_paths_skipped": s.dup_skipped,
            "entries_outside_library": s.skipped_outside,
            "bytes_read": s.bytes_read + s.media_bytes_read,
            "json_bytes_read": s.bytes_read,
            "media_bytes_read": s.media_bytes_read,
            "media_files_hashed": s.media_files_hashed,
            "bytes_uncompressed_total": s.total_uncompressed,
            "archive_bytes": getattr(s, "archive_bytes", None),
            "part_info": getattr(s, "part_info", None),
            "encoding_counts": {k: s.enc_counts[k] for k in
                                list(ENC_STATUSES) + ["filesystem"]},
        } for s in sources]

        lib = res["library_items"]
        orphans = res["orphans"]
        orphan_keys = {it.identity for it in orphans}
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
        identity_info = res["identity_resolution"]
        print("  repeated stem/time keys split by library permalink      : %7d"
              % identity_info["duplicate_membership_keys"])
        print("  duplicate items withheld due to album evidence          : %7d"
              % identity_info["duplicate_items_with_album_evidence"])
        print("  duplicate orphans resolved by upload mechanism          : %7d"
              % identity_info["orphan_duplicates_resolved_by_mechanism"])
        print("  duplicate orphans resolved by SHA-256                    : %7d"
              % identity_info["orphan_duplicates_resolved_by_sha256"])
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
                "bytes_read": sum(s.bytes_read + s.media_bytes_read for s in sources),
                "json_bytes_read": sum(s.bytes_read for s in sources),
                "media_bytes_read": sum(s.media_bytes_read for s in sources),
                "media_files_hashed": sum(s.media_files_hashed for s in sources),
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
                "hash_duplicate_media": args.hash_duplicate_media,
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
            "identity_resolution": res["identity_resolution"],
            "duplicate_membership_evidence": [
                {"title": it.display_name, "url": it.url,
                 "upload_mechanism": it.upload_mechanism,
                 "resolution": it.membership_resolution}
                for it in lib.values() if it.membership_resolution is not None
            ],
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
        print("\nRead %s from %d source(s) in %.1fs."
              % (human_bytes(sum(s.bytes_read + s.media_bytes_read for s in sources)), len(sources),
                 elapsed))
        print("\nRun the sanity checks in docs/finder-design.md")
        print("(section 'HOW TO SANITY-CHECK') first - especially")
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
