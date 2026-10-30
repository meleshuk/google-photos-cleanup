"""Logical Google Photos items and conservative orphan classification."""

from .finder_inventory import *
from .finder_evidence import resolve_duplicate_membership

# ---------------------------------------------------------------------------
# Logical items + set difference
# ---------------------------------------------------------------------------

class Item(object):
    """One Google Photos item: every file and membership proof that keys to it."""

    __slots__ = ("stem", "ts", "files", "folders", "identity", "duplicate_identity",
                 "ambiguous_motion", "membership_resolution")

    def __init__(self, stem, ts):
        self.stem = stem
        self.ts = ts
        self.files = []
        self.folders = set()
        self.identity = (stem, ts, None, None)
        self.duplicate_identity = False
        self.ambiguous_motion = False
        self.membership_resolution = None

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
    def upload_mechanism(self):
        mechanisms = {extract_upload_mechanism(f.data) for f in self.files}
        return next(iter(mechanisms)) if len(mechanisms) == 1 else None

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


def _paired_motion_companions(left, right):
    """Recognize two keyed halves using file names and upload metadata.

    Folded companions already inherit their base's URL. Two independent sidecars
    need stronger evidence than just the same title stem and capture second.
    """
    left_base = [f for f in left.files if not f.fold_kind]
    right_base = [f for f in right.files if not f.fold_kind]
    if len(left_base) != 1 or len(right_base) != 1:
        return False
    a, b = left_base[0], right_base[0]
    if a.sidecar_only or b.sidecar_only:
        return False
    if not ((a.ext in IMAGE_EXTS and b.ext in VIDEO_EXTS)
            or (b.ext in IMAGE_EXTS and a.ext in VIDEO_EXTS)):
        return False
    # Keep Takeout's numeric dedup suffix here: IMG_1(1) is not IMG_1.
    a_name = os.path.splitext(nfc(a.name).lower())[0]
    b_name = os.path.splitext(nfc(b.name).lower())[0]
    a_created = ((a.data or {}).get("creationTime") or {}).get("timestamp")
    b_created = ((b.data or {}).get("creationTime") or {}).get("timestamp")
    a_origin, b_origin = extract_upload_mechanism(a.data), extract_upload_mechanism(b.data)
    return (not (a_origin and b_origin and a_origin != b_origin)
            and a.folder == b.folder and a_name == b_name
            and a_created is not None and a_created == b_created)


