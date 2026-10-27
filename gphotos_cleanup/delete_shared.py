"""Deletion constants, validated input models, and the durable journal."""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VERSION = "1.3.0"

# How long after a first Ctrl-C a SECOND one still means "stop the run" rather
# than "skip this cooldown" (GUARDRAIL 10, v1.2.3). Long enough for a deliberate
# double press, short enough that resuming still feels immediate.
COOLDOWN_SKIP_WINDOW_SECONDS = 2.5

# GUARDRAIL 1: the only URL shape this tool will ever navigate to or act on.
PERMALINK_RE = re.compile(r"^https://photos\.google\.com/photo/AF1Qip[A-Za-z0-9_\-]+$")
PHOTO_PREFIX = "https://photos.google.com/photo/"

DEFAULT_INPUT = "~/Desktop/orphans.csv"
DEFAULT_JOURNAL = "~/Desktop/gphotos_delete_journal.jsonl"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"

# Journal statuses that mean "this URL needs no further action, ever".
TERMINAL_STATUSES = frozenset({"deleted", "already_gone"})

CONFIRM_PHRASE = "delete my orphans"

# Text that indicates the viewer is showing "this item does not exist / is gone /
# is sitting in Trash". A trashed permalink does NOT reliably 404: Google may
# redirect, may show a not-available page, or may show the item inside a trash
# view with Restore / Delete permanently affordances. All of those mean gone.
GONE_TEXT_PATTERNS = (
    re.compile(r"no longer\s+available", re.I),
    re.compile(r"item (?:is )?(?:not|no longer) (?:available|found)", re.I),
    re.compile(r"(?:photo|video|item) (?:was )?not found", re.I),
    re.compile(r"couldn'?t (?:find|load) (?:this|that) (?:photo|video|item)", re.I),
    re.compile(r"(?:this item is in|moved to) (?:the )?(?:trash|bin)", re.I),
    re.compile(r"page (?:isn'?t|is not) available", re.I),
    re.compile(r"delete permanently", re.I),
    re.compile(r"will be (?:permanently )?deleted (?:in|after|on)\b", re.I),
)

# The ordinary single-item confirm button, INSIDE the dialog. Anchored, because
# an unanchored "move to trash" also matches the viewer TOOLBAR button - and
# clicking that one re-opens the dialog instead of confirming it. The anchoring
# is belt-and-braces: candidates are already scoped to the dialog element.
TRASH_BUTTON_NAME_RE = re.compile(
    r"^\s*(?:move to (?:trash|bin)|delete|trash)\s*$", re.I
)

# POSITIVE success evidence: the post-action confirmation toast/snackbar. Google
# shows a "Moved to trash" style notification carrying an Undo affordance. Seeing
# it means the action landed, whatever the viewer DOM happens to say next.
TRASHED_TOAST_PATTERNS = (
    re.compile(r"moved \d+ items? to (?:the )?(?:trash|bin)", re.I),
    re.compile(r"\d+ items? moved to (?:the )?(?:trash|bin)", re.I),
    re.compile(r"moved to (?:the )?(?:trash|bin)", re.I),
    re.compile(r"(?:photo|video|item) (?:moved to|in) (?:the )?(?:trash|bin)", re.I),
)
UNDO_BUTTON_NAME_RE = re.compile(r"^undo$", re.I)

# Affordances that only exist for an item that is ALREADY in Trash.
TRASHED_STATE_SELECTORS = (
    '[aria-label*="Restore" i]',
    '[aria-label*="Delete permanently" i]',
    '[aria-label*="Delete from trash" i]',
    '[aria-label*="Delete from bin" i]',
)

# ------------------------------------------------------------------------- #
# The delete confirmation dialog, in its TWO forms. READ THE SAFETY NOTE.
# ------------------------------------------------------------------------- #
# Google Photos shows one of two dialogs after the trash shortcut, and v1.2.0 got
# this wrong in a way that broke every deletion. Both forms carry the SAME
# heading, so the heading cannot be used to tell them apart:
#
#     Remove from your Google Account, devices with backup turned on, and places
#     shared within Google Photos?
#
#   ORDINARY (a single item):         Cancel  |  Move to trash
#   GROUPED/STACKED (a burst or       All 8 photos  |  Current photo only
#   similar-photo group):
#
# v1.2.0 treated that shared heading as proof of a stack, so EVERY item looked
# stacked, no item could be answered, and a real run aborted after 3 attempts
# with 0 deletions. The discriminator is the OPTIONS, never the heading.
#
# *** GUARDRAIL 1 DECIDES THE STACKED CASE, AND IT IS NOT NEGOTIABLE: only ever
# *** "Current photo only". The other N-1 items in a stack are SEPARATE library
# *** items that are NOT in the input file, and some of them are very likely
# *** filed in the user's albums - they are not orphans at all. Clicking
# *** "All N photos" would delete items this tool was never authorised to touch,
# *** destroying the one property the whole design rests on.
#
# If neither a "Current photo only" nor a "Move to trash" option is found inside
# the dialog, this script clicks NOTHING, dismisses the dialog, and fails the
# item. A missed deletion costs a retry; a wrong click costs the user photos they
# meant to keep.
#
# Kept only as a WEAK corroborating signal for the logs. Never a discriminator:
# the ordinary single-item dialog shows exactly the same heading.
STACKED_DIALOG_TEXT_PATTERNS = (
    re.compile(r"remove from your google account", re.I),
    re.compile(r"devices with backup turned on", re.I),
    re.compile(r"places shared within google photos", re.I),
)

