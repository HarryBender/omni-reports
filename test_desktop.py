"""Offline tests: no real Telegram or CRM requests."""
import tempfile
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
import desktop

REPORT = '<input type="date"><input type="date"><button>Поиск</button><table><tr><th>Заявка</th><th>Реклама</th></tr><tr><td>123</td><td>Авито</td></tr></table>'
LOGIN = '''<form onsubmit="event.preventDefault(); document.cookie='signed=yes;path=/'; location.reload()">
<input name="login"><input type="password"><button type="submit">Войти</button></form>'''


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1120, 'height': 810})
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def test_login_with_form_and_reuse_session(self):
        self.page.route('**/*', lambda route: route.fulfill(body=REPORT if 'signed=yes' in route.request.headers.get('cookie', '') else LOGIN, content_type='text/html; charset=utf-8'))
        desktop.sign_in(self.page, 'employee-test', 'offline-password')
        self.assertIsNotNone(desktop.omni.rows(self.page))
        desktop.sign_in(self.page, '', '')
        self.assertIsNotNone(desktop.omni.rows(self.page))

    def test_credentials_not_filled_on_another_origin(self):
        self.page.route('**/*', lambda route: route.fulfill(body=LOGIN if 'other.invalid' in route.request.url else '<script>location.href="https://other.invalid/login"</script>', content_type='text/html; charset=utf-8'))
        with self.assertRaisesRegex(RuntimeError, 'другой сайт'):
            desktop.sign_in(self.page, 'secret-name', 'secret-password')
        self.assertEqual(self.page.locator('input[type=password]').input_value(), '')

    def test_three_help_panels_keyboard_and_password_toggle(self):
        self.page.goto(desktop.ui_file())
        for key in ('token', 'login', 'password'):
            self.page.locator('[data-help="' + key + '"]').click()
            self.page.wait_for_timeout(560)
            self.assertEqual(self.page.locator('#help-panel').get_attribute('aria-hidden'), 'false')
            self.page.keyboard.press('Escape')
            self.page.wait_for_timeout(560)
            self.assertEqual(self.page.locator('#help-panel').get_attribute('aria-hidden'), 'true')
        self.page.locator('#password').fill('offline-only')
        self.page.locator('#toggle-password').click()
        self.assertEqual(self.page.locator('#password').get_attribute('type'), 'text')
        self.page.locator('#toggle-password').click()
        self.assertEqual(self.page.locator('#password').get_attribute('type'), 'password')

    def test_responsive_layout_and_bridge(self):
        actions = []
        self.page.expose_binding('omniAction', lambda source, data: actions.append(data))
        self.page.goto(desktop.ui_file())
        self.page.locator('#manual').click()
        self.page.wait_for_timeout(100)
        self.assertEqual(actions, [{'action': 'manual'}])
        for width in (1120, 930, 800, 390):
            self.page.set_viewport_size({'width': width, 'height': 810})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_app_window_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            context = self.p.chromium.launch_persistent_context(folder, headless=False,
                no_viewport=True,
                args=['--app=' + desktop.ui_file(), '--window-size=1120,850'],
                ignore_default_args=['--enable-automation'])
            try:
                context.expose_binding('omniAction', lambda source, data: None)
                page = next((tab for tab in context.pages if tab.url == desktop.ui_file()), context.pages[0])
                page.wait_for_function('typeof window.renderState === "function"')
                self.assertEqual(page.locator('input').count(), 3)
                page.screenshot(path=str(desktop.DATA / 'glass-preview.png'))
                page.locator('[data-help="token"]').click()
                page.wait_for_timeout(700)
                page.screenshot(path=str(desktop.DATA / 'glass-help.png'))
            finally:
                context.close()


if __name__ == '__main__':
    unittest.main()
