import unittest
from types import SimpleNamespace

from gphotos_cleanup.delete_browser import PlaywrightDriver, ROUTE_TOOLBAR, ROUTE_SHORTCUT
from gphotos_cleanup.delete_cli import build_parser
from gphotos_cleanup.delete_shared import GuardrailViolation, NoDialogError, StackedDialogError


class BackgroundActionTests(unittest.TestCase):
    def driver(self, background=True):
        driver = PlaywrightDriver.__new__(PlaywrightDriver)
        driver._background = background
        driver.keys = []
        driver.activations = []
        driver.clicks = []
        driver.answers = []
        driver._page = SimpleNamespace(
            url='https://photos.google.com/photo/AF1QipFAKEBACKGROUND',
            keyboard=SimpleNamespace(press=driver.keys.append),
            bring_to_front=lambda: driver.activations.append(True),
        )
        driver.control = SimpleNamespace(click=lambda **_: driver.clicks.append('toolbar'))
        driver.scope = object()
        driver._toolbar_trash_control = lambda: (driver.control, 'Move to trash')
        driver._wait_for_dialog = lambda: driver.scope
        driver._answer_dialog = lambda scope: driver.answers.append(scope) or ('ordinary', 'Move to trash')
        driver._wait_for_trashed_toast = lambda: 'Moved to trash'
        driver._any_selector_present = lambda *_: False
        driver._page_has_crashed = lambda *_: False
        return driver

    def test_background_uses_toolbar_and_shared_confirmation_without_focus(self):
        driver = self.driver()
        outcome = driver.trash_current()
        self.assertEqual(driver.activations, [])
        self.assertEqual(driver.keys, [])
        self.assertEqual(driver.clicks, ['toolbar'])
        self.assertEqual(driver.answers, [driver.scope])
        self.assertEqual(outcome.route, ROUTE_TOOLBAR)
        self.assertFalse(outcome.shortcut_attempted)
        self.assertTrue(outcome.toast_seen)

    def test_background_never_falls_back_to_keyboard_when_toolbar_is_missing(self):
        driver = self.driver()
        driver._toolbar_trash_control = lambda: (None, '')
        with self.assertRaises(NoDialogError):
            driver.trash_current()
        self.assertEqual(driver.keys, [])
        self.assertEqual(driver.activations, [])
        self.assertEqual(driver.clicks, [])

    def test_background_rejects_unsafe_toolbar_option(self):
        driver = self.driver()
        driver._toolbar_trash_control = lambda: (driver.control, 'All 8 photos')
        with self.assertRaises(GuardrailViolation):
            driver.trash_current()
        self.assertEqual(driver.clicks, [])
        self.assertEqual(driver.activations, [])

    def test_unknown_background_dialog_never_sends_escape(self):
        driver = self.driver()
        driver._dialog_option_labels = lambda *_: ['All 8 photos', 'New unfamiliar option']
        driver._dialog_diagnostics = lambda *_: 'synthetic diagnostics'
        driver._answer_dialog = lambda scope: PlaywrightDriver._answer_dialog(driver, scope)
        with self.assertRaises(StackedDialogError):
            driver.trash_current()
        self.assertEqual(driver.keys, [])
        self.assertEqual(driver.activations, [])

    def test_foreground_compatibility_keeps_shortcut_behavior(self):
        driver = self.driver(background=False)
        outcome = driver.trash_current()
        self.assertEqual(driver.activations, [True])
        self.assertEqual(driver.keys, ['#'])
        self.assertEqual(driver.clicks, [])
        self.assertEqual(outcome.route, ROUTE_SHORTCUT)
        self.assertTrue(outcome.shortcut_attempted)

    def test_cli_defaults_to_background_and_foreground_is_opt_in(self):
        parser = build_parser()
        self.assertTrue(parser.parse_args([]).background)
        self.assertTrue(parser.parse_args(['--background']).background)
        self.assertFalse(parser.parse_args(['--foreground']).background)


class BackgroundTargetTests(unittest.TestCase):
    def test_created_target_is_verified_without_touching_a_racing_user_tab(self):
        driver = PlaywrightDriver.__new__(PlaywrightDriver)
        driver._background = True
        driver._background_sessions = {}
        driver._nav_timeout_ms = 1000
        foreign, owned = object(), object()
        calls, detached = [], []

        class Session:
            def __init__(self, target=None):
                self.target = target

            def send(self, method, params=None):
                calls.append((self.target, method, params))
                if method == 'Target.createTarget':
                    return {'targetId': 'owned-target'}
                if method == 'Target.getTargetInfo':
                    return {'targetInfo': {'targetId': self.target}}
                return {}

            def detach(self):
                detached.append(self.target)

        class Pending:
            value = foreign

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        driver._browser = SimpleNamespace(new_browser_cdp_session=lambda: Session())
        driver._context = SimpleNamespace(
            expect_page=lambda **_: Pending(), pages=[foreign, owned],
            new_cdp_session=lambda page: Session('owned-target' if page is owned else 'user-target'),
        )
        page = driver._new_automation_page()
        self.assertIs(page, owned)
        self.assertIn((None, 'Target.createTarget', {'url': 'about:blank', 'background': True}), calls)
        self.assertIn(('owned-target', 'Emulation.setFocusEmulationEnabled', {'enabled': True}), calls)
        self.assertNotIn(('user-target', 'Emulation.setFocusEmulationEnabled', {'enabled': True}), calls)
        self.assertIn('user-target', detached)
        self.assertNotIn('owned-target', detached)
        self.assertEqual(set(driver._background_sessions), {owned})


if __name__ == '__main__':
    unittest.main()