# Where a dialog's options may be read from. Candidates MUST come from inside one
# of these: the page at large also holds a "Move to trash" toolbar button, the
# nav rail, the search box and the Google apps menu, and clicking the toolbar
# button would re-open the dialog rather than confirm it.
#
# BUG A (v1.2.2): the real container does not reliably carry ANY of these
# markers, which is one half of why the stacked option was never found. The list
# is wider than it was, and a selector only counts as a scope if the container it
# finds actually holds an option row (see _dialog_scope); when none of them does,
# the container is derived from the heading instead
# (_dialog_scope_from_heading). Deliberately no Google-specific class names or
# jsname attributes: those are obfuscated and change without notice.
DIALOG_SCOPE_SELECTORS = (
    '[role="alertdialog"]',
    '[role="dialog"]',
    '[aria-modal="true"]',
    "[aria-modal]",
    "dialog[open]",
    "dialog",
)

# A JS predicate for "is this node inside a dialog?", built from the same
# selector list. Used to keep the TOOLBAR fallback (FIX 1, v1.3.0) strictly
# outside any dialog: the dialog's own confirm button must only ever be reached
# through the decide_dialog_action() path, never as a "toolbar" control.
INSIDE_DIALOG_JS = (
    "element => !!element.closest(%s)"
    % json.dumps(", ".join(DIALOG_SCOPE_SELECTORS))
)

# ------------------------------------------------------------------------- #
# FIX 1 (v1.3.0): the '#' shortcut silently does nothing for some items
# ------------------------------------------------------------------------- #
# The v1.2.2 production run failed its first three items with `mutation-no-op`
# and - tellingly - NO stacked-dialog classification, NO "All N photos" seen and
# NO dump: there was no dialog on screen to detect at all. The same permalink's
# dialog appeared normally when the user opened the item BY HAND. So for those
# items the keystroke never reaches the viewer and no dialog ever opens, which
# looks exactly like a silently-ignored delete.
#
# The fallback is the viewer toolbar's own "Move to trash" control, which the
# v1.2.0 page dump proved exists. It is pressed ONLY when no dialog appeared
# after the shortcut, and it is matched by accessible name, never by position.
#
# *** SAFETY. This control lives OUTSIDE the dialog, and that is enforced
# *** (_toolbar_trash_control refuses any candidate inside a dialog container):
# *** clicking the dialog's own button from here would bypass
# *** decide_dialog_action(), which is the only thing that keeps "All N photos"
# *** from being clicked on a stacked item. The toolbar press OPENS a dialog;
# *** answering it still goes through the existing path, unchanged.
TOOLBAR_TRASH_SELECTORS = (
    '[aria-label*="Move to trash" i]',
    '[aria-label*="Move to bin" i]',
    '[aria-label*="Delete" i]',
    '[aria-label*="Trash" i]',
    '[aria-label*="Bin" i]',
    '[title*="Move to trash" i]',
    '[title*="Move to bin" i]',
)

# What a toolbar trash control may be called. Anchored exactly like the dialog's
# confirm matcher, so no "All N photos" and no "Delete permanently" (a TRASH-view
# affordance, which this script must never press - GUARDRAIL 8) can match.
TOOLBAR_TRASH_NAME_RE = re.compile(
    r"^\s*(?:move (?:photo |video |item )?to (?:trash|bin)|"
    r"move to (?:trash|bin)|trash|delete)\s*$",
    re.I,
)

# Which route actually performed the action, reported per route in the summary
# so we learn how often the shortcut silently fails.
ROUTE_SHORTCUT = "shortcut"
ROUTE_TOOLBAR = "toolbar"
ACTION_ROUTES = (ROUTE_SHORTCUT, ROUTE_TOOLBAR)

