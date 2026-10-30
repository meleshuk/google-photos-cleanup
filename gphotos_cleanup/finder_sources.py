"""Takeout source resolution, ZIP decoding, constants, and shared helpers."""

import argparse
import bisect
import contextlib
import csv
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

TOOL_VERSION = "6.2.0"

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
UPLOAD_MECHANISMS = {"mobileUpload", "webUpload", "picasa", "composition",
                     "fromSharedAlbum", "driveSync"}

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
REASON_DUPLICATE_ALBUM_IDENTITY = (
    "distinct library items share a title stem and capture timestamp with album "
    "evidence; cannot determine which duplicate belongs to the album")
REASON_AMBIGUOUS_MOTION_IDENTITY = (
    "distinct still and video URLs share a title stem and capture timestamp "
    "without enough evidence to identify a Live Photo companion")
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


def extract_upload_mechanism(data):
    """The known Takeout upload mechanism, or None if absent/ambiguous.

    Device type is deliberately excluded: phone and tablet uploads both use the
    same mechanism. Unknown or malformed origin data must never disprove album
    membership.
    """
    origin = data.get("googlePhotosOrigin") if isinstance(data, dict) else None
    if not isinstance(origin, dict) or len(origin) != 1:
        return None
    mechanism = next(iter(origin))
    if mechanism not in UPLOAD_MECHANISMS or not isinstance(origin[mechanism], dict):
        return None
    return mechanism


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
        self.media_bytes_read = 0
        self.media_files_hashed = 0
        self._media_hashes = {}
        self.total_uncompressed = 0
        self.dup_skipped = 0
        self.warnings = []

    def close(self):
        pass

    def hash_media(self, entry):
        """Stream a requested media file for the optional duplicate resolver."""
        if not is_media_filename(entry.name):
            raise AssertionError("refusing to hash non-media: %s" % entry.relpath)
        key = entry.relpath
        if key in self._media_hashes:
            return self._media_hashes[key]
        digest = hashlib.sha256()
        if self.kind == "zip":
            stream = self.zf.open(entry.locator, "r")
        elif self.kind == "dir":
            stream = open(entry.locator, "rb")
        else:
            raise AssertionError("unsupported media source: %s" % self.kind)
        with stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                self.media_bytes_read += len(chunk)
                digest.update(chunk)
        self.media_files_hashed += 1
        self._media_hashes[key] = digest.hexdigest()
        return self._media_hashes[key]


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
    free. Default scans decompress .json only: `read_json` refuses anything else.
    The separate `hash_media` path is used only by the optional duplicate resolver.
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
