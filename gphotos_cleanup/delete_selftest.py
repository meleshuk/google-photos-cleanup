"""Offline guardrail and regression tests exposed through ``--self-test``."""

from __future__ import annotations

from .delete_cli import *
from .delete_execution import _sleep_returning_false, _sleep_with_jitter

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
        driver._background = False
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
        driver._background = False
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