def group_library_items(recs):
    """Preserve library addresses while using stem/time only for membership.

    Multiple still images can share the original name and capture second. Their
    distinct library URLs must survive into separate output rows. Album URLs are
    per-copy and cannot decide which duplicate is filed, so those collisions are
    marked for conservative classification.
    """
    items = {}
    for aggregate in group_items(recs).values():
        by_address = {}
        for rec in aggregate.files:
            address = rec.url or ("unaddressed:" + rec.relpath)
            if address not in by_address:
                by_address[address] = Item(rec.stem, rec.ts)
            by_address[address].add(rec)
        groups = list(by_address.values())
        # With no permalink evidence, retain the conservative legacy grouping.
        urls = {f.url for f in aggregate.files if f.url}
        if len(groups) == 1 or not urls:
            groups = [aggregate]
        else:
            # Merge only unambiguous one-to-one still/motion companions.
            partners = {
                i: [j for j, other in enumerate(groups) if j != i
                    and _paired_motion_companions(group, other)]
                for i, group in enumerate(groups)
            }
            merged, consumed = [], set()
            for i, group in enumerate(groups):
                if i in consumed:
                    continue
                candidates = partners[i]
                if len(candidates) == 1 and partners[candidates[0]] == [i]:
                    j = candidates[0]
                    other = groups[j]
                    # Prefer the still's permalink as the Live Photo address.
                    if other.exts & IMAGE_EXTS:
                        group, other = other, group
                    for rec in other.files:
                        group.add(rec)
                    consumed.add(j)
                consumed.add(i)
                merged.append(group)
            groups = merged
        collision = len(groups) > 1
        has_still = any(group.name_exts & IMAGE_EXTS for group in groups)
        has_motion = any(group.name_exts & VIDEO_EXTS and not group.is_live_pair
                         for group in groups)
        for group in groups:
            discriminator = group.url
            if collision and discriminator is None:
                discriminator = "unaddressed:" + group.files[0].relpath
            group.identity = (group.stem, group.ts, group.upload_mechanism, discriminator)
            group.duplicate_identity = collision
            group.ambiguous_motion = collision and has_still and has_motion
            items[group.identity] = group
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
        self.album_times = defaultdict(set)
        for stem, ts in album_items:
            self.album_times[stem].add(ts)

    def verdict(self, _it, stem, _ts, ignore_exact=False):
        if ignore_exact:
            return REASON_STEM_DIFF_TS if self.album_times[stem] - {_ts} else None
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

    def verdict(self, it, stem, ts, ignore_exact=False):
        candidates = self.by_stem.get(stem)
        if not candidates or self.window <= 0:
            return None
        lib_exts = frozenset(it.name_exts)
        weak = None
        for album_ts, album_exts in candidates:
            if ignore_exact and album_ts == ts:
                continue
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
    for it in library_items.values():
        stem, ts = it.stem, it.ts
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
        return {it.identity for it in items}

    legacy_filed = keys(legacy["in_album"])
    legacy_orphan = keys(legacy["orphans"])
    legacy_uncertain = {it.identity for it, _r in legacy["uncertain"]}
    new_orphan_items = {it.identity: it for it in new["orphans"]}
    new_orphan = set(new_orphan_items)
    new_uncertain = {it.identity: r for it, r in new["uncertain"]}

    filed_now_orphan = sorted(legacy_filed & new_orphan, key=repr)
    orphan_now_held = sorted(legacy_orphan & set(new_uncertain), key=repr)
    released = sorted(legacy_uncertain & new_orphan,
                      key=lambda k: (-(k[1] or 0), k[0]))
    legacy_reason = {it.identity: r for it, r in legacy["uncertain"]}
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
                     gen_folders_by_key, stem_guard, membership_decisions=None):
    """The funnel: every library item becomes in_album / orphan / uncertain /
    archived_excluded, biased so that anything doubtful is never an orphan.

    Factored out so the caller can run it again with a DIFFERENT album-key set, or a
    DIFFERENT same-stem guard, and measure what an earlier rule would have produced.
    `stem_guard` must therefore be built from the same album items as `album_keys`.
    """
    sorted_album_stems = sorted({k[0] for k in album_keys})

    orphans, uncertain, in_album, archived_excluded = [], [], [], []
    generated_only = []

    for it in library_items.values():
        stem, ts = it.stem, it.ts
        key = (stem, ts)
        decision = (membership_decisions or {}).get(it.identity)
        if it.duplicate_identity and key in album_keys:
            if decision is None or decision["verdict"] == "uncertain":
                uncertain.append((it, REASON_DUPLICATE_ALBUM_IDENTITY))
                continue
            if decision["verdict"] == "in_album":
                in_album.append(it)
                continue
        if it.ambiguous_motion:
            uncertain.append((it, REASON_AMBIGUOUS_MOTION_IDENTITY))
            continue
        if key in album_keys and not decision:
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
        collision = stem_guard.verdict(it, stem, ts, ignore_exact=bool(decision))
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
    Album keys represent membership evidence; library keys also carry their
    address discriminator so distinct items cannot disappear from the output.
    """
    album_items = group_items([f for f in files if f.scope == "album"])
    gen_items = group_items([f for f in files if f.scope == "generated"])
    archive_items = group_items([f for f in files if f.scope == "archive"])
    library_items = group_library_items([f for f in files if f.scope == "library"])
    library_keys = {(it.stem, it.ts) for it in library_items.values()}
    for key, it in archive_items.items():
        if key not in library_keys:
            for identity, archive_it in group_library_items(it.files).items():
                library_items[identity] = archive_it
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
            max_pairs_per_stem=64, examples_per_cell=3, hash_duplicate_media=False):
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
    decisions, evidence_warnings = resolve_duplicate_membership(
        library_items, album_items, hash_media=hash_duplicate_media)
    for item in library_items.values():
        item.membership_resolution = decisions.get(item.identity)
    res = classify_library(library_items, album_keys, archive_keys,
                           include_archived, gen_folders_by_key, guard, decisions)
    # Rule 1 replay: membership from media+sidecar pairs only.
    pair_decisions, _ = resolve_duplicate_membership(
        library_items, album_items_pairs_only, hash_media=hash_duplicate_media)
    cf_rule1 = classify_library(library_items, album_keys_pairs_only,
                                archive_keys, include_archived,
                                gen_folders_by_key,
                                StemCollisionGuard(album_items_pairs_only,
                                                   stem_window, stem_ext_window),
                                pair_decisions)
    # 4.x replay: identical in every respect EXCEPT the same-stem guard, so the
    # difference is attributable to the guard alone and nothing else. Both
    # replays use the same validated duplicate-membership evidence.
    legacy = classify_library(library_items, album_keys, archive_keys,
                              include_archived, gen_folders_by_key,
                              LegacyStemGuard(album_items), decisions)

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
        unfolded_decisions, _ = resolve_duplicate_membership(
            uf_lib, uf_album, hash_media=hash_duplicate_media)
        cf_rule2 = classify_library(uf_lib, set(uf_album.keys()),
                                    set(uf_arch.keys()), include_archived,
                                    gen_folders_by_key,
                                    StemCollisionGuard(uf_album, stem_window,
                                                       stem_ext_window),
                                    unfolded_decisions)
        live_before = sum(1 for it in uf_lib.values() if it.is_live_pair)
        unc_before = len(cf_rule2["uncertain"])
        orph_before = len(cf_rule2["orphans"])
    else:
        live_before, unc_before = live_now, len(res["uncertain"])
        orph_before = len(res["orphans"])

    res.update({
        "identity_resolution": {
            "library_membership_keys": len({(it.stem, it.ts)
                                            for it in library_items.values()}),
            "library_items": len(library_items),
            "duplicate_membership_keys": len({(it.stem, it.ts)
                for it in library_items.values() if it.duplicate_identity}),
            "duplicate_items_with_album_evidence": sum(
                reason == REASON_DUPLICATE_ALBUM_IDENTITY
                for _it, reason in res["uncertain"]),
            "ambiguous_motion_items": sum(
                reason == REASON_AMBIGUOUS_MOTION_IDENTITY
                for _it, reason in res["uncertain"]),
            "orphan_duplicates_resolved_by_mechanism": sum(
                it.membership_resolution is not None
                and "upload_mechanism" in it.membership_resolution["methods"]
                for it in res["orphans"]),
            "orphan_duplicates_resolved_by_sha256": sum(
                it.membership_resolution is not None
                and "sha256" in it.membership_resolution["methods"]
                for it in res["orphans"]),
        },
        "evidence_warnings": evidence_warnings,
        "stem_guard_label": guard.label,
        "stem_guard_window": stem_window,
        "stem_guard_ext_window": guard.ext_window,
        "stem_guard_crosscheck": crosscheck_stem_guard(legacy, res),
        "legacy_orphans": len(legacy["orphans"]),
        "legacy_orphan_keys": {it.identity for it in legacy["orphans"]},
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
        if it.identity in orphan_keys:
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
