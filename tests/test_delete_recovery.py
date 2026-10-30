import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gphotos_cleanup.delete_browser import FakeDriver, PlaywrightDriver, GONE, PRESENT, TrashOutcome
from gphotos_cleanup.delete_execution import Config, run
from gphotos_cleanup.delete_shared import (
    ABORT_FATAL, FAIL_BROWSER_CRASH, FAIL_NAVIGATION, FatalDriverError,
    FatalPageCrashError, Item, Journal, NavigationError, PageCrashError,
    GuardrailViolation, timestamped_logger,
)


URL = 'https://photos.google.com/photo/AF1QipFAKERECOVERY'
OTHER_URL = 'https://photos.google.com/photo/AF1QipFAKEOTHER'


class Page:
    """Model a renderer crash without Chrome, network access, or photo mutations."""
    def __init__(self, actions=(), state=PRESENT):
        self.actions = list(actions)
        self.state = state
        self.url = 'about:blank'
        self.closed = False
        self.handlers = {}
        self.navigations = []
        self.keyboard = SimpleNamespace(press=lambda *_: None)

    def on(self, event, handler):
        self.handlers[event] = handler

    def crash(self):
        self.handlers['crash'](self)

    def is_closed(self):
        return self.closed

    def set_default_timeout(self, milliseconds):
        self.timeout = milliseconds

    def goto(self, url, **kwargs):
        self.navigations.append(url)
        action = self.actions.pop(0) if self.actions else 'present'
        if action == 'crash':
            self.crash()
            raise RuntimeError('Page.goto: Page crashed')
        if action == 'crash-without-event':
            raise RuntimeError('Page.goto: Page crashed')
        if action == 'timeout':
            raise RuntimeError('Page.goto: timeout exceeded')
        self.url = 'https://accounts.google.com/signin' if action == 'signin' else url

    def wait_for_timeout(self, milliseconds):
        pass

    def inner_text(self, selector, **kwargs):
        return 'This item is in the trash' if self.state == GONE else 'Photo viewer'

    def close(self):
        self.closed = True

    def bring_to_front(self):
        pass


class Context:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created = []

    def new_page(self):
        if not self.pages:
            raise RuntimeError('browser context disconnected')
        page = self.pages.pop(0)
        self.created.append(page)
        return page


def driver_for(old, new_pages, maximum=3):
    driver = PlaywrightDriver.__new__(PlaywrightDriver)
    driver._page = old
    driver._context = Context(new_pages)
    driver._allowed = {URL, OTHER_URL}
    driver._nav_timeout_ms = 30000
    driver._settle_ms = 0
    driver._post_action_settle_ms = 0
    driver._max_tab_crash_recoveries = maximum
    driver.tab_crash_recoveries = 0
    driver._crashed = False
    driver._background = False
    driver._background_sessions = {}
    driver.messages = []
    driver._log = timestamped_logger(driver.messages.append)
    driver._watch_page(old)
    driver._viewer_is_live = lambda: driver._page.state == PRESENT
    driver._trashed_state_visible = lambda: driver._page.state == GONE
    return driver


