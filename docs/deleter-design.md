# Deleter design and safety notes

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