# How long to wait for a dialog to appear after the shortcut before concluding
# the keystroke did nothing and falling back to the toolbar.
DIALOG_WAIT_MS = 2500
DIALOG_POLL_MS = 250

# Where to dump markup from when there is no dialog (FIX 2): the viewer's own
# top-level container, tried outermost-last so the narrowest useful one wins.
VIEWER_CONTAINER_SELECTORS = (
    "c-wiz",
    '[role="main"]',
    "main",
    "body",
)

# Used only to LOCATE the heading node, as the starting point of the walk up to
# the dialog container. Never a discriminator between the two dialog forms.
HEADING_LOCATOR_RE = re.compile(r"remove from your google account", re.I)

# Elements that are never a dialog container, however many options they contain.
# *** <body> is the one that matters: every ancestor of the heading contains the
# *** heading, so a document-order search for "the ancestor holding the heading"
# *** lands on <body> and silently re-widens the scope to the whole page - which
# *** puts the viewer TOOLBAR's "Move to trash" back among the candidates.
NEVER_A_DIALOG_SCOPE = frozenset({"body", "html", "#document", "frameset"})

# How far up from the heading the container walk may go. A dialog is a handful of
# nodes deep; anything further up is a page region, not a dialog.
MAX_SCOPE_ANCESTOR_DEPTH = 12

# The nearest clickable ancestor of a node, used when an option is a text row
# that is not itself clickable. `(ancestor-or-self::*[...])[last()]` is the
# INNERMOST match: XPath predicates index in document order, so [last()] is the
# candidate closest to the row. [1] would be the outermost - the same mistake
# NEVER_A_DIALOG_SCOPE exists to prevent, one axis over.
CLICKABLE_ANCESTOR_XPATH = (
    "xpath=(ancestor-or-self::*["
    "self::button or self::a or self::input or"
    " @role='button' or @role='option' or @role='menuitem' or"
    " @role='menuitemradio' or @role='radio' or @role='link' or"
    " @tabindex or @jsaction or @onclick"
    "])[last()]"
)

# Roles an option may carry when it is not a plain text row.
DIALOG_OPTION_ROLE_SELECTORS = (
    "button",
    '[role="button"]',
    '[role="menuitem"]',
    '[role="menuitemradio"]',
    '[role="option"]',
    '[role="radio"]',
    '[role="link"]',
)

# A visible text row longer than this is prose (the heading, a legal notice),
# not an option label.
MAX_OPTION_ROW_CHARS = 120

# Diagnostic dump limits. The point of the dump is to reveal what the options
# ARE - tag, role, clickability - not just what they are called.
DIAGNOSTIC_HTML_CHARS = 2000
DIAGNOSTIC_NODE_LIMIT = 80
DIAGNOSTIC_TEXT_CHARS = 80

# Read-only: collects the container's own markup and one line per descendant.
# Clickability is reported, not acted on - the dump exists because the v1.2.1
# label-only dump could not show that the stacked options are inert text rows.
DIALOG_DUMP_JS = """
element => {
  const clickableRoles = new Set([
    'button', 'link', 'option', 'menuitem', 'menuitemradio', 'radio', 'tab'
  ]);
  const isClickable = (node) => {
    const role = (node.getAttribute('role') || '').toLowerCase();
    const tag = node.tagName.toLowerCase();
    if (['button', 'a', 'input', 'summary'].includes(tag)) return true;
    if (clickableRoles.has(role)) return true;
    if (node.hasAttribute('tabindex')) return true;
    if (node.hasAttribute('jsaction') || node.hasAttribute('onclick')) return true;
    return false;
  };
  const nodes = [];
  for (const node of element.querySelectorAll('*')) {
    if (nodes.length >= %(limit)d) break;
    const text = (node.innerText || node.textContent || '').trim();
    if (!text && !isClickable(node)) continue;
    nodes.push({
      tag: node.tagName.toLowerCase(),
      role: node.getAttribute('role'),
      clickable: isClickable(node),
      text: text.split('\\n')[0].slice(0, %(text)d),
    });
  }
  return {html: element.outerHTML || '', nodes: nodes};
}
""" % {"limit": DIAGNOSTIC_NODE_LIMIT, "text": DIAGNOSTIC_TEXT_CHARS}

# NEVER CLICKED. This pattern exists so the script can RECOGNISE the dangerous
# option and refuse it - it is never used to find something to press.
STACKED_ALL_PHOTOS_RE = re.compile(
    r"^\s*all\s+\d+\s+(?:photos?|videos?|items?)\s*$", re.I
)

