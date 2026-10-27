"""Deletion command-line parsing, validation, summaries, and orchestration."""

from __future__ import annotations

from .delete_execution import *

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
        from .delete_selftest import self_test

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
