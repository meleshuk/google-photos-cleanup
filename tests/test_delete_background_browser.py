"""Opt-in real CDP check using a fresh headless Chrome and synthetic Photos UI.

Run with GPHOTOS_BROWSER_TESTS=1. Optionally set GPHOTOS_TEST_CDP_URL to reuse
an existing browser; only owned synthetic tabs are changed, never real Photos.
"""

import os
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from gphotos_cleanup.delete_browser import PlaywrightDriver, PRESENT, ROUTE_TOOLBAR


URL = 'https://photos.google.com/photo/AF1QipFAKEBACKGROUNDTEST'
HTML = '''<!doctype html><meta charset="utf-8"><body data-gphotos-fixture="synthetic">
<button aria-label="Move to trash" onclick="document.getElementById('confirmation').style.display='block'">Trash</button>
<div id="confirmation" role="dialog" style="display:none">
<h2>Remove from your Google Account, devices with backup turned on, and places shared within Google Photos?</h2>
<button onclick="document.getElementById('confirmation').style.display='none'">Cancel</button>
<button onclick="window.trashCalls=(window.trashCalls||0)+1;document.body.innerHTML='<p>Moved to trash</p><button aria-label=Undo>Undo</button>'">Move to trash</button>
</div></body>'''


@unittest.skipUnless(os.environ.get('GPHOTOS_BROWSER_TESTS') == '1', 'opt-in Chrome integration test')
class BackgroundBrowserTests(unittest.TestCase):
    def exercise(self, cdp_url):
        factory = PlaywrightDriver._new_automation_page
        original = {}

        def checked_factory(driver):
            if not original:
                if not driver._context.pages:
                    self.skipTest('no existing browser window for background-target test')
                original['page'] = driver._context.pages[0]
                original['visibility'] = original['page'].evaluate('document.visibilityState')
            page = factory(driver)
            self.assertEqual(original['page'].evaluate('document.visibilityState'),
                             original['visibility'])
            return page

        with patch.object(PlaywrightDriver, '_new_automation_page', checked_factory):
            driver = PlaywrightDriver(cdp_url, [URL], settle_ms=0,
                                      post_action_settle_ms=0, background=True,
                                      log=lambda *_: None)
            print('Background target created without changing the existing tab.', flush=True)
            self.exercise_actions(driver, original, cdp_url)

    def exercise_actions(self, driver, original, cdp_url):
        other = original['page']
        original_visibility = original['visibility']
        peer = driver._context.new_cdp_session(other)
        original_target = peer.send('Target.getTargetInfo')['targetInfo']['targetId']
        peer.detach()
        try:
            # Render a local fixture in the owned blank tab. This never requests
            # Photos, touches cookies, or relies on routing an authenticated site.
            driver._page.set_content(HTML)
            self.assertEqual(driver._page.evaluate('document.body.dataset.gphotosFixture'), 'synthetic')
            print('Synthetic page loaded in background.', flush=True)
            self.assertEqual(other.evaluate('document.visibilityState'), original_visibility)
            self.assertTrue(driver._page.evaluate('document.hasFocus()'))
            # Only the fixture's location differs. Input/navigation guardrails
            # remain covered by the separate recovery and guardrail suites.
            with patch('gphotos_cleanup.delete_browser.PHOTO_PREFIX', 'about:blank'):
                outcome = driver.trash_current()
            print('Synthetic outcome:', outcome.note, 'dialog_answered=', outcome.dialog_answered,
                  'trash_calls=', driver._page.evaluate('window.trashCalls'), flush=True)
            self.assertTrue(outcome.toast_seen)
            self.assertEqual(outcome.route, ROUTE_TOOLBAR)
            self.assertFalse(outcome.shortcut_attempted)
            self.assertEqual(driver._page.evaluate('window.trashCalls'), 1)
            print('Guarded toolbar action verified without foreground activation.', flush=True)
            self.assertEqual(other.evaluate('document.visibilityState'), original_visibility)
            driver._recover_crashed_tab()
            print('Background replacement created.', flush=True)
            driver._page.set_content(HTML)
            self.assertEqual(other.evaluate('document.visibilityState'), original_visibility)
            self.assertFalse(other.is_closed())
        finally:
            print('Closing owned test tab.', flush=True)
            driver.close()
        with urlopen(cdp_url.rstrip('/') + '/json/list', timeout=3) as response:
            remaining_targets = {target['id'] for target in json.load(response)}
        self.assertIn(original_target, remaining_targets)

    def test_background_creation_clicks_and_recovery_keep_other_tab_visible(self):
        existing = os.environ.get('GPHOTOS_TEST_CDP_URL')
        if existing:
            self.exercise(existing)
            return
        chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        if not chrome.exists():
            self.skipTest('Chrome executable unavailable')
        with tempfile.TemporaryDirectory(prefix='gphotos-cdp-test-') as directory:
            profile = Path(directory) / 'profile'
            stderr_path = Path(directory) / 'chrome.stderr'
            stderr_file = stderr_path.open('wb')
            arguments = [
                str(chrome), '--headless=new', '--remote-debugging-port=0',
                '--user-data-dir=' + str(profile), '--no-first-run',
                '--no-default-browser-check', '--disable-background-networking',
                '--disable-extensions', '--disable-sync', 'about:blank',
            ]
            process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=stderr_file)
            try:
                port_file = profile / 'DevToolsActivePort'
                deadline = time.monotonic() + 20
                while not port_file.exists():
                    exited = process.poll() is not None
                    if exited or time.monotonic() > deadline:
                        self.fail('isolated headless Chrome did not start (exit %s): %s'
                                  % (process.poll(), stderr_path.read_text(errors='replace')[:3000]))
                    time.sleep(0.05)
                port = port_file.read_text().splitlines()[0]
                cdp_url = 'http://127.0.0.1:' + port
                self.exercise(cdp_url)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                stderr_file.close()


if __name__ == '__main__':
    unittest.main()
