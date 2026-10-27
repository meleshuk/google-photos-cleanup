"""Paced deletion execution, cooldown policy, and offline planning."""

from __future__ import annotations

from .delete_shared import *
from .delete_browser import *

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
