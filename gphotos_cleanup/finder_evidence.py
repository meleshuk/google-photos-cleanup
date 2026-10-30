"""Resolve duplicate album membership using upload origin and optional hashes."""

from .finder_sources import extract_upload_mechanism, zipfile


def _hint(entry):
    if entry is None:
        return None
    return (entry.size, getattr(entry.locator, "CRC", None))


def _primary_entries(item):
    return [rec.entry for rec in item.files if not rec.fold_kind]


def _can_match(left, right):
    """Cheap size/CRC rejection; positive membership still requires SHA-256."""
    if left is None or right is None:
        return True
    if left[0] != right[0]:
        return False
    return left[1] is None or right[1] is None or left[1] == right[1]


def _hash_choices(proof, candidates, warnings):
    """Narrow a proof only when at least one library file positively matches.

    Missing/unreadable files remain possible members. Re-encoded album copies
    that match no library file retain the original ambiguity.
    """
    entry = proof.entry
    if entry is None:
        return None
    hint = _hint(entry)
    primaries = [(item, _primary_entries(item)) for item in candidates]
    if not any(_can_match(hint, _hint(e)) for _it, entries in primaries for e in entries):
        return None
    # Equal ZIP size+CRC for every candidate cannot usefully distinguish them.
    # Keeping them uncertain avoids decompressing duplicate media unnecessarily.
    if hint[1] is not None and all(
        len(entries) == 1 and _hint(entries[0]) == hint for _it, entries in primaries
    ):
        return None

    def digest(media_entry):
        if media_entry is None:
            return None
        try:
            return media_entry.source.hash_media(media_entry)
        except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
            warnings.append("duplicate hash unavailable for %s: %s"
                            % (media_entry.relpath, type(exc).__name__))
            return None

    target = digest(entry)
    if target is None:
        return None
    matched, unknown = [], []
    for item, entries in primaries:
        has_match, has_unknown = False, not entries
        for media_entry in entries:
            if not _can_match(hint, _hint(media_entry)):
                continue
            value = digest(media_entry)
            has_match = has_match or value == target
            has_unknown = has_unknown or value is None
        if has_match:
            matched.append(item)
        elif has_unknown:
            unknown.append(item)
    return matched + unknown if matched else None


def resolve_duplicate_membership(library_items, album_items, hash_media=False):
    """Return exact-key verdicts, retaining every unresolved album proof.

    Upload mechanism distinguishes copies; it does not itself make one an
    orphan. Every album proof must have a possible library match before a
    duplicate outside those possibilities can be released.
    """
    by_key = {}
    for item in library_items.values():
        by_key.setdefault((item.stem, item.ts), []).append(item)
    decisions, warnings = {}, []
    for key, siblings in by_key.items():
        album = album_items.get(key)
        if len(siblings) < 2 or album is None:
            continue
        possible, certain, methods = set(), set(), set()
        broken = not any(not proof.fold_kind for proof in album.files)
        for proof in album.files:
            if proof.fold_kind:
                continue
            origin = extract_upload_mechanism(proof.data)
            choices = [item for item in siblings
                       if origin is None or item.upload_mechanism is None
                       or item.upload_mechanism == origin]
            if not choices:
                # Contradictory origin metadata cannot justify deletion.
                broken = True
                break
            hint = _hint(proof.entry)
            if hint is not None and hint[1] is not None and len(choices) < len(siblings):
                chosen_entries = [entry for item in choices for entry in _primary_entries(item)]
                excluded_entries = [entry for item in siblings if item not in choices
                                    for entry in _primary_entries(item)]
                if (chosen_entries and all(_hint(entry) is not None for entry in chosen_entries)
                        and not any(_can_match(hint, _hint(entry)) for entry in chosen_entries)
                        and any(_hint(entry) == hint for entry in excluded_entries)):
                    # The media hints contradict the proposed origin-only match.
                    broken = True
                    break
            if len(choices) < len(siblings):
                methods.add("upload_mechanism")
            if hash_media and len(choices) > 1 and not proof.sidecar_only:
                narrowed = _hash_choices(proof, choices, warnings)
                if narrowed is not None and len(narrowed) < len(choices):
                    methods.add("sha256")
                    choices = narrowed
            ids = {item.identity for item in choices}
            possible.update(ids)
            if len(ids) == 1:
                certain.update(ids)
        for item in siblings:
            if broken:
                verdict = "uncertain"
            elif item.identity in certain:
                verdict = "in_album"
            elif item.identity in possible:
                verdict = "uncertain"
            else:
                verdict = "orphan"
            decisions[item.identity] = {
                "verdict": verdict,
                "methods": sorted(methods) if not broken else [],
            }
    return decisions, warnings