# The ONLY option this script may ever click in that dialog. Anchored, singular,
# and requiring both "current" and "only", so no "All N photos" label can match
# it however many items the stack holds. Matched on accessible text, never by
# position in the dialog: button order is not a safety property.
STACKED_CURRENT_ONLY_RE = re.compile(
    r"^\s*current\s+(?:photo|video|item)\s+only\s*$", re.I
)


# Failure kinds. These are reported separately because they mean completely
# different things and call for completely different responses:
#
#   navigation-resolve  the page would not load (Page.goto timeout). The
#                       NAVIGATION limiter. Slow down --delay.
#   stacked-dialog      the grouped-item dialog appeared and the safe option
#                       could not be used. A CODE GAP, not a limit, and NOT a
#                       throttle signal - cooling down would fix nothing.
#   no-dialog           NEITHER the '#' shortcut NOR the toolbar fallback could
#                       get a delete dialog open, so the deletion was never
#                       actually requested. A CODE GAP (the controls have
#                       moved), never a limit - see FIX 1/FIX 2 in the header.
#                       Keeping this apart from `mutation-no-op` is the whole
#                       point: a delete that was never requested says NOTHING
#                       about whether a delete quota exists.
#   mutation-no-op      the page was fine, a dialog WAS answered and dismissed,
#                       and the delete was silently not honoured anyway. The
#                       TRASH-MUTATION limiter, and the only kind that is both
#                       evidence of a delete quota and worth cooling down for.
#   other               anything else unexpected.
FAIL_NAVIGATION = "navigation-resolve"
FAIL_STACKED = "stacked-dialog"
FAIL_NO_DIALOG = "no-dialog"
FAIL_MUTATION = "mutation-no-op"
FAIL_OTHER = "other"

FAILURE_KINDS = (
    FAIL_NAVIGATION, FAIL_STACKED, FAIL_NO_DIALOG, FAIL_MUTATION, FAIL_OTHER
)

# Failure kinds that mean "this script could not drive the UI", as opposed to
# "the UI refused us". Both abort with the code-gap verdict rather than sleeping.
CODE_GAP_KINDS = frozenset({FAIL_STACKED, FAIL_NO_DIALOG})

# The kinds that are read as Google throttling us, and so are worth waiting out
# with a cooldown. Both are rate limits on a real resource, just at different
# layers. The excluded kinds are bugs or unknowns in THIS script, where sleeping
# changes nothing: a stacked-dialog refusal needs new wording in the code, and
# `other` is by definition not understood.
THROTTLE_KINDS = frozenset({FAIL_NAVIGATION, FAIL_MUTATION})

# Why a run stopped early.
ABORT_COOLDOWNS_EXHAUSTED = "cooldowns-exhausted"
ABORT_NO_SUCCESS = "no-success"
ABORT_FATAL = "fatal"
ABORT_STACKED = "stacked-dialog"
ABORT_BREAKER = "breaker"


class DriverError(RuntimeError):
    """An unexpected browser/UI condition. Counts toward the circuit breaker."""

    kind = FAIL_OTHER


class NavigationError(DriverError):
    """The page would not load: the NAVIGATION limiter, not the mutation one."""

    kind = FAIL_NAVIGATION


class FatalDriverError(DriverError):
    """A condition no amount of waiting can fix: not signed in, wrong page identity.

    Never treated as throttling, never cooled down - the run stops at once.
    """

    kind = FAIL_NAVIGATION


class StackedDialogError(DriverError):
    """The grouped-item dialog appeared and the safe option was not usable.

    Raised INSTEAD of clicking anything. This is a code gap (new wording, a new
    dialog layout), never a rate limit, so it must never trigger a cooldown -
    sleeping does not teach the script a new button name.
    """

    kind = FAIL_STACKED


class GuardrailViolation(RuntimeError):
    """A programming error that would have exceeded the reviewed blast radius."""


