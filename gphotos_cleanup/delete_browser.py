"""Google Photos browser state, safety decisions, and Playwright driver."""

from __future__ import annotations

from .delete_shared import *

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
    shortcut_attempted: bool = True
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

    def failure_kind(self, error):
        return getattr(error, "kind", FAIL_OTHER)

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
        max_tab_crash_recoveries: int = 3,
        log=print,
        background: bool = True,
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
        self._max_tab_crash_recoveries = max(0, max_tab_crash_recoveries)
        self.tab_crash_recoveries = 0
        self._crashed = False
        self._log = timestamped_logger(log)
        self._background = background
        self._background_sessions = {}
        # Containers that were inspected and held no options at all. Recorded
        # rather than silently skipped: "no scope found" was indistinguishable
        # from "no dialog up" in v1.2.1, and that is how BUG A hid.
        self.empty_scopes: List[str] = []

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url, timeout=nav_timeout_ms)
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
        try:
            self._page = self._new_automation_page()
            self._page.set_default_timeout(nav_timeout_ms)
            self._watch_page(self._page)
        except Exception:
            self.close()
            raise

    # -- internals ---------------------------------------------------------- #

    def _new_automation_page(self):
        """Create only our target, with no foreground activation in background mode."""
        if not self._background:
            return self._context.new_page()
        session = self._browser.new_browser_cdp_session()
        target_id = None
        try:
            with self._context.expect_page(timeout=self._nav_timeout_ms) as pending:
                target_id = session.send("Target.createTarget", {
                    "url": "about:blank", "background": True,
                })["targetId"]
            candidates = [pending.value]
            seen = set()
            deadline = time.monotonic() + self._nav_timeout_ms / 1000.0
            while True:
                for page in candidates:
                    seen.add(page)
                    page_session = self._context.new_cdp_session(page)
                    keep_session = False
                    try:
                        info = page_session.send("Target.getTargetInfo")["targetInfo"]
                        if info["targetId"] != target_id:
                            continue
                        # Emulate DOM focus/visibility without activating the OS
                        # window. Keep the session alive for this owned page.
                        page_session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                        self._background_sessions[page] = page_session
                        keep_session = True
                        return page
                    finally:
                        if not keep_session:
                            page_session.detach()
                candidates = [page for page in self._context.pages if page not in seen]
                if candidates:
                    continue
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining <= 0:
                    raise DriverError("could not identify the newly created background tab")
                candidates = [self._context.wait_for_event("page", timeout=remaining)]
        except Exception as exc:
            if target_id is not None:
                try:
                    session.send("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass
            raise DriverError("could not create a background automation tab: %s" % exc) from exc
        finally:
            session.detach()

    def _close_owned_page(self, page):
        # Closing the target tears down its CDP sessions automatically. Detaching
        # focus emulation first can freeze a hidden renderer during cleanup.
        try:
            page.close()
        finally:
            self._background_sessions.pop(page, None)

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

    def _watch_page(self, page):
        def crashed(*_args):
            if self._page is page:
                self._crashed = True
        page.on("crash", crashed)

    def _page_has_crashed(self, error=None):
        if getattr(self, "_crashed", False):
            return True
        if error is not None and "page crashed" in str(error).lower():
            return True
        try:
            return self._page.is_closed()
        except Exception:
            return False

    def failure_kind(self, error):
        if self._page_has_crashed(error):
            return FAIL_BROWSER_CRASH
        return super().failure_kind(error)

    def _recover_crashed_tab(self):
        count = getattr(self, "tab_crash_recoveries", 0)
        limit = getattr(self, "_max_tab_crash_recoveries", 3)
        if count >= limit:
            raise FatalPageCrashError(
                "tab crash recovery budget exhausted (%d/%d); inspect Chrome "
                "and resume with the same journal" % (count, limit))
        fresh = None
        try:
            fresh = self._new_automation_page()
            fresh.set_default_timeout(self._nav_timeout_ms)
            self._watch_page(fresh)
        except Exception as exc:
            if fresh is not None:
                try:
                    self._close_owned_page(fresh)
                except Exception:
                    pass
            raise FatalPageCrashError("could not replace the crashed tab: %s" % exc) from exc
        old = self._page
        self._page = fresh
        self._crashed = False
        self.tab_crash_recoveries = count + 1
        try:
            self._close_owned_page(old)
        except Exception:
            pass
        logger = getattr(self, "_log", timestamped_logger(print))
        logger("  browser crash recovery %d/%d: replaced the script's tab; "
               "retrying read-only navigation" % (self.tab_crash_recoveries, limit))

    def _navigate(self, url: str) -> None:
        nav_guard(url, self._allowed)
        retried = False
        while True:
            if self._page_has_crashed():
                if retried:
                    raise FatalPageCrashError("the replacement tab is already closed or crashed")
                self._recover_crashed_tab()
                retried = True
            try:
                self._page.goto(
                    url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms
                )
                self._page.wait_for_timeout(self._settle_ms)
                if self._page_has_crashed():
                    raise PageCrashError("Page crashed after navigation")
                return
            except Exception as exc:
                if not self._page_has_crashed(exc):
                    raise NavigationError(f"navigation failed: {exc}") from exc
                if retried:
                    raise FatalPageCrashError(
                        "the fresh tab crashed while retrying navigation; stopping "
                        "without cooldown: %s" % exc) from exc
                self._recover_crashed_tab()
                retried = True

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
        if self._background:
            # Unknown controls stay untouched. The next guarded navigation
            # discards the dialog without sending any keyboard input.
            return
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

        background = self._background
        action_route = ROUTE_TOOLBAR if background else ROUTE_SHORTCUT
        shortcut_opened_dialog = False
        scope = None
        if not background:
            # Explicit foreground compatibility mode retains the shortcut.
            try:
                page.bring_to_front()
            except Exception:
                pass
            try:
                page.keyboard.press("#")
            except Exception as exc:
                raise DriverError(f"could not send the trash shortcut: {exc}") from exc
            scope = self._wait_for_dialog()
            shortcut_opened_dialog = scope is not None
        if background or scope is None:
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
                        f"toolbar trash control {control_label!r} could not be clicked: {exc}"
                    ) from exc
                scope = self._wait_for_dialog()
            elif background:
                raise NoDialogError("no guarded toolbar trash control found in background mode; "
                                    "no keyboard shortcut or foreground fallback was attempted")

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
            if not background and not shortcut_opened_dialog:
                diagnostics = (
                    "  the '#' shortcut opened no dialog; "
                    f"route taken: {action_route}\n" + diagnostics
                )

        if self._page_has_crashed() and not toast:
            raise PageCrashError(
                "tab crashed during the trash action; deletion is unverified "
                "and this item remains pending")
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
            shortcut_attempted=not background,
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
            fresh = self._new_automation_page()
        except Exception as exc:
            # Could not get a tab; settle for emptying the one we have.
            try:
                self._blank_out()
                return f"blanked existing tab (new tab failed: {exc})"
            except Exception as blank_exc:
                return f"could not recycle the tab ({blank_exc}); continuing anyway"
        old = self._page
        self._page = fresh
        self._crashed = False
        self._watch_page(fresh)
        self._page.set_default_timeout(self._nav_timeout_ms)
        try:
            self._close_owned_page(old)
        except Exception:
            return "opened a fresh tab (the old one would not close)"
        return "opened a fresh tab and closed the old one"

    def close(self) -> None:
        # GUARDRAIL 9: close only our own tab. Never close the user's browser.
        for closer in (
            lambda: self._close_owned_page(self._page),
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
