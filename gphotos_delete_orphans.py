#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gphotos_delete_orphans.py - paced, resumable, verifying deletion of Google Photos
items by permalink, driven from a reviewed input file.

This is the DESTRUCTIVE companion to the read-only `gphotos_find_orphans.py`, which
set-differenced a 5-part Google Takeout export to produce the list of items that are
in NO album. This script deletes (moves to Trash) exactly the items in that list, and
nothing else.

===============================================================================
READ THIS FIRST - RISKS AND TERMS
===============================================================================
* Automated access to Google Photos outside Google's published APIs is contrary to
  Google's Terms of Service. You are choosing to drive your own authenticated
  browser session against your own account.
* Scripted bulk deletion carries some risk of account flagging, rate limiting, or
  an interstitial challenge. Conservative pacing reduces that risk; it does NOT
  eliminate it. There is no pace that makes this officially sanctioned.
* Why this route at all: the Google Photos Library API never had a
  delete-media-item method, and the `photoslibrary*` OAuth scopes were removed on
  2025-03-31. The web UI has no "not in any album" filter. Driving the browser
  against stable per-item permalinks is the only remaining route.
* Deleted items sit in Trash for 60 days. THAT IS YOUR ONLY UNDO. This script never
  empties Trash and never asks to go there; if Google itself redirects a permalink
  into a Trash view, that is read as "already trashed" and no action is taken on
  that page. Do not empty Trash yourself until you are satisfied.
* Items are moved to Trash one at a time, each one verified. Nothing here is
  irreversible for 60 days, but 20,450 items is a lot to put back by hand. Start
  with --smoke, check the UI yourself, and only then scale up.

===============================================================================
HARD GUARDRAILS (stated here, enforced in code - see the GUARDRAIL comments)
===============================================================================
 1. INPUT-ONLY BLAST RADIUS. The script acts only on URLs present in the input
    file. It never discovers items by scrolling, searching, or walking the grid.
    Every navigation goes through nav_guard(), which refuses any URL that is not
    in the allow-list built from the input file. This is the single most important
    property of this tool.
    THE STACKED-ITEM COROLLARY: Google Photos groups bursts and similar shots
    under one viewer entry, and trashing one of those offers "All N photos" or
    "Current photo only" instead of a plain confirm. The other N-1 items are
    SEPARATE library items that are NOT in the input file, and many of them are
    in the user's albums - they are not orphans. So this script only ever clicks
    "Current photo only", matched on exact wording and never by position, and if
    it cannot find that option it clicks NOTHING and fails the item. A missed
    deletion costs one retry; a wrong click costs photos the user meant to keep.
    Which dialog is up is decided from its OPTIONS, read from INSIDE the dialog
    container - never from its heading (both forms share one) and never
    page-wide (the viewer toolbar has a "Move to trash" button of its own).
    Those options are read BOTH as roles AND as visible text rows, because in
    the real stacked dialog they ARE plain text rows and no role query finds
    them at all (the v1.2.2 fix - see BUG A below).
    See decide_dialog_action(), DIALOG_SCOPE_SELECTORS, dialog_lines() and
    PlaywrightDriver._dialog_scope_from_heading().
 2. DRY RUN IS THE DEFAULT. Deleting requires an explicit --execute. Without it the
    script resolves and reports what it WOULD do and changes nothing (no clicks, no
    journal writes).
 3. --limit N caps every run. Default 25. Raising it is a deliberate act; above
    --confirm-threshold (default 500) an --execute run also asks you to type a
    confirmation phrase (bypass for unattended runs with --assume-yes).
 4. THROTTLED, AND ADAPTIVE. --delay seconds (default 5.0) plus +/- --jitter
    fraction (default 0.35) between items. 20k deletions is not human-scale:
    pacing and resuming across many sessions is the normal mode of operation, not
    an edge case. Rate limiting is real but has been observed at exactly ONE
    layer, navigation (see WHY ITEMS FAIL below; a delete quota remains
    unproven), and the delay GROWS after every cooldown
    (--delay-growth, capped by --delay-max) because a pace that has just been
    throttled is by definition too fast to resume at.
 5. RESUMABLE AND IDEMPOTENT. Every outcome is appended to a JSONL journal keyed by
    URL. On start, any URL already journalled as `deleted` or `already_gone` is
    skipped. Re-running the same input can never double-act.
 6. VERIFIED, POSITIVELY. Success is established from positive evidence, in this
    order: the "Moved to trash" confirmation toast (with its Undo affordance), or
    the viewer being closed/redirected away from the permalink, or - only if
    neither of those appeared - a FRESH navigation back to the permalink after a
    settle delay (--post-action-settle, default 3.0s) showing a not-available or
    in-trash state. The DOM left behind by the action is never trusted: reading it
    before the UI updates is what made correct deletions look like failures. An
    item that was already gone or already in Trash (redirect / not found / trash
    view) is recorded as `already_gone`, which is DONE, not an error. Only when
    none of the three signals holds is an item reported FAILED.
 7. CIRCUIT BREAKER, THEN COOLDOWN - BUT ONLY FOR THE RIGHT FAILURE KIND.
    --max-consecutive-failures (default 3) failures in a row stops the item loop.
    What happens next depends on WHAT kind of failures they were and on whether
    the run has proved it can still delete:
      - a run of THROTTLE_KINDS failures only (`navigation-resolve` and/or
        `mutation-no-op`) AND at least one success since the last resume ->
        THROTTLING. Sleep --cooldown, recycle the tab, slow down, carry on
        (GUARDRAIL 10). Navigation throttling is real and has been observed
        twice, so it is worth waiting out rather than abandoning the run.
        `mutation-no-op` is only ever recorded when the dialog WAS answered and
        is gone from the screen and the item survived anyway (v1.2.2): an
        unanswerable dialog used to be filed here and bought a 20-minute sleep
        that could not possibly fix a selector.
      - the same, but with no success at all since the last resume -> NOT
        throttling. Abort loudly (exit 6): a UI change, a challenge page, or
        lost auth, none of which waiting fixes.
      - any `stacked-dialog` or `no-dialog` failure in the streak -> a CODE GAP,
        never a limit. Abort (exit 7) without sleeping; a cooldown cannot teach
        the script a new button name, nor find a control that has moved.
      - anything else (`other`) -> plain breaker abort (exit 5). Go look.
    Anything that says the page identity or the input is wrong rather than
    rate-limited (sign-in redirect, guardrail violation) still aborts at once.
    AND, since v1.3.0: a streak made up ENTIRELY of items that had already
    failed in an earlier run is not evidence of anything new, so it neither
    aborts the run nor takes a cooldown - see GUARDRAIL 11.
10. BOUNDED, SELF-SLOWING COOLDOWNS. Cooldowns are exponential
    (--cooldown * --cooldown-backoff ** n, each capped by --cooldown-cap) and
    there are at most --max-cooldowns of them; after that the run gives up.
    A cooldown is interruptible, and since v1.2.3 Ctrl-C during one means SKIP
    THE WAIT, not quit: the remaining sleep is abandoned and the run carries on
    with the next item. Press Ctrl-C TWICE (within 2.5s) to stop the run
    instead. A skipped cooldown still counts toward --max-cooldowns and the
    delay still grows, because the throttle condition really did occur -
    skipping the wait does not undo it. Outside a cooldown, Ctrl-C still stops
    the run at once. Every cooldown is logged with a wall-clock timestamp and
    the running totals, and the summary reports how many were taken, how many
    were skipped, and how long they cost.
11. FRESH WORK FIRST, AND A BOUNDED NUMBER OF RETRIES (v1.3.0). Items that have
    never been attempted are processed BEFORE items that failed in an earlier
    run, and an item that has failed --max-attempts times (default 3) is
    QUARANTINED: skipped, listed in the summary, still in the journal, and
    retried only with --retry-quarantined. Items with a failure history also do
    not count toward the zero-successes safeguard in GUARDRAIL 7.
    WHY: a real run had three stubborn items at the front of the queue. They
    were retried first on every run, produced zero successes, tripped that
    safeguard, and aborted the run before it reached any of the 9,753 items
    that would have worked. The safeguard was right; the ordering was wrong.
 8. NARROW ACTIONS. The only mutation performed is "move this one item to Trash".
    Never empties Trash, never touches album membership, never edits, never shares,
    never changes anything not named in the input file. The delete is driven by
    the '#' viewer shortcut and, when that opens no dialog, by the viewer
    TOOLBAR's own "Move to trash" control (v1.3.0, FIX 1) - matched by exact
    accessible name, required to be OUTSIDE any dialog, and never a permanent
    delete or a Trash-view affordance.
 9. NO AUTOMATED SIGN-IN. The script attaches over CDP to a Chrome you started and
    signed into yourself, and reuses that browser's existing context. There are no
    credentials in this file, it never creates a fresh profile, and it never types
    into a login form. If the browser is not already authenticated, the run fails.

===============================================================================
INPUT CONTRACT
===============================================================================
Default input: ~/Desktop/orphans.csv (or pass --input, which also accepts the
sibling orphans.json - a JSON list of objects, or an object with an "items" list).

Required column/key: `url`, holding a permalink of the form
    https://photos.google.com/photo/AF1Qip...
Rows whose url does not match that exact shape are reported as INVALID and are
never acted on.

Other columns are optional and used only for logging: title, date, taken_time_utc,
taken_time_epoch, live_photo_pair, file_count, extensions, folded_renditions,
generated_albums, orphan_solely_due_to_generated_album, takeout_paths,
source_archives.

LIVE PHOTOS: ~117 rows have live_photo_pair=true, meaning one Google Photos item
holds both the still and the motion file. Deleting the item removes BOTH halves
together. That is unavoidable and this script does not try to split them; it just
labels those items LIVE in the log so you can see them go.