class CircuitBreakerTripped(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Item:
    url: str
    title: str = ""
    date: str = ""
    live_photo_pair: bool = False
    file_count: str = ""
    row_number: int = 0

    def label(self) -> str:
        bits = [self.title or "(untitled)"]
        if self.date:
            bits.append(self.date)
        if self.live_photo_pair:
            bits.append("LIVE(still+motion)")
        return " | ".join(bits)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def _item_from_mapping(row: Dict[str, Any], row_number: int) -> Item:
    return Item(
        url=str(row.get("url", "")).strip(),
        title=str(row.get("title", "") or "").strip(),
        date=str(row.get("date", "") or row.get("taken_time_utc", "") or "").strip(),
        live_photo_pair=_truthy(row.get("live_photo_pair", False)),
        file_count=str(row.get("file_count", "") or "").strip(),
        row_number=row_number,
    )


def _rows_from_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        for key in ("items", "orphans", "rows", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError(
                f"{path}: JSON object has no 'items'/'orphans'/'rows'/'data' list"
            )
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of row objects")
    out: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: expected objects in list, got {type(entry)}")
        out.append(entry)
    return out


def _rows_from_csv(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: file is empty (no header row)")
        if "url" not in reader.fieldnames:
            raise ValueError(
                f"{path}: required column 'url' not found; header is "
                f"{reader.fieldnames}"
            )
        return [dict(row) for row in reader]


@dataclass
class LoadResult:
    items: List[Item] = field(default_factory=list)
    invalid: List[Tuple[int, str, str]] = field(default_factory=list)  # row, url, why
    duplicates: int = 0


def load_items(path: str) -> LoadResult:
    """Parse the reviewed input file into validated Items.

    GUARDRAIL 1: anything that is not an exact photos.google.com/photo/AF1Qip...
    permalink is rejected here and can never reach the browser.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"input file not found: {path}")
    if path.lower().endswith(".json"):
        raw_rows = _rows_from_json(path)
    else:
        raw_rows = _rows_from_csv(path)

    result = LoadResult()
    seen: set[str] = set()
    for offset, row in enumerate(raw_rows):
        row_number = offset + 2  # header is line 1 for CSV; close enough for JSON
        item = _item_from_mapping(row, row_number)
        if not item.url:
            result.invalid.append((row_number, "", "empty url"))
            continue
        if not PERMALINK_RE.match(item.url):
            result.invalid.append((row_number, item.url, "not a photo permalink"))
            continue
        if item.url in seen:
            result.duplicates += 1
            continue
        seen.add(item.url)
        result.items.append(item)
    return result


# --------------------------------------------------------------------------- #
# Journal (resume + idempotency)
# --------------------------------------------------------------------------- #


class Journal:
    """Append-only JSONL progress journal keyed by URL.

    GUARDRAIL 5: loaded on start; URLs with a terminal status are skipped, so a
    re-run of the same input can never act twice on the same item.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        self.status_by_url: Dict[str, str] = {}
        # FIX 3 (v1.3.0): how many times each URL has already been attempted and
        # failed, counted across ALL runs. This is what makes "retry after the
        # fresh items" and "--max-attempts, then quarantine" possible.
        self.failures_by_url: Dict[str, int] = {}
        self.malformed_lines = 0

    def load(self) -> None:
        self.status_by_url = {}
        self.failures_by_url = {}
        self.malformed_lines = 0
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    url = record["url"]
                    status = record["status"]
                except Exception:
                    self.malformed_lines += 1
                    continue
                if status not in TERMINAL_STATUSES:
                    # Every non-terminal line is one spent attempt. Counted even
                    # when a later line finishes the item, so the count is a
                    # faithful history; callers only consult it for items that
                    # are still pending.
                    self.failures_by_url[url] = self.failures_by_url.get(url, 0) + 1
                previous = self.status_by_url.get(url)
                # A terminal status is sticky: a later `failed` line (e.g. from a
                # different input file) must never un-finish a finished item.
                if previous in TERMINAL_STATUSES:
                    continue
                self.status_by_url[url] = status
        return

    def is_done(self, url: str) -> bool:
        return self.status_by_url.get(url) in TERMINAL_STATUSES

    def done_count(self) -> int:
        return sum(1 for s in self.status_by_url.values() if s in TERMINAL_STATUSES)

    def failure_count(self, url: str) -> int:
        """How many failed attempts this URL already has on the record."""
        return self.failures_by_url.get(url, 0)

    def has_been_attempted(self, url: str) -> bool:
        """True if this URL has ever been tried, successfully or not.

        FIX 3: a never-attempted item is new information; a known-bad one
        re-failing is not, which is why only the former may trip the
        zero-successes safeguard.
        """
        return url in self.status_by_url or url in self.failures_by_url

    def record(self, item: Item, status: str, detail: str = "") -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "url": item.url,
            "status": status,
            "detail": detail,
            "title": item.title,
            "date": item.date,
            "live_photo_pair": item.live_photo_pair,
            "script_version": VERSION,
        }
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Flush + fsync after every item: an interrupted run must not lose its
        # place, and must not leave a half-written line behind.
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self.status_by_url.get(item.url) not in TERMINAL_STATUSES:
            self.status_by_url[item.url] = status