class RecoveryTests(unittest.TestCase):
    def test_navigation_crash_replaces_only_our_tab_and_retries_same_url(self):
        old, fresh, unrelated = Page(['crash']), Page(), Page()
        driver = driver_for(old, [fresh])
        self.assertEqual(driver.probe(URL), PRESENT)
        self.assertEqual(old.navigations, [URL])
        self.assertEqual(fresh.navigations, [URL])
        self.assertTrue(old.closed)
        self.assertFalse(unrelated.closed)
        self.assertEqual(driver.tab_crash_recoveries, 1)

    def test_native_crash_message_is_detected_without_crash_event(self):
        driver = driver_for(Page(['crash-without-event']), [Page()])
        self.assertEqual(driver.probe(URL), PRESENT)
        self.assertEqual(driver.tab_crash_recoveries, 1)

    def test_normal_timeout_remains_navigation_failure(self):
        driver = driver_for(Page(['timeout']), [])
        with self.assertRaises(NavigationError) as caught:
            driver.probe(URL)
        self.assertEqual(driver.failure_kind(caught.exception), FAIL_NAVIGATION)
        self.assertEqual(driver.tab_crash_recoveries, 0)

    def test_crash_on_fresh_tab_stops_without_another_retry(self):
        driver = driver_for(Page(['crash']), [Page(['crash']), Page()])
        with self.assertRaises(FatalPageCrashError):
            driver.probe(URL)
        self.assertEqual(len(driver._context.created), 1)

    def test_already_closed_replacement_is_not_replaced_repeatedly(self):
        fresh = Page()
        fresh.closed = True
        driver = driver_for(Page(['crash']), [fresh, Page()])
        with self.assertRaises(FatalPageCrashError):
            driver.probe(URL)
        self.assertEqual(len(driver._context.created), 1)

    def test_recovery_budget_is_per_run_and_bounded(self):
        driver = driver_for(Page(), [Page(), Page(), Page()])
        for _ in range(3):
            driver._page.crash()
            self.assertEqual(driver.probe(URL), PRESENT)
        driver._page.crash()
        with self.assertRaises(FatalPageCrashError):
            driver.probe(URL)
        self.assertEqual(driver.tab_crash_recoveries, 3)

    def test_zero_budget_stops_on_first_crash(self):
        driver = driver_for(Page(['crash']), [Page()], maximum=0)
        with self.assertRaises(FatalPageCrashError):
            driver.probe(URL)
        self.assertEqual(driver._context.created, [])

    def test_disconnected_browser_aborts_without_restarting_chrome(self):
        driver = driver_for(Page(['crash']), [])
        with self.assertRaises(FatalPageCrashError):
            driver.probe(URL)

    def test_recovery_does_not_bypass_signin_or_input_guards(self):
        driver = driver_for(Page(['crash']), [Page(['signin'])])
        with self.assertRaises(FatalDriverError):
            driver.probe(URL)
        with self.assertRaises(GuardrailViolation):
            driver.probe('https://photos.google.com/photo/AF1QipFAKEUNAUTHORIZED')

    def test_old_tab_crash_event_cannot_mark_fresh_tab_crashed(self):
        old = Page(['crash'])
        driver = driver_for(old, [Page()])
        driver.probe(URL)
        old.crash()
        self.assertFalse(driver._crashed)

    def run_items(self, driver, items):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        journal = Journal(str(Path(directory.name) / 'journal.jsonl'))
        cooldowns, messages = [], []
        summary = run(items, driver, journal,
                      Config(execute=True, limit=len(items), delay=0, journal_path=journal.path),
                      log=messages.append, sleeper=lambda *_: None,
                      cooldown_sleeper=lambda *args: cooldowns.append(args))
        return summary, journal, cooldowns, messages

    def test_crash_during_verification_rechecks_before_recording_success(self):
        driver = driver_for(Page(['present', 'crash']), [Page(state=GONE)])
        mutations = []
        driver.trash_current = lambda: mutations.append(URL) or TrashOutcome()
        summary, journal, cooldowns, _ = self.run_items(driver, [Item(URL)])
        self.assertEqual(mutations, [URL])
        self.assertEqual(summary.deleted, 1)
        self.assertTrue(journal.is_done(URL))
        self.assertEqual(summary.tab_crash_recoveries, 1)
        self.assertEqual(cooldowns, [])

    def test_crash_after_observed_confirmation_toast_keeps_verified_success(self):
        old = Page()
        driver = driver_for(old, [])
        driver._wait_for_dialog = lambda: object()
        driver._answer_dialog = lambda *_: ('ordinary', 'Move to trash')
        driver._wait_for_trashed_toast = lambda: old.crash() or 'Moved to trash'
        driver._any_selector_present = lambda *_: False
        summary, journal, cooldowns, _ = self.run_items(driver, [Item(URL)])
        self.assertEqual(summary.deleted, 1)
        self.assertTrue(journal.is_done(URL))
        self.assertEqual(cooldowns, [])

    def test_fatal_crash_leaves_item_pending_and_never_cools_down(self):
        driver = driver_for(Page(['crash']), [Page(['crash'])])
        summary, journal, cooldowns, _ = self.run_items(driver, [Item(URL)])
        self.assertEqual(summary.abort_kind, ABORT_FATAL)
        self.assertEqual(summary.failed_browser_crash, 1)
        self.assertEqual(summary.failed_navigation, 0)
        self.assertFalse(journal.is_done(URL))
        self.assertEqual(cooldowns, [])

    def test_unverified_mutation_crash_stays_pending_and_resume_probes_gone(self):
        class CrashesDuringAction(FakeDriver):
            def trash_current(self):
                if self._current == URL:
                    self.states[URL] = GONE
                    raise PageCrashError('Page crashed after a possible deletion')
                return super().trash_current()
        driver = CrashesDuringAction([URL, OTHER_URL])
        summary, journal, cooldowns, _ = self.run_items(driver, [Item(URL), Item(OTHER_URL)])
        self.assertEqual(summary.failed_browser_crash, 1)
        self.assertEqual(summary.deleted, 1)
        self.assertFalse(journal.is_done(URL))
        self.assertEqual(cooldowns, [])
        resumed = FakeDriver([URL], states={URL: GONE})
        resumed_summary = run([Item(URL)], resumed, journal,
                              Config(execute=True, delay=0), log=lambda *_: None,
                              sleeper=lambda *_: None)
        self.assertEqual(resumed_summary.already_gone, 1)
        self.assertEqual(resumed.trashed, [])
        self.assertTrue(journal.is_done(URL))

    def test_per_item_output_and_completion_have_timestamps(self):
        driver = FakeDriver([URL])
        with patch('gphotos_cleanup.delete_shared.time.strftime',
                   return_value='2026-10-30 13:45:00-0700'):
            summary, _, _, messages = self.run_items(driver, [Item(URL)])
        self.assertEqual(summary.deleted, 1)
        prefix = '[2026-10-30 13:45:00-0700] '
        self.assertTrue(all(line.startswith(prefix) for line in messages if line))
        self.assertTrue(any('[1/1]' in line for line in messages))
        self.assertTrue(any('deleted via' in line for line in messages))

    def test_multiline_errors_have_timestamp_on_every_line(self):
        messages = []
        with patch('gphotos_cleanup.delete_shared.time.strftime', return_value='LOCAL-TIME'):
            logger = timestamped_logger(messages.append)
            logger('Page crashed\nCall log:\n  navigating to a reviewed URL')
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(line.startswith('[LOCAL-TIME] ') for line in messages))


if __name__ == '__main__':
    unittest.main()