===============================================================================
WHY ITEMS FAIL - AND WHY THERE IS NO EVIDENCE OF A DELETE QUOTA
===============================================================================
Two different things produce the SAME symptom, and conflating them is what the
last two versions each got wrong in opposite directions:

    FAILED: no confirmation toast, viewer did not close, and the item is still
    there on a fresh reload

  1. AN UNANSWERED CONFIRMATION DIALOG -> kind `stacked-dialog`.
     *** This, not a rate limit, is what the historical failures were. ***

     After the trash shortcut, Google Photos shows one of two dialogs. BOTH
     carry the identical heading:

         Remove from your Google Account, devices with backup turned on, and
         places shared within Google Photos?

     ORDINARY (a single item):            Cancel  |  Move to trash
     GROUPED/STACKED (burst or similar    All 8 photos  |  Current photo only
     photos grouped under one entry):

     v1.1.0 looked for "Move to trash". On a STACKED dialog there is no such
     button, so it clicked nothing and reported exactly the symptom above. That
     explains the v1.1.0 record completely: the 42 failures in run 1 were
     SCATTERED because stacked groups are scattered through a library; the 3
     CONSECUTIVE failures that tripped the breaker are unremarkable because
     orphans from one burst sit adjacent in the input file; and run 3 dying at
     item ~94 after ~3,465 cumulative deletions was a cluster of stacks early in
     that batch, not a rolling quota being reached.

     v1.2.0 then fixed this the wrong way round: it detected "stacked" from the
     shared HEADING, so every ordinary single-item confirm also looked stacked,
     nothing could be answered, and a real run aborted after 3 attempts with 0
     deletions.

     v1.2.1 decides from the OPTIONS, scoped to the dialog container:
         "Current photo only" present -> stacked; click it, never "All N photos"
         else "Move to trash" present -> ordinary; click it (as v1.1.0 did for
                                         1,693 successful deletions)
         else                         -> click NOTHING, fail the item
     The heading is kept only as a weak corroborating signal for the logs.

     *** BUG A, FOUND IN THE v1.2.1 PRODUCTION RUN AND FIXED IN v1.2.2. ***
     v1.2.1 deleted 588 items in 96 minutes (6.3/min) with 13 failures, and
     EVERY ONE of those 13 was stack-related: the stacked dialog's options were
     never found. "Current photo only" was plainly on screen in a screenshot of
     a failing item, yet the option hunt matched nothing, because:
       (a) the options are PLAIN TEXT ROWS, not buttons. A page-wide
           accessible-label dump for an ORDINARY item listed 'Cancel' and two
           'Move to trash' and no 'Current photo only' anywhere - role queries
           simply do not see those rows; and
       (b) the container holding them need not carry role=dialog,
           role=alertdialog or aria-modal at all, so there was often no scope
           to read options from in the first place.
     v1.2.2 therefore reads options from VISIBLE TEXT as well as from roles
     (dialog_lines()), widens DIALOG_SCOPE_SELECTORS, and - when none of those
     selectors yields a container with options in it - derives the container
     from the HEADING by walking up to the SMALLEST ancestor that actually
     holds an option row (never <body>/<html>: every ancestor of the heading
     contains the heading, so taking the first match in document order silently
     re-widens the scope to the whole page and puts the TOOLBAR's "Move to
     trash" back in reach). A text row that is not itself clickable is clicked
     via its nearest clickable ancestor, and that ancestor is refused if it
     turns out to cover any other option. assert_safe_to_click() remains the
     last gate: "All N photos" is never clickable from any path, and if
     "Current photo only" cannot be positively identified, NOTHING is clicked.

  1b. THE '#' SHORTCUT SILENTLY DOING NOTHING -> kind `no-dialog`.
     *** FOUND IN THE v1.2.2 PRODUCTION RUN, FIXED IN v1.3.0 (FIX 1). ***
     That run failed its first three items and then aborted. What the output
     did NOT contain is the evidence: no `stacked-dialog` classification, no
     "All N photos" seen, and no outerHTML dump - because no dialog was ever
     DETECTED, and up to v1.2.2 the dump was gated on detecting one. The user
     also reported that the same items delete normally "unless I manually click
     in browser", and one of the three is the very permalink whose stacked
     dialog he had screenshotted earlier - a dialog that appeared when he
     opened the item BY HAND.
     So for some item types the keystroke never reaches the viewer: no dialog
     opens, there is nothing to scope, nothing to classify, and the result is
     indistinguishable from a silently-ignored delete. v1.3.0:
       * waits briefly for a dialog after the shortcut (_wait_for_dialog);
       * if none appears, clicks the viewer TOOLBAR's own "Move to trash"
         (proved to exist by the v1.2.0 page dump), which is required to be
         OUTSIDE any dialog - pressing a dialog's own button from here would
         bypass decide_dialog_action(), the one thing that keeps "All N photos"
         unclickable - and then answers whatever dialog that opens through the
         existing path;
       * records which route acted and reports per-route counts, so how often
         the shortcut fails is measured rather than guessed;
       * and when NEITHER route opens a dialog, files the failure as
         `no-dialog` - a CODE GAP that never cools down, and explicitly NOT
         evidence about a delete quota, because the delete was never requested.

  2. THE NAVIGATION LIMITER -> kind `navigation-resolve`.
     Genuinely real, and the only throttling actually observed. Too-fast page
     loads produce `Page.goto` timeouts and the item never resolves at all. Seen
     twice: at ~2s spacing it died around item 17 while 10s ran clean, and a
     cold-start timeout hit the first item after an idle gap.

  3. A TRASH-MUTATION LIMITER -> kind `mutation-no-op`. HYPOTHETICAL, AND THE
     EVIDENCE SO FAR SAYS IT DOES NOT EXIST.
     This would be a delete quota: the page loads, the dialog is answered and
     dismissed, and the deletion is silently not honoured anyway. The counter
     and the cooldown for it are kept because the failure mode is cheap to
     handle and would be expensive to meet unprepared - but NOTHING observed so
     far requires it to exist. The strongest evidence to date is the v1.2.1
     production run: 588 deletions in 96 minutes at a sustained 6.3/min with no
     slowdown and no refusal, and all 13 of its failures explained by (1).
     Every failure ever seen is explained by (1) or (2), so treat a delete
     quota as UNPROVEN - and do not read this section as saying there is one.

     *** BUG B, ALSO FIXED IN v1.2.2: that conclusion was being corrupted by
     the counter itself. *** An unanswerable stacked dialog produces exactly
     the symptom above, and v1.2.1 filed it as `mutation-no-op` - which is in
     THROTTLE_KINDS, so three of them bought a 20-minute cooldown. Sleeping
     cannot fix a selector. Before classifying a no-toast failure, v1.2.2 now
     INSPECTS the screen (classify_noop_failure()): if a dialog is still up, or
     the stacked heading is still present, or any "All N photos" option exists,
     the kind is `stacked-dialog` - a code gap, never cooled down.
     `mutation-no-op` can now only fire when the dialog was answered and
     dismissed and the item still survived, which is what makes that counter
     real evidence about the open question above rather than noise.
     v1.3.0 narrows it once more, for the same reason: if NO dialog was ever
     opened (1b), the kind is `no-dialog`, because a delete that was never
     requested cannot be evidence of a delete limiter. The three
     `mutation-no-op` failures reported by the v1.2.2 run were all of that
     kind, so they are NOT evidence of a quota either.

Both throttle kinds cool down rather than ending the run: the breaker sleeps
(default 20min, then 40, then 80..., at most --max-cooldowns times), recycles the
tab so SPA state does not accumulate across thousands of navigations, raises
--delay by --delay-growth (up to --delay-max), and resumes where it stopped.
`stacked-dialog`, `no-dialog` and `other` never cool down - they are bugs here,
not limits. Failed items stay PENDING and are retried by a later run (after the
never-attempted ones, and at most --max-attempts times - GUARDRAIL 11).

The safeguard that keeps this from spinning for hours against a broken UI: a
cooldown is only ever taken if the run has deleted something since it last
resumed. Zero successes in a post-resume window means the problem is not pacing,
and the run aborts with exit code 6 instead of sleeping six more times. Items
that already had failures before this run began do not count toward it
(GUARDRAIL 11): a known-bad item failing again says nothing about the UI.

DIAGNOSTICS ON ANY NO-TOAST FAILURE (v1.3.0, FIX 2 - was: on any DIALOG failure,
v1.2.2). Three rounds of guessing at markup came out of gating the dump on
detecting a dialog, because the failures we could not explain are exactly the
ones where no dialog is detected. The dump is now UNCONDITIONAL on every
no-toast failure and reports: whether a dialog was detected at all, the chosen
scope (or "none"), the option labels seen BOTH by role and as visible text rows,
whether a toolbar trash control was found, and the outerHTML (<=2000 chars) of
the dialog if there is one - else of the viewer's own top-level container. Per
descendant it also gives tag, role, clickability and text; containers that were
inspected and held no options are listed too. The v1.2.0/v1.2.1 dumps reported
accessible LABELS only, which is why it took a user's screenshot to discover
that the stacked options are not buttons.

===============================================================================
EXIT CODES
===============================================================================
    0  clean
    1  finished, but with failures (those items stay pending)
    2  usage / input error
    3  could not attach to Chrome
    4  guardrail violation (a bug; nothing further attempted)
    5  circuit breaker: repeated failures that are not trash-mutation no-ops,
       or gave up after --max-cooldowns cooldowns while still being throttled
    6  NOT throttling: UI change, challenge page, or lost auth. Go look.
    7  a delete dialog could not be driven safely, so nothing was clicked:
       either the grouped/stacked dialog's options were not understood
       (`stacked-dialog`) or no dialog opened at all from either the '#'
       shortcut or the toolbar fallback (`no-dialog`). A code gap in this
       script, not a limit.
  130  interrupted with Ctrl-C (safe; the journal is current). Note that a
       SINGLE Ctrl-C during a cooldown only skips the wait and does not stop the
       run, so it does not produce this code; press twice to stop.

===============================================================================
SETUP (one dependency: playwright)
===============================================================================
    python3 -m pip install --user playwright

You do NOT need `python3 -m playwright install` - that downloads bundled browsers,
and this script never launches one. It only attaches to your own Chrome.

Start Chrome with a debugging port. Chrome 136+ ignores --remote-debugging-port for
the default profile directory, so use a dedicated profile directory and sign in to
Google once, by hand, in that window:

    # quit Chrome completely first (Cmd-Q), then:
    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
        --remote-debugging-port=9222 \\
        --user-data-dir="$HOME/chrome-cdp-profile"

In that window, sign in to your Google account and open https://photos.google.com
once to confirm you are logged in. That profile persists, so later sessions only
need the same command again. Confirm the port is live:

    curl -s http://127.0.0.1:9222/json/version | head -3

===============================================================================
HOW TO RUN IT (recommended order)
===============================================================================
    # 0. offline sanity check - parses the file, no browser at all
    python3 gphotos_delete_orphans.py --offline --limit 10

    # 1. dry run through the real browser: resolve 25 items, change nothing
    python3 gphotos_delete_orphans.py

    # 2. first real run: 5 items, extra verbose. Check them in the UI yourself.
    python3 gphotos_delete_orphans.py --smoke --execute

    # 3. a modest batch
    python3 gphotos_delete_orphans.py --execute --limit 100

    # 4. steady state: repeat as often as you like. Already-done items are
    #    skipped from the journal, so this is just "do the next 500".
    python3 gphotos_delete_orphans.py --execute --limit 500 --delay 10

    # 5. long unattended run: ride out the mutation limiter instead of aborting.
    #    Expect it to sleep for hours in total across the session.
    python3 gphotos_delete_orphans.py --execute --limit 25000 --assume-yes \\
        --delay 6 --cooldown 1200 --max-cooldowns 6 --delay-max 30

    # 6. a handful of items keep failing and you have fixed the cause: let the
    #    quarantined ones back in (they are skipped by default so they cannot
    #    block the run - GUARDRAIL 11).
    python3 gphotos_delete_orphans.py --execute --limit 50 --retry-quarantined

    # verify the script's own logic at any time (no browser, no network):
    python3 gphotos_delete_orphans.py --self-test

Interrupting with Ctrl-C is safe: the journal is flushed after every item, so the
next run picks up exactly where this one stopped. What a Ctrl-C MEANS depends on
whether a cooldown is sleeping (v1.2.3):

    while cooling down   one press  -> skip the rest of the wait, carry on
                         two presses (within 2.5s) -> stop the run, exit 130
    anywhere else        one press  -> stop the run, exit 130

A heartbeat line every --heartbeat-every items (default 100) reports elapsed
time, rate and how much is left.

Each run processes NEVER-ATTEMPTED items first and previously-failed ones after
them, and an item that has failed --max-attempts times (default 3) is skipped as
QUARANTINED until you pass --retry-quarantined (GUARDRAIL 11). The summary lists
the quarantined URLs; nothing is ever dropped from the journal.

Journal default: ~/Desktop/gphotos_delete_journal.jsonl  (--journal to move it).
KEEP THAT FILE. It is what makes the work resumable and non-repeating, and since
v1.3.0 it is also where the per-item attempt counts come from.
"""

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


# --------------------------------------------------------------------------- #
# Browser driver
# --------------------------------------------------------------------------- #

PRESENT = "present"
GONE = "gone"
UNKNOWN = "unknown"


@dataclass
class TrashOutcome:
    """What the trash action itself observed. See GUARDRAIL 6."""

    note: str = ""
    toast_seen: bool = False
    toast_text: str = ""
    left_viewer: bool = False
    # True when this item turned out to be part of a grouped/stacked group and
    # the two-option dialog had to be answered with "Current photo only".
    stacked: bool = False
    stacked_choice: str = ""
    # What the screen still showed after the action, read only when there was
    # neither a toast nor a closed viewer. BUG B: this is what separates an
    # unanswered dialog (a code gap) from a silently-ignored delete (a quota).
    lingering_dialog: bool = False
    lingering_heading: bool = False
    all_photos_seen: bool = False
    # The dump for the log. FIX 2 (v1.3.0): filled in on ANY no-toast failure,
    # whether or not a dialog was detected.
    diagnostics: str = ""
    # FIX 1 (v1.3.0): which route performed the action, and whether the '#'
    # shortcut opened a dialog at all. Reported per route in the summary so we
    # learn how often the shortcut silently fails.
    route: str = ROUTE_SHORTCUT
    shortcut_opened_dialog: bool = True
    # Whether a delete dialog was actually found and answered. False means the
    # deletion was never requested at all, which is a code gap in this script
    # (`no-dialog`) and NOT evidence about a delete quota.
    dialog_answered: bool = True


def is_trash_url(url: str) -> bool:
    """True for a Google Photos trash/bin view URL."""
    path = (url or "").split("?")[0].split("#")[0]
    return bool(re.search(r"photos\.google\.com/(?:[^/]+/)*(?:trash|bin)\b", path, re.I))


def classify_page_state(
    current_url: str,
    permalink: str,
    text_getter,
    viewer_getter,
    trashed_state_getter,
) -> str:
    """Decide whether the loaded page shows the item, or shows it as gone.

    The DOM getters are callables so the expensive ones are only run when the URL
    alone has not already settled the question. Returns UNKNOWN when there is
    neither 'gone' evidence nor live-viewer evidence; the caller refuses to guess.
    """
    current = (current_url or "").split("#")[0]
    if is_trash_url(current):
        # Google redirected the permalink into Trash: the item is trashed. We
        # never act here (GUARDRAIL 8) - this is read-only evidence.
        return GONE
    if not current.startswith(PHOTO_PREFIX):
        # Redirected out of the viewer (usually to the grid) => item is gone.
        return GONE
    if current.rstrip("/") != permalink.rstrip("/"):
        return GONE
    if trashed_state_getter():
        # Restore / Delete permanently are only offered for a trashed item.
        return GONE
    text = text_getter() or ""
    if any(pattern.search(text) for pattern in GONE_TEXT_PATTERNS):
        return GONE
    if viewer_getter():
        return PRESENT
    return UNKNOWN


def toast_text_says_trashed(text: str) -> str:
    """Return the matched snippet of a 'Moved to trash' toast, or ''."""
    for pattern in TRASHED_TOAST_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(0)
    return ""


# --------------------------------------------------------------------------- #
# The confirm dialog: deciding which option - if any - may be clicked
# --------------------------------------------------------------------------- #

# What decide_dialog_action() concluded about a dialog.
DIALOG_STACKED = "stacked"
DIALOG_ORDINARY = "ordinary"


def stacked_heading_present(text: str) -> bool:
    """True if the 'Remove from your Google Account...' heading is on the page.

    WEAK SIGNAL, FOR LOGGING ONLY. Both the ordinary single-item confirm and the
    grouped-item dialog carry this heading, so it says a delete dialog is
    probably up - never which kind. Using it as the discriminator is the v1.2.0
    bug: it made every single-item confirm look like a stack.
    """
    return any(pattern.search(text or "") for pattern in STACKED_DIALOG_TEXT_PATTERNS)


def is_all_photos_option(label: str) -> bool:
    """True for the DANGEROUS 'All N photos' option. Only ever used to refuse it."""
    return bool(STACKED_ALL_PHOTOS_RE.match(label or ""))


def is_current_only_option(label: str) -> bool:
    """True for the one option this script is allowed to click."""
    return bool(STACKED_CURRENT_ONLY_RE.match(label or ""))


def assert_safe_to_click(label: str) -> None:
    """GUARDRAIL 1, last line of defence before any dialog click.

    Nothing should ever reach a click with an 'All N photos' label; if it does,
    that is a bug in this file and the run must stop rather than delete items
    that were never in the input.
    """
    if is_all_photos_option(label):
        raise GuardrailViolation(
            f"refusing to click {label!r}: that would delete every item in the "
            "stack, including items that are not in the input file"
        )


def is_confirm_option(label: str) -> bool:
    """True for the ordinary single-item confirm button ('Move to trash')."""
    return bool(TRASH_BUTTON_NAME_RE.match((label or "").strip()))


def is_toolbar_trash_label(label: str) -> bool:
    """True for a viewer-toolbar control that moves THIS item to Trash.

    FIX 1 (v1.3.0). Anchored, so it can never match "All N photos", and it
    deliberately refuses "Delete permanently" / "Delete from trash": those exist
    only in a Trash view and pressing one would destroy an item for good, which
    GUARDRAIL 8 forbids. A label that is not exactly a trash action is not a
    candidate - if the toolbar has been reworded, the fallback simply does not
    fire and the item fails safe with a dump (FIX 2).
    """
    cleaned = (label or "").strip()
    if not cleaned:
        return False
    if is_all_photos_option(cleaned):
        return False
    if re.search(r"permanent|from (?:trash|bin)|empty|restore", cleaned, re.I):
        return False
    return bool(TOOLBAR_TRASH_NAME_RE.match(cleaned))


def dialog_lines(text: str) -> List[str]:
    """The VISIBLE TEXT ROWS of a dialog, one candidate option per row.

    *** BUG A, v1.2.2. The stacked dialog's options are plain text rows, not
    *** buttons: `get_by_role("button", name=...)` matches nothing inside that
    *** dialog, which is why "Current photo only" was never found or clicked
    *** even though it was plainly on screen. The page-wide label dump taken
    *** for an ORDINARY item said the same thing from the other direction - it
    *** listed 'Cancel' and two 'Move to trash' and no 'Current photo only'
    *** anywhere on the page. So options are read this way TOO, not instead:
    *** roles still find the ordinary confirm button, and this finds the rows.

    Dropped: blank rows, the shared heading (it is not an option), and anything
    long enough to be prose. Everything else is passed through verbatim for
    decide_dialog_action() to accept or refuse - this function never decides
    which row is safe, and rows it lets through are still only ever clicked by
    exact wording.
    """
    rows: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if stacked_heading_present(line):
            continue
        if len(line) > MAX_OPTION_ROW_CHARS:
            continue
        rows.append(line)
    return rows


def merge_option_labels(*groups: Sequence[str]) -> List[str]:
    """Union of several views of the SAME dialog, in order, without duplicates.

    The role query and the text-row read see one node twice: the ordinary
    confirm button is both a button and a line of visible text. Two copies of
    'Current photo only' would make decide_dialog_action() refuse an answerable
    dialog as ambiguous, so they are collapsed here - in the collector, which
    knows they are one node, and never in the decider, which must keep treating
    two genuinely different safe-looking options as something to refuse.
    """
    seen: set[str] = set()
    merged: List[str] = []
    for group in groups:
        for label in group or ():
            cleaned = str(label or "").strip()
            if not cleaned:
                continue
            key = " ".join(cleaned.lower().split())
            if key in seen:
                continue
            seen.add(key)
            merged.append(cleaned)
    return merged


def container_holds_options(labels: Sequence[str]) -> bool:
    """True if these labels look like a delete dialog's options.

    "All N photos" counts: a container offering only the dangerous option is
    still the dialog, and recognising it is what lets the run report a stacked
    dialog it cannot answer instead of mistaking it for a silent no-op (BUG B).
    """
    return any(
        is_current_only_option(label)
        or is_confirm_option(label)
        or is_all_photos_option(label)
        for label in labels or ()
    )


def decide_dialog_action(labels: Sequence[str]) -> Tuple[str, str]:
    """Decide what to click in a delete dialog, from its OPTIONS alone.

    `labels` must be the accessible text of the options found INSIDE the dialog
    element - never page-wide. The page also carries a "Move to trash" toolbar
    button, and clicking that would re-open the dialog instead of confirming it.

    Returns (kind, label) where kind is DIALOG_STACKED or DIALOG_ORDINARY.
    Precedence is deliberate and is the whole fix:

      1. "Current photo only" present  -> GROUPED/STACKED. Click it. Checking
         this FIRST means a stack is never mistaken for a single item, so
         "All N photos" is never the thing a confirm-hunt lands on.
      2. else "Move to trash" present  -> ORDINARY single-item confirm. Click
         it. This is exactly what v1.1.0 did for 1,693 successful deletions.
      3. else                          -> raise, having chosen NOTHING.

    Can never return an "All N photos" label: every returned label matched
    either STACKED_CURRENT_ONLY_RE or TRASH_BUTTON_NAME_RE, and neither pattern
    can match an "All N ..." string.
    """
    cleaned = [str(label or "").strip() for label in labels]
    offered = ", ".join(repr(label) for label in cleaned if label) or "(none readable)"

    safe = [label for label in cleaned if is_current_only_option(label)]
    if len(safe) > 1:
        raise StackedDialogError(
            "the grouped/stacked item dialog offered more than one option matching "
            f"'Current photo only' ({offered}). Refusing to guess which is real; "
            "nothing was clicked and this item stays pending."
        )
    if safe:
        assert_safe_to_click(safe[0])
        return DIALOG_STACKED, safe[0]

    confirm = [label for label in cleaned if is_confirm_option(label)]
    if confirm:
        assert_safe_to_click(confirm[0])
        return DIALOG_ORDINARY, confirm[0]

    raise StackedDialogError(
        "a delete confirmation dialog was open but neither a 'Current photo only' "
        f"option (grouped/stacked item) nor a 'Move to trash' option (ordinary "
        f"single item) was found inside it. Options inside the dialog: {offered}. "
        "NOTHING was clicked - on a stacked dialog the remaining choice deletes "
        "the whole stack, which would act on items that are not in the input "
        "file. This item was left alone and stays pending. The dialog wording "
        "has probably changed: compare it against STACKED_CURRENT_ONLY_RE and "
        "TRASH_BUTTON_NAME_RE."
    )


def pick_dialog_container(candidates: Sequence[Any], tag_of, holds_options) -> int:
    """Choose the dialog container from the heading's ancestor chain.

    `candidates` is that chain in DOCUMENT ORDER, so index 0 is the OUTERMOST
    (<html>) and the last entry is the heading node itself. Returns the index of
    the chosen container, or -1 if none of them qualifies.

    *** THE TRAP THIS FUNCTION EXISTS TO AVOID. The obvious implementation -
    *** "find the ancestor that contains the heading, take the first one" - is
    *** wrong in the worst possible way: EVERY ancestor of the heading contains
    *** the heading, and the first in document order is <body>. The scope then
    *** silently re-widens to the whole page, and the viewer TOOLBAR's "Move to
    *** trash" is back among the candidate options - on a stacked item that is
    *** the click that deletes photos this tool was never authorised to touch.
    ***
    *** So: scan from the INNERMOST outwards, stop at the SMALLEST ancestor that
    *** actually holds an option row, and reject <body>/<html> outright even if
    *** they do hold one. The direction of this loop is a safety property; see
    *** t_heading_walk_takes_the_innermost_ancestor_not_body.
    """
    for index in range(len(candidates) - 1, -1, -1):
        node = candidates[index]
        if (tag_of(node) or "").strip().lower() in NEVER_A_DIALOG_SCOPE:
            continue
        if holds_options(node):
            return index
    return -1


def classify_noop_failure(
    all_photos_seen: bool, lingering_dialog: bool, lingering_heading: bool,
    dialog_answered: bool = True,
) -> str:
    """Which failure kind a no-toast, still-present item really is.

    *** BUG B, v1.2.2. These two produce the identical symptom - no toast, the
    *** viewer did not close, the item is still there on a fresh reload - and
    *** v1.2.1 filed both of them as `mutation-no-op`:
    ***
    ***   a dialog is still on screen that this script could not answer
    ***       -> `stacked-dialog`. A CODE GAP. Never cooled down.
    ***   the dialog was answered and dismissed and the item survived anyway
    ***       -> `mutation-no-op`. The hypothetical delete quota.
    ***
    *** Because `mutation-no-op` is in THROTTLE_KINDS, the misfiling bought a
    *** 20-minute cooldown for a problem no amount of sleeping can fix: a
    *** selector. It also poisoned the only counter that could ever answer
    *** "does Google rate-limit the delete mutation at all?" - so keeping it
    *** honest is what makes a future non-zero reading mean something.

    v1.3.0 adds the third case, which is what the last real run actually hit:

        no dialog was ever answered - neither the '#' shortcut nor the toolbar
        fallback got one open -> `no-dialog`. Also a CODE GAP. A delete that
        was never REQUESTED cannot be evidence of a delete quota, and three of
        these in a row must not buy a cooldown either.
    """
    if all_photos_seen or lingering_dialog or lingering_heading:
        return FAIL_STACKED
    if not dialog_answered:
        return FAIL_NO_DIALOG
    return FAIL_MUTATION


class PhotosDriver:
    """Interface used by the run loop. Implemented by Playwright and by the fake."""

    def probe(self, url: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def trash_current(self) -> TrashOutcome:  # pragma: no cover - interface
        raise NotImplementedError

    def verify_trashed(self, url: str) -> str:  # pragma: no cover - interface
        """Re-check the permalink on a FRESH navigation after the settle delay."""
        raise NotImplementedError

    def recycle(self) -> str:  # pragma: no cover - interface
        """Throw away accumulated page state between cooldowns. Returns a note."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def nav_guard(url: str, allowed: Iterable[str]) -> None:
    """GUARDRAIL 1: the blast radius is the reviewed file and nothing else."""
    if not PERMALINK_RE.match(url):
        raise GuardrailViolation(f"refusing to navigate to non-permalink URL: {url!r}")
    if url not in allowed:
        raise GuardrailViolation(
            f"refusing to navigate to URL that is not in the input file: {url!r}"
        )


class PlaywrightDriver(PhotosDriver):
    """Attaches over CDP to an already-authenticated Chrome.

    GUARDRAIL 9: connect only. Never launches a browser, never creates a profile,
    never touches a sign-in form, holds no credentials.
    """

    def __init__(
        self,
        cdp_url: str,
        allowed_urls: Sequence[str],
        nav_timeout_ms: int = 30000,
        settle_ms: int = 1200,
        post_action_settle_ms: int = 3000,
        verbose: bool = False,
    ) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - env dependent
            raise DriverError(
                "playwright is not installed. Run:  python3 -m pip install --user "
                "playwright   (you do NOT need `playwright install`; this script "
                "only attaches to your own Chrome). Or re-run with --offline to "
                "plan without a browser."
            ) from exc

        self._allowed = set(allowed_urls)
        self._nav_timeout_ms = nav_timeout_ms
        self._settle_ms = settle_ms
        self._post_action_settle_ms = max(0, post_action_settle_ms)
        self._verbose = verbose
        # Containers that were inspected and held no options at all. Recorded
        # rather than silently skipped: "no scope found" was indistinguishable
        # from "no dialog up" in v1.2.1, and that is how BUG A hid.
        self.empty_scopes: List[str] = []

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            self._pw.stop()
            raise DriverError(
                f"could not attach to Chrome at {cdp_url}: {exc}\n"
                "Start Chrome yourself with:\n"
                '  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
                '--remote-debugging-port=9222 --user-data-dir="$HOME/chrome-cdp-profile"\n'
                "then sign in to Google in that window (by hand) and retry."
            ) from exc

        contexts = self._browser.contexts
        if not contexts:
            self.close()
            raise DriverError(
                "attached to Chrome but it has no browser context; open a window "
                "in that Chrome and sign in to Google first."
            )
        # Reuse the EXISTING context: that is where the user's session cookies are.
        self._context = contexts[0]
        self._page = self._context.new_page()
        self._page.set_default_timeout(nav_timeout_ms)

    # -- internals ---------------------------------------------------------- #

    def _page_text(self) -> str:
        try:
            return self._page.inner_text("body", timeout=5000) or ""
        except Exception:
            return ""

    def _any_selector_present(self, selectors: Sequence[str]) -> bool:
        for selector in selectors:
            try:
                if self._page.locator(selector).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _trashed_state_visible(self) -> bool:
        """Positive evidence that this item is already sitting in Trash."""
        return self._any_selector_present(TRASHED_STATE_SELECTORS)

    def _viewer_is_live(self) -> bool:
        """Positive evidence that an item viewer is actually rendered.

        If we can find neither 'gone' evidence nor viewer evidence, we refuse to
        guess: the caller raises and the circuit breaker does its job.
        """
        return self._any_selector_present(
            (
                '[aria-label*="More options" i]',
                '[aria-label*="Delete" i]',
                '[aria-label*="trash" i]',
                '[aria-label*="bin" i]',
                '[aria-label*="Open info" i]',
                '[aria-label*="Info" i]',
                "c-wiz img",
                "c-wiz video",
            )
        )

    def _navigate(self, url: str) -> None:
        try:
            self._page.goto(
                url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms
            )
        except Exception as exc:
            raise NavigationError(f"navigation failed: {exc}") from exc
        self._page.wait_for_timeout(self._settle_ms)

    def _note_empty_scope(self, where: str, labels: Sequence[str]) -> None:
        """Record a container that was inspected and held no options."""
        # Tests build this driver with __new__, so the list may not exist yet.
        scopes = getattr(self, "empty_scopes", None)
        if scopes is None:
            scopes = self.empty_scopes = []
        scopes.append(
            f"{where}: {len(labels)} text/label row(s) read, none of them an "
            f"option: {[str(label)[:40] for label in list(labels)[:12]]}"
        )

    def _tag_name_of(self, node) -> str:
        try:
            return (node.evaluate("element => element.tagName") or "").lower()
        except Exception:
            return ""

    def _holds_options(self, node) -> bool:
        return container_holds_options(self._dialog_option_labels(node))

    def _dialog_scope(self):
        """The open dialog container, or None if no dialog is up.

        EVERYTHING about answering a dialog is scoped to this element. Reading
        options page-wide is what put a toolbar "Move to trash" next to the
        dialog's own confirm button in the v1.2.0 diagnostics.

        BUG A (v1.2.2): the ARIA selectors are tried first, but a container only
        counts as the dialog if it actually holds an option row - in the failing
        production case none of them did, and the one that mattered carried no
        role=dialog/alertdialog/aria-modal at all. When no selector qualifies,
        the container is derived from the heading instead. An ARIA container with
        no options in it is kept as a last resort so that _answer_dialog() can
        refuse it loudly, with a dump, rather than the run concluding that no
        dialog was ever up.
        """
        option_less = None
        for selector in DIALOG_SCOPE_SELECTORS:
            try:
                locator = self._page.locator(selector)
                if locator.count() == 0:
                    continue
                scope = locator.last
                if not scope.is_visible(timeout=1000):
                    continue
            except Exception:
                continue
            labels = self._dialog_option_labels(scope)
            if container_holds_options(labels):
                return scope
            self._note_empty_scope(f"selector {selector}", labels)
            if option_less is None:
                option_less = scope
        from_heading = self._dialog_scope_from_heading()
        if from_heading is not None:
            return from_heading
        return option_less

    def _dialog_scope_from_heading(self):
        """Derive the dialog container by walking UP from the shared heading.

        The heading is still never a discriminator between the two dialog forms
        - this uses it only as a landmark for finding the container that holds
        the options, which are what decide.

        *** The ancestor chain is handed to pick_dialog_container() in document
        *** order and scanned from the INNERMOST outwards, stopping at the
        *** smallest ancestor that holds an option row, with <body>/<html>
        *** rejected outright. Taking the first ancestor "containing the
        *** heading" instead selects <body> - every ancestor contains the
        *** heading - and that re-widens the scope to the whole page.
        """
        if not stacked_heading_present(self._page_text()):
            return None
        try:
            heading = self._page.get_by_text(HEADING_LOCATOR_RE).last
            if heading.count() == 0:
                return None
            chain = heading.locator("xpath=ancestor-or-self::*")
            total = chain.count()
        except Exception:
            return None
        if total <= 0:
            return None
        # Document order, outermost first; keep only the innermost few, since a
        # dialog is a handful of nodes deep and the rest are page regions.
        start = max(0, total - MAX_SCOPE_ANCESTOR_DEPTH)
        candidates = [chain.nth(index) for index in range(start, total)]
        index = pick_dialog_container(
            candidates, self._tag_name_of, self._holds_options
        )
        if index < 0:
            self._note_empty_scope(
                f"heading ancestor walk ({len(candidates)} ancestor(s))", []
            )
            return None
        return candidates[index]

    def _role_option_labels(self, scope) -> List[str]:
        """Options inside `scope` that carry an interactive ROLE. Never clicks.

        Split out from _dialog_option_labels so the diagnostics (FIX 2) can
        report the role view and the text-row view SEPARATELY - which view saw
        an option, and which saw nothing, is the evidence that identified BUG A.
        """
        role_labels: List[str] = []
        for selector in DIALOG_OPTION_ROLE_SELECTORS:
            try:
                locator = scope.locator(selector)
                for index in range(min(locator.count(), 40)):
                    entry = locator.nth(index)
                    text = ""
                    try:
                        text = entry.inner_text(timeout=1000) or ""
                    except Exception:
                        text = ""
                    if not text.strip():
                        try:
                            text = entry.get_attribute("aria-label") or ""
                        except Exception:
                            text = ""
                    if text.strip():
                        role_labels.append(text.strip())
            except Exception:
                continue
        return role_labels

    def _dialog_option_labels(self, scope) -> List[str]:
        """Options INSIDE the given container, read BOTH ways. Never clicks.

        1. by ROLE, which finds the ordinary confirm button; and
        2. as VISIBLE TEXT ROWS, which is the only way the stacked dialog's
           options can be seen at all (BUG A: they are not buttons, so every
           role query above returns nothing for them).

        `scope` is required - there is no page-wide mode.
        """
        role_labels = self._role_option_labels(scope)

        text_rows: List[str] = []
        try:
            text_rows = dialog_lines(scope.inner_text(timeout=2000) or "")
        except Exception:
            text_rows = []

        # Roles first, so the ordinary confirm keeps the exact accessible name
        # it has always been clicked by; duplicates of one node are collapsed.
        return merge_option_labels(role_labels, text_rows)

    def _wait_for_dialog(self, budget_ms: int = DIALOG_WAIT_MS):
        """Poll for a delete dialog, returning its container or None.

        FIX 1: the shortcut is asynchronous, and for some items it does nothing
        at all. Waiting a bounded time and then reporting None is what lets the
        toolbar fallback fire instead of the run concluding "silent no-op".
        """
        deadline = time.monotonic() + max(0, budget_ms) / 1000.0
        while True:
            scope = self._dialog_scope()
            if scope is not None:
                return scope
            if time.monotonic() >= deadline:
                return None
            try:
                self._page.wait_for_timeout(DIALOG_POLL_MS)
            except Exception:
                return None

    def _is_inside_dialog(self, node) -> bool:
        """True if this node sits inside a dialog container.

        The toolbar fallback uses this to REFUSE such a node: the dialog's own
        buttons may only ever be reached through decide_dialog_action().
        """
        try:
            return bool(node.evaluate(INSIDE_DIALOG_JS))
        except Exception:
            # Cannot prove it is outside a dialog, so treat it as inside and
            # leave it alone. Failing closed costs a retry; failing open could
            # click an unvetted button in a stacked dialog.
            return True

    def _toolbar_trash_control(self):
        """The viewer toolbar's own "Move to trash" control, or None.

        Returns (locator, label). Every candidate must pass all of:
          * an accessible name that is exactly a trash action
            (is_toolbar_trash_label - never "All N photos", never a permanent
            delete), and
          * assert_safe_to_click(), the same last gate the dialog path uses, and
          * NOT being inside a dialog container.
        """
        for selector in TOOLBAR_TRASH_SELECTORS:
            try:
                locator = self._page.locator(selector)
                count = min(locator.count(), 20)
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                label = ""
                try:
                    label = candidate.get_attribute("aria-label") or ""
                except Exception:
                    label = ""
                if not label.strip():
                    try:
                        label = candidate.get_attribute("title") or ""
                    except Exception:
                        label = ""
                if not label.strip():
                    try:
                        label = candidate.inner_text(timeout=1000) or ""
                    except Exception:
                        label = ""
                label = label.strip()
                if not is_toolbar_trash_label(label):
                    continue
                # GUARDRAIL 1, same gate as the dialog path.
                assert_safe_to_click(label)
                if self._is_inside_dialog(candidate):
                    # The dialog's own confirm button. Not ours to press here.
                    continue
                try:
                    if not candidate.is_visible(timeout=1000):
                        continue
                except Exception:
                    continue
                return candidate, label
        return None, ""

    def _viewer_container_html(self) -> Tuple[str, str]:
        """(description, outerHTML) of the viewer's top-level container.

        FIX 2: when there is no dialog there is still markup worth dumping, and
        it is the thing we have been guessing at for three rounds.
        """
        for selector in VIEWER_CONTAINER_SELECTORS:
            try:
                locator = self._page.locator(selector).first
                if locator.count() == 0:
                    continue
                html = locator.evaluate("element => element.outerHTML") or ""
            except Exception:
                continue
            if html:
                return selector, str(html)
        return "none", ""

    def _failure_diagnostics(self, scope) -> str:
        """The dump for ANY no-toast failure. See FIX 2 (v1.3.0).

        *** Unconditional, and that is the entire point. Up to 1.2.2 the dump
        *** was gated on having DETECTED a dialog - so the one failure mode we
        *** could not explain, "no dialog was ever detected", produced no
        *** diagnostics whatsoever. Three rounds of guessing at markup came out
        *** of that. Whatever the state of the screen, this reports:
        ***   whether a dialog was detected, and which container was chosen;
        ***   the option labels seen (roles AND visible text rows, separately);
        ***   whether a toolbar trash control could be found;
        ***   the outerHTML of the dialog if there is one, else of the viewer's
        ***   own top-level container.
        """
        lines = ["failure diagnostics (v1.3.0, unconditional):"]
        lines.append(f"  dialog detected: {scope is not None}")
        if scope is not None:
            try:
                lines.append(f"  chosen scope: <{self._tag_name_of(scope) or '?'}>")
            except Exception:
                lines.append("  chosen scope: (could not read its tag)")
        else:
            lines.append("  chosen scope: none")

        role_labels: List[str] = []
        text_rows: List[str] = []
        if scope is not None:
            role_labels = self._role_option_labels(scope)
            try:
                text_rows = dialog_lines(scope.inner_text(timeout=2000) or "")
            except Exception:
                text_rows = []
        else:
            # No container: the page's own text can still hold the option rows,
            # and its buttons are worth listing for comparison.
            text_rows = dialog_lines(self._page_text())
        lines.append(f"  option labels by role: {role_labels}")
        lines.append(f"  option labels as text rows: {text_rows}")
        heading_up = stacked_heading_present(self._page_text())
        lines.append(f"  stacked heading present: {heading_up}")

        try:
            control, control_label = self._toolbar_trash_control()
        except GuardrailViolation:
            raise
        except Exception as exc:
            control, control_label = None, f"(lookup failed: {exc})"
        lines.append(
            f"  toolbar trash control found: {control is not None}"
            f"{f' ({control_label!r})' if control_label else ''}"
        )

        if scope is not None:
            lines.append(self._dialog_diagnostics(scope))
        else:
            where, html = self._viewer_container_html()
            truncated = html[:DIAGNOSTIC_HTML_CHARS]
            lines.append(
                f"  no dialog, so dumping the viewer container {where} "
                f"({len(html)} chars"
                f"{', truncated' if len(html) > len(truncated) else ''}):"
            )
            lines.append(f"    {truncated}")
        for note in getattr(self, "empty_scopes", []) or []:
            lines.append(f"  inspected but option-less -> {note}")
        return "\n".join(lines)

    def _dialog_diagnostics(self, scope) -> str:
        """Dump a dialog container for the log: WHAT its options are, not just
        what they are called.

        The v1.2.0/v1.2.1 diagnostics dumped accessible LABELS page-wide. That
        dump could not reveal the actual defect - that the stacked options are
        plain text rows with no role at all - and it took a screenshot from the
        user to find it. So this reports the container's outerHTML plus, per
        descendant, tag / role / clickable / text.
        """
        lines = ["dialog diagnostics (v1.2.2):"]
        try:
            dump = scope.evaluate(DIALOG_DUMP_JS)
        except Exception as exc:
            dump = None
            lines.append(f"  (could not dump the container: {exc})")
        if isinstance(dump, dict):
            html = str(dump.get("html") or "")
            truncated = html[:DIAGNOSTIC_HTML_CHARS]
            lines.append(
                f"  outerHTML ({len(html)} chars"
                f"{', truncated' if len(html) > len(truncated) else ''}):"
            )
            lines.append(f"    {truncated}")
            nodes = dump.get("nodes") or []
            lines.append(f"  descendants ({len(nodes)} shown):")
            for node in nodes:
                lines.append(
                    "    tag={tag} role={role} clickable={clickable} "
                    "text={text!r}".format(
                        tag=node.get("tag"),
                        role=node.get("role") or "-",
                        clickable=node.get("clickable"),
                        text=str(node.get("text") or "")[:DIAGNOSTIC_TEXT_CHARS],
                    )
                )
        labels = self._dialog_option_labels(scope)
        lines.append(f"  options read from this container: {labels}")
        for note in getattr(self, "empty_scopes", []) or []:
            lines.append(f"  inspected but option-less -> {note}")
        return "\n".join(lines)

    def _clickable_ancestor(self, node):
        """The nearest clickable ancestor of a text row, or None.

        BUG A: the stacked options are text rows. A row may be inert while its
        wrapper carries the click handler, so when the row itself will not take
        a click, the INNERMOST clickable ancestor is used - see
        CLICKABLE_ANCESTOR_XPATH for why [last()] and not [1].
        """
        try:
            candidate = node.locator(CLICKABLE_ANCESTOR_XPATH)
            if candidate.count() == 0:
                return None
            return candidate.first
        except Exception:
            return None

    def _assert_click_target_safe(self, target, choice: str) -> None:
        """Refuse a click target that covers any option other than `choice`.

        GUARDRAIL 1. Walking up to a clickable ancestor can land on a node that
        wraps the WHOLE option list; clicking that is clicking an unknown one of
        them, and on a stacked dialog one of them deletes the whole stack.
        """
        assert_safe_to_click(choice)
        text = ""
        try:
            text = target.inner_text(timeout=1000) or ""
        except Exception:
            text = ""
        wanted = " ".join(choice.lower().split())
        for row in dialog_lines(text):
            if " ".join(row.lower().split()) == wanted:
                continue
            if not (
                is_all_photos_option(row)
                or is_current_only_option(row)
                or is_confirm_option(row)
            ):
                # Icon text, a selection hint, a sublabel: not another option.
                continue
            # Refused, not a GuardrailViolation: NOTHING has been clicked, so
            # the item can simply stay pending. assert_safe_to_click() above is
            # still the gate on the label this would have clicked.
            stakes = (
                " - and that one deletes the whole stack"
                if is_all_photos_option(row)
                else ""
            )
            raise StackedDialogError(
                f"refusing to click the element found for {choice!r}: it also "
                f"covers the option {row!r}, so clicking it would be a guess "
                f"between two options{stakes}. NOTHING was clicked."
            )

    def _click_dialog_option(self, scope, kind: str, choice: str) -> str:
        """Click exactly one option inside the dialog, by its exact wording.

        Every locator here is scoped to the dialog container, and each candidate
        passes assert_safe_to_click()/_assert_click_target_safe() immediately
        before the click. Tried in order of certainty:
          1. role=button with that exact accessible name (the ordinary confirm);
          2. the other option roles, same exact name;
          3. the exact VISIBLE TEXT row - the only thing that matches a stacked
             option at all (BUG A), and
          4. that row's nearest clickable ancestor, when the row is inert.
        Returns a short note describing which route landed the click.
        """
        assert_safe_to_click(choice)
        exact = re.compile(rf"^\s*{re.escape(choice)}\s*$", re.I)
        routes: List[Tuple[str, Any]] = []
        try:
            routes.append(("role=button", scope.get_by_role("button", name=exact)))
        except Exception:
            pass
        for role in ("menuitem", "menuitemradio", "option", "radio", "link"):
            try:
                routes.append(
                    (f"role={role}", scope.get_by_role(role, name=exact))
                )
            except Exception:
                continue
        try:
            routes.append(("exact text row", scope.get_by_text(exact)))
        except Exception:
            pass

        attempts: List[str] = []
        for describe, locator in routes:
            try:
                if locator.count() == 0:
                    attempts.append(f"{describe}: no match")
                    continue
            except Exception as exc:
                attempts.append(f"{describe}: unusable ({exc})")
                continue
            node = locator.first
            targets: List[Tuple[str, Any]] = [(describe, node)]
            ancestor = self._clickable_ancestor(node)
            if ancestor is not None:
                targets.append((f"{describe} -> nearest clickable ancestor", ancestor))
            for target_describe, target in targets:
                try:
                    self._assert_click_target_safe(target, choice)
                    target.click(timeout=8000)
                    return target_describe
                except GuardrailViolation:
                    raise
                except Exception as exc:
                    attempts.append(f"{target_describe}: {exc}")
        raise StackedDialogError(
            f"decided on {choice!r} inside the {kind} delete dialog but could not "
            f"click it by any route, so NOTHING was clicked and this item stays "
            f"pending. Routes tried: {'; '.join(attempts) or '(none)'}"
        )

    def _dismiss_dialog(self) -> None:
        """Close a dialog we refuse to answer, so the next item starts clean.

        Escape only: on a dialog this script could not read, no button is known
        to be safe to press.
        """
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(400)
        except Exception:
            pass

    def _answer_dialog(self, scope) -> Tuple[str, str]:
        """Answer an open delete dialog. Returns (kind, label clicked).

        Raises StackedDialogError without clicking anything when the options
        inside the dialog match neither known form; the dialog is dismissed
        first so the run can carry on with the next item. Either way, a dialog
        this script could not answer is dumped in full (GUARDRAIL C) - reading
        labels alone is what hid BUG A for two versions.
        """
        labels = self._dialog_option_labels(scope)
        try:
            kind, choice = decide_dialog_action(labels)
        except StackedDialogError as exc:
            dump = self._dialog_diagnostics(scope)
            self._dismiss_dialog()
            raise StackedDialogError(f"{exc}\n{dump}") from exc

        # Click by exact wording, never by index: position is not safety. Scoped
        # to the dialog, so a toolbar button of the same name cannot win.
        try:
            route = self._click_dialog_option(scope, kind, choice)
        except StackedDialogError as exc:
            dump = self._dialog_diagnostics(scope)
            self._dismiss_dialog()
            raise StackedDialogError(f"{exc}\n{dump}") from exc
        # Kept for the log line: which of the four routes landed the click says
        # whether this dialog's options were buttons or text rows.
        self._last_click_route = route
        self._page.wait_for_timeout(600)
        return kind, choice

    def _wait_for_trashed_toast(self) -> str:
        """Poll for the confirmation toast for up to the post-action settle time."""
        budget_ms = max(self._post_action_settle_ms, 1000)
        deadline = time.monotonic() + budget_ms / 1000.0
        while True:
            snippet = toast_text_says_trashed(self._page_text())
            if snippet:
                return snippet
            if time.monotonic() >= deadline:
                return ""
            try:
                self._page.wait_for_timeout(250)
            except Exception:
                return ""

    # -- interface ---------------------------------------------------------- #

    def probe(self, url: str) -> str:
        nav_guard(url, self._allowed)
        self._navigate(url)

        current = self._page.url or ""
        if "accounts.google.com" in current or "signin" in current:
            # Not a rate limit: waiting cannot sign you in. Abort immediately.
            raise FatalDriverError(
                "Chrome is not signed in to Google Photos (redirected to a sign-in "
                "page). Sign in by hand in that Chrome window and re-run; this "
                "script will not do it for you."
            )

        state = classify_page_state(
            current_url=current,
            permalink=url,
            text_getter=self._page_text,
            viewer_getter=self._viewer_is_live,
            trashed_state_getter=self._trashed_state_visible,
        )
        if state != UNKNOWN:
            return state
        raise DriverError(
            "could not tell whether the item is present or gone - the page matched "
            "neither a live item viewer nor a 'not available' page. The UI may have "
            "changed, or a challenge/consent page may be in the way. Look at the "
            "Chrome window."
        )

    def trash_current(self) -> TrashOutcome:
        """Move the currently-open item to Trash, reporting what was observed."""
        page = self._page
        if not (page.url or "").startswith(PHOTO_PREFIX):
            raise DriverError(f"refusing to trash: not on an item page ({page.url})")

        # The keystroke goes to whichever tab Chrome considers active, so raise
        # ours first rather than depending on window-focus luck.
        try:
            page.bring_to_front()
        except Exception:
            pass

        # '#' is the Google Photos viewer shortcut for "move to trash". Using the
        # shortcut avoids hunting through a frequently-reshuffled toolbar, and it
        # worked for thousands of real deletions.
        try:
            page.keyboard.press("#")
        except Exception as exc:
            raise DriverError(f"could not send the trash shortcut: {exc}") from exc

        # FIX 1: wait for a dialog rather than assuming one appeared. For some
        # items the keystroke silently does nothing - no dialog, nothing to
        # scope, nothing to classify - which is exactly what made three real
        # items look like a silently-ignored delete.
        action_route = ROUTE_SHORTCUT
        shortcut_opened_dialog = True
        scope = self._wait_for_dialog()
        if scope is None:
            shortcut_opened_dialog = False
            # Fall back to the viewer toolbar's own control. It is OUTSIDE the
            # dialog by construction (_toolbar_trash_control refuses anything
            # inside one), so this press OPENS a dialog; answering it still goes
            # through decide_dialog_action() below, unchanged.
            control, control_label = self._toolbar_trash_control()
            if control is not None:
                assert_safe_to_click(control_label)
                try:
                    control.click(timeout=8000)
                    action_route = ROUTE_TOOLBAR
                except Exception as exc:
                    raise DriverError(
                        f"the trash shortcut opened no dialog and the toolbar "
                        f"control {control_label!r} could not be clicked: {exc}"
                    ) from exc
                scope = self._wait_for_dialog()

        note = action_route
        stacked = False
        choice = ""

        # Answer the confirmation dialog, whichever of its two forms is up. The
        # dialog element is located first and everything is read from INSIDE it:
        # which form this is can only be told from its options (the heading is
        # identical for both), and the page outside the dialog has a "Move to
        # trash" toolbar button that would re-open the dialog, not confirm it.
        dialog_answered = False
        if scope is not None:
            # Raises (having clicked nothing) if neither known form matches.
            kind, choice = self._answer_dialog(scope)
            stacked = kind == DIALOG_STACKED
            dialog_answered = True
            note = f"{action_route}+{kind}({choice})"
            route = getattr(self, "_last_click_route", "")
            if route:
                note += f"[{route}]"
        elif stacked_heading_present(self._page_text()):
            # Corroborating signal only: the heading is up but no dialog
            # container could be located. Verification below decides the truth.
            note = f"{action_route}+heading-without-dialog"
        else:
            note = f"{action_route}+no-dialog"

        toast = self._wait_for_trashed_toast()
        if toast and self._any_selector_present(('[aria-label*="Undo" i]',)):
            toast = f"{toast} (Undo offered)"
        left_viewer = not (page.url or "").startswith(PHOTO_PREFIX)
        if toast:
            note += "+toast"
        elif left_viewer:
            note += "+left-viewer"

        # BUG B: when there is no positive evidence, WHAT IS STILL ON SCREEN is
        # what separates a dialog this script could not answer (a code gap) from
        # a delete that was accepted and silently ignored (a quota). v1.2.1 did
        # not look, called every one of them `mutation-no-op`, and bought a
        # 20-minute cooldown for a selector bug. Only read in that case: on the
        # happy path it is one more round-trip for nothing.
        lingering_dialog = False
        lingering_heading = False
        all_photos_seen = False
        diagnostics = ""
        if not toast and not left_viewer:
            lingering_heading = stacked_heading_present(self._page_text())
            after = self._dialog_scope()
            lingering_dialog = after is not None
            if after is not None:
                labels = self._dialog_option_labels(after)
            else:
                # No container, but the heading's own page text can still show
                # the options as rows.
                labels = dialog_lines(self._page_text())
            all_photos_seen = any(is_all_photos_option(label) for label in labels)
            if lingering_dialog or lingering_heading or all_photos_seen:
                note += "+dialog-still-up"
                if dialog_answered:
                    note += "(after answering)"
            # FIX 2: UNCONDITIONAL. Every no-toast failure gets the full dump,
            # including - especially - the case where no dialog was ever
            # detected, which is the one that produced no evidence at all up to
            # v1.2.2 and cost three rounds of guessing at markup.
            diagnostics = self._failure_diagnostics(after)
            if not shortcut_opened_dialog:
                diagnostics = (
                    "  the '#' shortcut opened no dialog; "
                    f"route taken: {action_route}\n" + diagnostics
                )

        return TrashOutcome(
            note=note,
            toast_seen=bool(toast),
            toast_text=toast,
            left_viewer=left_viewer,
            stacked=stacked,
            stacked_choice=choice if stacked else "",
            lingering_dialog=lingering_dialog,
            lingering_heading=lingering_heading,
            all_photos_seen=all_photos_seen,
            diagnostics=diagnostics,
            route=action_route,
            shortcut_opened_dialog=shortcut_opened_dialog,
            dialog_answered=dialog_answered,
        )

    def verify_trashed(self, url: str) -> str:
        """Absence check, done properly: settle, then navigate to the permalink again.

        Never inspects the DOM the trash action left behind - that stale viewer is
        exactly what used to report a successful deletion as a failure.
        """
        nav_guard(url, self._allowed)
        remaining_ms = self._post_action_settle_ms
        if remaining_ms:
            try:
                self._page.wait_for_timeout(remaining_ms)
            except Exception:
                time.sleep(remaining_ms / 1000.0)
        return self.probe(url)

    def _blank_out(self) -> None:
        """Navigate our own tab to the inert blank page.

        Takes no URL argument on purpose: this is the one navigation that does not
        go through nav_guard(), so it must not be possible to point it anywhere.
        """
        self._page.goto("about:blank", timeout=self._nav_timeout_ms)

    def recycle(self) -> str:
        """Drop accumulated SPA state between cooldowns.

        Thousands of client-side navigations through one Google Photos tab leave a
        lot behind. Preferred route is a brand-new tab in the SAME context, which
        keeps the user's session cookies and starts out blank, so there is no
        navigation to guard. GUARDRAIL 9: this only ever touches our own tab - the
        user's Chrome process and their other tabs are never restarted or closed.
        """
        try:
            fresh = self._context.new_page()
        except Exception as exc:
            # Could not get a tab; settle for emptying the one we have.
            try:
                self._blank_out()
                return f"blanked existing tab (new tab failed: {exc})"
            except Exception as blank_exc:
                return f"could not recycle the tab ({blank_exc}); continuing anyway"
        old = self._page
        self._page = fresh
        self._page.set_default_timeout(self._nav_timeout_ms)
        try:
            old.close()
        except Exception:
            return "opened a fresh tab (the old one would not close)"
        return "opened a fresh tab and closed the old one"

    def close(self) -> None:
        # GUARDRAIL 9: close only our own tab. Never close the user's browser.
        for closer in (
            lambda: self._page.close(),
            lambda: self._browser.close(),
            lambda: self._pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass


class FakeDriver(PhotosDriver):
    """Scripted driver for --self-test. Never touches a browser or the network."""

    def __init__(
        self,
        allowed_urls: Sequence[str],
        states: Optional[Dict[str, str]] = None,
        behaviour: Optional[Dict[str, str]] = None,
    ) -> None:
        self._allowed = set(allowed_urls)
        self.states: Dict[str, str] = dict(states or {})
        # behaviour[url]:
        #   "ok"         - trashed, confirmation toast shown
        #   "silent"     - trashed, no toast, and the viewer DOM goes stale (it
        #                  still shows the item until the next fresh navigation)
        #   "toast_stale"- trashed, toast shown, DOM stale: the real-world case
        #                  that used to be misreported as a failure
        #   "left"       - trashed, no toast, but the viewer was closed
        #   "noop"       - nothing happened at all
        #   "raise"      - the UI broke
        #   "ordinary"   - a SINGLE item: the ordinary confirm dialog appears
        #                  (Cancel + Move to trash) and is confirmed. The
        #                  overwhelmingly common real case.
        #   "stacked"    - a GROUPED item: the two-option dialog appears with the
        #                  real wording, so "Current photo only" is clicked and
        #                  only this item is trashed
        #   "stacked_weird"  - the grouped dialog appears, but the safe option is
        #                  worded differently: nothing may be clicked
        #   "stacked_all_only" - the dialog offers ONLY "All N photos": the
        #                  nightmare case, and nothing may be clicked
        #   "dialog_weird"   - a dialog with neither known option: fail safe
        #   "stack_unseen"   - *** THE BUG B CASE, verbatim from production: ***
        #                  the stacked dialog is on screen but its container was
        #                  never located, so nothing is clicked, no toast
        #                  appears, the dialog stays up and the item survives.
        #                  v1.2.1 filed this as `mutation-no-op` and slept 20
        #                  minutes over it.
        self.behaviour: Dict[str, str] = dict(behaviour or {})
        self.probes: List[str] = []
        self.trashed: List[str] = []
        self.verifications: List[str] = []
        self.recycles = 0
        self.closed = False
        # Every dialog option this fake ever clicked, so a test can assert that
        # "All N photos" was never one of them.
        self.clicked: List[str] = []
        self.dialogs_seen = 0
        # Page-wide labels that were available but outside the dialog, so a test
        # can prove the toolbar "Move to trash" was never the one clicked.
        self.page_labels_offered: List[List[str]] = []
        # FIX 1: which route each action took, and how often the shortcut
        # silently opened nothing.
        self.routes: List[str] = []
        self.shortcut_failures = 0
        self._current: Optional[str] = None
        self._stale: Optional[str] = None

    def probe(self, url: str) -> str:
        nav_guard(url, self._allowed)
        self.probes.append(url)
        self._current = url
        if self._stale == url:
            # A stale viewer lies: it keeps showing an item that is already in
            # Trash. Only a fresh navigation clears it.
            self._stale = None
            return PRESENT
        return self.states.get(url, PRESENT)

    # Option labels each fake dialog offers, INSIDE the dialog element. Verbatim
    # from the real UI: "ordinary" is the single-item confirm (Cancel + Move to
    # trash) and "stacked" is the grouped-item dialog. Both carry the same
    # heading on the page, which is exactly why the heading cannot be used.
    DIALOG_OPTIONS: Dict[str, List[str]] = {
        "ordinary": ["Cancel", "Move to trash"],
        "stacked": ["All 8 photos", "Current photo only"],
        "stacked_weird": ["All 8 photos", "This photo only", "Just this one"],
        "stacked_all_only": ["All 8 photos"],
        "dialog_weird": ["Cancel", "Discard"],
    }

    # Options that exist on the PAGE but OUTSIDE the dialog element. Taken from
    # the real v1.2.0 diagnostic dump. The toolbar's own "Move to trash" is here:
    # if a test ever sees it clicked, the dialog scoping has regressed.
    PAGE_OPTIONS_OUTSIDE_DIALOG: List[str] = [
        "Main menu", "Close", "Search your photos and albums",
        "Clear search query", "Create and add photos", "Help & Feedback",
        "More options", "Turn on motion", "Share", "Edit", "Zoom in",
        "Open info", "Favorite", "Move to trash", "More options",
        "Google apps", "Expand albums list",
    ]

    def trash_current(self) -> TrashOutcome:
        assert self._current is not None, "trash_current called before probe"
        url = self._current
        mode = self.behaviour.get(url, "ok")
        if mode == "raise":
            raise DriverError("fake UI breakage")

        choice = ""
        kind = ""
        if mode == "shortcut_dead":
            # FIX 1: the '#' keystroke does nothing, so no dialog opens. The
            # toolbar fallback then opens the ordinary confirm and it is
            # answered through the normal decision path.
            self.shortcut_failures += 1
            self.dialogs_seen += 1
            kind, choice = decide_dialog_action(self.DIALOG_OPTIONS["ordinary"])
            assert_safe_to_click(choice)
            self.clicked.append(choice)
            self.routes.append(ROUTE_TOOLBAR)
            self.trashed.append(url)
            self.states[url] = GONE
            return TrashOutcome(
                note=f"fake:{ROUTE_TOOLBAR}+{kind}({choice})",
                toast_seen=True,
                toast_text="Moved to trash",
                route=ROUTE_TOOLBAR,
                shortcut_opened_dialog=False,
            )
        if mode == "shortcut_dead_no_toolbar":
            # The shortcut did nothing AND no toolbar control could be found:
            # nothing was clicked, so the item survives. FIX 2 says this must
            # still come back with a full dump even though no dialog was seen.
            self.shortcut_failures += 1
            self.trashed.append(url)
            return TrashOutcome(
                note=f"fake:{ROUTE_SHORTCUT}+no-dialog",
                route=ROUTE_SHORTCUT,
                shortcut_opened_dialog=False,
                dialog_answered=False,
                diagnostics=(
                    "failure diagnostics (fake):\n"
                    "  dialog detected: False\n"
                    "  chosen scope: none\n"
                    "  option labels by role: []\n"
                    "  option labels as text rows: []\n"
                    "  toolbar trash control found: False\n"
                    "  no dialog, so dumping the viewer container c-wiz"
                ),
            )
        if mode == "stack_unseen":
            # No container was found, so the real decision logic is never even
            # reached: nothing is clicked and the dialog is left standing.
            self.dialogs_seen += 1
            self.trashed.append(url)
            return TrashOutcome(
                note="fake:stack_unseen",
                dialog_answered=False,
                lingering_dialog=True,
                lingering_heading=True,
                all_photos_seen=True,
                diagnostics="fake dialog dump: tag=div role=None clickable=False "
                "text='Current photo only'",
            )
        if mode in self.DIALOG_OPTIONS:
            # Run the REAL decision logic against the real wording, so the
            # safety property is what is under test rather than a restatement of
            # it. Only DIALOG-SCOPED labels are passed, mirroring the driver;
            # decide_dialog_action raises before anything is clicked.
            self.dialogs_seen += 1
            self.page_labels_offered.append(list(self.PAGE_OPTIONS_OUTSIDE_DIALOG))
            kind, choice = decide_dialog_action(self.DIALOG_OPTIONS[mode])
            assert_safe_to_click(choice)
            self.clicked.append(choice)

        self.trashed.append(url)
        if mode != "noop":
            self.states[url] = GONE
        if mode in ("silent", "toast_stale"):
            self._stale = url
        toast = (
            "Moved to trash"
            if mode in ("ok", "toast_stale", "stacked", "ordinary")
            else ""
        )
        self.routes.append(ROUTE_SHORTCUT)
        return TrashOutcome(
            note=f"fake:{mode}",
            toast_seen=bool(toast),
            toast_text=toast,
            left_viewer=mode == "left",
            stacked=kind == DIALOG_STACKED,
            stacked_choice=choice if kind == DIALOG_STACKED else "",
            route=ROUTE_SHORTCUT,
            diagnostics=(
                "" if toast or mode == "left" else
                "failure diagnostics (fake): dialog detected: False"
            ),
        )

    def verify_trashed(self, url: str) -> str:
        nav_guard(url, self._allowed)
        self.verifications.append(url)
        # A fresh navigation: whatever the action left on screen is discarded.
        self._stale = None
        return self.probe(url)

    def recycle(self) -> str:
        self.recycles += 1
        self._current = None
        self._stale = None
        return f"fake tab recycle #{self.recycles}"

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    execute: bool = False
    limit: int = 25
    delay: float = 5.0
    jitter: float = 0.35
    max_consecutive_failures: int = 3
    verbose: bool = False
    journal_path: str = DEFAULT_JOURNAL
    # Cooldowns (GUARDRAIL 7 and 10).
    cooldown: float = 1200.0
    cooldown_backoff: float = 2.0
    cooldown_cap: float = 7200.0
    max_cooldowns: int = 6
    post_cooldown_window: int = 3
    # Adaptive pacing.
    delay_growth: float = 1.5
    delay_max: float = 30.0
    heartbeat_every: int = 100
    # FIX 3 (v1.3.0): how many failed attempts an item gets before it is
    # quarantined out of the default queue, and whether to let the quarantined
    # ones back in.
    max_attempts: int = 3
    retry_quarantined: bool = False


@dataclass
class Summary:
    considered: int = 0
    skipped_done: int = 0
    attempted: int = 0
    deleted: int = 0
    already_gone: int = 0
    failed: int = 0
    # Per-kind failure counts, so the limiter layers and the code gaps stay
    # visibly separate. Keyed by the FAIL_* constants.
    failed_by_kind: Dict[str, int] = field(
        default_factory=lambda: {kind: 0 for kind in FAILURE_KINDS}
    )
    # Grouped/stacked dialogs met, and how many of those were answered safely
    # with "Current photo only". stacked_dialogs - stacked_handled is the number
    # that failed safe without clicking anything.
    stacked_dialogs: int = 0
    stacked_handled: int = 0
    invalid: int = 0
    duplicates: int = 0
    remaining: int = 0
    aborted_reason: str = ""
    abort_kind: str = ""
    cooldowns: int = 0
    # Cooldowns the user cut short with a single Ctrl-C. A subset of
    # `cooldowns`: a skipped cooldown still counts (GUARDRAIL 10).
    cooldowns_skipped: int = 0
    cooldown_seconds: float = 0.0
    starting_delay: float = 0.0
    final_delay: float = 0.0
    elapsed_seconds: float = 0.0
    interrupted: bool = False
    # FIX 1 (v1.3.0): how the successful actions were performed. A non-zero
    # toolbar count is the measure of how often the '#' shortcut silently does
    # nothing - the thing we could only guess at before.
    deleted_by_route: Dict[str, int] = field(
        default_factory=lambda: {route: 0 for route in ACTION_ROUTES}
    )
    shortcut_opened_no_dialog: int = 0
    toolbar_fallbacks: int = 0
    # FIX 3 (v1.3.0): the queue composition. Fresh items go first; items with
    # prior failures are retried after them; items past --max-attempts are
    # quarantined out of the run entirely unless --retry-quarantined.
    fresh_attempted: int = 0
    retried_attempted: int = 0
    quarantined: int = 0
    quarantined_urls: List[str] = field(default_factory=list)

    @property
    def deleted_via_toolbar(self) -> int:
        return self.deleted_by_route.get(ROUTE_TOOLBAR, 0)

    @property
    def deleted_via_shortcut(self) -> int:
        return self.deleted_by_route.get(ROUTE_SHORTCUT, 0)

    @property
    def failed_navigation(self) -> int:
        return self.failed_by_kind.get(FAIL_NAVIGATION, 0)

    @property
    def failed_stacked(self) -> int:
        return self.failed_by_kind.get(FAIL_STACKED, 0)

    @property
    def failed_no_dialog(self) -> int:
        return self.failed_by_kind.get(FAIL_NO_DIALOG, 0)

    @property
    def failed_mutation(self) -> int:
        return self.failed_by_kind.get(FAIL_MUTATION, 0)

    @property
    def failed_other(self) -> int:
        return self.failed_by_kind.get(FAIL_OTHER, 0)


def _sleep_with_jitter(cfg: Config, log) -> None:
    """GUARDRAIL 4: human-paced, with jitter so the cadence is not machine-regular."""
    if cfg.delay <= 0:
        return
    low = max(0.0, cfg.delay * (1.0 - cfg.jitter))
    high = cfg.delay * (1.0 + cfg.jitter)
    nap = random.uniform(low, high)
    if cfg.verbose:
        log(f"    (pausing {nap:.1f}s)")
    time.sleep(nap)


# --------------------------------------------------------------------------- #
# Cooldowns: ride out the trash-mutation limiter instead of abandoning the run
# --------------------------------------------------------------------------- #


def cooldown_seconds_for(cfg: Config, cooldowns_taken: int) -> float:
    """Exponential backoff for the Nth cooldown, capped by --cooldown-cap.

    cooldowns_taken is how many have already happened, so the first call gets the
    base --cooldown: 1200, 2400, 4800, then flat at the cap.
    """
    if cfg.cooldown <= 0:
        return 0.0
    growth = cfg.cooldown_backoff ** max(0, cooldowns_taken)
    return min(cfg.cooldown * growth, max(0.0, cfg.cooldown_cap))


def grown_delay(cfg: Config) -> float:
    """The inter-item delay to resume at after a cooldown.

    Being throttled is proof the current pace is too fast, so never resume at it.
    """
    grown = max(cfg.delay, 0.0) * max(1.0, cfg.delay_growth)
    if cfg.delay_max > 0:
        grown = min(grown, cfg.delay_max)
    return grown


@dataclass
class Queue:
    """How a run's pending items were split up. See plan_queue()."""

    fresh: List[Item] = field(default_factory=list)
    retries: List[Item] = field(default_factory=list)
    quarantined: List[Item] = field(default_factory=list)

    def ordered(self) -> List[Item]:
        """Never-attempted items first, previously-failed ones after them."""
        return self.fresh + self.retries


def plan_queue(
    pending: Sequence[Item], journal: Journal, max_attempts: int,
    retry_quarantined: bool = False,
) -> Queue:
    """Split the pending items into fresh / retry / quarantined.

    *** FIX 3 (v1.3.0), and the reason a real run did no work at all. The
    *** journal is read back in input order, so three stubborn items sitting
    *** near the front were retried FIRST on every single run. They failed,
    *** produced zero successes, and tripped the "not one item has succeeded
    *** since the start of the run" safeguard - which aborted the run before it
    *** reached any of the 9,753 items that would have worked. The safeguard is
    *** right; the ORDERING was wrong.
    ***
    *** So: never-attempted items go first, items with a failure history are
    *** retried only after them, and an item that has burned through
    *** --max-attempts is QUARANTINED - skipped by default, reported in its own
    *** bucket, and still in the journal. Nothing is lost and nothing is
    *** deleted twice: this only reorders and filters work that was already
    *** pending.
    """
    cap = max(1, int(max_attempts))
    queue = Queue()
    for item in pending:
        failures = journal.failure_count(item.url)
        if failures <= 0:
            queue.fresh.append(item)
        elif failures < cap or retry_quarantined:
            queue.retries.append(item)
        else:
            queue.quarantined.append(item)
    return queue


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


@dataclass
class CooldownSleep:
    """What a cooldown sleep actually did, so the caller can log and count it."""

    requested: float = 0.0
    slept: float = 0.0
    # True when the user pressed Ctrl-C once to skip the rest of the wait. The
    # cooldown still happened as far as the policy is concerned; only the
    # sleeping was cut short.
    skipped: bool = False


def cooldown_was_skipped(result: Any) -> bool:
    """True if a cooldown sleep was cut short by a single Ctrl-C.

    Reads the flag defensively: a sleeper injected by a test (or any caller
    written against the 1.2.2 signature, which returned None) simply never
    reports a skip.
    """
    return bool(getattr(result, "skipped", False))


def cooldown_slept_seconds(result: Any, requested: float) -> float:
    """How long a cooldown sleep really lasted, falling back to what was asked."""
    slept = getattr(result, "slept", None)
    if slept is None:
        return max(0.0, requested)
    return max(0.0, float(slept))


def _sleep_returning_false(seconds: float) -> bool:
    """Plain sleep for when no SIGINT handler could be installed."""
    time.sleep(seconds)
    return False


def interruptible_sleep(
    total: float,
    log,
    slice_seconds: float = 5.0,
    report_every: float = 300.0,
    skip_window: float = COOLDOWN_SKIP_WINDOW_SECONDS,
    clock=time.monotonic,
    waiter=None,
) -> CooldownSleep:
    """Sleep out a cooldown, where Ctrl-C means SKIP THE WAIT - not quit.

    *** THE v1.2.3 CHANGE. Up to 1.2.2 a Ctrl-C anywhere, including inside a
    *** cooldown, ended the run. That was the original spec and it was the
    *** wrong default: someone watching a 20-minute sleep almost always wants
    *** to get on with it, not to stop. So, while a cooldown is sleeping:
    ***
    ***   one press  -> abandon the remaining sleep and carry on with the next
    ***                 item. The cooldown still COUNTS toward --max-cooldowns
    ***                 and the delay still grows: the throttle condition
    ***                 really did occur, and skipping the wait does not undo
    ***                 it (see breaker_reached, which says so in the log).
    ***   two presses within `skip_window` -> the escape hatch. The handler
    ***                 raises KeyboardInterrupt, which unwinds the run loop
    ***                 exactly as before: journal flushed, summary printed,
    ***                 exit 130.
    ***
    *** OUTSIDE a cooldown nothing changes, because the handler is installed
    *** only for the duration of this call and restored in a `finally`. After
    *** a skip, Ctrl-C is back to meaning "stop the run" immediately.

    If no handler can be installed (not the main thread, no signal support),
    this degrades to the 1.2.2 behaviour: KeyboardInterrupt propagates and the
    run ends. `clock` and `waiter` exist so the self-test can drive the slicing
    logic without real time passing.
    """
    started = clock()
    requested = max(0.0, float(total))
    if total <= 0:
        return CooldownSleep(requested=requested, slept=0.0, skipped=False)

    deadline = started + total
    next_report = started + report_every
    pressed = threading.Event()
    presses = {"count": 0}

    def on_sigint(_signum, _frame) -> None:
        presses["count"] += 1
        if presses["count"] >= 2:
            # The user means it. Unwind the run the way a Ctrl-C always has.
            raise KeyboardInterrupt
        pressed.set()

    previous_handler: Any = None
    armed = False
    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, on_sigint)
        armed = True
    except (AttributeError, OSError, ValueError, RuntimeError):
        # Not the main thread, or a platform without SIGINT. Old behaviour.
        armed = False

    if waiter is None:
        # Event.wait returns as soon as the handler sets the flag, so a press
        # lands at once instead of behind the rest of the current slice.
        waiter = pressed.wait if armed else _sleep_returning_false

    try:
        while True:
            now = clock()
            remaining = deadline - now
            if remaining <= 0:
                return CooldownSleep(requested, now - started, False)
            if report_every > 0 and now >= next_report:
                log(f"    ... still cooling down, {format_duration(remaining)} to "
                    f"go (Ctrl-C to skip this cooldown - press twice to quit)")
                next_report = now + report_every
            if not waiter(min(max(0.01, slice_seconds), remaining)):
                continue

            # First press: skip the rest of the wait, but stay listening long
            # enough for a second press to mean "no, really stop".
            log(f"    ^C skipping the rest of this cooldown - press Ctrl-C again "
                f"within {max(0.0, skip_window):.0f}s to stop the run instead")
            pressed.clear()
            if skip_window > 0:
                # A second press raises KeyboardInterrupt from the handler and
                # unwinds straight out of here, through the finally below.
                time.sleep(skip_window)
            return CooldownSleep(requested, clock() - started, True)
    finally:
        # GUARDRAIL 10: the handler belongs to this call only. Whatever happens
        # above - normal return, skip, or KeyboardInterrupt - Ctrl-C goes back
        # to meaning "stop the run" before this function returns.
        if armed:
            signal.signal(signal.SIGINT, previous_handler)


def run(  # noqa: C901 - one linear item loop; splitting it hides the control flow
    items: Sequence[Item],
    driver: PhotosDriver,
    journal: Journal,
    cfg: Config,
    log=print,
    sleeper=_sleep_with_jitter,
    cooldown_sleeper=interruptible_sleep,
) -> Summary:
    summary = Summary(
        considered=len(items), starting_delay=cfg.delay, final_delay=cfg.delay
    )
    started = time.monotonic()

    pending = [item for item in items if not journal.is_done(item.url)]
    summary.skipped_done = len(items) - len(pending)

    # FIX 3: fresh items first, retries after them, quarantined ones out.
    queue = plan_queue(pending, journal, cfg.max_attempts, cfg.retry_quarantined)
    summary.quarantined = len(queue.quarantined)
    summary.quarantined_urls = [item.url for item in queue.quarantined]
    runnable = queue.ordered()
    batch = runnable[: max(0, cfg.limit)]
    summary.remaining = max(0, len(pending) - len(batch))

    log("")
    log(
        f"{len(items)} item(s) in input; {summary.skipped_done} already done per "
        f"journal; {len(pending)} pending; acting on {len(batch)} this run "
        f"(--limit {cfg.limit})."
    )
    batch_fresh = sum(1 for item in batch if journal.failure_count(item.url) == 0)
    log(
        f"queue order: {batch_fresh} never-attempted item(s) first, then "
        f"{len(batch) - batch_fresh} retry(ies) of items that failed before "
        f"(--max-attempts {cfg.max_attempts})."
    )
    if queue.quarantined:
        log(
            f"QUARANTINED: {len(queue.quarantined)} item(s) have failed "
            f"{cfg.max_attempts}+ times and are being skipped so they cannot "
            f"block the run. They stay in the journal and in the summary; "
            f"re-include them with --retry-quarantined."
        )
    elif cfg.retry_quarantined:
        log("--retry-quarantined: items past --max-attempts are included again.")
    if not cfg.execute:
        log("DRY RUN: nothing will be deleted and nothing will be journalled.")
    elif cfg.cooldown > 0 and cfg.max_cooldowns > 0:
        log(
            f"throttling policy: {cfg.max_consecutive_failures} consecutive "
            f"{'/'.join(sorted(THROTTLE_KINDS))} failures -> cool down "
            f"{format_duration(cfg.cooldown)} (x{cfg.cooldown_backoff} each time, "
            f"at most {cfg.max_cooldowns}), then resume slower. Other failure "
            f"kinds never cool down."
        )
    log("")

    # Breaker / cooldown state.
    #
    # TWO SEPARATE COUNTERS, because the failure kinds mean different things:
    #
    #   consecutive_throttle - only THROTTLE_KINDS, i.e. navigation timeouts and
    #       trash-mutation no-ops. This is the throttle detector and the only
    #       counter that can trigger a cooldown. Navigation throttling is real
    #       and has been observed twice (~2s spacing dying at item 17, and a
    #       cold-start timeout after an idle gap), so waiting it out is right.
    #       A stacked-dialog failure is excluded: it is a code gap, and sleeping
    #       cannot teach this script a new button name.
    #   consecutive_failures - failures of ANY kind. This is the plain circuit
    #       breaker that stops the run grinding on pointlessly, as in v1.1.0.
    #
    # "Progress since resume" is what separates throttling from breakage: a
    # deleted or already-gone item proves the page is still understood and the
    # session is still good, so repeated failures AFTER one are worth waiting
    # out. Zero progress is not something sleeping can fix.
    consecutive_failures = 0
    consecutive_throttle = 0
    progress_since_resume = 0
    attempts_since_resume = 0
    # FIX 3: attempts on NEVER-ATTEMPTED items only. The zero-successes
    # safeguard exists to catch a changed UI, and a known-bad item failing again
    # is not new information about the UI - so only these count toward it.
    fresh_attempts_since_resume = 0
    streak_kinds: List[str] = []

    def is_fresh(item: Item) -> bool:
        """True if this item has no failure history in the journal."""
        return journal.failure_count(item.url) == 0

    def note_attempt(item: Item) -> None:
        """Count an attempt, keeping fresh items and retries apart."""
        nonlocal attempts_since_resume, fresh_attempts_since_resume
        attempts_since_resume += 1
        if is_fresh(item):
            fresh_attempts_since_resume += 1
            summary.fresh_attempted += 1
        else:
            summary.retried_attempted += 1

    def record_failure(kind: str) -> None:
        nonlocal consecutive_failures, consecutive_throttle
        summary.failed += 1
        summary.failed_by_kind[kind] = summary.failed_by_kind.get(kind, 0) + 1
        consecutive_failures += 1
        streak_kinds.append(kind)
        if kind in THROTTLE_KINDS:
            consecutive_throttle += 1
        else:
            # A non-throttle failure breaks the throttle streak: three timeouts
            # separated by a stacked dialog are not three in a row.
            consecutive_throttle = 0

    def record_progress() -> None:
        nonlocal consecutive_failures, consecutive_throttle, progress_since_resume
        consecutive_failures = 0
        consecutive_throttle = 0
        progress_since_resume += 1
        streak_kinds.clear()

    def reset_streak() -> None:
        nonlocal consecutive_failures, consecutive_throttle
        nonlocal progress_since_resume, attempts_since_resume
        nonlocal fresh_attempts_since_resume
        consecutive_failures = 0
        consecutive_throttle = 0
        progress_since_resume = 0
        attempts_since_resume = 0
        fresh_attempts_since_resume = 0
        streak_kinds.clear()

    def fail_kind_breakdown() -> str:
        return ", ".join(
            f"{summary.failed_by_kind.get(kind, 0)} {kind}"
            for kind in FAILURE_KINDS
            if summary.failed_by_kind.get(kind, 0)
        ) or "none"

    def heartbeat(done_so_far: int) -> None:
        """Periodic liveness line for long unattended runs."""
        if cfg.heartbeat_every <= 0 or done_so_far % cfg.heartbeat_every:
            return
        elapsed = time.monotonic() - started
        rate = (done_so_far / elapsed * 60.0) if elapsed > 0 else 0.0
        left = max(0, len(pending) - summary.deleted - summary.already_gone)
        eta = f", eta {format_duration(left / rate * 60.0)}" if rate > 0 else ""
        log(
            f"  -- heartbeat: {done_so_far}/{len(batch)} attempted in "
            f"{format_duration(elapsed)} ({rate:.1f}/min), {summary.deleted} deleted, "
            f"{summary.failed} failed, {left} still pending overall{eta}"
        )
        if summary.failed:
            log(f"     failures by kind: {fail_kind_breakdown()}")
        if summary.stacked_dialogs:
            log(
                f"     grouped/stacked dialogs: {summary.stacked_dialogs} met, "
                f"{summary.stacked_handled} answered 'Current photo only'"
            )
        if summary.cooldowns:
            log(
                f"     {summary.cooldowns} cooldown(s) totalling "
                f"{format_duration(summary.cooldown_seconds)}; delay now "
                f"{cfg.delay:.1f}s"
            )

    def stop(kind: str, reason: str) -> None:
        summary.abort_kind = kind
        summary.aborted_reason = reason

    def breaker_reached(what: str, last_error: str) -> bool:
        """The breaker tripped. Returns True to carry on, False to end the run.

        GUARDRAIL 7: the verdict here is throttling vs breakage, and ONLY
        throttling is allowed to sleep.
        """
        kinds = set(streak_kinds)

        # A streak containing anything outside THROTTLE_KINDS is not a throttle
        # signal. Say which kind it was, because the fix differs per kind.
        if kinds and not kinds.issubset(THROTTLE_KINDS):
            if kinds & CODE_GAP_KINDS:
                unanswered = streak_kinds.count(FAIL_STACKED)
                never_opened = streak_kinds.count(FAIL_NO_DIALOG)
                parts = []
                if unanswered:
                    parts.append(
                        f"{unanswered} delete dialog(s) this script could not "
                        f"answer safely"
                    )
                if never_opened:
                    parts.append(
                        f"{never_opened} item(s) where NEITHER the '#' shortcut "
                        f"NOR the toolbar fallback opened a delete dialog at all"
                    )
                stop(
                    ABORT_STACKED,
                    f"{consecutive_failures} consecutive failures while {what}, "
                    f"including {' and '.join(parts)}. That is a CODE GAP, not a "
                    f"rate limit, so no cooldown was taken - sleeping cannot teach "
                    f"this script a new button name or find a control that has "
                    f"moved. Nothing was clicked on those items and they are "
                    f"untouched and still pending. Last error: {last_error}",
                )
            else:
                stop(
                    ABORT_BREAKER,
                    f"{consecutive_failures} consecutive failures while {what} "
                    f"({', '.join(sorted(kinds))}). These are not the kinds that "
                    f"throttling produces, so no cooldown was taken. Look at the "
                    f"Chrome window. Last error: {last_error}",
                )
            return False

        if progress_since_resume == 0:
            # Nothing at all has worked since this run (or this resume) began.
            # That is not pacing: the UI has probably changed, a challenge page
            # is in the way, or the session is no longer signed in.
            #
            # FIX 3: unless every one of those attempts was a RETRY of an item
            # that was already known to fail. Three stubborn items re-failing
            # says nothing new about the UI, and aborting on them is what kept a
            # real run from ever reaching the 9,753 items that would have
            # worked. Carry on into the fresh items instead; the attempt cap
            # (--max-attempts) is what eventually retires them for good.
            if fresh_attempts_since_resume == 0:
                log(
                    f"  .. {consecutive_failures} consecutive failures, but every "
                    f"one was a RETRY of an item that has failed before "
                    f"(--max-attempts {cfg.max_attempts}). A known-bad item "
                    f"failing again is not evidence of a changed UI, so this is "
                    f"NOT treated as 'no successes since the start of the run'. "
                    f"Carrying on to the never-attempted items; these will be "
                    f"quarantined once they pass the attempt cap. Last error: "
                    f"{last_error}"
                )
                reset_streak()
                return True

            where = (
                f"since resuming from cooldown #{summary.cooldowns}"
                if summary.cooldowns
                else "from the start of the run"
            )
            stop(
                ABORT_NO_SUCCESS,
                f"{consecutive_failures} consecutive failures while {what}, and "
                f"NOT ONE item has succeeded {where} ({attempts_since_resume} "
                f"attempt(s), of which {fresh_attempts_since_resume} were "
                f"never-attempted items, 0 deleted, 0 already-gone). This is not "
                f"rate limiting - the UI has probably changed, or a "
                f"challenge/consent page or a sign-out is in the way - so no "
                f"cooldown was taken. Last error: {last_error}",
            )
            return False

        if summary.cooldowns >= cfg.max_cooldowns or cfg.cooldown <= 0:
            stop(
                ABORT_COOLDOWNS_EXHAUSTED,
                f"{consecutive_failures} consecutive failures while {what} after "
                f"{summary.cooldowns} cooldown(s) totalling "
                f"{format_duration(summary.cooldown_seconds)} (--max-cooldowns "
                f"{cfg.max_cooldowns}). Still being throttled at a "
                f"{cfg.delay:.1f}s delay; give the account a longer rest and "
                f"resume later. Last error: {last_error}",
            )
            return False

        wait = cooldown_seconds_for(cfg, summary.cooldowns)
        log("")
        log(
            f"  !! COOLDOWN {summary.cooldowns + 1}/{cfg.max_cooldowns} at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}: {consecutive_failures} "
            f"consecutive {'/'.join(sorted(kinds))} failures while {what}."
        )
        log(
            f"     so far this run: {summary.deleted} deleted, "
            f"{summary.already_gone} already gone, {summary.failed} failed "
            f"({fail_kind_breakdown()}); "
            f"{max(0, len(pending) - summary.deleted - summary.already_gone)} "
            f"still pending overall."
        )
        log(f"     last error: {last_error}")
        log(
            f"     reading this as throttling, not breakage: "
            f"{progress_since_resume} item(s) did succeed since the last resume. "
            f"Sleeping {format_duration(wait)}, then continuing where this left "
            f"off. Ctrl-C SKIPS the wait and carries on; press it twice to stop "
            f"the run. Failed items stay pending either way."
        )
        sleep_result = cooldown_sleeper(wait, log)
        slept = cooldown_slept_seconds(sleep_result, wait)
        summary.cooldowns += 1
        summary.cooldown_seconds += slept
        if cooldown_was_skipped(sleep_result):
            # The wait was skipped, not the cooldown: the throttle condition
            # really did happen, so it still counts and the delay still grows.
            # Said out loud, because a cooldown that "did not happen" silently
            # using up one of --max-cooldowns would be a surprise.
            summary.cooldowns_skipped += 1
            log(
                f"     cooldown skipped by user after {format_duration(slept)} of "
                f"{format_duration(wait)}, resuming now. It still COUNTS as "
                f"cooldown {summary.cooldowns}/{cfg.max_cooldowns} and the delay "
                f"still grows below - the throttling did happen; only the waiting "
                f"was cut short."
            )

        try:
            note = driver.recycle()
        except Exception as exc:  # hygiene is best-effort, never fatal
            note = f"tab recycle failed ({exc}); continuing with the current tab"
        log(f"     browser hygiene: {note}")

        resumed_delay = grown_delay(cfg)
        if resumed_delay > cfg.delay:
            log(
                f"     slowing down: --delay {cfg.delay:.1f}s -> "
                f"{resumed_delay:.1f}s (x{cfg.delay_growth}, cap "
                f"{cfg.delay_max:.1f}s)"
            )
        else:
            log(
                f"     delay already at the {cfg.delay_max:.1f}s cap; resuming at "
                f"{cfg.delay:.1f}s. If this keeps happening, raise --delay-max."
            )
        cfg.delay = resumed_delay
        summary.final_delay = resumed_delay

        reset_streak()
        log(f"  >> resuming at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        log("")
        return True

    def post_resume_window_blown() -> bool:
        """SAFEGUARD: the first K attempts after a resume all failed.

        Checked eagerly, before the breaker, so a run that comes back from a
        cooldown into a changed UI stops after K attempts instead of burning
        every remaining cooldown on it.

        FIX 3: counted over NEVER-ATTEMPTED items, for the same reason as the
        zero-successes guard - a window filled with known-bad retries says
        nothing about the UI.
        """
        return (
            summary.cooldowns > 0
            and progress_since_resume == 0
            and fresh_attempts_since_resume >= cfg.post_cooldown_window
        )

    def no_success_after_cooldown(last_error: str) -> None:
        stop(
            ABORT_NO_SUCCESS,
            f"the first {attempts_since_resume} attempt(s) after cooldown "
            f"#{summary.cooldowns} ALL failed ({fail_kind_breakdown()}), with no "
            f"deletion and no already-gone item among them "
            f"(--post-cooldown-window {cfg.post_cooldown_window}). Waiting longer "
            f"is not the answer: this is a UI change, a challenge/consent page, or "
            f"a lost sign-in. Remaining cooldowns were NOT used. "
            f"Last error: {last_error}",
        )

    try:
        for index, item in enumerate(batch, start=1):
            prefix = f"[{index}/{len(batch)}]"
            log(f"{prefix} {item.url}")
            if cfg.verbose:
                log(f"    {item.label()}")
                if item.file_count:
                    log(f"    files in takeout: {item.file_count}")
            if item.live_photo_pair:
                log("    LIVE PHOTO: deleting this item removes BOTH still and motion.")

            # ---- resolve ---------------------------------------------------- #
            try:
                state = driver.probe(item.url)
            except GuardrailViolation:
                raise
            except FatalDriverError as exc:
                # Identity / auth problems never get a cooldown.
                summary.attempted += 1
                note_attempt(item)
                record_failure(FAIL_NAVIGATION)
                log(f"    FAILED (fatal): {exc}")
                if cfg.execute:
                    journal.record(item, "failed", f"fatal: {exc}")
                stop(
                    ABORT_FATAL,
                    f"the page is not what this script expects, and waiting cannot "
                    f"change that: {exc}",
                )
                break
            except DriverError as exc:
                summary.attempted += 1
                note_attempt(item)
                record_failure(getattr(exc, "kind", FAIL_OTHER))
                log(f"    FAILED (resolve): {exc}")
                if cfg.execute:
                    journal.record(item, "failed", f"resolve: {exc}")
                if post_resume_window_blown():
                    no_success_after_cooldown(str(exc))
                    break
                if consecutive_failures >= cfg.max_consecutive_failures:
                    if not breaker_reached("resolving items", str(exc)):
                        break
                    heartbeat(index)
                    continue
                sleeper(cfg, log)
                heartbeat(index)
                continue

            if state == GONE:
                # GUARDRAIL 6: already gone is DONE, not an error.
                summary.already_gone += 1
                note_attempt(item)
                record_progress()
                log("    already gone (not found / redirected / in Trash) -> DONE")
                if cfg.execute:
                    journal.record(
                        item,
                        "already_gone",
                        "probe: not present (gone or already in Trash)",
                    )
                sleeper(cfg, log)
                heartbeat(index)
                continue

            if not cfg.execute:
                # GUARDRAIL 2: resolve and report only.
                summary.attempted += 1
                log("    present -> WOULD move to Trash (dry run; use --execute)")
                sleeper(cfg, log)
                heartbeat(index)
                continue

            # ---- delete ----------------------------------------------------- #
            summary.attempted += 1
            note_attempt(item)
            evidence = ""
            try:
                outcome = driver.trash_current()
                if outcome.stacked:
                    # Reported as its own outcome: this is the case that used to
                    # look exactly like a rate limit.
                    summary.stacked_dialogs += 1
                    summary.stacked_handled += 1
                    log(
                        f"    GROUPED/STACKED item: answered the two-option dialog "
                        f"with {outcome.stacked_choice!r} (the other items in the "
                        f"stack are not in the input file and were left alone)"
                    )
                # FIX 1: the shortcut silently doing nothing is worth saying out
                # loud every time, and counting - it is the measurement that
                # tells us how widespread the problem is.
                if not outcome.shortcut_opened_dialog:
                    summary.shortcut_opened_no_dialog += 1
                    if outcome.route == ROUTE_TOOLBAR:
                        summary.toolbar_fallbacks += 1
                        log(
                            "    the '#' shortcut opened no dialog; used the "
                            "viewer toolbar's own 'Move to trash' instead"
                        )
                    else:
                        log(
                            "    the '#' shortcut opened no dialog and no toolbar "
                            "trash control could be found"
                        )
                if cfg.verbose:
                    log(f"    trash action: {outcome.note}")
                # GUARDRAIL 6: positive evidence first, and never from the DOM the
                # action left behind.
                if outcome.toast_seen:
                    shown = outcome.toast_text or "moved to trash"
                    evidence = f"confirmation toast: {shown}"
                elif outcome.left_viewer:
                    evidence = "viewer closed / redirected away from the permalink"
                elif driver.verify_trashed(item.url) == GONE:
                    evidence = "not available on a fresh reload of the permalink"
                if evidence and outcome.stacked:
                    evidence += f" [stacked: {outcome.stacked_choice}]"
            except GuardrailViolation:
                raise
            except StackedDialogError as exc:
                # A dialog was open and neither known option was inside it, so
                # NOTHING was clicked. A code gap, not a throttle: never cooled
                # down, and the item stays pending for a later run.
                summary.stacked_dialogs += 1
                record_failure(FAIL_STACKED)
                log(f"    FAILED (delete dialog, nothing clicked): {exc}")
                journal.record(item, "failed", f"stacked-dialog: {exc}")
                if post_resume_window_blown():
                    no_success_after_cooldown(str(exc))
                    break
                if consecutive_failures >= cfg.max_consecutive_failures:
                    if not breaker_reached("deleting items", str(exc)):
                        break
                    heartbeat(index)
                    continue
                sleeper(cfg, log)
                heartbeat(index)
                continue
            except FatalDriverError as exc:
                record_failure(FAIL_NAVIGATION)
                log(f"    FAILED (fatal): {exc}")
                journal.record(item, "failed", f"fatal: {exc}")
                stop(
                    ABORT_FATAL,
                    f"the page is not what this script expects, and waiting cannot "
                    f"change that: {exc}",
                )
                break
            except DriverError as exc:
                record_failure(getattr(exc, "kind", FAIL_OTHER))
                log(f"    FAILED (delete/verify): {exc}")
                journal.record(item, "failed", f"delete: {exc}")
                if post_resume_window_blown():
                    no_success_after_cooldown(str(exc))
                    break
                if consecutive_failures >= cfg.max_consecutive_failures:
                    if not breaker_reached("deleting items", str(exc)):
                        break
                    heartbeat(index)
                    continue
                sleeper(cfg, log)
                heartbeat(index)
                continue

            if evidence:
                summary.deleted += 1
                route = outcome.route or ROUTE_SHORTCUT
                summary.deleted_by_route[route] = (
                    summary.deleted_by_route.get(route, 0) + 1
                )
                record_progress()
                log(
                    f"    deleted via {route} ({evidence}) -> in Trash for 60 days"
                )
                journal.record(item, "deleted", f"{evidence} [route: {route}]")
            else:
                # No positive evidence. Two completely different things look
                # like this, so ASK WHAT IS STILL ON SCREEN before filing it
                # (BUG B): a dialog that is still up is a code gap and must
                # never cool down, and only a dialog that was answered and
                # dismissed leaves `mutation-no-op` meaning what it says.
                kind = classify_noop_failure(
                    all_photos_seen=outcome.all_photos_seen,
                    lingering_dialog=outcome.lingering_dialog,
                    lingering_heading=outcome.lingering_heading,
                    dialog_answered=outcome.dialog_answered,
                )
                record_failure(kind)
                if kind == FAIL_STACKED:
                    summary.stacked_dialogs += 1
                    detail = (
                        "a delete dialog was still on screen afterwards"
                        f"{' (All N photos offered)' if outcome.all_photos_seen else ''}"
                        " and this item is still there, so the dialog was never "
                        "answered. NOTHING was clicked. A code gap, not a rate "
                        "limit: no cooldown."
                    )
                    log(f"    FAILED (stacked dialog unanswered): {detail}")
                    last_error = detail
                    journal.record(item, "failed", f"{FAIL_STACKED}: {detail}")
                elif kind == FAIL_NO_DIALOG:
                    # FIX 1/FIX 2: no dialog ever appeared and the toolbar
                    # fallback could not stand in either, so the deletion was
                    # never actually requested. A code gap (the controls have
                    # moved), NOT the delete limiter - and precisely the case
                    # that produced no evidence at all up to v1.2.2.
                    last_error = (
                        "neither the '#' shortcut nor the toolbar fallback opened "
                        "a delete dialog, so the deletion was never requested and "
                        "the item is still there. The viewer controls have "
                        "probably moved; this is a code gap, not a rate limit, so "
                        "no cooldown was taken"
                    )
                    log(f"    FAILED (no dialog, no toolbar control): {last_error}")
                    journal.record(item, "failed", f"{FAIL_NO_DIALOG}: {last_error}")
                else:
                    last_error = (
                        "items are not actually being trashed (dialog answered "
                        "and dismissed, no toast, still present after a reload)"
                    )
                    log(
                        "    FAILED: the dialog was answered and dismissed, there was "
                        "no confirmation toast, the viewer did not close, and the item "
                        "is still there on a fresh reload; not counting it"
                    )
                    log(
                        "           (this is the FIRST kind of evidence that a delete "
                        "quota exists at all - see the header)"
                    )
                    journal.record(
                        item, "failed", "no trash confirmation and still present"
                    )
                # FIX 2: the dump goes out on EVERY no-toast failure, whatever
                # the classification and whether or not a dialog was detected.
                if outcome.diagnostics:
                    for dump_line in outcome.diagnostics.splitlines():
                        log(f"      {dump_line}")
                else:
                    log(
                        "      (the driver returned no diagnostics for this "
                        "failure - that is itself a bug; see _failure_diagnostics)"
                    )
                if post_resume_window_blown():
                    no_success_after_cooldown(last_error)
                    break
                # A stacked failure is not a throttle signal, so it is gated by
                # the plain breaker rather than the throttle counter.
                streak = (
                    consecutive_throttle
                    if kind in THROTTLE_KINDS
                    else consecutive_failures
                )
                if streak >= cfg.max_consecutive_failures:
                    if not breaker_reached("deleting items", last_error):
                        break
                    heartbeat(index)
                    continue

            sleeper(cfg, log)
            heartbeat(index)
    except KeyboardInterrupt:
        # Lands here immediately, including from inside a cooldown sleep.
        summary.interrupted = True
        log("")
        log(
            f"  ^C interrupted at {time.strftime('%Y-%m-%d %H:%M:%S')}: stopping now. "
            f"The journal is current through the last completed item."
        )

    summary.elapsed_seconds = time.monotonic() - started
    summary.final_delay = cfg.delay
    if cfg.execute:
        # Recount from the journal: items the breaker stopped us reaching are
        # still pending, and so are any that failed.
        summary.remaining = len(
            [item for item in items if not journal.is_done(item.url)]
        )
    # In dry run the journal is untouched, so `remaining` stays as computed above
    # (pending minus the items this run resolved).
    return summary


# --------------------------------------------------------------------------- #
# Offline planner (no browser)
# --------------------------------------------------------------------------- #


def run_offline(
    items: Sequence[Item], journal: Journal, cfg: Config, log=print
) -> Summary:
    """Dry-run planning with no browser at all: what would the next run touch?"""
    summary = Summary(considered=len(items))
    pending = [item for item in items if not journal.is_done(item.url)]
    summary.skipped_done = len(items) - len(pending)
    batch = pending[: max(0, cfg.limit)]
    summary.remaining = max(0, len(pending) - len(batch))
    summary.attempted = len(batch)

    log("")
    log("OFFLINE DRY RUN: no browser, no network, nothing changed.")
    log(
        f"{len(items)} item(s) in input; {summary.skipped_done} already done per "
        f"journal; {len(pending)} pending; next run would act on {len(batch)}."
    )
    log("")
    for index, item in enumerate(batch, start=1):
        live = "  LIVE(still+motion)" if item.live_photo_pair else ""
        log(f"[{index}/{len(batch)}] WOULD move to Trash: {item.url}{live}")
        if cfg.verbose:
            log(f"    {item.label()}")
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gphotos_delete_orphans.py",
        description=(
            "Move Google Photos items to Trash, one permalink at a time, from a "
            "reviewed input file. Dry run unless --execute."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Guardrails: input-only blast radius, dry run by default, --limit cap, "
            "paced with jitter, resumable JSONL journal, per-item verification, "
            "circuit breaker with bounded cooldowns, never empties Trash, never "
            "automates sign-in. Exit codes: 0 clean, 1 finished with failures, "
            "2 usage, 3 no Chrome, 4 guardrail bug, 5 gave up while throttled, "
            "6 NOT throttling (UI change / challenge / sign-out), 130 Ctrl-C."
        ),
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"orphans.csv or orphans.json (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--journal",
        default=DEFAULT_JOURNAL,
        help=f"JSONL progress journal (default: {DEFAULT_JOURNAL})",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually move items to Trash. Without this, nothing changes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="maximum items to act on this run (default: 25)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="first-real-run mode: 5 items, extra verbosity (override with --limit)",
    )
    parser.add_argument(
        "--smoke-count",
        type=int,
        default=5,
        help="how many items --smoke does (default: 5)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help=(
            "seconds to pause between items (default: 5.0). Lower is rate-limited: "
            "~2s produced navigation timeouts after ~17 items; 10s ran clean."
        ),
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.35,
        help="random +/- fraction applied to --delay (default: 0.35)",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=3,
        help="circuit breaker: abort after this many failures in a row (default: 3)",
    )
    parser.add_argument(
        "--cdp-url",
        default=DEFAULT_CDP_URL,
        help=f"CDP endpoint of your already-signed-in Chrome (default: {DEFAULT_CDP_URL})",
    )
    parser.add_argument(
        "--nav-timeout",
        type=float,
        default=30.0,
        help="per-navigation timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=1.2,
        help="seconds to let a page settle after navigation (default: 1.2)",
    )
    parser.add_argument(
        "--post-action-settle",
        type=float,
        default=3.0,
        help=(
            "seconds to let the UI catch up after a trash action: how long the "
            "confirmation toast is waited for, and the delay before any reload "
            "check (default: 3.0)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=1200.0,
        help=(
            "seconds to sleep when the circuit breaker trips but the run is still "
            "making progress, i.e. being throttled rather than broken "
            "(default: 1200 = 20min). 0 disables cooldowns and restores the "
            "v1.1.0 abort-immediately behaviour."
        ),
    )
    parser.add_argument(
        "--cooldown-backoff",
        type=float,
        default=2.0,
        help=(
            "multiplier applied to --cooldown for each successive cooldown "
            "(default: 2.0, i.e. 20min, 40min, 80min...)"
        ),
    )
    parser.add_argument(
        "--cooldown-cap",
        type=float,
        default=7200.0,
        help="ceiling on any single cooldown sleep, in seconds (default: 7200 = 2h)",
    )
    parser.add_argument(
        "--max-cooldowns",
        type=int,
        default=6,
        help=(
            "give up after this many cooldowns in one run (default: 6). Failed and "
            "unreached items stay pending for the next run."
        ),
    )
    parser.add_argument(
        "--post-cooldown-window",
        type=int,
        default=3,
        help=(
            "SAFEGUARD: if this many attempts since the last resume all fail with "
            "no success at all, the problem is not throttling (UI change, "
            "challenge page, lost auth) and the run aborts with exit code 6 "
            "instead of cooling down (default: 3)"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help=(
            "how many times one item may be attempted across all runs before it "
            "is QUARANTINED (default: 3). Quarantined items are skipped so a "
            "handful of stubborn ones cannot block every run; they stay in the "
            "journal and are listed in the summary."
        ),
    )
    parser.add_argument(
        "--retry-quarantined",
        action="store_true",
        help=(
            "also attempt items that have already passed --max-attempts. Use "
            "after fixing whatever made them fail."
        ),
    )
    parser.add_argument(
        "--delay-growth",
        type=float,
        default=1.5,
        help=(
            "multiply --delay by this after every cooldown (default: 1.5). A pace "
            "that was just throttled is too fast to resume at."
        ),
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=30.0,
        help="ceiling for the grown --delay, in seconds (default: 30)",
    )
    parser.add_argument(
        "--heartbeat-every",
        type=int,
        default=100,
        help=(
            "print a progress heartbeat (elapsed, rate, remaining) every N items; "
            "0 disables it (default: 100)"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="plan only: parse input + journal and list what the next run would do",
    )
    parser.add_argument(
        "--confirm-threshold",
        type=int,
        default=500,
        help="--execute runs above this --limit ask for typed confirmation (default: 500)",
    )
    parser.add_argument(
        "--assume-yes",
        action="store_true",
        help="skip the typed confirmation for large --execute runs",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="more output")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the built-in logic tests (no browser, no network) and exit",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def apply_smoke_mode(args: argparse.Namespace, raw_argv: Sequence[str]) -> None:
    """--smoke: a handful of items, extra verbosity, unless --limit was explicit."""
    if not args.smoke:
        return
    args.verbose = True
    limit_given = any(
        arg == "--limit" or arg.startswith("--limit=") for arg in raw_argv
    )
    if not limit_given:
        args.limit = args.smoke_count


def print_summary(summary: Summary, cfg: Config, args: argparse.Namespace) -> None:
    print("")
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"  mode              : {'EXECUTE' if cfg.execute else 'DRY RUN'}"
          f"{' (offline)' if args.offline else ''}")
    print(f"  input             : {os.path.expanduser(args.input)}")
    print(f"  items in input    : {summary.considered}")
    if summary.invalid:
        print(f"  invalid rows      : {summary.invalid} (ignored, never acted on)")
    if summary.duplicates:
        print(f"  duplicate urls    : {summary.duplicates} (collapsed)")
    print(f"  skipped (done)    : {summary.skipped_done}")
    print(f"  attempted         : {summary.attempted}")
    print(f"  deleted           : {summary.deleted}")
    print(f"  already gone      : {summary.already_gone}")
    print(f"  failed            : {summary.failed}")
    if summary.failed:
        # Kept apart on purpose - these call for completely different responses.
        # See FAILURE KINDS and RATE LIMITING AND COOLDOWNS in the header.
        print(f"    navigation      : {summary.failed_navigation:<5} "
              f"{FAIL_NAVIGATION} - page would not load; throttling, cools down")
        print(f"    stacked dialog  : {summary.failed_stacked:<5} "
              f"{FAIL_STACKED} - delete dialog not answered; CODE GAP, no cooldown")
        print(f"    no dialog       : {summary.failed_no_dialog:<5} "
              f"{FAIL_NO_DIALOG} - no dialog ever opened (shortcut AND toolbar "
              f"both failed); CODE GAP, no cooldown")
        if summary.failed_no_dialog:
            print("                      ^ the delete was never REQUESTED for these,")
            print("                        so they say nothing about a delete quota.")
            print("                        The viewer controls have probably moved:")
            print("                        read the dumps above this summary.")
        print(f"    trash mutation  : {summary.failed_mutation:<5} "
              f"{FAIL_MUTATION} - dialog answered and dismissed, item survived "
              f"anyway; throttling, cools down")
        if summary.failed_mutation:
            print("                      ^ the only evidence a delete quota would")
            print("                        ever produce. Nothing before v1.2.2 is")
            print("                        trustworthy here: unanswerable dialogs")
            print("                        used to be counted on this line.")
        print(f"    other           : {summary.failed_other:<5} "
              f"{FAIL_OTHER} - unexpected; no cooldown")
    if summary.stacked_dialogs:
        print(f"  stacked dialogs   : {summary.stacked_dialogs} met, "
              f"{summary.stacked_handled} answered 'Current photo only', "
              f"{summary.stacked_dialogs - summary.stacked_handled} refused safely")
        print("                      (only the current item was ever deleted; the")
        print("                       rest of each stack is not in the input file)")
    # FIX 1: per-route accounting, so "how often does the '#' shortcut silently
    # do nothing?" stops being a guess.
    if cfg.execute and (summary.deleted or summary.shortcut_opened_no_dialog):
        print(f"  deleted by route  : {summary.deleted_via_shortcut} via the '#' "
              f"shortcut, {summary.deleted_via_toolbar} via the toolbar fallback")
        if summary.shortcut_opened_no_dialog:
            print(f"  shortcut opened no dialog on "
                  f"{summary.shortcut_opened_no_dialog} item(s); the toolbar "
                  f"fallback was used on {summary.toolbar_fallbacks}")
            if summary.deleted_via_toolbar:
                print("                      ^ the shortcut DOES silently fail for")
                print("                        some items; the fallback is what")
                print("                        deleted them.")
    # FIX 3: the queue composition and the quarantine bucket.
    if cfg.execute and (summary.retried_attempted or summary.quarantined):
        print(f"  attempted by kind : {summary.fresh_attempted} never-attempted, "
              f"{summary.retried_attempted} retry(ies) of earlier failures")
    if summary.quarantined:
        print(f"  QUARANTINED       : {summary.quarantined} item(s) past "
              f"--max-attempts {cfg.max_attempts}, skipped this run")
        for url in summary.quarantined_urls[:10]:
            print(f"                      {url}")
        if len(summary.quarantined_urls) > 10:
            print(f"                      ... and "
                  f"{len(summary.quarantined_urls) - 10} more")
        print("                      They are still in the journal and nothing is")
        print("                      lost. Re-include them with --retry-quarantined")
        print("                      once the cause is fixed.")
    print(f"  still pending     : {summary.remaining}")
    if summary.elapsed_seconds:
        print(f"  elapsed           : {format_duration(summary.elapsed_seconds)}")
    if cfg.execute:
        print(f"  cooldowns taken   : {summary.cooldowns}"
              f"{f' of {cfg.max_cooldowns}' if cfg.max_cooldowns else ''}")
        if summary.cooldowns:
            print(f"  total cooldown    : "
                  f"{format_duration(summary.cooldown_seconds)}")
        if summary.cooldowns_skipped:
            print(f"  cooldowns skipped : {summary.cooldowns_skipped} cut short "
                  f"with Ctrl-C (they still counted, and the delay still grew)")
        if summary.starting_delay != summary.final_delay:
            print(f"  delay             : {summary.starting_delay:.1f}s -> "
                  f"{summary.final_delay:.1f}s (cap {cfg.delay_max:.1f}s)")
        else:
            print(f"  delay             : {summary.final_delay:.1f}s")
    print(f"  journal           : {cfg.journal_path}")
    if summary.interrupted:
        print("")
        print("  ^C INTERRUPTED - stopped at your request, nothing lost.")
        print("     The journal is current through the last completed item; the")
        print("     same command picks up exactly where this left off.")
    if summary.aborted_reason:
        print("")
        if summary.abort_kind == ABORT_NO_SUCCESS:
            print("  !! ABORTED - THIS IS NOT RATE LIMITING. GO LOOK AT CHROME.")
            print(f"     {summary.aborted_reason}")
            if summary.cooldowns:
                print(f"     {cfg.max_cooldowns - summary.cooldowns} further "
                      f"cooldown(s) were available and deliberately NOT used:")
                print("     waiting again would just have wasted more hours.")
            else:
                print("     No cooldown was taken and none would have helped.")
            print("     Open the Chrome window: check you are still signed in, that")
            print("     no challenge or consent page is up, and that the viewer")
            print("     still looks normal. Re-run --smoke --execute by hand first.")
        elif summary.abort_kind == ABORT_FATAL:
            print("  !! ABORTED - the page is not what this script expects")
            print(f"     {summary.aborted_reason}")
            print("     Fix that in the Chrome window, then re-run.")
        elif summary.abort_kind == ABORT_STACKED:
            print("  !! ABORTED - DELETE CONFIRMATION DIALOG NOT UNDERSTOOD")
            print(f"     {summary.aborted_reason}")
            print("     NOTHING was clicked on those dialogs - in particular not")
            print("     'All N photos', which would have deleted items that are not")
            print("     in your input file. Those items are untouched and pending.")
            print("     This is a code gap: the dialog wording has changed. Compare")
            print("     the options the dialog shows in Chrome against")
            print("     STACKED_CURRENT_ONLY_RE and TRASH_BUTTON_NAME_RE.")
        elif summary.abort_kind == ABORT_COOLDOWNS_EXHAUSTED:
            print(f"  !! GAVE UP after {summary.cooldowns} cooldown(s) "
                  f"({format_duration(summary.cooldown_seconds)} asleep)")
            print(f"     {summary.aborted_reason}")
            print("     Deletions that DID land are journalled and will be skipped.")
            print("     Leave the account alone for a few hours, then resume; or")
            print("     resume now with a higher --delay and --cooldown.")
        else:
            print("  !! CIRCUIT BREAKER TRIPPED - run aborted early")
            print(f"     {summary.aborted_reason}")
            print("     Nothing further was attempted. Look at the Chrome window")
            print("     before re-running.")
    elif summary.cooldowns and not summary.interrupted:
        print("")
        print(f"  note: rode out {summary.cooldowns} throttling cooldown(s) and "
              f"carried on.")
        if summary.final_delay > 0:
            print(f"        Resume at --delay {summary.final_delay:.1f} or higher "
                  f"to spend less time asleep.")
    print("")
    if summary.remaining:
        resume = ["python3", sys.argv[0]]
        if args.input != DEFAULT_INPUT:
            resume += ["--input", args.input]
        if args.journal != DEFAULT_JOURNAL:
            resume += ["--journal", args.journal]
        resume += ["--execute", "--limit", str(cfg.limit)]
        # Resume at the pace this run ended up at, not the one it started at: the
        # cooldowns are evidence about what this account currently tolerates.
        resume_delay = max(summary.final_delay, summary.starting_delay)
        if resume_delay and resume_delay != 5.0:
            resume += ["--delay", f"{resume_delay:g}"]
        print("  To resume (already-done items are skipped automatically):")
        print("    " + " ".join(resume))
    elif cfg.execute:
        print("  Nothing pending. Every item in the input file is done.")
    if cfg.execute and summary.deleted:
        print("")
        print("  Deleted items are in Trash for 60 days - that is your only undo.")
        print("  This script never empties Trash. Check them in the UI before you do.")
    print("")


def confirm_large_run(limit: int, threshold: int) -> bool:
    print("")
    print(f"You are about to delete up to {limit} items (threshold {threshold}).")
    print(f"Type exactly:  {CONFIRM_PHRASE}")
    try:
        answer = input("> ").strip()
    except EOFError:
        return False
    return answer == CONFIRM_PHRASE


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test:
        return self_test()

    apply_smoke_mode(args, list(argv if argv is not None else sys.argv[1:]))

    if args.offline and args.execute:
        print(
            "--offline is a planning mode and cannot delete anything; drop "
            "--execute (or drop --offline to run for real).",
            file=sys.stderr,
        )
        return 2
    if args.limit <= 0:
        print("--limit must be >= 1", file=sys.stderr)
        return 2
    if not 0.0 <= args.jitter < 1.0:
        print("--jitter must be in [0, 1)", file=sys.stderr)
        return 2
    if args.max_consecutive_failures < 1:
        print("--max-consecutive-failures must be >= 1", file=sys.stderr)
        return 2
    if args.post_action_settle < 0:
        print("--post-action-settle must be >= 0", file=sys.stderr)
        return 2
    if args.cooldown < 0:
        print("--cooldown must be >= 0 (0 disables cooldowns)", file=sys.stderr)
        return 2
    if args.cooldown_backoff < 1.0:
        print("--cooldown-backoff must be >= 1.0", file=sys.stderr)
        return 2
    if args.cooldown_cap < 0:
        print("--cooldown-cap must be >= 0", file=sys.stderr)
        return 2
    if args.max_cooldowns < 0:
        print("--max-cooldowns must be >= 0 (0 disables cooldowns)", file=sys.stderr)
        return 2
    if args.post_cooldown_window < 1:
        print("--post-cooldown-window must be >= 1", file=sys.stderr)
        return 2
    if args.max_attempts < 1:
        print("--max-attempts must be >= 1", file=sys.stderr)
        return 2
    if args.delay_growth < 1.0:
        print("--delay-growth must be >= 1.0 (the pace may only slow down)",
              file=sys.stderr)
        return 2
    if args.delay_max < 0:
        print("--delay-max must be >= 0", file=sys.stderr)
        return 2
    if args.heartbeat_every < 0:
        print("--heartbeat-every must be >= 0 (0 disables it)", file=sys.stderr)
        return 2

    cfg = Config(
        execute=args.execute,
        limit=args.limit,
        delay=args.delay,
        jitter=args.jitter,
        max_consecutive_failures=args.max_consecutive_failures,
        verbose=args.verbose,
        journal_path=os.path.expanduser(args.journal),
        cooldown=args.cooldown,
        cooldown_backoff=args.cooldown_backoff,
        cooldown_cap=args.cooldown_cap,
        max_cooldowns=args.max_cooldowns,
        post_cooldown_window=args.post_cooldown_window,
        delay_growth=args.delay_growth,
        delay_max=args.delay_max,
        heartbeat_every=args.heartbeat_every,
        max_attempts=args.max_attempts,
        retry_quarantined=args.retry_quarantined,
    )

    print(f"gphotos_delete_orphans.py {VERSION}")
    try:
        loaded = load_items(args.input)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if loaded.invalid:
        print(f"WARNING: {len(loaded.invalid)} row(s) rejected and never acted on:")
        for row_number, url, why in loaded.invalid[:10]:
            print(f"  line ~{row_number}: {why}: {url!r}")
        if len(loaded.invalid) > 10:
            print(f"  ... and {len(loaded.invalid) - 10} more")
    if loaded.duplicates:
        print(f"note: collapsed {loaded.duplicates} duplicate url(s)")
    if not loaded.items:
        print("ERROR: no valid permalinks in the input file; nothing to do.",
              file=sys.stderr)
        return 2

    live_count = sum(1 for item in loaded.items if item.live_photo_pair)
    print(f"loaded {len(loaded.items)} permalink(s) from {os.path.expanduser(args.input)}")
    if live_count:
        print(
            f"note: {live_count} are live photo pairs - deleting those items removes "
            "both the still and the motion file together."
        )

    journal = Journal(cfg.journal_path)
    journal.load()
    if journal.malformed_lines:
        print(
            f"WARNING: {journal.malformed_lines} unparseable line(s) in the journal "
            "were ignored. Those items will be re-checked (and verified) rather "
            "than assumed done."
        )
    print(f"journal: {cfg.journal_path} ({journal.done_count()} item(s) already done)")

    if cfg.execute and cfg.limit > args.confirm_threshold and not args.assume_yes:
        if not confirm_large_run(cfg.limit, args.confirm_threshold):
            print("not confirmed; nothing done.")
            return 1

    if args.offline:
        summary = run_offline(loaded.items, journal, cfg)
        summary.invalid = len(loaded.invalid)
        summary.duplicates = loaded.duplicates
        print_summary(summary, cfg, args)
        return 0

    try:
        driver: PhotosDriver = PlaywrightDriver(
            cdp_url=args.cdp_url,
            allowed_urls=[item.url for item in loaded.items],
            nav_timeout_ms=int(args.nav_timeout * 1000),
            settle_ms=int(args.settle * 1000),
            post_action_settle_ms=int(args.post_action_settle * 1000),
            verbose=cfg.verbose,
        )
    except DriverError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    summary = Summary(starting_delay=cfg.delay, final_delay=cfg.delay)
    try:
        # run() handles Ctrl-C itself (including mid-cooldown) so that the
        # summary still gets printed; this catch is for an interrupt anywhere
        # else, e.g. while the driver is being torn down.
        summary = run(loaded.items, driver, journal, cfg)
    except KeyboardInterrupt:
        summary.interrupted = True
        print("\ninterrupted by user; the journal is up to date through the last "
              "completed item.")
    except GuardrailViolation as exc:
        print(f"\nGUARDRAIL VIOLATION (this is a bug, nothing further attempted): "
              f"{exc}", file=sys.stderr)
        return 4
    finally:
        driver.close()
        summary.invalid = len(loaded.invalid)
        summary.duplicates = loaded.duplicates
        if not summary.considered:
            # run() died before it could report; fall back to the journal.
            summary.considered = len(loaded.items)
            summary.remaining = len(
                [item for item in loaded.items if not journal.is_done(item.url)]
            )
        print_summary(summary, cfg, args)

    # Exit codes are how an unattended run tells you WHY it stopped. 6 and 7 both
    # mean "stop retrying and go look", for different reasons.
    if summary.abort_kind == ABORT_STACKED:
        return 7
    if summary.abort_kind in (ABORT_NO_SUCCESS, ABORT_FATAL):
        return 6
    if summary.aborted_reason:
        return 5
    if summary.interrupted:
        return 130
    return 0 if summary.failed == 0 else 1


# --------------------------------------------------------------------------- #
# Built-in tests (--self-test). No browser, no network, no real files touched
# outside a temporary directory.
# --------------------------------------------------------------------------- #


def self_test() -> int:  # noqa: C901 - a flat list of checks reads better here
    import inspect
    import tempfile
    import traceback

    passed: List[str] = []
    failed: List[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            passed.append(name)
            print(f"  PASS  {name}")
        except Exception:
            failed.append(name)
            print(f"  FAIL  {name}")
            traceback.print_exc()

    url = lambda n: f"https://photos.google.com/photo/AF1QipFAKE{n:05d}"  # noqa: E731
    quiet = lambda *_a, **_k: None  # noqa: E731
    no_sleep = lambda *_a, **_k: None  # noqa: E731

    tmpdir = tempfile.mkdtemp(prefix="gphotos-selftest-")

    def write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> str:
        fields = [
            "url", "title", "date", "taken_time_utc", "taken_time_epoch",
            "live_photo_pair", "file_count", "extensions", "folded_renditions",
            "generated_albums", "orphan_solely_due_to_generated_album",
            "takeout_paths", "source_archives",
        ]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        return path

    def sample_rows(count: int) -> List[Dict[str, Any]]:
        return [
            {
                "url": url(index),
                "title": f"IMG_{index:04d}.HEIC",
                "date": "2019-07-04",
                "taken_time_utc": "2019-07-04T18:02:11Z",
                "live_photo_pair": "true" if index % 7 == 0 else "false",
                "file_count": "2" if index % 7 == 0 else "1",
            }
            for index in range(count)
        ]

    csv_path = write_csv(os.path.join(tmpdir, "orphans.csv"), sample_rows(10))

    # --- input parsing ----------------------------------------------------- #

    def t_csv_parse() -> None:
        loaded = load_items(csv_path)
        assert len(loaded.items) == 10, loaded.items
        assert loaded.invalid == []
        assert loaded.items[0].url == url(0)
        assert loaded.items[0].title == "IMG_0000.HEIC"
        assert loaded.items[0].live_photo_pair is True  # index 0 % 7 == 0
        assert loaded.items[1].live_photo_pair is False

    def t_json_parse() -> None:
        json_path = os.path.join(tmpdir, "orphans.json")
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(sample_rows(4), handle)
        loaded = load_items(json_path)
        assert len(loaded.items) == 4
        wrapped = os.path.join(tmpdir, "orphans_wrapped.json")
        with open(wrapped, "w", encoding="utf-8") as handle:
            json.dump({"items": sample_rows(3)}, handle)
        assert len(load_items(wrapped).items) == 3

    def t_reject_bad_urls() -> None:
        rows = sample_rows(2) + [
            {"url": "https://evil.example.com/photo/AF1Qipzzz"},
            {"url": "https://photos.google.com/search/dogs"},
            {"url": "https://photos.google.com/trash"},
            {"url": "https://photos.google.com/photo/NOTAF1QIP"},
            {"url": ""},
            {"url": "https://photos.google.com/photo/AF1Qipok?x=1"},
        ]
        path = write_csv(os.path.join(tmpdir, "bad.csv"), rows)
        loaded = load_items(path)
        assert len(loaded.items) == 2, [i.url for i in loaded.items]
        assert len(loaded.invalid) == 6, loaded.invalid

    def t_dedupe() -> None:
        rows = sample_rows(3) + [{"url": url(0)}, {"url": url(1)}]
        path = write_csv(os.path.join(tmpdir, "dupes.csv"), rows)
        loaded = load_items(path)
        assert len(loaded.items) == 3
        assert loaded.duplicates == 2

    def t_missing_column_and_file() -> None:
        path = os.path.join(tmpdir, "nourl.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("title,date\nfoo,2020-01-01\n")
        try:
            load_items(path)
            raise AssertionError("expected ValueError for missing url column")
        except ValueError:
            pass
        try:
            load_items(os.path.join(tmpdir, "does_not_exist.csv"))
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError:
            pass

    check("csv parsing", t_csv_parse)
    check("json parsing", t_json_parse)
    check("rejects non-permalink urls", t_reject_bad_urls)
    check("collapses duplicate urls", t_dedupe)
    check("errors on bad input files", t_missing_column_and_file)

    # --- guardrail 1: blast radius ----------------------------------------- #

    def t_nav_guard() -> None:
        allowed = {url(0)}
        nav_guard(url(0), allowed)
        for bad in (
            url(1),
            "https://photos.google.com/",
            "https://photos.google.com/trash",
            "https://photos.google.com/search/x",
        ):
            try:
                nav_guard(bad, allowed)
                raise AssertionError(f"nav_guard allowed {bad}")
            except GuardrailViolation:
                pass

    def t_driver_refuses_unlisted() -> None:
        driver = FakeDriver(allowed_urls=[url(0)])
        try:
            driver.probe(url(9))
            raise AssertionError("driver probed a URL not in the input file")
        except GuardrailViolation:
            pass
        assert driver.probes == []

    check("nav_guard blocks everything not in the input", t_nav_guard)
    check("driver refuses URLs outside the input", t_driver_refuses_unlisted)

    # --- dry run ----------------------------------------------------------- #

    def t_dry_run_changes_nothing() -> None:
        items = load_items(csv_path).items
        journal = Journal(os.path.join(tmpdir, "dry.jsonl"))
        journal.load()
        driver = FakeDriver([i.url for i in items])
        cfg = Config(execute=False, limit=25, delay=0, journal_path=journal.path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert driver.trashed == [], driver.trashed
        assert len(driver.probes) == 10
        assert summary.attempted == 10
        assert summary.deleted == 0
        assert not os.path.exists(journal.path), "dry run wrote a journal"

    def t_dry_run_reports_already_gone() -> None:
        items = load_items(csv_path).items
        journal = Journal(os.path.join(tmpdir, "dry2.jsonl"))
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], states={items[0].url: GONE, items[1].url: GONE}
        )
        cfg = Config(execute=False, limit=25, delay=0, journal_path=journal.path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.already_gone == 2, summary
        assert summary.attempted == 8, summary
        assert not os.path.exists(journal.path)

    def t_offline_plan() -> None:
        items = load_items(csv_path).items
        journal = Journal(os.path.join(tmpdir, "off.jsonl"))
        journal.load()
        cfg = Config(execute=False, limit=3, delay=0, journal_path=journal.path)
        summary = run_offline(items, journal, cfg, log=quiet)
        assert summary.attempted == 3
        assert summary.remaining == 7
        assert not os.path.exists(journal.path)

    check("dry run performs no deletions and writes no journal", t_dry_run_changes_nothing)
    check("dry run reports already-gone items", t_dry_run_reports_already_gone)
    check("offline planner respects limit and writes nothing", t_offline_plan)

    # --- limit ------------------------------------------------------------- #

    def t_limit() -> None:
        items = load_items(csv_path).items
        journal = Journal(os.path.join(tmpdir, "limit.jsonl"))
        journal.load()
        driver = FakeDriver([i.url for i in items])
        cfg = Config(execute=True, limit=4, delay=0, journal_path=journal.path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.deleted == 4, summary
        assert len(driver.trashed) == 4
        assert driver.trashed == [i.url for i in items[:4]]
        assert summary.remaining == 6, summary

    check("--limit caps the run", t_limit)

    # --- execute, verify, journal, resume, idempotency ---------------------- #

    def t_execute_and_verify() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "exec.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver([i.url for i in items])
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.deleted == 10, summary
        assert summary.failed == 0
        # The toast is positive proof, so no item needs a second navigation.
        assert len(driver.probes) == 10, len(driver.probes)
        assert driver.verifications == [], driver.verifications
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        assert len(lines) == 10
        records = [json.loads(line) for line in lines]
        assert all(r["status"] == "deleted" for r in records)
        assert {r["url"] for r in records} == {i.url for i in items}
        assert any(r["live_photo_pair"] for r in records)

    def t_unverified_delete_is_failure() -> None:
        items = load_items(csv_path).items[:2]
        path = os.path.join(tmpdir, "noop.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], behaviour={items[0].url: "noop"}
        )
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.failed == 1, summary
        assert summary.deleted == 1, summary
        statuses = {
            json.loads(line)["url"]: json.loads(line)["status"]
            for line in open(path, encoding="utf-8")
        }
        assert statuses[items[0].url] == "failed"
        assert statuses[items[1].url] == "deleted"

    def t_resume_and_idempotency() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "resume.jsonl")
        states: Dict[str, str] = {}

        # First run: 3 items.
        journal = Journal(path)
        journal.load()
        driver = FakeDriver([i.url for i in items], states=states)
        cfg = Config(execute=True, limit=3, delay=0, journal_path=path)
        first = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert first.deleted == 3

        # Second run, fresh journal object (as a new process would have): the
        # first three must be skipped, not re-acted on.
        journal2 = Journal(path)
        journal2.load()
        driver2 = FakeDriver([i.url for i in items], states=states)
        cfg2 = Config(execute=True, limit=4, delay=0, journal_path=path)
        second = run(items, driver2, journal2, cfg2, log=quiet, sleeper=no_sleep)
        assert second.skipped_done == 3, second
        assert second.deleted == 4, second
        assert driver2.trashed == [i.url for i in items[3:7]], driver2.trashed
        assert all(i.url not in driver2.probes for i in items[:3])

        # Third run over the same input with everything done: no actions at all.
        journal3 = Journal(path)
        journal3.load()
        driver3 = FakeDriver([i.url for i in items], states=states)
        cfg3 = Config(execute=True, limit=100, delay=0, journal_path=path)
        third = run(items, driver3, journal3, cfg3, log=quiet, sleeper=no_sleep)
        assert third.skipped_done == 7, third
        assert third.deleted == 3, third  # the remaining 3
        journal4 = Journal(path)
        journal4.load()
        driver4 = FakeDriver([i.url for i in items], states=states)
        fourth = run(items, driver4, journal4, cfg3, log=quiet, sleeper=no_sleep)
        assert fourth.attempted == 0, fourth
        assert fourth.skipped_done == 10, fourth
        assert driver4.probes == [] and driver4.trashed == []

    def t_already_gone_is_terminal() -> None:
        items = load_items(csv_path).items[:3]
        path = os.path.join(tmpdir, "gone.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], states={i.url: GONE for i in items}
        )
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.already_gone == 3, summary
        assert summary.failed == 0
        assert driver.trashed == []
        journal2 = Journal(path)
        journal2.load()
        assert all(journal2.is_done(i.url) for i in items)
        driver2 = FakeDriver([i.url for i in items])
        again = run(items, driver2, journal2, cfg, log=quiet, sleeper=no_sleep)
        assert again.attempted == 0 and driver2.probes == []

    def t_failed_is_retried_but_terminal_is_sticky() -> None:
        items = load_items(csv_path).items[:1]
        path = os.path.join(tmpdir, "retry.jsonl")
        journal = Journal(path)
        journal.load()
        journal.record(items[0], "failed", "earlier breakage")
        journal2 = Journal(path)
        journal2.load()
        assert not journal2.is_done(items[0].url)
        driver = FakeDriver([i.url for i in items])
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, journal=journal2, driver=driver, cfg=cfg,
                      log=quiet, sleeper=no_sleep)
        assert summary.deleted == 1, summary
        # A stray later "failed" line must not resurrect a finished item.
        journal2.record(items[0], "failed", "stale writer")
        journal3 = Journal(path)
        journal3.load()
        assert journal3.is_done(items[0].url)

    def t_malformed_journal_lines_ignored() -> None:
        items = load_items(csv_path).items[:2]
        path = os.path.join(tmpdir, "malformed.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json at all\n")
            handle.write(json.dumps({"url": items[0].url, "status": "deleted"}) + "\n")
            handle.write("{truncated\n")
            handle.write("\n")
        journal = Journal(path)
        journal.load()
        assert journal.malformed_lines == 2, journal.malformed_lines
        assert journal.is_done(items[0].url)
        assert not journal.is_done(items[1].url)

    # --- verification: positive evidence, no stale-DOM false failures ------- #

    def t_toast_is_enough() -> None:
        """A toast means success even if the viewer DOM still shows the item."""
        items = load_items(csv_path).items[:3]
        path = os.path.join(tmpdir, "toast.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items],
            behaviour={i.url: "toast_stale" for i in items},
        )
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.deleted == 3, summary
        assert summary.failed == 0, summary
        assert not summary.aborted_reason
        # Seeing the toast means we never go back to the permalink at all.
        assert driver.verifications == [], driver.verifications
        details = [json.loads(line)["detail"] for line in open(path, encoding="utf-8")]
        assert all("toast" in detail for detail in details), details

    def t_stale_dom_is_not_a_failure() -> None:
        """No toast + a stale viewer: the fresh reload must find it gone."""
        items = load_items(csv_path).items[:3]
        path = os.path.join(tmpdir, "stale.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], behaviour={i.url: "silent" for i in items}
        )
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.deleted == 3, summary
        assert summary.failed == 0, summary
        assert not summary.aborted_reason, summary.aborted_reason
        assert driver.verifications == [i.url for i in items], driver.verifications
        statuses = {
            json.loads(line)["url"]: json.loads(line)["status"]
            for line in open(path, encoding="utf-8")
        }
        assert all(statuses[i.url] == "deleted" for i in items), statuses

    def t_left_viewer_is_success() -> None:
        items = load_items(csv_path).items[:2]
        path = os.path.join(tmpdir, "left.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], behaviour={i.url: "left" for i in items}
        )
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.deleted == 2, summary
        assert driver.verifications == [], driver.verifications

    def t_already_trashed_is_already_gone() -> None:
        """An item trashed by an earlier run, missing from the journal."""
        items = load_items(csv_path).items[:2]
        path = os.path.join(tmpdir, "trashed.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver([i.url for i in items], states={i.url: GONE for i in items})
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        assert summary.already_gone == 2, summary
        assert summary.failed == 0 and driver.trashed == []
        records = [json.loads(line) for line in open(path, encoding="utf-8")]
        assert len(records) == 2, records
        assert all(r["status"] == "already_gone" for r in records), records
        # Journalled exactly once: a re-run touches nothing.
        journal2 = Journal(path)
        journal2.load()
        driver2 = FakeDriver([i.url for i in items])
        again = run(items, driver2, journal2, cfg, log=quiet, sleeper=no_sleep)
        assert again.attempted == 0 and driver2.probes == []
        assert len(open(path, encoding="utf-8").read().strip().split("\n")) == 2

    def t_classify_page_state() -> None:
        permalink = url(1)
        present = lambda: True  # noqa: E731
        absent = lambda: False  # noqa: E731
        no_text = lambda: ""  # noqa: E731

        def state(current, text="", viewer=True, trashed=False) -> str:
            return classify_page_state(
                current_url=current,
                permalink=permalink,
                text_getter=(lambda: text),
                viewer_getter=(present if viewer else absent),
                trashed_state_getter=(present if trashed else absent),
            )

        # A live viewer on the right URL is the only PRESENT answer.
        assert state(permalink) == PRESENT
        assert state(permalink + "#fullscreen") == PRESENT
        # Trashed / gone states, none of which need to be a 404.
        assert state("https://photos.google.com/trash") == GONE
        assert state("https://photos.google.com/u/0/trash/abc") == GONE
        assert state("https://photos.google.com/") == GONE
        assert state(url(2)) == GONE
        assert state(permalink, trashed=True) == GONE
        assert state(permalink, text="This item is in the trash") == GONE
        assert state(permalink, text="Will be deleted in 60 days") == GONE
        # Neither signal: refuse to guess, let the breaker decide.
        assert state(permalink, viewer=False) == UNKNOWN
        assert classify_page_state(
            current_url=permalink,
            permalink=permalink,
            text_getter=no_text,
            viewer_getter=absent,
            trashed_state_getter=absent,
        ) == UNKNOWN

    def t_toast_text_patterns() -> None:
        for text in (
            "Moved to trash\nUndo",
            "Moved to bin  Undo",
            "1 item moved to trash",
            "Moved 1 item to trash",
            "Photo moved to trash",
        ):
            assert toast_text_says_trashed(text), text
        for text in ("", "Undo", "Share", "Added to album", "Download"):
            assert not toast_text_says_trashed(text), text

    check("a confirmation toast alone proves success", t_toast_is_enough)
    check("a stale viewer DOM no longer causes a false failure",
          t_stale_dom_is_not_a_failure)
    check("leaving the viewer counts as success", t_left_viewer_is_success)
    check("an item already in Trash resolves as already_gone, journalled once",
          t_already_trashed_is_already_gone)
    check("page-state classification covers trash, redirect and unknown",
          t_classify_page_state)
    check("'moved to trash' toast detection", t_toast_text_patterns)

    check("execute deletes and verifies, journal is complete", t_execute_and_verify)
    check("a click that did not delete is a failure", t_unverified_delete_is_failure)
    check("resume skips done items; re-runs never double-act", t_resume_and_idempotency)
    check("already-gone counts as done and is terminal", t_already_gone_is_terminal)
    check("failed items retry; done items stay done",
          t_failed_is_retried_but_terminal_is_sticky)
    check("malformed journal lines are ignored safely",
          t_malformed_journal_lines_ignored)

    # --- circuit breaker --------------------------------------------------- #

    # Every test from here down passes an explicit cooldown sleeper. A test that
    # forgot one would sit through a real 20-minute sleep, so they all record
    # instead: `naps` is also the assertion that no cooldown was taken.
    def recorder() -> Tuple[List[float], Any]:
        naps: List[float] = []

        def fake_cooldown(total: float, log_fn, *_a, **_k) -> None:
            naps.append(total)

        return naps, fake_cooldown

    def t_breaker_on_raises() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "breaker1.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items],
            behaviour={i.url: "raise" for i in items},
        )
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.failed == 3, summary
        assert summary.aborted_reason, "breaker did not record a reason"
        assert len(driver.trashed) == 0
        # Stopped after 3 of 10: 7 untouched.
        assert len([p for p in driver.probes]) == 3, driver.probes
        # Generic UI breakage is not a mutation no-op, so it must not cool down.
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        assert summary.failed_by_kind[FAIL_OTHER] == 3, summary.failed_by_kind
        assert summary.failed_mutation == 0, summary.failed_by_kind

    def t_breaker_on_silent_noops() -> None:
        """Mutation no-ops with NOT ONE success: breakage, not throttling."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "breaker2.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], behaviour={i.url: "noop" for i in items}
        )
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=2)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.failed == 2, summary
        assert summary.deleted == 0
        assert summary.abort_kind == ABORT_NO_SUCCESS, summary.abort_kind
        assert "NOT ONE item has succeeded" in summary.aborted_reason
        # The whole point of the safeguard: it did not sleep.
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        assert summary.failed_mutation == 2, summary.failed_by_kind

    def t_breaker_resets_on_success() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "breaker3.jsonl")
        journal = Journal(path)
        journal.load()
        # fail, fail, succeed, fail, fail, succeed... never 3 in a row.
        behaviour = {}
        for index, item in enumerate(items):
            behaviour[item.url] = "noop" if index % 3 != 2 else "ok"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.deleted == 3, summary
        assert summary.failed == 7, summary
        assert naps == [], naps

    def t_breaker_counts_resolve_failures() -> None:
        items = load_items(csv_path).items

        class Exploding(FakeDriver):
            def probe(self, probe_url: str) -> str:
                nav_guard(probe_url, self._allowed)
                self.probes.append(probe_url)
                raise NavigationError("Page.goto timeout of 30000ms exceeded")

        path = os.path.join(tmpdir, "breaker4.jsonl")
        journal = Journal(path)
        journal.load()
        driver = Exploding([i.url for i in items])
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.failed == 3, summary
        assert summary.aborted_reason
        assert "resolving" in summary.aborted_reason
        assert summary.failed_navigation == 3, summary.failed_by_kind
        assert summary.failed_mutation == 0, summary.failed_by_kind
        # Navigation failures DO cool down (see the test below) - but not here:
        # nothing has ever succeeded, so this is breakage, not throttling.
        assert summary.abort_kind == ABORT_NO_SUCCESS, summary.abort_kind
        assert naps == [], naps

    def t_navigation_streak_cools_down_and_resumes() -> None:
        """*** Navigation throttling is real and must be waited out. ***

        Observed twice on the real account: ~2s spacing died around item 17, and
        a cold-start timeout hit the first item after an idle gap. A run that has
        been deleting fine and then hits a wall of timeouts should sleep and
        carry on, not abandon ~24,000 pending items.
        """
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "navcool.jsonl")
        journal = Journal(path)
        journal.load()

        class Flaky(FakeDriver):
            """Items 2-4 time out; everything else resolves normally."""

            def probe(self, probe_url: str) -> str:
                if probe_url in self._timeouts:
                    nav_guard(probe_url, self._allowed)
                    self.probes.append(probe_url)
                    raise NavigationError("Page.goto timeout of 30000ms exceeded")
                return super().probe(probe_url)

        driver = Flaky(
            [i.url for i in items], behaviour={i.url: "ordinary" for i in items}
        )
        driver._timeouts = {i.url for i in items[2:5]}
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=2.0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     delay_growth=1.5, delay_max=30.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        # Slept once, then kept going: 2 before + 5 after = 7 deleted.
        assert naps == [1200.0], naps
        assert summary.cooldowns == 1, summary
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.deleted == 7, summary
        assert summary.failed_navigation == 3, summary.failed_by_kind
        assert summary.failed_mutation == 0, summary.failed_by_kind
        # And it resumed slower, because the pace that timed out is too fast.
        assert summary.starting_delay == 2.0, summary
        assert summary.final_delay == 3.0, summary
        assert driver.recycles == 1, driver.recycles
        # The timed-out items stay pending for a later run.
        journal2 = Journal(path)
        journal2.load()
        assert [i.url for i in items if not journal2.is_done(i.url)] == [
            i.url for i in items[2:5]
        ]

    def t_mixed_throttle_kinds_cool_down_together() -> None:
        """A streak mixing both throttle kinds is still throttling."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "mixcool.jsonl")
        journal = Journal(path)
        journal.load()

        class Flaky(FakeDriver):
            def probe(self, probe_url: str) -> str:
                if probe_url in self._timeouts:
                    nav_guard(probe_url, self._allowed)
                    self.probes.append(probe_url)
                    raise NavigationError("Page.goto timeout")
                return super().probe(probe_url)

        # item 2 times out, items 3-4 are mutation no-ops: 3 in a row, two kinds.
        behaviour = {i.url: "ordinary" for i in items}
        behaviour[items[3].url] = "noop"
        behaviour[items[4].url] = "noop"
        driver = Flaky([i.url for i in items], behaviour=behaviour)
        driver._timeouts = {items[2].url}
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=600.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert naps == [600.0], naps
        assert summary.cooldowns == 1, summary
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.failed_navigation == 1, summary.failed_by_kind
        assert summary.failed_mutation == 2, summary.failed_by_kind
        assert summary.deleted == 7, summary

    def t_stacked_breaks_a_throttle_streak() -> None:
        """A stacked failure between no-ops means they are not 3 in a row."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "streakbreak.jsonl")
        journal = Journal(path)
        journal.load()
        # noop, noop, stacked_weird, noop, noop, ... : never 3 throttle failures
        # consecutively, so the throttle breaker must not fire. The all-kinds
        # breaker does, with a stacked verdict and no sleep.
        behaviour = {i.url: "noop" for i in items}
        behaviour[items[0].url] = "ordinary"
        behaviour[items[3].url] = "stacked_weird"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        assert summary.abort_kind == ABORT_STACKED, summary.abort_kind
        assert summary.failed_stacked == 1, summary.failed_by_kind

    def t_fatal_aborts_without_cooldown() -> None:
        items = load_items(csv_path).items

        class SignedOut(FakeDriver):
            def probe(self, probe_url: str) -> str:
                nav_guard(probe_url, self._allowed)
                self.probes.append(probe_url)
                raise FatalDriverError("redirected to a sign-in page")

        path = os.path.join(tmpdir, "fatal.jsonl")
        journal = Journal(path)
        journal.load()
        driver = SignedOut([i.url for i in items])
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        # One attempt, then stop: no point walking 10 items while signed out.
        assert summary.failed == 1, summary
        assert summary.abort_kind == ABORT_FATAL, summary.abort_kind
        assert len(driver.probes) == 1, driver.probes
        assert naps == [], naps

    check("circuit breaker trips on repeated UI errors", t_breaker_on_raises)
    check("mutation no-ops with zero successes abort instead of cooling down",
          t_breaker_on_silent_noops)
    check("circuit breaker resets after a success", t_breaker_resets_on_success)
    check("navigation failures with zero successes abort rather than sleep",
          t_breaker_counts_resolve_failures)
    check("a navigation streak cools down, slows down and resumes",
          t_navigation_streak_cools_down_and_resumes)
    check("a streak mixing both throttle kinds still cools down",
          t_mixed_throttle_kinds_cool_down_together)
    check("a stacked failure breaks a throttle streak",
          t_stacked_breaks_a_throttle_streak)
    check("a sign-out aborts at once without a cooldown",
          t_fatal_aborts_without_cooldown)

    # --- cooldown: ride out the trash-mutation limiter --------------------- #

    def t_cooldown_triggered_and_resumed() -> None:
        """The headline behaviour: throttled mid-run, sleep, carry on."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool1.jsonl")
        journal = Journal(path)
        journal.load()
        # Items 0-1 delete, 2-4 are no-ops (trips the breaker at 3), the rest
        # delete again - as they would once the limiter relaxed.
        behaviour = {item.url: "ok" for item in items}
        for item in items[2:5]:
            behaviour[item.url] = "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     delay_growth=1.5, delay_max=30.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        # It slept once and then kept going, rather than abandoning the run.
        assert summary.cooldowns == 1, summary
        assert naps == [1200.0], naps
        assert summary.cooldown_seconds == 1200.0, summary
        assert not summary.aborted_reason, summary.aborted_reason
        # 2 before + 5 after = 7 deleted, 3 no-ops failed, all 10 visited.
        assert summary.deleted == 7, summary
        assert summary.failed == 3, summary
        assert summary.failed_mutation == 3, summary.failed_by_kind
        assert summary.attempted == 10, summary
        # The tab was recycled so SPA state does not accumulate.
        assert driver.recycles == 1, driver.recycles
        # Failed items stay PENDING for a later run.
        journal2 = Journal(path)
        journal2.load()
        assert [i.url for i in items if not journal2.is_done(i.url)] == [
            i.url for i in items[2:5]
        ]

    def t_cooldown_backoff_is_exponential_and_capped() -> None:
        cfg = Config(cooldown=1200.0, cooldown_backoff=2.0, cooldown_cap=7200.0)
        assert cooldown_seconds_for(cfg, 0) == 1200.0
        assert cooldown_seconds_for(cfg, 1) == 2400.0
        assert cooldown_seconds_for(cfg, 2) == 4800.0
        # Capped from here on: 9600 would exceed --cooldown-cap.
        assert cooldown_seconds_for(cfg, 3) == 7200.0
        assert cooldown_seconds_for(cfg, 9) == 7200.0
        # Disabled.
        assert cooldown_seconds_for(Config(cooldown=0), 0) == 0.0

    def t_delay_growth_and_cap() -> None:
        cfg = Config(delay=5.0, delay_growth=1.5, delay_max=30.0)
        assert grown_delay(cfg) == 7.5
        cfg.delay = 7.5
        assert grown_delay(cfg) == 11.25
        # Growth stops at the ceiling and never exceeds it.
        cfg.delay = 25.0
        assert grown_delay(cfg) == 30.0
        cfg.delay = 30.0
        assert grown_delay(cfg) == 30.0, "delay must not pass --delay-max"
        # A growth factor of 1.0 holds the pace steady.
        assert grown_delay(Config(delay=5.0, delay_growth=1.0)) == 5.0

    def t_delay_grows_across_cooldowns() -> None:
        """Two cooldowns in one run: the pace must slow each time, then cap."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool2.jsonl")
        journal = Journal(path)
        journal.load()
        # ok, noop x2, ok, noop x2, ok ... with a threshold of 2 that is two
        # cooldowns with real progress in between.
        behaviour = {}
        for index, item in enumerate(items):
            behaviour[item.url] = "ok" if index % 3 == 0 else "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=4.0, journal_path=path,
                     max_consecutive_failures=2, cooldown=600.0,
                     cooldown_backoff=2.0, delay_growth=2.0, delay_max=10.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.cooldowns == 3, summary
        assert naps == [600.0, 1200.0, 2400.0], naps
        assert summary.starting_delay == 4.0, summary
        # 4 -> 8 -> 10 (capped, not 16) -> 10.
        assert summary.final_delay == 10.0, summary
        assert cfg.delay == 10.0, cfg.delay
        assert summary.cooldown_seconds == 4200.0, summary

    def t_max_cooldowns_aborts() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool3.jsonl")
        journal = Journal(path)
        journal.load()
        # ok, noop, noop repeating: a success before each breaker trip, so every
        # trip is read as throttling and takes a cooldown.
        behaviour = {}
        for index, item in enumerate(items):
            behaviour[item.url] = "ok" if index % 3 == 0 else "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=2, cooldown=600.0,
                     max_cooldowns=2)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.cooldowns == 2, summary
        assert len(naps) == 2, naps
        assert summary.abort_kind == ABORT_COOLDOWNS_EXHAUSTED, summary.abort_kind
        assert "--max-cooldowns 2" in summary.aborted_reason
        # It stopped rather than sleeping a third time.
        assert summary.remaining > 0, summary

    def t_zero_success_after_cooldown_aborts() -> None:
        """*** The safeguard. A changed UI must not burn every cooldown. ***"""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool4.jsonl")
        journal = Journal(path)
        journal.load()
        # Two real deletions, then everything is a no-op forever: one cooldown is
        # justified (there was progress), but after resuming nothing works, so
        # the run must give up instead of taking the remaining 5 cooldowns.
        behaviour = {item.url: "noop" for item in items}
        behaviour[items[0].url] = "ok"
        behaviour[items[1].url] = "ok"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     max_cooldowns=6, post_cooldown_window=3)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.deleted == 2, summary
        # Exactly ONE cooldown, not six.
        assert summary.cooldowns == 1, summary
        assert len(naps) == 1, naps
        assert summary.abort_kind == ABORT_NO_SUCCESS, summary.abort_kind
        assert "ALL failed" in summary.aborted_reason, summary.aborted_reason
        assert "--post-cooldown-window 3" in summary.aborted_reason
        # It stopped after the window, not after the whole batch.
        assert summary.attempted <= 2 + 3 + 3, summary

    def t_double_ctrl_c_during_cooldown_exits() -> None:
        """The escape hatch: a sleep that raises KeyboardInterrupt ends the run.

        That is what interruptible_sleep does on a SECOND press inside the skip
        window (v1.2.3), and what it did on any press before then.
        """
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool5.jsonl")
        journal = Journal(path)
        journal.load()
        behaviour = {item.url: "ok" for item in items}
        for item in items[2:5]:
            behaviour[item.url] = "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)

        interrupted_at: List[float] = []

        def impatient(total: float, log_fn, *_a, **_k) -> None:
            # Exactly what interruptible_sleep does on a double press.
            interrupted_at.append(total)
            raise KeyboardInterrupt

        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=impatient)
        assert interrupted_at == [1200.0], interrupted_at
        assert summary.interrupted is True, summary
        # Did NOT finish the batch, and did NOT count the interrupted cooldown.
        assert summary.cooldowns == 0, summary
        assert summary.deleted == 2, summary
        assert summary.attempted == 5, summary
        # The journal is complete through the last finished item.
        journal2 = Journal(path)
        journal2.load()
        assert journal2.is_done(items[0].url) and journal2.is_done(items[1].url)
        assert not journal2.is_done(items[5].url)

    def fake_clock_and_waiter(naps: List[float]):
        """A clock that only advances when the sleeper waits. No real time."""
        state = {"now": 1000.0}

        def clock() -> float:
            return state["now"]

        def waiter(seconds: float) -> bool:
            naps.append(seconds)
            state["now"] += seconds
            return False  # never interrupted

        return clock, waiter

    def t_interruptible_sleep_is_sliced() -> None:
        """It must nap in slices, so a Ctrl-C is not stuck behind a 20min sleep."""
        naps: List[float] = []
        clock, waiter = fake_clock_and_waiter(naps)
        result = interruptible_sleep(12.0, quiet, slice_seconds=5.0,
                                     report_every=0, clock=clock, waiter=waiter)
        assert len(naps) >= 3, naps
        assert all(nap <= 5.0 for nap in naps), naps
        assert abs(sum(naps) - 12.0) < 0.01, naps
        # A sleep that ran its course is not a skip.
        assert result.skipped is False, result
        assert abs(result.slept - 12.0) < 0.01, result
        # And a zero/negative request sleeps not at all.
        naps.clear()
        clock, waiter = fake_clock_and_waiter(naps)
        assert interruptible_sleep(0, quiet, clock=clock,
                                   waiter=waiter).slept == 0.0
        assert interruptible_sleep(-5, quiet, clock=clock,
                                   waiter=waiter).skipped is False
        assert naps == [], naps

    # ----------------------------------------------------------------------- #
    # v1.2.3: Ctrl-C during a cooldown SKIPS THE WAIT instead of quitting
    # ----------------------------------------------------------------------- #

    def press_ctrl_c_after(delays: Sequence[float]) -> None:
        """Send real SIGINTs to this process from a helper thread.

        Signals are delivered to the main thread, which is where the handler
        under test is installed - so this exercises the real mechanism rather
        than calling the handler directly.
        """
        def go() -> None:
            previous = 0.0
            for delay in delays:
                time.sleep(max(0.0, delay - previous))
                previous = delay
                os.kill(os.getpid(), signal.SIGINT)

        threading.Thread(target=go, daemon=True).start()

    def t_one_sigint_skips_the_cooldown_and_resumes() -> None:
        """*** THE v1.2.3 FIX: one press skips the wait, it does not quit. ***"""
        before = signal.getsignal(signal.SIGINT)
        lines: List[str] = []
        press_ctrl_c_after([0.15])
        started = time.monotonic()
        # A 30s cooldown that must come back in well under a second.
        result = interruptible_sleep(30.0, lines.append, slice_seconds=0.05,
                                     report_every=0, skip_window=0.2)
        elapsed = time.monotonic() - started
        # It returned, rather than raising: the run carries on.
        assert result.skipped is True, result
        assert result.requested == 30.0, result
        assert elapsed < 5.0, elapsed
        assert result.slept < 5.0, result
        # The log tells the user what just happened and how to really quit.
        joined = "\n".join(lines)
        assert "skipping the rest of this cooldown" in joined, joined
        assert "press Ctrl-C again" in joined, joined
        # *** And the handler is back to normal, not swallowing anything.
        assert signal.getsignal(signal.SIGINT) is before

    def t_two_sigints_in_the_window_exit() -> None:
        """The escape hatch: a second press inside the window really stops."""
        before = signal.getsignal(signal.SIGINT)
        press_ctrl_c_after([0.15, 0.35])
        try:
            interruptible_sleep(30.0, quiet, slice_seconds=0.05,
                                report_every=0, skip_window=1.5)
            raise AssertionError("a double Ctrl-C did not stop the sleep")
        except KeyboardInterrupt:
            pass
        # Still restored, even though it unwound through an exception.
        assert signal.getsignal(signal.SIGINT) is before

    def t_countdown_line_names_the_keys() -> None:
        """While sleeping, the heartbeat must say what Ctrl-C now does."""
        lines: List[str] = []
        naps: List[float] = []
        clock, waiter = fake_clock_and_waiter(naps)
        interruptible_sleep(600.0, lines.append, slice_seconds=5.0,
                            report_every=60.0, clock=clock, waiter=waiter)
        countdown = [line for line in lines if "still cooling down" in line]
        assert countdown, lines
        for line in countdown:
            assert "Ctrl-C to skip this cooldown" in line, line
            assert "press twice to quit" in line, line

    def t_a_skipped_cooldown_still_counts_and_still_grows_the_delay() -> None:
        """A skipped cooldown is a cooldown: the throttling really happened."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool_skip.jsonl")
        journal = Journal(path)
        journal.load()
        behaviour = {item.url: "ok" for item in items}
        for item in items[2:5]:
            behaviour[item.url] = "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)

        asked: List[float] = []

        def skipping_sleeper(total: float, log_fn, *_a, **_k) -> CooldownSleep:
            asked.append(total)
            return CooldownSleep(requested=total, slept=1.5, skipped=True)

        lines: List[str] = []
        cfg = Config(execute=True, limit=10, delay=4.0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     delay_growth=1.5, delay_max=30.0, post_cooldown_window=99)
        summary = run(items, driver, journal, cfg, log=lines.append,
                      sleeper=no_sleep, cooldown_sleeper=skipping_sleeper)
        assert asked == [1200.0], asked
        # It did NOT end the run: the remaining items were processed.
        assert summary.interrupted is False, summary
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.deleted == 7, summary
        # It still counts toward --max-cooldowns...
        assert summary.cooldowns == 1, summary
        assert summary.cooldowns_skipped == 1, summary
        # ...only the time actually slept is charged to the total...
        assert abs(summary.cooldown_seconds - 1.5) < 0.01, summary
        # ...and the delay still grew.
        assert cfg.delay == 6.0, cfg.delay
        assert summary.final_delay == 6.0, summary
        # And the log says all of that, so none of it is a surprise.
        joined = "\n".join(lines)
        assert "cooldown skipped by user" in joined, joined
        assert "still COUNTS" in joined, joined
        assert "delay" in joined and "still grows" in joined, joined

    def t_sigint_outside_a_cooldown_still_exits() -> None:
        """Unchanged behaviour away from a cooldown, including after a skip."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool_skip_then_quit.jsonl")
        journal = Journal(path)
        journal.load()

        # items 2-4 no-op (-> a cooldown, which the user skips), and the Ctrl-C
        # arrives later, on item 7, while the run is working normally again.
        behaviour = {item.url: "ok" for item in items}
        for item in items[2:5]:
            behaviour[item.url] = "noop"

        class QuitsLater(FakeDriver):
            def probe(self, probe_url: str) -> str:
                if probe_url == items[7].url:
                    raise KeyboardInterrupt
                return super().probe(probe_url)

        driver = QuitsLater([i.url for i in items], behaviour=behaviour)

        def skipping_sleeper(total: float, log_fn, *_a, **_k) -> CooldownSleep:
            return CooldownSleep(requested=total, slept=0.5, skipped=True)

        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     post_cooldown_window=99)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=skipping_sleeper)
        # The skip happened and the run went on...
        assert summary.cooldowns_skipped == 1, summary
        # ...and the LATER Ctrl-C still stopped it.
        assert summary.interrupted is True, summary
        # 0 and 1 deleted, 2-4 no-op into the skipped cooldown, 5 and 6 deleted,
        # then the ^C on item 7.
        assert summary.deleted == 4, summary
        # The journal is flushed through the last completed item.
        journal2 = Journal(path)
        journal2.load()
        assert journal2.is_done(items[6].url), "journal not flushed before the ^C"
        assert not journal2.is_done(items[7].url)
        # And that is exit code 130, not an abort code.
        assert not summary.aborted_reason, summary.aborted_reason

    def t_sleep_degrades_when_no_handler_can_be_installed() -> None:
        """Off the main thread there is no SIGINT to catch: old behaviour."""
        outcome: List[Any] = []

        def body() -> None:
            try:
                naps: List[float] = []
                clock, waiter = fake_clock_and_waiter(naps)
                outcome.append(
                    interruptible_sleep(10.0, quiet, slice_seconds=5.0,
                                        report_every=0, clock=clock,
                                        waiter=waiter)
                )
            except Exception as exc:  # pragma: no cover - would be a bug
                outcome.append(exc)

        thread = threading.Thread(target=body)
        thread.start()
        thread.join(timeout=10)
        assert outcome and isinstance(outcome[0], CooldownSleep), outcome
        assert outcome[0].skipped is False, outcome
        # The main thread's handler is untouched by a sleep in another thread.
        assert signal.getsignal(signal.SIGINT) is not None

    def t_skip_helpers_tolerate_a_none_returning_sleeper() -> None:
        """The 1.2.2 sleeper signature returned None; it must not look skipped."""
        assert cooldown_was_skipped(None) is False
        assert cooldown_slept_seconds(None, 1200.0) == 1200.0
        assert cooldown_was_skipped(CooldownSleep(skipped=True)) is True
        assert cooldown_slept_seconds(CooldownSleep(slept=7.0), 1200.0) == 7.0
        # A negative or nonsense reading never becomes negative time.
        assert cooldown_slept_seconds(CooldownSleep(slept=-5.0), 10.0) == 0.0
        assert cooldown_slept_seconds(None, -10.0) == 0.0

    def t_cooldown_disabled_restores_old_behaviour() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "cool6.jsonl")
        journal = Journal(path)
        journal.load()
        behaviour = {item.url: "ok" for item in items}
        for item in items[2:6]:
            behaviour[item.url] = "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=0.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        assert summary.abort_kind == ABORT_COOLDOWNS_EXHAUSTED, summary.abort_kind

    # ----------------------------------------------------------------------- #
    # FIX 3: a cluster of stubborn items must not block every run
    # ----------------------------------------------------------------------- #

    def fail_in_journal(path: str, urls: Sequence[str], times: int = 1) -> None:
        """Pre-load the journal with failures, as earlier runs would have."""
        journal = Journal(path)
        for _attempt in range(times):
            for url in urls:
                journal.record(Item(url=url), "failed", "earlier run")

    def t_plan_queue_orders_fresh_first_and_quarantines() -> None:
        """The partition itself: fresh, then retries, with the cap applied."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "queue_plan.jsonl")
        # items[0] failed twice, items[1] three times, items[2] once.
        fail_in_journal(path, [items[0].url], times=2)
        fail_in_journal(path, [items[1].url], times=3)
        fail_in_journal(path, [items[2].url], times=1)
        journal = Journal(path)
        journal.load()
        assert journal.failure_count(items[0].url) == 2
        assert journal.failure_count(items[1].url) == 3
        assert journal.failure_count(items[2].url) == 1
        assert journal.failure_count(items[3].url) == 0
        assert journal.has_been_attempted(items[0].url)
        assert not journal.has_been_attempted(items[3].url)

        queue = plan_queue(items, journal, max_attempts=3)
        # Never-attempted items keep their input order and come first...
        assert [i.url for i in queue.fresh] == [i.url for i in items[3:]]
        # ...retries follow, under the cap...
        assert [i.url for i in queue.retries] == [items[0].url, items[2].url]
        # ...and the one at the cap is quarantined out.
        assert [i.url for i in queue.quarantined] == [items[1].url]
        ordered = queue.ordered()
        assert ordered[: len(items) - 3] == queue.fresh
        assert ordered[len(items) - 3:] == queue.retries
        assert items[1].url not in [i.url for i in ordered]
        # Every fresh item precedes every retry - the property that matters.
        first_retry = min(
            index for index, item in enumerate(ordered)
            if journal.failure_count(item.url) > 0
        )
        assert all(
            journal.failure_count(item.url) == 0
            for item in ordered[:first_retry]
        )

        # --retry-quarantined puts the capped item back, still after the fresh.
        reopened = plan_queue(items, journal, max_attempts=3,
                              retry_quarantined=True)
        assert reopened.quarantined == []
        assert items[1].url in [i.url for i in reopened.retries]
        assert [i.url for i in reopened.fresh] == [i.url for i in items[3:]]
        # A cap of 1 quarantines everything that has ever failed.
        strict = plan_queue(items, journal, max_attempts=1)
        assert [i.url for i in strict.quarantined] == [
            items[0].url, items[1].url, items[2].url
        ]
        # The cap is never below 1, however it is passed in.
        assert plan_queue(items, journal, max_attempts=0).retries == []

    def t_stubborn_cluster_no_longer_blocks_the_run() -> None:
        """*** FIX 3, the exact production failure, end to end. ***

        11,107 items, 1,351 done, and the first three pending ones are
        known-bad. v1.2.2 retried those three FIRST, got zero successes, and
        tripped the "not one item has succeeded from the start of the run"
        safeguard - aborting before it touched any of the items that work.
        """
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "stubborn.jsonl")
        # The three stubborn ones are at the FRONT of the input, as in the run.
        stubborn = [items[0].url, items[1].url, items[2].url]
        fail_in_journal(path, stubborn, times=1)
        journal = Journal(path)
        journal.load()
        behaviour = {item.url: "ok" for item in items}
        for url in stubborn:
            behaviour[url] = "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     max_attempts=3)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)

        # The run did the real work instead of aborting on the known-bad three.
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.abort_kind != ABORT_NO_SUCCESS, summary.abort_kind
        assert summary.deleted == 7, summary
        # The seven good items were attempted BEFORE the three retries.
        assert driver.probes[:7] == [i.url for i in items[3:10]], driver.probes[:7]
        assert set(driver.probes[7:]) == set(stubborn), driver.probes[7:]
        assert summary.fresh_attempted == 7, summary
        assert summary.retried_attempted == 3, summary
        # The stubborn three still failed, and are still pending...
        assert summary.failed == 3, summary
        journal2 = Journal(path)
        journal2.load()
        assert all(not journal2.is_done(url) for url in stubborn)
        # ...with their attempt counts now at the cap, so the NEXT run
        # quarantines them instead of retrying them again.
        assert all(journal2.failure_count(url) == 2 for url in stubborn)
        next_queue = plan_queue(items, journal2, max_attempts=2)
        assert sorted(i.url for i in next_queue.quarantined) == sorted(stubborn)

    def t_known_bad_retries_do_not_trip_the_zero_success_guard() -> None:
        """The guard must not fire when every attempt was a known-bad retry.

        A changed UI is what that guard is for; a known-bad item failing again
        is not news. Here the whole batch is retries, so the run grinds through
        them and reports failures - but it must NOT abort with 'no successes'.
        """
        items = load_items(csv_path).items[:5]
        path = os.path.join(tmpdir, "all_retries.jsonl")
        fail_in_journal(path, [i.url for i in items], times=1)
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items],
            behaviour={i.url: "noop" for i in items},
        )
        naps, fake_cooldown = recorder()
        lines: List[str] = []
        cfg = Config(execute=True, limit=5, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     max_attempts=5)
        summary = run(items, driver, journal, cfg, log=lines.append,
                      sleeper=no_sleep, cooldown_sleeper=fake_cooldown)
        assert summary.failed == 5, summary
        assert summary.fresh_attempted == 0, summary
        assert summary.retried_attempted == 5, summary
        # Not an abort, and above all not the no-success verdict...
        assert summary.abort_kind != ABORT_NO_SUCCESS, summary.aborted_reason
        assert not summary.aborted_reason, summary.aborted_reason
        # ...and no cooldown was spent on them either.
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        joined = "\n".join(lines)
        assert "was a RETRY of an item that has failed before" in joined, joined
        # Every item was still attempted: nothing was silently skipped. (Each
        # one is probed twice - once to resolve, once to verify.)
        assert summary.attempted == 5, summary
        assert sorted(set(driver.probes)) == sorted(i.url for i in items)

        # A FRESH item failing alongside them still trips the guard, because
        # that IS new information.
        fresh_items = load_items(csv_path).items[5:10]
        path2 = os.path.join(tmpdir, "fresh_fails.jsonl")
        journal2 = Journal(path2)
        journal2.load()
        driver2 = FakeDriver(
            [i.url for i in fresh_items],
            behaviour={i.url: "noop" for i in fresh_items},
        )
        cfg2 = Config(execute=True, limit=5, delay=0, journal_path=path2,
                      max_consecutive_failures=3, cooldown=1200.0)
        summary2 = run(fresh_items, driver2, journal2, cfg2, log=quiet,
                       sleeper=no_sleep, cooldown_sleeper=fake_cooldown)
        assert summary2.abort_kind == ABORT_NO_SUCCESS, summary2.abort_kind
        assert summary2.fresh_attempted == 3, summary2

    def t_post_cooldown_window_ignores_known_bad_retries() -> None:
        """The post-resume window is about the UI, so it counts fresh items only.

        A cooldown resumes straight into the retry tail of the queue. If those
        known-bad retries filled the window, the run would abort with the
        "nothing works since the resume" verdict - the same false alarm as the
        zero-success guard, one safeguard over.
        """
        items = load_items(csv_path).items[:7]
        path = os.path.join(tmpdir, "window_retries.jsonl")
        stubborn = [items[4].url, items[5].url, items[6].url]
        fail_in_journal(path, stubborn, times=1)
        journal = Journal(path)
        journal.load()
        # Fresh: one success then three no-ops, which trips the breaker and
        # takes a cooldown (there WAS progress since the start).
        behaviour = {items[0].url: "ok"}
        for item in items[1:4]:
            behaviour[item.url] = "noop"
        for url in stubborn:
            behaviour[url] = "noop"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=7, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     max_cooldowns=6, post_cooldown_window=3, max_attempts=5)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        # The cooldown happened on the FRESH failures...
        assert summary.cooldowns == 1, summary
        assert naps == [1200.0], naps
        # ...and the retries that followed did NOT trigger the post-resume
        # abort, even though they filled the window with failures.
        assert summary.abort_kind != ABORT_NO_SUCCESS, (
            summary.abort_kind, summary.aborted_reason
        )
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.retried_attempted == 3, summary
        assert summary.deleted == 1, summary
        assert summary.failed == 6, summary

    def t_quarantine_is_reported_and_reopened_on_request() -> None:
        """Past the cap an item is skipped, reported, and never lost."""
        items = load_items(csv_path).items[:4]
        path = os.path.join(tmpdir, "quarantine.jsonl")
        capped = [items[0].url, items[1].url]
        fail_in_journal(path, capped, times=3)
        journal = Journal(path)
        journal.load()
        driver = FakeDriver([i.url for i in items])
        lines: List[str] = []
        cfg = Config(execute=True, limit=4, delay=0, journal_path=path,
                     max_attempts=3)
        summary = run(items, driver, journal, cfg, log=lines.append,
                      sleeper=no_sleep)
        # Only the two uncapped items ran.
        assert summary.deleted == 2, summary
        assert driver.probes == [items[2].url, items[3].url], driver.probes
        assert summary.quarantined == 2, summary
        assert sorted(summary.quarantined_urls) == sorted(capped)
        joined = "\n".join(lines)
        assert "QUARANTINED" in joined, joined
        assert "--retry-quarantined" in joined, joined
        # Nothing was deleted from the journal: the history is still there.
        journal2 = Journal(path)
        journal2.load()
        assert all(journal2.failure_count(url) == 3 for url in capped)

        # --retry-quarantined brings them back, and they are attempted LAST.
        driver2 = FakeDriver([i.url for i in items])
        journal3 = Journal(path)
        journal3.load()
        cfg2 = Config(execute=True, limit=4, delay=0, journal_path=path,
                      max_attempts=3, retry_quarantined=True)
        summary2 = run(items, driver2, journal3, cfg2, log=quiet,
                       sleeper=no_sleep)
        assert summary2.quarantined == 0, summary2
        assert summary2.deleted == 2, summary2  # the other two are done now
        assert driver2.probes == capped, driver2.probes
        assert summary2.retried_attempted == 2, summary2

    def t_max_attempts_cli_validation() -> None:
        parser = build_parser()
        assert parser.parse_args([]).max_attempts == 3
        assert parser.parse_args([]).retry_quarantined is False
        assert parser.parse_args(["--max-attempts", "5"]).max_attempts == 5
        assert parser.parse_args(["--retry-quarantined"]).retry_quarantined is True
        assert main(["--max-attempts", "0", "--input", csv_path]) == 2

    check("the queue puts never-attempted items first and quarantines the rest",
          t_plan_queue_orders_fresh_first_and_quarantines)
    check("*** a stubborn cluster no longer blocks the whole run (FIX 3) ***",
          t_stubborn_cluster_no_longer_blocks_the_run)
    check("known-bad retries do not trip the zero-success guard",
          t_known_bad_retries_do_not_trip_the_zero_success_guard)
    check("the post-cooldown window ignores known-bad retries",
          t_post_cooldown_window_ignores_known_bad_retries)
    check("quarantined items are reported and reopened on request",
          t_quarantine_is_reported_and_reopened_on_request)
    check("--max-attempts / --retry-quarantined are wired up",
          t_max_attempts_cli_validation)

    check("a cooldown is taken and the run resumes where it stopped",
          t_cooldown_triggered_and_resumed)
    check("cooldown backoff is exponential and capped",
          t_cooldown_backoff_is_exponential_and_capped)
    check("delay growth respects --delay-max", t_delay_growth_and_cap)
    check("the delay grows after each cooldown and stops at the cap",
          t_delay_grows_across_cooldowns)
    check("--max-cooldowns eventually aborts", t_max_cooldowns_aborts)
    check("zero successes after a cooldown aborts instead of sleeping again",
          t_zero_success_after_cooldown_aborts)
    check("a double Ctrl-C during a cooldown exits",
          t_double_ctrl_c_during_cooldown_exits)
    check("a cooldown sleeps in interruptible slices",
          t_interruptible_sleep_is_sliced)
    check("*** one Ctrl-C skips the cooldown and resumes (v1.2.3) ***",
          t_one_sigint_skips_the_cooldown_and_resumes)
    check("two Ctrl-Cs inside the window stop the run",
          t_two_sigints_in_the_window_exit)
    check("the cooldown countdown names the keys",
          t_countdown_line_names_the_keys)
    check("a skipped cooldown still counts and still grows the delay",
          t_a_skipped_cooldown_still_counts_and_still_grows_the_delay)
    check("Ctrl-C outside a cooldown still exits, even after a skip",
          t_sigint_outside_a_cooldown_still_exits)
    check("the cooldown sleep degrades safely off the main thread",
          t_sleep_degrades_when_no_handler_can_be_installed)
    check("the skip helpers tolerate a None-returning sleeper",
          t_skip_helpers_tolerate_a_none_returning_sleeper)
    check("--cooldown 0 restores the abort-immediately behaviour",
          t_cooldown_disabled_restores_old_behaviour)

    # --- the delete confirmation dialog (the safety-critical part) ---------- #

    # Verbatim wording from the real UI. Both dialogs share the heading, so only
    # the options differ - which is the entire point of these tests.
    REAL_HEADING = (
        "Remove from your Google Account, devices with backup turned on, "
        "and places shared within Google Photos?"
    )
    REAL_STACKED_OPTIONS = ["All 8 photos", "Current photo only"]
    REAL_ORDINARY_OPTIONS = ["Cancel", "Move to trash"]

    # The page-wide label dump from the v1.2.0 failure report. Note the TWO
    # "Move to trash" entries: one is the viewer toolbar, one is the dialog's.
    REAL_PAGE_DUMP = [
        "Main menu", "Close", "Search your photos and albums",
        "Clear search query", "Create and add photos", "Help & Feedback",
        "More options", "Turn on motion", "Share", "Edit", "Zoom in",
        "Open info", "Favorite", "Move to trash", "More options", "Cancel",
        "Move to trash", "Google apps", "Google Account: someone",
        "Expand albums list",
    ]

    def t_heading_is_not_the_discriminator() -> None:
        """*** The v1.2.0 regression, pinned down. ***

        The heading appears on BOTH dialogs, so it can corroborate that a delete
        dialog is up and nothing more. Treating it as proof of a stack is what
        made every single-item confirm unanswerable.
        """
        assert stacked_heading_present(REAL_HEADING)
        # Any one of the three sentence fragments is enough for the weak signal.
        assert stacked_heading_present("Remove from your Google Account?")
        assert stacked_heading_present("devices with backup turned on")
        assert stacked_heading_present("places shared within Google Photos")
        for text in ("", "Move to trash", "Moved to trash  Undo", "July 4, 2019"):
            assert not stacked_heading_present(text), text

        # The heading is present in both cases below, and the decision differs
        # purely on the options. This is the regression test.
        assert decide_dialog_action(REAL_ORDINARY_OPTIONS) == (
            DIALOG_ORDINARY, "Move to trash"
        )
        assert decide_dialog_action(REAL_STACKED_OPTIONS) == (
            DIALOG_STACKED, "Current photo only"
        )

    def t_ordinary_dialog_is_confirmed_not_treated_as_stacked() -> None:
        kind, label = decide_dialog_action(REAL_ORDINARY_OPTIONS)
        assert kind == DIALOG_ORDINARY, kind
        assert label == "Move to trash", label
        # Order, case and padding must not matter.
        for options in (
            ["Move to trash", "Cancel"],
            ["  Move to trash  ", "Cancel"],
            ["Cancel", "MOVE TO TRASH"],
            ["Cancel", "Move to bin"],
            ["Cancel", "Delete"],
        ):
            kind, label = decide_dialog_action(options)
            assert kind == DIALOG_ORDINARY, (options, kind)
            assert not is_all_photos_option(label), (options, label)

        # The confirm matcher must be ANCHORED. An unanchored one also matches
        # the viewer toolbar's own button and labels that merely begin with the
        # phrase - and clicking the toolbar re-opens the dialog, never confirms.
        for label in (
            "Move to trash and 7 more",
            "Move to trash?",
            "Move to trash all 8 photos",
            "Don't move to trash",
            "Undo move to trash",
        ):
            assert not is_confirm_option(label), label
        # Which means a dialog offering only those is a refusal, not a click.
        try:
            decide_dialog_action(["Cancel", "Move to trash all 8 photos"])
            raise AssertionError("an unanchored confirm label was accepted")
        except StackedDialogError:
            pass

    def t_stacked_dialog_still_chooses_current_photo_only() -> None:
        assert decide_dialog_action(REAL_STACKED_OPTIONS)[1] == "Current photo only"
        # Order must not matter: the choice is by wording, never by position.
        assert decide_dialog_action(
            ["Current photo only", "All 8 photos"]
        ) == (DIALOG_STACKED, "Current photo only")
        # Stack size, media type, case and surrounding whitespace must not matter.
        for options, expected in (
            (["All 2 photos", "Current photo only"], "Current photo only"),
            (["All 147 photos", "  Current photo only  "], "Current photo only"),
            (["All 3 videos", "Current video only"], "Current video only"),
            (["All 4 items", "CURRENT PHOTO ONLY"], "CURRENT PHOTO ONLY"),
        ):
            kind, label = decide_dialog_action(options)
            assert kind == DIALOG_STACKED, (options, kind)
            assert label.strip() == expected.strip(), options

    def t_stacked_wins_over_a_confirm_button() -> None:
        """If both are somehow present, the stacked option must win.

        Precedence matters: were "Move to trash" checked first, a stacked dialog
        that also carried one would be confirmed blind - and on a stack, a blind
        confirm is how "All N photos" becomes the effective choice.
        """
        for options in (
            ["All 8 photos", "Current photo only", "Move to trash"],
            ["Move to trash", "All 8 photos", "Current photo only"],
            ["Cancel", "Move to trash", "Current photo only"],
        ):
            kind, label = decide_dialog_action(options)
            assert kind == DIALOG_STACKED, (options, kind)
            assert is_current_only_option(label), (options, label)

    def t_all_n_photos_is_never_chosen() -> None:
        """*** The one test that must never regress. ***

        Across every shape of dialog - including ones with no safe option, ones
        that are empty, and adversarial labels designed to look safe - the
        decision must never return a label that deletes the whole stack.
        """
        dangerous = [
            "All 8 photos",
            "all 8 photos",
            "ALL 2 PHOTOS",
            "  All 147 photos  ",
            "All 3 videos",
            "All 12 items",
            "All 1 photo",
        ]
        candidate_sets = [
            dangerous,
            [],
            [""],
            ["All 8 photos"],
            ["All 8 photos", "Cancel"],
            ["All 8 photos", "This photo only"],
            ["All 8 photos", "Just this one"],
            ["All 8 photos", "Current photo only", "Current photo only"],
            REAL_STACKED_OPTIONS,
            REAL_ORDINARY_OPTIONS,
            REAL_PAGE_DUMP,
            # Adversarial: labels that contain the safe words but are not it.
            ["All 8 photos current photo only"],
            ["Current photo only and all 8 photos"],
            ["All 8 photos (current photo only)"],
            ["Not current photo only"],
            ["Current photo only?"],
            [None, 0, "All 8 photos"],  # type: ignore[list-item]
        ]
        for options in candidate_sets:
            try:
                _kind, chosen = decide_dialog_action(options)  # type: ignore[arg-type]
            except StackedDialogError:
                continue  # refused: the safe outcome
            assert not is_all_photos_option(chosen), (options, chosen)
            assert is_current_only_option(chosen) or is_confirm_option(chosen), (
                options, chosen
            )

        # Every dangerous label is recognised as dangerous...
        for label in dangerous:
            assert is_all_photos_option(label), label
            assert not is_current_only_option(label), label
            # ...and cannot pass for an ordinary confirm either.
            assert not is_confirm_option(label), label
        # ...and the guard refuses to click any of them, whatever asks it to.
        for label in dangerous:
            try:
                assert_safe_to_click(label)
                raise AssertionError(f"assert_safe_to_click allowed {label!r}")
            except GuardrailViolation:
                pass
        # Both legitimate choices pass the same guard.
        assert_safe_to_click("Current photo only")
        assert_safe_to_click("Move to trash")

    def t_unexpected_wording_fails_safe() -> None:
        """New wording must click NOTHING, not fall back to another button."""
        for options in (
            ["All 8 photos", "This photo only"],
            ["All 8 photos", "Only this photo"],
            ["All 8 photos", "Just this one"],
            ["All 8 photos"],
            ["All 8 photos", "Cancel"],
            ["Cancel", "Discard"],
            ["Cancel"],
            [],
        ):
            try:
                kind, chosen = decide_dialog_action(options)
                raise AssertionError(
                    f"chose {kind}/{chosen!r} from {options!r}; expected a refusal"
                )
            except StackedDialogError as exc:
                assert "NOTHING was clicked" in str(exc) or "Refusing" in str(exc)
        # Two matching safe options is also a refusal: do not guess which is real.
        try:
            decide_dialog_action(["Current photo only", "Current photo only"])
            raise AssertionError("expected a refusal on an ambiguous dialog")
        except StackedDialogError:
            pass

    def t_confirm_candidates_must_be_dialog_scoped() -> None:
        """*** A toolbar 'Move to trash' must never be clicked as the confirm. ***

        The real page has one in the viewer toolbar and one in the dialog;
        clicking the toolbar one re-opens the dialog instead of confirming it.
        This drives PlaywrightDriver against a stub page to prove the labels it
        reads, and the element it clicks, come only from inside the dialog.
        """
        clicked: List[str] = []

        class StubLocator:
            def __init__(self, labels, scoped=False, name=""):
                self._labels = labels
                self._scoped = scoped
                self._name = name

            # -- scoping ---------------------------------------------------- #
            def locator(self, selector):
                if selector == "button":
                    return StubLocator(self._labels, self._scoped)
                return StubLocator([], self._scoped)

            def get_by_role(self, role, name=None):
                hits = [lab for lab in self._labels if name.match(lab)]
                return StubLocator(hits, self._scoped, name="role")

            def get_by_text(self, pattern):
                hits = [lab for lab in self._labels if pattern.match(lab)]
                return StubLocator(hits, self._scoped, name="text")

            # -- leaf behaviour --------------------------------------------- #
            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            def nth(self, index):
                return StubLocator([self._labels[index]], self._scoped)

            def count(self):
                return len(self._labels)

            def is_visible(self, timeout=None):
                return True

            def inner_text(self, timeout=None):
                return self._labels[0] if self._labels else ""

            def get_attribute(self, _name):
                return None

            def click(self, timeout=None):
                assert self._scoped, (
                    "clicked an element that was not scoped to the dialog"
                )
                clicked.append(self._labels[0])

        class StubPage:
            url = PHOTO_PREFIX + "AF1QipFAKE00000"

            def locator(self, selector):
                if selector in DIALOG_SCOPE_SELECTORS:
                    # Inside the dialog: only its own two options.
                    return StubLocator(list(REAL_ORDINARY_OPTIONS), scoped=True)
                # Page-wide: the whole dump, including the toolbar button.
                return StubLocator(list(REAL_PAGE_DUMP), scoped=False)

            def get_by_role(self, role, name=None):
                raise AssertionError(
                    "page-wide get_by_role must not be used to find the confirm"
                )

            def get_by_text(self, pattern):
                raise AssertionError(
                    "page-wide get_by_text must not be used to find the confirm"
                )

            def inner_text(self, _selector, timeout=None):
                return REAL_HEADING + "\n" + "\n".join(REAL_PAGE_DUMP)

            def keyboard_press(self, _key):
                pass

            def wait_for_timeout(self, _ms):
                pass

        driver = PlaywrightDriver.__new__(PlaywrightDriver)
        driver._page = StubPage()
        driver._nav_timeout_ms = 30000
        driver._verbose = False

        scope = driver._dialog_scope()
        assert scope is not None, "the dialog element was not found"

        # The labels read must be the DIALOG's, not the page's.
        labels = driver._dialog_option_labels(scope)
        assert labels == REAL_ORDINARY_OPTIONS, labels
        assert labels.count("Move to trash") == 1, labels
        assert "Main menu" not in labels and "Google apps" not in labels

        kind, choice = driver._answer_dialog(scope)
        assert (kind, choice) == (DIALOG_ORDINARY, "Move to trash"), (kind, choice)
        # Exactly one click, and it came from the dialog-scoped locator.
        assert clicked == ["Move to trash"], clicked

    # ----------------------------------------------------------------------- #
    # BUG A: a DOM fixture, because the defect was in the DOM, not the wording
    # ----------------------------------------------------------------------- #
    #
    # The v1.2.1 failures could not be caught by any test that passes option
    # STRINGS around: the strings were right and the decision logic was right.
    # What was wrong is that the stacked dialog's options are not buttons and
    # its container carries no ARIA marker, so nothing ever reached the
    # decision. These checks therefore drive PlaywrightDriver against a stub DOM
    # with the real shape: plain text rows, no role=dialog, and a viewer toolbar
    # outside the dialog holding a real "Move to trash" BUTTON.

    class DomNode:
        """A DOM node with just enough behaviour to be located and clicked."""

        def __init__(self, tag, text="", role=None, clickable=False,
                     attrs=None, children=(), inert=False):
            self.tag = tag
            self.own_text = text
            self.role = role
            self.clickable = clickable or tag in ("button", "a", "input")
            self.attrs = dict(attrs or {})
            self.children = list(children)
            # inert: refuses clicks the way a plain text row does.
            self.inert = inert
            self.parent = None
            for child in self.children:
                child.parent = self

        def descendants(self):
            for child in self.children:
                yield child
                for grandchild in child.descendants():
                    yield grandchild

        def ancestors_or_self(self):
            """DOCUMENT ORDER: outermost (<html>) first, self last."""
            chain = []
            node = self
            while node is not None:
                chain.append(node)
                node = node.parent
            chain.reverse()
            return chain

        def text(self):
            rows = [self.own_text] if self.own_text else []
            for child in self.children:
                child_text = child.text()
                if child_text:
                    rows.append(child_text)
            return "\n".join(rows)

        def attribute(self, name):
            if name == "role":
                return self.role
            return self.attrs.get(name)

        def accessible_name(self):
            return self.attrs.get("aria-label") or self.text()

        def effective_role(self):
            if self.role:
                return self.role
            return {"button": "button", "a": "link"}.get(self.tag)

        def matches(self, selector):
            selector = selector.strip()
            if selector == "dialog[open]":
                return self.tag == "dialog" and "open" in self.attrs
            if selector.startswith("[") and selector.endswith("]"):
                body = selector[1:-1]
                # "[aria-label*="Move to trash" i]" - substring, case-insensitive.
                insensitive = body.endswith(" i")
                if insensitive:
                    body = body[: -len(" i")].strip()
                if "*=" in body:
                    name, _, value = body.partition("*=")
                    actual = self.attribute(name.strip()) or ""
                    wanted = value.strip().strip('"')
                    if insensitive:
                        return wanted.lower() in actual.lower()
                    return wanted in actual
                if "=" in body:
                    name, _, value = body.partition("=")
                    actual = self.attribute(name.strip())
                    wanted = value.strip().strip('"')
                    if insensitive:
                        return (actual or "").lower() == wanted.lower()
                    return actual == wanted
                return self.attribute(body) is not None
            return self.tag == selector

        def closest(self, selectors):
            """Nearest ancestor-or-self matching any of these selectors."""
            for node in reversed(self.ancestors_or_self()):
                if any(node.matches(one) for one in selectors):
                    return node
            return None

        def outer_html(self):
            inner = "".join(child.outer_html() for child in self.children)
            attrs = "".join(f' {k}="{v}"' for k, v in self.attrs.items())
            if self.role:
                attrs += f' role="{self.role}"'
            return f"<{self.tag}{attrs}>{self.own_text}{inner}</{self.tag}>"

    class DomLocator:
        def __init__(self, nodes, clicks):
            self.nodes = list(nodes)
            self.clicks = clicks

        # -- scoping ---------------------------------------------------- #
        def locator(self, selector):
            if selector.startswith("xpath="):
                return self._xpath(selector[len("xpath="):])
            hits = []
            for node in self.nodes:
                hits.extend(d for d in node.descendants() if d.matches(selector))
            return DomLocator(hits, self.clicks)

        def _xpath(self, expression):
            if expression == "ancestor-or-self::*":
                hits = []
                for node in self.nodes:
                    hits.extend(node.ancestors_or_self())
                return DomLocator(hits, self.clicks)
            if expression == CLICKABLE_ANCESTOR_XPATH[len("xpath="):]:
                # [last()] in document order == the INNERMOST clickable match.
                hits = []
                for node in self.nodes:
                    clickable = [n for n in node.ancestors_or_self() if n.clickable]
                    if clickable:
                        hits.append(clickable[-1])
                return DomLocator(hits, self.clicks)
            raise AssertionError(f"unexpected xpath: {expression}")

        def get_by_role(self, role, name=None):
            hits = []
            for node in self.nodes:
                for candidate in list(node.descendants()) + [node]:
                    if candidate.effective_role() != role:
                        continue
                    if name is not None and not name.match(
                        candidate.accessible_name() or ""
                    ):
                        continue
                    hits.append(candidate)
            return DomLocator(hits, self.clicks)

        def get_by_text(self, pattern):
            """Playwright's text engine matches the SMALLEST element that holds
            the text, so an ancestor whose text merely starts with it is not a
            hit. Emulated, because the difference decides what gets clicked."""
            matched = []
            for node in self.nodes:
                for candidate in list(node.descendants()) + [node]:
                    if pattern.match(candidate.text() or ""):
                        matched.append(candidate)
            hits = [
                candidate for candidate in matched
                if not any(
                    other is not candidate and other in matched
                    for other in candidate.descendants()
                )
            ]
            return DomLocator(hits, self.clicks)

        # -- leaf behaviour --------------------------------------------- #
        @property
        def first(self):
            return DomLocator(self.nodes[:1], self.clicks)

        @property
        def last(self):
            return DomLocator(self.nodes[-1:], self.clicks)

        def nth(self, index):
            return DomLocator(self.nodes[index:index + 1], self.clicks)

        def count(self):
            return len(self.nodes)

        def is_visible(self, timeout=None):
            return bool(self.nodes)

        def inner_text(self, timeout=None):
            return self.nodes[0].text() if self.nodes else ""

        def get_attribute(self, name):
            return self.nodes[0].attribute(name) if self.nodes else None

        def evaluate(self, script):
            node = self.nodes[0]
            if "tagName" in script and "outerHTML" not in script:
                return node.tag.upper()
            if "closest" in script:
                # INSIDE_DIALOG_JS. The selector list is embedded in the script
                # as a JSON string, so parse it back out rather than restating
                # it here - the test must exercise the real selector list.
                quoted = script[script.index("(") + 1: script.rindex(")")]
                selectors = [
                    one.strip() for one in json.loads(quoted).split(",")
                    if one.strip()
                ]
                return node.closest(selectors) is not None
            if "querySelectorAll" in script:
                return {
                    "html": node.outer_html(),
                    "nodes": [
                        {
                            "tag": d.tag,
                            "role": d.role,
                            "clickable": d.clickable,
                            "text": (d.text() or "").split("\n")[0],
                        }
                        for d in node.descendants()
                    ],
                }
            if "outerHTML" in script:
                return node.outer_html()
            raise AssertionError(f"unexpected script: {script[:40]}")

        def click(self, timeout=None):
            node = self.nodes[0]
            if node.inert or not node.clickable:
                raise RuntimeError("element is not clickable")
            self.clicks.append(node)

    class DomPage:
        url = PHOTO_PREFIX + "AF1QipFAKE00000"

        def __init__(self, root):
            self.root = root
            self.clicks: List[Any] = []

        def _root_locator(self):
            return DomLocator([self.root], self.clicks)

        def locator(self, selector):
            return self._root_locator().locator(selector)

        def get_by_text(self, pattern):
            return self._root_locator().get_by_text(pattern)

        def get_by_role(self, role, name=None):
            raise AssertionError(
                "page-wide get_by_role must not be used to find an option"
            )

        def inner_text(self, _selector, timeout=None):
            return self.root.text()

        def wait_for_timeout(self, _ms):
            pass

    def text_row(label, inert=False):
        """An option as the real stacked dialog renders it: a div with a click
        handler, NO role and NO button tag, so every role query misses it."""
        return DomNode(
            "div", text=label, attrs={"jsaction": "click:trash"},
            clickable=not inert, inert=inert,
        )

    def inert_text_row(label):
        """A row with nothing clickable about it at all."""
        return DomNode("div", text=label, inert=True)

    def toolbar(trash_label="Move to trash"):
        """The viewer toolbar, OUTSIDE the dialog.

        Its "Move to trash" is a real button carrying a real aria-label, exactly
        as the v1.2.0 page dump showed. In the dialog-answering path it is the
        thing that must NEVER be clicked (clicking it re-opens the dialog); in
        the FIX 1 fallback path it is the thing that SHOULD be clicked, because
        by then no dialog is open at all.
        """
        children = [
            DomNode("button", text="More options",
                    attrs={"aria-label": "More options"}),
            DomNode("button", text="Share", attrs={"aria-label": "Share"}),
        ]
        if trash_label is not None:
            children.insert(
                1,
                DomNode("button", text=trash_label,
                        attrs={"aria-label": trash_label}),
            )
        return DomNode("div", role="toolbar", children=children)

    def stacked_page(current_row=None, extra_rows=(), container_attrs=None):
        """A page whose stacked dialog carries NO aria marker at all."""
        rows = [text_row("All 8 photos")]
        rows.append(current_row if current_row is not None
                    else text_row("Current photo only"))
        rows.extend(extra_rows)
        dialog = DomNode(
            "div",
            attrs=dict(container_attrs or {}),
            children=[DomNode("h1", text=REAL_HEADING)] + rows,
        )
        body = DomNode("body", children=[
            toolbar(),
            DomNode("div", children=[dialog]),  # the overlay wrapper
        ])
        return DomPage(DomNode("html", children=[body])), dialog

    def dom_driver(page):
        driver = PlaywrightDriver.__new__(PlaywrightDriver)
        driver._page = page
        driver._nav_timeout_ms = 30000
        driver._post_action_settle_ms = 0
        driver._verbose = False
        driver.empty_scopes = []
        return driver

    def viewer_page(dialog=None, trash_label="Move to trash"):
        """A viewer with a toolbar and, optionally, an open dialog."""
        children = [toolbar(trash_label=trash_label)]
        if dialog is not None:
            children.append(DomNode("div", children=[dialog]))
        wiz = DomNode("c-wiz", children=children)
        return DomPage(DomNode("html", children=[DomNode("body", children=[wiz])]))

    def ordinary_dialog():
        return DomNode("div", role="dialog", children=[
            DomNode("h1", text=REAL_HEADING),
            DomNode("button", text="Cancel", attrs={"aria-label": "Cancel"}),
            DomNode("button", text="Move to trash",
                    attrs={"aria-label": "Move to trash"}),
        ])

    def shortcut_driver(page, opens=None):
        """A driver whose '#' keystroke installs `opens` into the page, or not.

        `opens=None` models FIX 1's root cause: the keystroke is accepted and
        silently does nothing, so no dialog ever appears.
        """
        driver = dom_driver(page)
        pressed: List[str] = []

        class Keyboard:
            @staticmethod
            def press(key):
                pressed.append(key)
                if opens is not None:
                    wiz = page.root.children[0].children[0]
                    overlay = DomNode("div", children=[opens])
                    overlay.parent = wiz
                    wiz.children.append(overlay)

        page.keyboard = Keyboard()
        page.bring_to_front = lambda: None
        driver.pressed = pressed
        return driver

    # ----------------------------------------------------------------------- #
    # FIX 1: the '#' shortcut silently does nothing for some items
    # ----------------------------------------------------------------------- #

    def t_shortcut_opening_no_dialog_falls_back_to_the_toolbar() -> None:
        """*** FIX 1, the likely root cause of the remaining failures. ***

        The real run failed three items with `mutation-no-op`, no dialog
        detected, no "All N photos" seen and no dump - and the same item's
        dialog appeared normally when opened BY HAND. So the keystroke does
        nothing for those items. The toolbar control must stand in for it.
        """
        page = viewer_page()
        driver = shortcut_driver(page, opens=None)

        # The toolbar control is found, and it is the toolbar's - not a
        # dialog's, because no dialog is open at all.
        control, label = driver._toolbar_trash_control()
        assert control is not None, "the toolbar trash control was not found"
        assert label == "Move to trash", label
        assert not driver._is_inside_dialog(control), "that control is in a dialog"

        # Pressing it opens the ordinary confirm, which is answered by the
        # existing decision path.
        opened = ordinary_dialog()

        real_click = DomLocator.click
        clicked_control: List[Any] = []

        def click_opens_dialog(self, timeout=None):
            node = self.nodes[0]
            real_click(self, timeout=timeout)
            if node.attribute("aria-label") == "Move to trash" and not clicked_control:
                clicked_control.append(node)
                wiz = page.root.children[0].children[0]
                overlay = DomNode("div", children=[opened])
                overlay.parent = wiz
                wiz.children.append(overlay)

        try:
            DomLocator.click = click_opens_dialog  # type: ignore[assignment]
            outcome = driver.trash_current()
        finally:
            DomLocator.click = real_click  # type: ignore[assignment]

        assert driver.pressed == ["#"], driver.pressed
        assert outcome.shortcut_opened_dialog is False, outcome
        assert outcome.route == ROUTE_TOOLBAR, outcome.route
        # The toolbar button was clicked, and then the DIALOG's own confirm.
        assert len(page.clicks) == 2, [n.text() for n in page.clicks]
        assert page.clicks[0] is clicked_control[0]
        assert page.clicks[1].text().strip() == "Move to trash"
        # The second click was inside the dialog, the first outside it.
        assert opened in page.clicks[1].ancestors_or_self()
        assert opened not in page.clicks[0].ancestors_or_self()

    def t_a_slow_dialog_is_waited_for_not_overtaken_by_the_fallback() -> None:
        """The dialog is ASYNCHRONOUS: one look is not enough.

        The shortcut works here, but the dialog takes a few hundred ms to
        render. A single _dialog_scope() check would conclude "the shortcut did
        nothing", press the toolbar, and open a SECOND dialog - so the wait is
        what keeps the fallback from firing on a dialog that was merely slow.
        """
        page = viewer_page()
        appearing = ordinary_dialog()
        polls = {"count": 0}

        def wait_for_timeout(_ms):
            polls["count"] += 1
            if polls["count"] == 2:
                wiz = page.root.children[0].children[0]
                overlay = DomNode("div", children=[appearing])
                overlay.parent = wiz
                wiz.children.append(overlay)

        page.wait_for_timeout = wait_for_timeout
        driver = shortcut_driver(page, opens=None)

        outcome = driver.trash_current()
        # It waited, found the dialog, and never touched the toolbar.
        assert polls["count"] >= 2, polls
        assert outcome.route == ROUTE_SHORTCUT, outcome.route
        assert outcome.shortcut_opened_dialog is True, outcome
        assert len(page.clicks) == 1, [n.text() for n in page.clicks]
        # The one click was the dialog's own confirm.
        assert appearing in page.clicks[0].ancestors_or_self()

    def t_toolbar_fallback_never_clicks_inside_a_dialog() -> None:
        """*** The safety property of FIX 1. ***

        A dialog's own confirm button must never be reachable as a "toolbar"
        control: that would bypass decide_dialog_action(), which is the only
        thing standing between a stacked item and "All N photos".
        """
        # A page whose ONLY trash-labelled control is inside a dialog.
        dialog = DomNode("div", role="dialog", children=[
            DomNode("h1", text=REAL_HEADING),
            DomNode("button", text="Move to trash",
                    attrs={"aria-label": "Move to trash"}),
        ])
        page = viewer_page(dialog=dialog, trash_label=None)
        driver = dom_driver(page)
        control, label = driver._toolbar_trash_control()
        assert control is None, (
            f"a dialog button was offered as a toolbar control ({label!r})"
        )

        # And on a STACKED dialog, where the only other option deletes
        # everything, there is still nothing for the fallback to press.
        stacked = DomNode("div", role="dialog", children=[
            DomNode("h1", text=REAL_HEADING),
            text_row("All 8 photos"),
            text_row("Current photo only"),
        ])
        stacked_page_only = viewer_page(dialog=stacked, trash_label=None)
        stacked_driver = dom_driver(stacked_page_only)
        control, _ = stacked_driver._toolbar_trash_control()
        assert control is None, "a stacked dialog's row was offered as a toolbar control"
        assert stacked_page_only.clicks == [], stacked_page_only.clicks

    def t_toolbar_label_matching_is_anchored_and_safe() -> None:
        """What may and may not pass for a toolbar trash control."""
        for label in ("Move to trash", "move to bin", "  Move to Trash  ",
                      "Trash", "Delete"):
            assert is_toolbar_trash_label(label), label
            assert_safe_to_click(label)
        for label in (
            "All 8 photos", "All 2 videos",          # the whole stack
            "Delete permanently", "Delete from trash",  # destroys an item
            "Delete from bin", "Restore", "Empty trash",
            "Move to trash and 7 more", "Move to trash?",  # unanchored
            "Undo move to trash", "Don't move to trash",
            "Share", "Edit", "More options", "", "   ", None,
        ):
            assert not is_toolbar_trash_label(label), label
        # A toolbar whose wording has changed simply yields no control, and the
        # item then fails safe with a dump rather than something being guessed.
        page = viewer_page(trash_label="Bin it now")
        driver = dom_driver(page)
        control, _ = driver._toolbar_trash_control()
        assert control is None, "an unrecognised toolbar label was accepted"

    def t_shortcut_that_works_does_not_use_the_toolbar() -> None:
        """When the keystroke does open a dialog, nothing else is pressed."""
        page = viewer_page()
        driver = shortcut_driver(page, opens=ordinary_dialog())
        outcome = driver.trash_current()
        assert outcome.shortcut_opened_dialog is True, outcome
        assert outcome.route == ROUTE_SHORTCUT, outcome.route
        # Exactly one click: the dialog's own confirm.
        assert len(page.clicks) == 1, [n.text() for n in page.clicks]
        assert page.clicks[0].text().strip() == "Move to trash"
        assert page.clicks[0].attribute("aria-label") == "Move to trash"
        # ...and it was the dialog's, not the toolbar's.
        assert driver._is_inside_dialog(DomLocator([page.clicks[0]], page.clicks))

    def t_toolbar_fallback_on_a_stacked_item_still_refuses_all_photos() -> None:
        """FIX 1 must not weaken the one invariant that matters.

        The shortcut does nothing, the toolbar opens the dialog, and the dialog
        turns out to be a STACK whose safe option is missing. Nothing may be
        clicked inside it.
        """
        page = viewer_page()
        driver = shortcut_driver(page, opens=None)
        unanswerable = DomNode("div", role="dialog", children=[
            DomNode("h1", text=REAL_HEADING),
            text_row("All 8 photos"),
            text_row("This photo only"),   # not the wording we accept
        ])
        real_click = DomLocator.click

        def click_opens_dialog(self, timeout=None):
            node = self.nodes[0]
            real_click(self, timeout=timeout)
            if node.attribute("aria-label") == "Move to trash":
                wiz = page.root.children[0].children[0]
                overlay = DomNode("div", children=[unanswerable])
                overlay.parent = wiz
                wiz.children.append(overlay)

        try:
            DomLocator.click = click_opens_dialog  # type: ignore[assignment]
            try:
                driver.trash_current()
                raise AssertionError("answered a dialog with no safe option")
            except StackedDialogError as exc:
                assert "NOTHING was clicked" in str(exc) or "Refusing" in str(exc)
        finally:
            DomLocator.click = real_click  # type: ignore[assignment]

        # Only the toolbar button was ever clicked; nothing inside the dialog.
        assert len(page.clicks) == 1, [n.text() for n in page.clicks]
        assert unanswerable not in page.clicks[0].ancestors_or_self()
        for node in page.clicks:
            assert not is_all_photos_option(node.text().strip()), node.text()

    def t_route_counts_are_reported() -> None:
        """FIX 1 asks how often the shortcut fails; the summary must say."""
        items = load_items(csv_path).items[:4]
        path = os.path.join(tmpdir, "routes.jsonl")
        journal = Journal(path)
        journal.load()
        behaviour = {items[0].url: "ok", items[1].url: "shortcut_dead",
                     items[2].url: "shortcut_dead", items[3].url: "ok"}
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        lines: List[str] = []
        cfg = Config(execute=True, limit=4, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=lines.append,
                      sleeper=no_sleep)
        assert summary.deleted == 4, summary
        assert summary.deleted_via_shortcut == 2, summary.deleted_by_route
        assert summary.deleted_via_toolbar == 2, summary.deleted_by_route
        assert summary.shortcut_opened_no_dialog == 2, summary
        assert summary.toolbar_fallbacks == 2, summary
        assert driver.shortcut_failures == 2, driver.shortcut_failures
        assert driver.routes.count(ROUTE_TOOLBAR) == 2, driver.routes
        joined = "\n".join(lines)
        assert "shortcut opened no dialog" in joined, joined
        # The route is in the journal too, so a later run can see it.
        with open(path, "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        details = {r["url"]: r["detail"] for r in records}
        assert f"route: {ROUTE_TOOLBAR}" in details[items[1].url], details
        assert f"route: {ROUTE_SHORTCUT}" in details[items[0].url], details
        # Nothing stacked happened here, so those counters stay clean.
        assert summary.stacked_dialogs == 0, summary
        assert driver.clicked == ["Move to trash"] * 2, driver.clicked

    # ----------------------------------------------------------------------- #
    # FIX 2: diagnostics on ANY no-toast failure, dialog or no dialog
    # ----------------------------------------------------------------------- #

    def t_diagnostics_fire_with_no_dialog_present() -> None:
        """*** FIX 2: the dump must not be gated on detecting a dialog. ***

        The failures we cannot explain are exactly the ones where no dialog is
        detected, and up to v1.2.2 those produced no diagnostics at all. Three
        rounds of guessing at markup came out of that.
        """
        page = viewer_page()
        driver = dom_driver(page)
        # No dialog anywhere on this page.
        assert driver._dialog_scope() is None, "a dialog was found where none exists"

        dump = driver._failure_diagnostics(None)
        assert "dialog detected: False" in dump, dump
        assert "chosen scope: none" in dump, dump
        assert "option labels by role: []" in dump, dump
        assert "option labels as text rows:" in dump, dump
        # It says whether the fallback was available - the key new fact.
        assert "toolbar trash control found: True" in dump, dump
        assert "'Move to trash'" in dump, dump
        # And it dumps the VIEWER container, since there is no dialog.
        assert "no dialog, so dumping the viewer container c-wiz" in dump, dump
        assert "<c-wiz" in dump, dump
        assert "aria-label=\"Move to trash\"" in dump, dump
        assert "stacked heading present: False" in dump, dump

    def t_trash_current_dumps_when_no_dialog_was_ever_detected() -> None:
        """*** FIX 2 through the real driver, on the production failure. ***

        The shortcut does nothing, there is no toolbar control either, and the
        item survives. v1.2.2 produced NO dump in exactly this case, because the
        dump was gated on having detected a dialog. The outcome must now carry
        the full report, and be classified as a code gap rather than a quota.
        """
        page = viewer_page(trash_label=None)   # no toolbar trash control at all
        driver = shortcut_driver(page, opens=None)

        outcome = driver.trash_current()
        assert driver.pressed == ["#"], driver.pressed
        # Nothing was clicked anywhere.
        assert page.clicks == [], page.clicks
        assert outcome.toast_seen is False and outcome.left_viewer is False
        assert outcome.shortcut_opened_dialog is False, outcome
        assert outcome.dialog_answered is False, outcome
        assert outcome.route == ROUTE_SHORTCUT, outcome.route
        # *** The dump is there even though no dialog was ever detected. ***
        assert outcome.diagnostics, "no diagnostics for a no-dialog failure"
        assert "the '#' shortcut opened no dialog" in outcome.diagnostics
        assert "dialog detected: False" in outcome.diagnostics
        assert "chosen scope: none" in outcome.diagnostics
        assert "toolbar trash control found: False" in outcome.diagnostics
        assert "dumping the viewer container" in outcome.diagnostics
        assert "<c-wiz" in outcome.diagnostics
        # And it is a code gap, not the delete limiter.
        assert classify_noop_failure(
            all_photos_seen=outcome.all_photos_seen,
            lingering_dialog=outcome.lingering_dialog,
            lingering_heading=outcome.lingering_heading,
            dialog_answered=outcome.dialog_answered,
        ) == FAIL_NO_DIALOG

    def t_diagnostics_truncate_and_still_report_without_a_toolbar() -> None:
        """The dump stays bounded, and reports a missing fallback honestly."""
        huge = DomNode("c-wiz", children=[
            DomNode("div", text="x" * 50, attrs={"data-n": str(n)})
            for n in range(200)
        ])
        page = DomPage(DomNode("html", children=[
            DomNode("body", children=[huge]),
        ]))
        driver = dom_driver(page)
        dump = driver._failure_diagnostics(None)
        assert "toolbar trash control found: False" in dump, dump[:300]
        assert ", truncated)" in dump, dump[:300]
        body = dump[dump.index("no dialog, so dumping"):]
        # The markup itself is capped, whatever the page size.
        assert len(body) < DIAGNOSTIC_HTML_CHARS + 500, len(body)

    def t_diagnostics_reported_for_every_no_toast_failure() -> None:
        """Through run(): a no-dialog failure is logged WITH its dump."""
        items = load_items(csv_path).items[:2]
        path = os.path.join(tmpdir, "nodialog.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items],
            behaviour={items[0].url: "shortcut_dead_no_toolbar",
                       items[1].url: "ok"},
        )
        lines: List[str] = []
        cfg = Config(execute=True, limit=2, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=lines.append,
                      sleeper=no_sleep)
        assert summary.failed == 1, summary
        assert summary.deleted == 1, summary
        joined = "\n".join(lines)
        # The failure is named for what it is, not filed as a delete quota...
        assert "no dialog, no toolbar control" in joined, joined
        assert summary.failed_no_dialog == 1, summary.failed_by_kind
        assert summary.failed_mutation == 0, summary.failed_by_kind
        # ...and it is a code gap, so it can never buy a cooldown.
        assert FAIL_NO_DIALOG not in THROTTLE_KINDS
        assert FAIL_NO_DIALOG in CODE_GAP_KINDS
        # ...and the dump came out even though no dialog was ever detected.
        assert "dialog detected: False" in joined, joined
        assert "toolbar trash control found: False" in joined, joined
        assert "chosen scope: none" in joined, joined
        # A plain mutation no-op gets a dump too.
        lines.clear()
        path2 = os.path.join(tmpdir, "noop_dump.jsonl")
        journal2 = Journal(path2)
        journal2.load()
        driver2 = FakeDriver(
            [i.url for i in items],
            behaviour={items[0].url: "noop", items[1].url: "ok"},
        )
        cfg2 = Config(execute=True, limit=2, delay=0, journal_path=path2)
        run(items, driver2, journal2, cfg2, log=lines.append, sleeper=no_sleep)
        assert "failure diagnostics" in "\n".join(lines), lines

    def t_stacked_options_are_text_rows_not_buttons() -> None:
        """*** THE REGRESSION THAT MATTERS (BUG A). ***

        The failing dialog's options are plain text rows. Role queries find
        NOTHING in it - which is exactly why v1.2.1 clicked nothing while
        "Current photo only" sat on screen - so the option must be read from
        visible text, and clicked from there.
        """
        page, dialog = stacked_page()
        driver = dom_driver(page)

        # 1. The premise: inside this dialog there are no button-ish roles at
        #    all, so the v1.2.1 locator could not have matched anything.
        scope = DomLocator([dialog], page.clicks)
        for role in ("button", "menuitem", "option", "radio", "link"):
            assert scope.get_by_role(
                role, name=STACKED_CURRENT_ONLY_RE
            ).count() == 0, role
        for selector in DIALOG_OPTION_ROLE_SELECTORS:
            assert scope.locator(selector).count() == 0, selector
        # ...and the page's only real buttons are the toolbar's.
        assert page.locator("button").count() == 3

        # 2. The container is still found, despite carrying no ARIA marker.
        found = driver._dialog_scope()
        assert found is not None, "no dialog container was found"
        assert found.nodes == [dialog], "the container found was not the dialog"

        # 3. The options are read anyway, from visible text.
        labels = driver._dialog_option_labels(found)
        assert "Current photo only" in labels, labels
        assert "All 8 photos" in labels, labels
        # The toolbar's button is outside the dialog and must not appear.
        assert "Move to trash" not in labels, labels
        assert "Share" not in labels, labels

        # 4. End to end: it is decided, clicked, and it is the safe one.
        kind, choice = driver._answer_dialog(found)
        assert (kind, choice) == (DIALOG_STACKED, "Current photo only")
        assert len(page.clicks) == 1, page.clicks
        clicked_text = page.clicks[0].text()
        assert is_current_only_option(clicked_text.strip()), clicked_text
        assert not is_all_photos_option(clicked_text.strip()), clicked_text

    def t_clickable_ancestor_walk_lands_the_click() -> None:
        """An inert text row is clicked via its nearest clickable ancestor."""
        row = DomNode("span", text="Current photo only", inert=True)
        # A wrapper that takes the click, with an extra non-option row in it so
        # the wrapper itself does not match the exact-text locator.
        wrapper = DomNode(
            "div",
            clickable=True,
            attrs={"jsaction": "click:maybe"},
            children=[DomNode("span", text="Selected"), row],
        )
        page, dialog = stacked_page(current_row=wrapper)
        driver = dom_driver(page)

        scope = driver._dialog_scope()
        assert scope is not None
        # The row itself is what matches, and it refuses the click...
        direct = scope.get_by_text(
            re.compile(r"^\s*Current photo only\s*$", re.I)
        )
        assert direct.count() == 1 and direct.nodes == [row], direct.nodes
        # ...so the walk goes up exactly one clickable step.
        ancestor = driver._clickable_ancestor(direct.first)
        assert ancestor is not None and ancestor.nodes == [wrapper]
        assert driver._tag_name_of(ancestor) not in NEVER_A_DIALOG_SCOPE

        kind, choice = driver._answer_dialog(scope)
        assert (kind, choice) == (DIALOG_STACKED, "Current photo only")
        assert page.clicks == [wrapper], page.clicks
        # And the wrapper really is inside the dialog, not the page.
        assert dialog in wrapper.ancestors_or_self()

    def t_click_target_covering_two_options_is_refused() -> None:
        """A walk that lands on a node wrapping BOTH options must not click.

        That node's click is a guess between "Current photo only" and the
        option that deletes the whole stack.
        """
        both = DomNode("div", clickable=True, children=[
            DomNode("span", text="All 8 photos", inert=True),
            DomNode("span", text="Current photo only", inert=True),
        ])
        page = DomPage(DomNode("html", children=[DomNode("body", children=[
            DomNode("div", children=[DomNode("h1", text=REAL_HEADING), both]),
        ])]))
        driver = dom_driver(page)
        scope = driver._dialog_scope()
        assert scope is not None
        try:
            driver._answer_dialog(scope)
            raise AssertionError("clicked a node that covers both options")
        except StackedDialogError as exc:
            assert "NOTHING was clicked" in str(exc) or "covers the option" in str(exc)
        assert page.clicks == [], page.clicks

    def t_heading_walk_takes_the_innermost_ancestor_not_body() -> None:
        """*** FINDING #4: the heading walk must never widen back to <body>. ***

        Every ancestor of the heading contains the heading, so "the first
        ancestor containing the heading" is <body> - and <body> holds the viewer
        toolbar's own "Move to trash" button. A scope that wide is how a stacked
        item gets confirmed blind.
        """
        page, dialog = stacked_page()
        driver = dom_driver(page)

        heading = page.get_by_text(HEADING_LOCATOR_RE).last
        chain = heading.locator("xpath=ancestor-or-self::*")
        candidates = [chain.nth(i) for i in range(chain.count())]
        tags = [driver._tag_name_of(c) for c in candidates]
        assert tags[0] == "html" and tags[1] == "body", tags
        assert tags[-1] == "h1", tags

        # The BUG, written out: scan forward and stop at the first ancestor that
        # holds an option - <body> does, via the toolbar button.
        def first_match_in_document_order():
            for index, candidate in enumerate(candidates):
                if driver._holds_options(candidate):
                    return index
            return -1

        wrong = first_match_in_document_order()
        # It lands on the whole page - <html> here, <body> when the chain starts
        # inside it - and both are exactly what NEVER_A_DIALOG_SCOPE lists.
        assert wrong != -1, (wrong, tags)
        assert tags[wrong] in NEVER_A_DIALOG_SCOPE, (wrong, tags)
        assert "body" in tags[: wrong + 2], tags
        for scope_tag in ("html", "body"):
            wrong_index = tags.index(scope_tag)
            assert driver._holds_options(candidates[wrong_index]), scope_tag
            wrong_labels = driver._dialog_option_labels(candidates[wrong_index])
            # ...and that scope puts the TOOLBAR's confirm button back in reach.
            assert "Move to trash" in wrong_labels, (scope_tag, wrong_labels)

        # The real implementation: innermost first, <body>/<html> rejected.
        index = pick_dialog_container(
            candidates, driver._tag_name_of, driver._holds_options
        )
        assert index != -1 and tags[index] == "div", (index, tags)
        assert candidates[index].nodes == [dialog]
        labels = driver._dialog_option_labels(candidates[index])
        assert "Current photo only" in labels and "Move to trash" not in labels

        # Same conclusion through the driver's own entry point.
        scope = driver._dialog_scope_from_heading()
        assert scope is not None and scope.nodes == [dialog]
        assert driver._tag_name_of(scope) not in NEVER_A_DIALOG_SCOPE

        # And when the ONLY ancestor holding an option is <body>, the answer is
        # "no container", not "<body>".
        body_only = DomNode("body", children=[
            toolbar(), DomNode("h1", text=REAL_HEADING),
        ])
        body_page = DomPage(DomNode("html", children=[body_only]))
        body_driver = dom_driver(body_page)
        body_heading = body_page.get_by_text(HEADING_LOCATOR_RE).last
        body_chain = body_heading.locator("xpath=ancestor-or-self::*")
        body_candidates = [
            body_chain.nth(i) for i in range(body_chain.count())
        ]
        assert pick_dialog_container(
            body_candidates, body_driver._tag_name_of, body_driver._holds_options
        ) == -1
        assert body_driver._dialog_scope_from_heading() is None
        assert body_driver.empty_scopes, "an option-less walk was not recorded"

    def t_all_photos_is_unreachable_in_the_dom() -> None:
        """A stacked dialog offering ONLY the dangerous option clicks nothing."""
        page = DomPage(DomNode("html", children=[DomNode("body", children=[
            toolbar(),
            DomNode("div", children=[
                DomNode("h1", text=REAL_HEADING),
                text_row("All 8 photos"),
            ]),
        ])]))
        driver = dom_driver(page)
        scope = driver._dialog_scope()
        assert scope is not None, "the container was not found"
        labels = driver._dialog_option_labels(scope)
        assert labels == ["All 8 photos"], labels
        try:
            driver._answer_dialog(scope)
            raise AssertionError("answered a dialog with only 'All 8 photos'")
        except StackedDialogError as exc:
            # GUARDRAIL C: the dump must say WHAT the options are.
            assert "dialog diagnostics" in str(exc), str(exc)[:200]
            assert "clickable=False" in str(exc), str(exc)[:400]
        assert page.clicks == [], page.clicks

    def t_ordinary_dialog_in_the_dom_is_still_answered() -> None:
        """The common case must keep working through the same code path."""
        confirm = DomNode("button", text="Move to trash")
        page = DomPage(DomNode("html", children=[DomNode("body", children=[
            toolbar(),
            DomNode("div", role="dialog", children=[
                DomNode("h1", text=REAL_HEADING),
                DomNode("button", text="Cancel"),
                confirm,
            ]),
        ])]))
        driver = dom_driver(page)
        scope = driver._dialog_scope()
        assert scope is not None
        labels = driver._dialog_option_labels(scope)
        # Read twice (role + text row) but offered once, or the decision would
        # refuse this dialog as ambiguous.
        assert labels.count("Move to trash") == 1, labels
        kind, choice = driver._answer_dialog(scope)
        assert (kind, choice) == (DIALOG_ORDINARY, "Move to trash")
        # The dialog's own button, not the toolbar's.
        assert page.clicks == [confirm], page.clicks

    def t_dialog_still_up_after_the_click_is_a_code_gap() -> None:
        """The real driver's post-action inspection, on a dialog that stayed up.

        An option was decided and clicked and the dialog did not go away, so
        the item survives. That is still a code gap - the click landed on the
        wrong thing - and must never be filed as the delete limiter (BUG B).
        """
        page, dialog = stacked_page()
        page.keyboard = type("K", (), {"press": staticmethod(lambda _key: None)})()
        page.bring_to_front = lambda: None
        driver = dom_driver(page)

        outcome = driver.trash_current()
        # The option was clicked...
        assert len(page.clicks) == 1, page.clicks
        assert is_current_only_option(page.clicks[0].text().strip())
        assert outcome.stacked and outcome.stacked_choice == "Current photo only"
        # ...and the dialog is still there, with no toast and no redirect.
        assert not outcome.toast_seen and not outcome.left_viewer
        assert outcome.lingering_dialog, outcome
        assert outcome.lingering_heading, outcome
        assert outcome.all_photos_seen, outcome
        # GUARDRAIL C: the dump comes back with the outcome, from the real DOM.
        assert "dialog diagnostics" in outcome.diagnostics, outcome.diagnostics
        assert "clickable=" in outcome.diagnostics, outcome.diagnostics
        assert "All 8 photos" in outcome.diagnostics, outcome.diagnostics
        assert "dialog-still-up" in outcome.note, outcome.note
        # And that is a code gap, not a quota.
        assert classify_noop_failure(
            all_photos_seen=outcome.all_photos_seen,
            lingering_dialog=outcome.lingering_dialog,
            lingering_heading=outcome.lingering_heading,
        ) == FAIL_STACKED

    def t_widened_scope_selectors_find_their_containers() -> None:
        """Containers that the v1.2.1 three-selector list would have missed.

        No heading on these pages on purpose: if the selector does not match,
        there is no fallback to rescue the test and no dialog is found at all.
        """
        for tag, attrs, role in (
            ("dialog", {"open": "open"}, None),       # native <dialog open>
            ("dialog", {}, None),                     # native <dialog>
            ("div", {"aria-modal": ""}, None),        # aria-modal with no value
            ("div", {"aria-modal": "true"}, None),    # the v1.2.1 selector
            ("div", {}, "dialog"),                    # role=dialog
            ("div", {}, "alertdialog"),               # role=alertdialog
        ):
            container = DomNode(tag, attrs=attrs, role=role, children=[
                text_row("All 8 photos"), text_row("Current photo only"),
            ])
            page = DomPage(DomNode("html", children=[DomNode("body", children=[
                toolbar(), container,
            ])]))
            driver = dom_driver(page)
            assert not stacked_heading_present(page.inner_text("body"))
            scope = driver._dialog_scope()
            assert scope is not None, (tag, attrs, role)
            assert scope.nodes == [container], (tag, attrs, role)
            labels = driver._dialog_option_labels(scope)
            assert "Current photo only" in labels, (tag, attrs, labels)
            assert "Move to trash" not in labels, (tag, attrs, labels)

    def t_option_less_container_is_recorded_not_skipped() -> None:
        """An ARIA dialog with no options in it must be reported, not ignored."""
        page = DomPage(DomNode("html", children=[DomNode("body", children=[
            toolbar(),
            DomNode("div", role="dialog", children=[
                DomNode("h1", text="Share this photo"),
                DomNode("button", text="Copy link"),
            ]),
        ])]))
        driver = dom_driver(page)
        scope = driver._dialog_scope()
        # Kept as a last resort so it can be refused loudly, with a dump...
        assert scope is not None
        # ...and the fact that it held no options is on the record.
        assert driver.empty_scopes, driver.empty_scopes
        assert "none of them an option" in driver.empty_scopes[0]
        try:
            driver._answer_dialog(scope)
            raise AssertionError("answered a dialog with no known option")
        except StackedDialogError as exc:
            assert "option-less" in str(exc), str(exc)[:300]
        assert page.clicks == [], page.clicks

    def t_dialog_lines_reads_rows_and_drops_the_heading() -> None:
        """The text-row reader, on the real dialog's visible text."""
        visible = f"{REAL_HEADING}\n\nAll 8 photos\nCurrent photo only\n"
        rows = dialog_lines(visible)
        assert rows == ["All 8 photos", "Current photo only"], rows
        assert decide_dialog_action(rows) == (DIALOG_STACKED, "Current photo only")
        # The heading is dropped however it wraps, and prose never becomes an
        # option.
        assert dialog_lines(REAL_HEADING) == []
        assert dialog_lines("x" * (MAX_OPTION_ROW_CHARS + 1)) == []
        assert dialog_lines("") == [] and dialog_lines(None) == []
        # An ordinary dialog reads the same way.
        assert decide_dialog_action(
            dialog_lines(f"{REAL_HEADING}\nCancel\nMove to trash")
        ) == (DIALOG_ORDINARY, "Move to trash")
        # Two views of one node collapse; two different options do not.
        assert merge_option_labels(
            ["Move to trash"], ["  move to TRASH  ", "Cancel"]
        ) == ["Move to trash", "Cancel"]
        try:
            decide_dialog_action(["Current photo only", "Current photo only"])
            raise AssertionError("the decider must still refuse two safe options")
        except StackedDialogError:
            pass
        assert container_holds_options(["All 8 photos"])
        assert container_holds_options(["Cancel", "Move to trash"])
        assert not container_holds_options(["Copy link", "Share"])
        assert not container_holds_options([])

    def t_ordinary_item_deletes_normally() -> None:
        """The common case end to end: 10 single items, all confirmed."""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "ordinary.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items], behaviour={i.url: "ordinary" for i in items}
        )
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        # The v1.2.0 regression was 0 deleted / 10 failed here.
        assert summary.deleted == 10, summary
        assert summary.failed == 0, summary
        assert summary.failed_stacked == 0, summary.failed_by_kind
        # None of these are stacked, so the stacked counters stay clean.
        assert summary.stacked_dialogs == 0, summary
        assert driver.clicked == ["Move to trash"] * 10, driver.clicked
        assert naps == [], naps

    def t_stacked_item_deletes_only_itself() -> None:
        items = load_items(csv_path).items[:4]
        path = os.path.join(tmpdir, "stacked_ok.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items],
            behaviour={
                items[0].url: "ordinary",
                items[1].url: "stacked",
                items[2].url: "stacked",
                items[3].url: "ordinary",
            },
        )
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.deleted == 4, summary
        assert summary.failed == 0, summary
        # Reported as its own outcome, separate from the ordinary confirms.
        assert summary.stacked_dialogs == 2, summary
        assert summary.stacked_handled == 2, summary
        assert driver.clicked == [
            "Move to trash", "Current photo only",
            "Current photo only", "Move to trash",
        ], driver.clicked
        assert not any(is_all_photos_option(c) for c in driver.clicked)
        # Only the items in the input file were trashed.
        assert driver.trashed == [i.url for i in items], driver.trashed
        details = {
            json.loads(line)["url"]: json.loads(line)["detail"]
            for line in open(path, encoding="utf-8")
        }
        assert "stacked: Current photo only" in details[items[1].url]
        assert "stacked" not in details[items[0].url]

    def t_stacked_failure_does_not_cool_down() -> None:
        """*** A dialog-wording failure is a code gap, never a throttle. ***"""
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "stacked_fail.jsonl")
        journal = Journal(path)
        journal.load()
        # Two real deletions first, so "progress since resume" is satisfied and
        # a throttle streak WOULD have cooled down here. Then three dialogs in a
        # row with wording this script does not know.
        behaviour = {item.url: "stacked_weird" for item in items}
        behaviour[items[0].url] = "ordinary"
        behaviour[items[1].url] = "ordinary"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0,
                     max_cooldowns=6)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.deleted == 2, summary
        # NOT ONE second of sleep, and no cooldown counted.
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        # Reported under its own kind, not as a mutation no-op.
        assert summary.failed_stacked == 3, summary.failed_by_kind
        assert summary.failed_mutation == 0, summary.failed_by_kind
        assert summary.stacked_dialogs == 3, summary
        assert summary.stacked_handled == 0, summary
        # Aborted with the code-gap verdict and its own exit kind.
        assert summary.abort_kind == ABORT_STACKED, summary.abort_kind
        assert "CODE GAP" in summary.aborted_reason
        # Only the two real confirms were ever clicked.
        assert driver.clicked == ["Move to trash"] * 2, driver.clicked
        # And the refused items are untouched and still pending.
        journal2 = Journal(path)
        journal2.load()
        for item in items[2:5]:
            assert not journal2.is_done(item.url), item.url

    def t_dialog_with_neither_option_touches_nothing() -> None:
        for mode in ("stacked_all_only", "dialog_weird"):
            items = load_items(csv_path).items[:2]
            path = os.path.join(tmpdir, f"neither_{mode}.jsonl")
            journal = Journal(path)
            journal.load()
            driver = FakeDriver(
                [i.url for i in items], behaviour={i.url: mode for i in items}
            )
            naps, fake_cooldown = recorder()
            cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                         max_consecutive_failures=5)
            summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                          cooldown_sleeper=fake_cooldown)
            assert summary.deleted == 0, (mode, summary)
            assert summary.failed_stacked == 2, (mode, summary.failed_by_kind)
            assert driver.clicked == [], (mode, driver.clicked)
            assert driver.trashed == [], (mode, driver.trashed)
            assert naps == [], (mode, naps)
            journal2 = Journal(path)
            journal2.load()
            assert not any(journal2.is_done(i.url) for i in items)

    def t_mixed_failure_kinds_are_reported_separately() -> None:
        """The whole point of the per-kind counters: see which layer you fight."""
        items = load_items(csv_path).items

        class Mixed(FakeDriver):
            def probe(self, probe_url: str) -> str:
                if probe_url == self._nav_fail:
                    nav_guard(probe_url, self._allowed)
                    self.probes.append(probe_url)
                    raise NavigationError("Page.goto timeout")
                return super().probe(probe_url)

        path = os.path.join(tmpdir, "kinds.jsonl")
        journal = Journal(path)
        journal.load()
        behaviour = {item.url: "ordinary" for item in items}
        behaviour[items[3].url] = "stacked_weird"
        behaviour[items[5].url] = "noop"
        behaviour[items[7].url] = "raise"
        driver = Mixed([i.url for i in items], behaviour=behaviour)
        driver._nav_fail = items[1].url
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert not summary.aborted_reason, summary.aborted_reason
        assert summary.failed == 4, summary
        assert summary.failed_navigation == 1, summary.failed_by_kind
        assert summary.failed_stacked == 1, summary.failed_by_kind
        assert summary.failed_mutation == 1, summary.failed_by_kind
        assert summary.failed_other == 1, summary.failed_by_kind
        assert summary.deleted == 6, summary
        # Scattered single failures of any kind never trigger a cooldown.
        assert naps == [], naps

    def t_dialog_decision_comes_before_any_click() -> None:
        """Source-level guard on the properties that keep this safe."""
        answer = inspect.getsource(PlaywrightDriver._answer_dialog)
        # Options are read, then decided on, and only then is anything clicked.
        assert answer.index("_dialog_option_labels") < answer.index(
            "decide_dialog_action"
        ) < answer.index("_click_dialog_option"), answer

        click = inspect.getsource(PlaywrightDriver._click_dialog_option)
        # The guard sits between the decision and every click, and the target
        # is re-checked immediately before the click itself.
        assert click.index("assert_safe_to_click") < click.index(".click("), click
        assert click.index("_assert_click_target_safe") < click.index(".click("), click
        # Every locator in the click path is scoped to the dialog container.
        assert "self._page" not in click, click

        # FINDING #4: the container walk goes innermost-first and refuses
        # <body>/<html>. A forward scan would select <body>, whose scope
        # includes the viewer toolbar's own "Move to trash".
        picker = inspect.getsource(pick_dialog_container)
        assert "range(len(candidates) - 1, -1, -1)" in picker, picker
        assert "NEVER_A_DIALOG_SCOPE" in picker, picker
        assert "body" in NEVER_A_DIALOG_SCOPE and "html" in NEVER_A_DIALOG_SCOPE
        walk = inspect.getsource(PlaywrightDriver._dialog_scope_from_heading)
        assert "pick_dialog_container" in walk, walk
        assert ".first" not in walk, walk

        trash = inspect.getsource(PlaywrightDriver.trash_current)
        # The dialog is located before anything is answered, and the heading is
        # never the discriminator - it may only be read for the log note.
        assert "_wait_for_dialog()" in trash, trash
        assert trash.index("_wait_for_dialog()") < trash.index("_answer_dialog"), trash
        heading_at = trash.find("stacked_heading_present")
        if heading_at != -1:
            assert heading_at > trash.index("_answer_dialog"), (
                "the heading must not gate answering the dialog"
            )
        # FIX 1: the toolbar fallback only ever fires BEFORE a dialog is
        # answered, and whatever it opens is answered through the same
        # decide_dialog_action() path. It must never be a way to click a
        # dialog's own button.
        assert trash.index("_toolbar_trash_control") < trash.index("_answer_dialog")
        assert "assert_safe_to_click" in trash, trash
        assert trash.index("assert_safe_to_click") < trash.index(".click("), trash
        control = inspect.getsource(PlaywrightDriver._toolbar_trash_control)
        assert "_is_inside_dialog" in control, control
        assert "is_toolbar_trash_label" in control, control
        assert "assert_safe_to_click" in control, control
        # It never clicks: the caller does, after its own guard.
        assert ".click(" not in control, control
        # Candidates are never gathered page-wide in the answering path.
        labels = inspect.getsource(PlaywrightDriver._dialog_option_labels)
        assert "self._page.locator" not in labels, labels
        role_labels = inspect.getsource(PlaywrightDriver._role_option_labels)
        assert "self._page.locator" not in role_labels, role_labels
        assert "scope.locator" in role_labels, role_labels
        # TRASH_BUTTON_NAME_RE must not be able to match a whole-stack label,
        # nor an unanchored toolbar string.
        for label in ("All 8 photos", "All 2 videos", "All 12 items"):
            assert not TRASH_BUTTON_NAME_RE.match(label), label

    # ----------------------------------------------------------------------- #
    # BUG B: an unanswerable stack is not a rate limit
    # ----------------------------------------------------------------------- #

    def t_classify_noop_failure_truth_table() -> None:
        """One flag at a time: any dialog evidence at all means code gap."""
        assert classify_noop_failure(
            all_photos_seen=False, lingering_dialog=False, lingering_heading=False
        ) == FAIL_MUTATION
        for flags in (
            {"all_photos_seen": True},
            {"lingering_dialog": True},
            {"lingering_heading": True},
            {"all_photos_seen": True, "lingering_dialog": True},
            {"lingering_dialog": True, "lingering_heading": True},
        ):
            kwargs = {
                "all_photos_seen": False,
                "lingering_dialog": False,
                "lingering_heading": False,
            }
            kwargs.update(flags)
            assert classify_noop_failure(**kwargs) == FAIL_STACKED, flags
        # And the kinds land on the right side of the cooldown.
        assert FAIL_MUTATION in THROTTLE_KINDS
        assert FAIL_STACKED not in THROTTLE_KINDS

    def t_unanswerable_stack_is_not_a_mutation_noop() -> None:
        """*** BUG B, end to end: the production misclassification. ***

        The dialog is up, its container was never located, nothing was clicked
        and the item survived. v1.2.1 called that `mutation-no-op`, which is a
        THROTTLE_KIND, and slept 20 minutes over a selector.
        """
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "stack_unseen.jsonl")
        journal = Journal(path)
        journal.load()
        # One deletion first, so a throttle streak WOULD have been allowed to
        # cool down (that is the condition v1.2.1 met in production).
        behaviour = {item.url: "stack_unseen" for item in items}
        behaviour[items[0].url] = "ok"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0, max_cooldowns=6)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.deleted == 1, summary
        assert summary.failed == 3, summary
        # Classified as what it is...
        assert summary.failed_stacked == 3, summary.failed_by_kind
        # ...and NOT as the delete limiter.
        assert summary.failed_mutation == 0, summary.failed_by_kind
        # No sleeping: a cooldown cannot find a selector.
        assert naps == [], naps
        assert summary.cooldowns == 0, summary
        # The abort says code gap, not rate limit.
        assert summary.abort_kind == ABORT_STACKED, summary.abort_kind
        assert "CODE GAP" in summary.aborted_reason, summary.aborted_reason
        # Counted as a stacked dialog met, and none of them answered.
        assert summary.stacked_dialogs == 3, summary
        assert summary.stacked_handled == 0, summary
        # Nothing was clicked on any of them.
        assert driver.clicked == [], driver.clicked
        # The items stay pending for a later run.
        journal2 = Journal(path)
        journal2.load()
        assert not any(journal2.is_done(i.url) for i in items[1:4])

    def t_mutation_noop_still_cools_down_when_the_dialog_was_answered() -> None:
        """The other half of BUG B: a real no-op must still be read as throttling.

        `mutation-no-op` is narrowed, not removed - it is the only reading that
        would ever be evidence of a delete quota, so it has to still fire when
        the dialog WAS answered and dismissed and the item survived anyway.
        """
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "real_noop.jsonl")
        journal = Journal(path)
        journal.load()
        behaviour = {item.url: "noop" for item in items}
        behaviour[items[0].url] = "ok"
        driver = FakeDriver([i.url for i in items], behaviour=behaviour)
        naps, fake_cooldown = recorder()
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     max_consecutive_failures=3, cooldown=1200.0, max_cooldowns=1,
                     post_cooldown_window=99)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep,
                      cooldown_sleeper=fake_cooldown)
        assert summary.failed_mutation >= 3, summary.failed_by_kind
        assert summary.failed_stacked == 0, summary.failed_by_kind
        assert naps == [1200.0], naps
        assert summary.cooldowns == 1, summary

    def t_a_lingering_dialog_is_reported_with_its_dump() -> None:
        """The failure line must carry the dialog dump, not just a verdict."""
        items = load_items(csv_path).items[:2]
        path = os.path.join(tmpdir, "stack_unseen_log.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver(
            [i.url for i in items],
            behaviour={items[0].url: "stack_unseen", items[1].url: "ok"},
        )
        lines: List[str] = []
        cfg = Config(execute=True, limit=2, delay=0, journal_path=path)
        summary = run(items, driver, journal, cfg, log=lines.append,
                      sleeper=no_sleep)
        assert summary.failed_stacked == 1, summary.failed_by_kind
        joined = "\n".join(lines)
        assert "stacked dialog unanswered" in joined, joined
        assert "fake dialog dump" in joined, joined
        assert "clickable=False" in joined, joined
        # The journal says which kind it was, for the next run's benefit.
        with open(path, "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        detail = [r["detail"] for r in records if r["status"] == "failed"]
        assert detail and detail[0].startswith(FAIL_STACKED), detail

    check("the shared heading is not the discriminator (v1.2.0 regression)",
          t_heading_is_not_the_discriminator)
    check("the ordinary confirm dialog is answered, not treated as stacked",
          t_ordinary_dialog_is_confirmed_not_treated_as_stacked)
    check("'Current photo only' is still the stacked choice",
          t_stacked_dialog_still_chooses_current_photo_only)
    check("a stacked option outranks a confirm button",
          t_stacked_wins_over_a_confirm_button)
    check("'All N photos' is NEVER chosen, under any dialog",
          t_all_n_photos_is_never_chosen)
    check("unexpected dialog wording fails safe without clicking anything",
          t_unexpected_wording_fails_safe)
    check("confirm candidates come only from inside the dialog element",
          t_confirm_candidates_must_be_dialog_scoped)
    check("*** a dead '#' shortcut falls back to the toolbar (FIX 1) ***",
          t_shortcut_opening_no_dialog_falls_back_to_the_toolbar)
    check("a slow dialog is waited for, not overtaken by the fallback",
          t_a_slow_dialog_is_waited_for_not_overtaken_by_the_fallback)
    check("*** the toolbar fallback never clicks inside a dialog (FIX 1) ***",
          t_toolbar_fallback_never_clicks_inside_a_dialog)
    check("toolbar label matching is anchored and refuses permanent deletes",
          t_toolbar_label_matching_is_anchored_and_safe)
    check("a working shortcut never touches the toolbar",
          t_shortcut_that_works_does_not_use_the_toolbar)
    check("the toolbar fallback still refuses 'All N photos'",
          t_toolbar_fallback_on_a_stacked_item_still_refuses_all_photos)
    check("deletions are counted per route", t_route_counts_are_reported)
    check("*** diagnostics fire with NO dialog present (FIX 2) ***",
          t_diagnostics_fire_with_no_dialog_present)
    check("*** trash_current dumps when NO dialog was detected (FIX 2) ***",
          t_trash_current_dumps_when_no_dialog_was_ever_detected)
    check("diagnostics stay bounded and report a missing toolbar",
          t_diagnostics_truncate_and_still_report_without_a_toolbar)
    check("every no-toast failure is logged with its dump",
          t_diagnostics_reported_for_every_no_toast_failure)
    check("dialog options are read from visible text rows too",
          t_dialog_lines_reads_rows_and_drops_the_heading)
    check("*** stacked options are TEXT ROWS, not buttons (BUG A) ***",
          t_stacked_options_are_text_rows_not_buttons)
    check("an inert option row is clicked via its clickable ancestor",
          t_clickable_ancestor_walk_lands_the_click)
    check("a click target covering two options is refused",
          t_click_target_covering_two_options_is_refused)
    check("*** the heading walk takes the innermost ancestor, never <body> "
          "(FINDING #4) ***",
          t_heading_walk_takes_the_innermost_ancestor_not_body)
    check("'All N photos' is unreachable in a real DOM",
          t_all_photos_is_unreachable_in_the_dom)
    check("an ordinary dialog in the DOM is still answered",
          t_ordinary_dialog_in_the_dom_is_still_answered)
    check("a dialog still up after the click is a code gap, not a quota",
          t_dialog_still_up_after_the_click_is_a_code_gap)
    check("the widened scope selectors find their containers",
          t_widened_scope_selectors_find_their_containers)
    check("an option-less container is recorded, not silently skipped",
          t_option_less_container_is_recorded_not_skipped)
    check("no-toast failures are classified before they are counted",
          t_classify_noop_failure_truth_table)
    check("*** an unanswerable stack never cools down (BUG B) ***",
          t_unanswerable_stack_is_not_a_mutation_noop)
    check("a real mutation no-op still cools down",
          t_mutation_noop_still_cools_down_when_the_dialog_was_answered)
    check("a lingering dialog is reported with its dump",
          t_a_lingering_dialog_is_reported_with_its_dump)
    check("ordinary single items delete normally", t_ordinary_item_deletes_normally)
    check("a stacked item deletes only itself", t_stacked_item_deletes_only_itself)
    check("a dialog-wording failure never triggers a cooldown",
          t_stacked_failure_does_not_cool_down)
    check("a dialog with neither known option touches nothing",
          t_dialog_with_neither_option_touches_nothing)
    check("failure kinds are counted separately",
          t_mixed_failure_kinds_are_reported_separately)
    check("the dialog is scoped and decided before any click",
          t_dialog_decision_comes_before_any_click)

    # --- heartbeat --------------------------------------------------------- #

    def t_heartbeat_every_n_items() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "beat.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver([i.url for i in items])
        lines: List[str] = []
        cfg = Config(execute=True, limit=10, delay=0, journal_path=path,
                     heartbeat_every=4)
        run(items, driver, journal, cfg, log=lines.append, sleeper=no_sleep)
        beats = [line for line in lines if "heartbeat" in line]
        assert len(beats) == 2, beats  # after items 4 and 8
        assert "10 item" not in beats[0]
        assert "/min" in beats[0] and "still pending" in beats[0]
        # Disabled.
        lines.clear()
        journal2 = Journal(os.path.join(tmpdir, "beat2.jsonl"))
        journal2.load()
        driver2 = FakeDriver([i.url for i in items])
        cfg2 = Config(execute=True, limit=10, delay=0,
                      journal_path=journal2.path, heartbeat_every=0)
        run(items, driver2, journal2, cfg2, log=lines.append, sleeper=no_sleep)
        assert not [line for line in lines if "heartbeat" in line]

    def t_format_duration() -> None:
        assert format_duration(0) == "0s"
        assert format_duration(45) == "45s"
        assert format_duration(90) == "1m30s"
        assert format_duration(1200) == "20m00s"
        assert format_duration(7200) == "2h00m00s"
        assert format_duration(3661) == "1h01m01s"
        assert format_duration(-5) == "0s"

    check("a heartbeat is printed every N items", t_heartbeat_every_n_items)
    check("durations are formatted readably", t_format_duration)

    # --- pacing ------------------------------------------------------------ #

    def t_jitter_bounds() -> None:
        cfg = Config(delay=4.0, jitter=0.35)
        naps: List[float] = []
        original_sleep = time.sleep
        try:
            time.sleep = naps.append  # type: ignore[assignment]
            for _ in range(200):
                _sleep_with_jitter(cfg, quiet)
        finally:
            time.sleep = original_sleep  # type: ignore[assignment]
        assert len(naps) == 200
        assert all(2.6 <= nap <= 5.4 for nap in naps), (min(naps), max(naps))
        assert min(naps) < 3.5 < max(naps), "jitter does not look random"
        zero = Config(delay=0.0)
        naps.clear()
        try:
            time.sleep = naps.append  # type: ignore[assignment]
            _sleep_with_jitter(zero, quiet)
        finally:
            time.sleep = original_sleep  # type: ignore[assignment]
        assert naps == []

    def t_paced_between_items() -> None:
        items = load_items(csv_path).items
        path = os.path.join(tmpdir, "pace.jsonl")
        journal = Journal(path)
        journal.load()
        driver = FakeDriver([i.url for i in items])
        calls: List[int] = []
        cfg = Config(execute=True, limit=5, delay=0.01, journal_path=path)
        run(items, driver, journal, cfg, log=quiet,
            sleeper=lambda *_a, **_k: calls.append(1))
        assert len(calls) == 5, calls

    check("jitter stays inside the configured band", t_jitter_bounds)
    check("every item is followed by a pause", t_paced_between_items)

    # --- CLI argument handling --------------------------------------------- #

    def t_cli_defaults_and_validation() -> None:
        args = build_parser().parse_args([])
        assert args.execute is False, "execute must default to off"
        assert args.limit == 25
        assert args.delay == 5.0, "rate limiting is real; default must be 5s"
        assert args.post_action_settle == 3.0
        assert args.max_consecutive_failures == 3
        assert args.offline is False
        # v1.2.0 cooldown / pacing defaults, as documented in the header.
        assert args.cooldown == 1200.0, "default cooldown must be 20 minutes"
        assert args.cooldown_backoff == 2.0
        assert args.cooldown_cap == 7200.0
        assert args.max_cooldowns == 6
        assert args.post_cooldown_window == 3
        assert args.delay_growth == 1.5
        assert args.delay_max == 30.0
        assert args.heartbeat_every == 100
        assert main(["--offline", "--execute", "--input", csv_path]) == 2
        assert main(["--limit", "0", "--input", csv_path]) == 2
        assert main(["--jitter", "1.5", "--input", csv_path]) == 2
        assert main(["--max-consecutive-failures", "0", "--input", csv_path]) == 2
        assert main(["--post-action-settle", "-1", "--input", csv_path]) == 2
        assert main(["--cooldown", "-1", "--input", csv_path]) == 2
        assert main(["--cooldown-backoff", "0.5", "--input", csv_path]) == 2
        assert main(["--cooldown-cap", "-1", "--input", csv_path]) == 2
        assert main(["--max-cooldowns", "-1", "--input", csv_path]) == 2
        assert main(["--post-cooldown-window", "0", "--input", csv_path]) == 2
        # The pace may only ever slow down, never speed up.
        assert main(["--delay-growth", "0.9", "--input", csv_path]) == 2
        assert main(["--delay-max", "-1", "--input", csv_path]) == 2
        assert main(["--heartbeat-every", "-1", "--input", csv_path]) == 2
        assert main(["--input", os.path.join(tmpdir, "nope.csv")]) == 2

    def t_smoke_defaults() -> None:
        argv = ["--smoke", "--offline", "--input", csv_path,
                "--journal", os.path.join(tmpdir, "smoke.jsonl")]
        assert main(argv) == 0
        args = build_parser().parse_args(["--smoke"])
        assert args.smoke_count == 5

        def resolved(flags: Sequence[str]) -> argparse.Namespace:
            parsed = build_parser().parse_args(list(flags))
            apply_smoke_mode(parsed, list(flags))
            return parsed

        assert resolved(["--smoke"]).limit == 5
        assert resolved(["--smoke"]).verbose is True
        assert resolved(["--smoke", "--smoke-count", "3"]).limit == 3
        # An explicit --limit wins over the smoke default, in either spelling.
        assert resolved(["--smoke", "--limit", "9"]).limit == 9
        assert resolved(["--smoke", "--limit=9"]).limit == 9
        assert resolved(["--limit", "40"]).limit == 40

    def t_dry_run_remaining() -> None:
        items = load_items(csv_path).items
        journal = Journal(os.path.join(tmpdir, "dryrem.jsonl"))
        journal.load()
        driver = FakeDriver([i.url for i in items])
        cfg = Config(execute=False, limit=4, delay=0, journal_path=journal.path)
        summary = run(items, driver, journal, cfg, log=quiet, sleeper=no_sleep)
        # Dry run writes no journal, so "remaining" must still be the honest
        # pending-minus-this-batch, not the whole file.
        assert summary.attempted == 4, summary
        assert summary.remaining == 6, summary

    def t_gone_text_patterns() -> None:
        for text in (
            "Sorry, this item is no longer available.",
            "Photo not found",
            "This item is in the trash",
            "We couldn't find this photo",
            "That page isn't available",
        ):
            assert any(p.search(text) for p in GONE_TEXT_PATTERNS), text
        for text in ("Edit", "Share", "Add to album", "July 4, 2019"):
            assert not any(p.search(text) for p in GONE_TEXT_PATTERNS), text

    check("CLI defaults are safe and bad flags are rejected",
          t_cli_defaults_and_validation)
    check("--smoke works end to end offline", t_smoke_defaults)
    check("dry run reports honest remaining count", t_dry_run_remaining)
    check("'already gone' text detection", t_gone_text_patterns)

    print("")
    print(f"self-test: {len(passed)} passed, {len(failed)} failed")
    print(f"(temp files in {tmpdir})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
