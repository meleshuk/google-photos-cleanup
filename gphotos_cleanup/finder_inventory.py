"""Global inventory construction, sidecar pairing, and folder scanning."""

from .finder_sources import *

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
