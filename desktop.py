"""Omni Reports desktop application."""
import json
import os
from pathlib import Path
import queue
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
import omni

DATA = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OmniReports'
DATA.mkdir(parents=True, exist_ok=True)
omni.ROOT = DATA
omni.CONFIG = DATA / 'config.json'
ASSETS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
if getattr(sys, 'frozen', False):
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ASSETS / 'browsers')
NEXT = 'a[rel="next"], .pagination li.next a, .pagination a:has-text("Следующая"), .pagination a:has-text("»"), .pagination a:has-text("›")'


def sign_in(page, username, password):
    from urllib.parse import urlsplit
    page.goto(omni.URL, wait_until='networkidle', timeout=60000)
    pwd = page.locator('input[type=password]').filter(visible=True)
    if pwd.count() == 0 and omni.rows(page) is not None:
        return
    if pwd.count() != 1:
        raise RuntimeError('Не найдена форма входа. Используйте «Войти через браузер».')
    origin = urlsplit(page.url)
    if origin.scheme != 'https' or origin.hostname != 'omni.business-controller.one':
        raise RuntimeError('CRM открыла другой сайт. Используйте «Войти через браузер».')
    form = pwd.locator('xpath=ancestor::form[1]')
    if form.count() != 1:
        raise RuntimeError('Не найдена форма входа. Используйте «Войти через браузер».')
    name = form.locator('input:not([type]), input[type=text], input[type=email], input[type=tel]').filter(visible=True)
    if name.count() != 1:
        raise RuntimeError('Не удалось определить поле логина. Используйте «Войти через браузер».')
    if not username or not password:
        raise RuntimeError('Введите логин и пароль CRM или войдите через браузер.')
    name.fill(username)
    pwd.fill(password)
    submits = form.locator('button[type=submit], input[type=submit], button:not([type])').filter(visible=True)
    if submits.count() == 1:
        submits.click()
    else:
        pwd.press('Enter')
    try:
        pwd.wait_for(state='hidden', timeout=30000)
    except Exception:
        raise RuntimeError('CRM не подтвердила вход. Проверьте данные или войдите через браузер.') from None
    page.goto(omni.URL, wait_until='networkidle', timeout=60000)
    if omni.rows(page) is None:
        raise RuntimeError('Отчёты ОКК недоступны. Проверьте права или войдите через браузер.')


class Service:
    def __init__(self):
        self.cfg = json.loads(omni.CONFIG.read_text(encoding='utf-8-sig')) if omni.CONFIG.exists() else {}
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.state = {'phase': 'idle', 'message': 'Укажите данные подключения и запустите проверку.', 'busy': False,
                      'telegram': bool(self.cfg.get('chat_id')), 'crm': bool(self.cfg.get('url')),
                      'found': None, 'sent': None, 'last': None, 'token': self.cfg.get('token', '')}

    def post(self, message, phase=None, **values):
        update = dict(message=message, **values)
        if phase:
            update['phase'] = phase
        self.events.put(update)

    def task(self, fn):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.events.put({'busy': True})
        def worker():
            try:
                fn()
            except Exception as exc:
                detail = str(exc) if isinstance(exc, RuntimeError) else 'Не удалось завершить операцию. Проверьте подключение или войдите через браузер.'
                self.post(detail.replace('setup.cmd', '«Войти через браузер»').replace('update.cmd', 'обновление программы'), 'error')
            finally:
                if self.stop.is_set():
                    self.post('Мониторинг на паузе. История отправок сохранена.', 'paused')
                self.busy = False
                self.events.put({'busy': False})
        threading.Thread(target=worker, daemon=True).start()

    def telegram_connect(self, token):
        if token == self.cfg.get('token') and self.cfg.get('chat_id'):
            omni.telegram(token, 'getMe')
            return
        bot = omni.telegram(token, 'getMe')
        code = secrets.token_hex(12)
        url = f"https://t.me/{bot['username']}?start={code}"
        self.events.put({'open_url': url})
        self.post('Открылся ваш Telegram-бот. Нажмите в нём «Запустить» — подключение продолжится автоматически.', 'telegram', telegram_url=url)
        offset = 0
        deadline = time.monotonic() + 180
        while not self.stop.is_set() and time.monotonic() < deadline:
            updates = omni.telegram(token, 'getUpdates', offset=offset, timeout=0, limit=100)
            for update in updates:
                offset = max(offset, update['update_id'] + 1)
                msg = update.get('message', {})
                if msg.get('text') == '/start ' + code and msg.get('chat', {}).get('type') == 'private':
                    chat = msg['chat']['id']
                    omni.telegram(token, 'sendMessage', chat_id=chat, text='Omni: Telegram подключён. Здесь будут ваши отчёты ОКК.')
                    self.cfg.update(token=token, chat_id=chat)
                    omni.save_config(self.cfg)
                    self.post('Telegram подключён.', telegram=True, telegram_url=None)
                    return
            self.stop.wait(2)
        if not self.stop.is_set():
            raise RuntimeError('Не дождались запуска бота. Нажмите «Подключить и запустить» ещё раз.')

    def start(self, token, username, password):
        def action():
            nonlocal password
            try:
                self.post('Подключение к Telegram…', 'connecting')
                self.telegram_connect(token)
                if self.stop.is_set():
                    return
                from playwright.sync_api import sync_playwright
                self.post('Вход в Omni CRM…', 'connecting')
                with sync_playwright() as p:
                    with p.chromium.launch_persistent_context(str(DATA / 'profile'), headless=True) as context:
                        page = context.pages[0] if context.pages else context.new_page()
                        sign_in(page, username, password)
                        self.cfg.update(url=self.cfg.get('url', omni.URL), next_selector=self.cfg.get('next_selector', NEXT), lookback_days=1, interval_seconds=60)
                        omni.save_config(self.cfg)
            finally:
                password = ''
                self.events.put({'clear_password': True})
            self.post('CRM подключена. Проверка отчётов…', crm=True)
            self.monitor()
        self.task(action)

    def manual(self):
        def action():
            from playwright.sync_api import sync_playwright
            while not self.commands.empty():
                self.commands.get_nowait()
            with sync_playwright() as p:
                with p.chromium.launch_persistent_context(str(DATA / 'profile'), headless=False) as context:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(self.cfg.get('url', omni.URL), wait_until='domcontentloaded', timeout=60000)
                    self.post('Войдите в CRM, откройте отчёты ОКК, выберите фильтры. Затем нажмите «Сохранить отчёты» здесь.', 'manual')
                    while not self.stop.is_set():
                        page.wait_for_timeout(250)
                        try:
                            self.commands.get_nowait()
                        except queue.Empty:
                            continue
                        pages = [tab for tab in context.pages if not tab.is_closed()]
                        report = next((tab for tab in pages if omni.rows(tab) is not None), None)
                        if report is None:
                            self.post('Откройте таблицу с колонками «Заявка» и «Реклама», затем повторите.', 'manual')
                            continue
                        self.cfg.update(url=report.url, next_selector=self.cfg.get('next_selector', NEXT), lookback_days=1, interval_seconds=60)
                        found = omni.collect(report, self.cfg)
                        omni.save_config(self.cfg)
                        self.post('Фильтры сохранены. Нажмите кнопку запуска.', 'idle', crm=True, found=len(found))
                        return
        self.task(action)

    def monitor(self):
        from playwright.sync_api import sync_playwright
        with sqlite3.connect(DATA / 'sent.sqlite3') as db, sync_playwright() as p:
            db.execute('CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)')
            while not self.stop.is_set():
                try:
                    with p.chromium.launch_persistent_context(str(DATA / 'profile'), headless=True) as context:
                        page = context.pages[0] if context.pages else context.new_page()
                        while not self.stop.is_set():
                            self.post('Проверка отчётов за вчера и сегодня…', 'checking')
                            items = omni.collect(page, dict(self.cfg, lookback_days=1))
                            count = 0
                            for item in items:
                                if self.stop.is_set():
                                    break
                                if not db.execute('SELECT 1 FROM sent WHERE id=?', (item['Заявка'],)).fetchone():
                                    omni.deliver(db, [item], self.cfg, omni.telegram)
                                    count += 1
                            self.post('Проверка завершена. Следующая проверка через минуту.', 'running', found=len(items), sent=count, last=time.strftime('%H:%M'))
                            if self.stop.wait(60):
                                break
                except Exception as exc:
                    detail = str(exc) if isinstance(exc, RuntimeError) else 'Не удалось связаться с CRM.'
                    self.post(detail.replace('setup.cmd', '«Войти через браузер»') + ' Повторная попытка через минуту.', 'error')
                    self.stop.wait(60)
        self.post('Мониторинг на паузе. История отправок сохранена.', 'paused')

    def dispatch(self, payload):
        action = payload.get('action')
        if action == 'start':
            self.start(str(payload.get('token', '')).strip(), str(payload.get('login', '')).strip(), str(payload.get('password', '')))
        elif action == 'stop':
            self.stop.set()
            self.post('Ожидание завершения текущей проверки…', 'stopping')
        elif action == 'manual':
            self.manual()
        elif action == 'save':
            self.commands.put('save')
        elif action == 'folder':
            os.startfile(DATA)
        elif action == 'botfather':
            webbrowser.open('https://t.me/BotFather')
        elif action == 'open_telegram' and self.state.get('telegram_url'):
            webbrowser.open(self.state['telegram_url'])

    def drain(self, page):
        changed = False
        while not self.events.empty():
            update = self.events.get_nowait()
            url = update.pop('open_url', None)
            if url:
                webbrowser.open(url)
            if update.pop('clear_password', False):
                page.evaluate('document.getElementById("password").value = ""')
            self.state.update(update)
            changed = True
        if changed:
            page.evaluate('(state) => window.renderState(state)', self.state)


def ui_file():
    return (ASSETS / 'desktop_ui.html').as_uri()


def main():
    import msvcrt
    from playwright.sync_api import sync_playwright
    lock = open(DATA / 'desktop.lock', 'a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo('Omni', 'Программа уже запущена. Найдите её окно на панели задач.')
        root.destroy()
        return
    service = Service()
    try:
        with sync_playwright() as p:
            with p.chromium.launch_persistent_context(str(DATA / 'interface'), headless=False,
                    no_viewport=True,
                    args=['--app=' + ui_file(), '--window-size=1120,850'],
                    ignore_default_args=['--enable-automation']) as context:
                context.expose_binding('omniAction', lambda source, payload: service.dispatch(payload) if source['frame'] == source['page'].main_frame and source['page'].url == ui_file() else None)
                page = next((tab for tab in context.pages if tab.url == ui_file()), context.pages[0])
                if page.url != ui_file():
                    page.goto(ui_file())
                page.wait_for_function('typeof window.renderState === "function"')
                page.evaluate('(state) => window.renderState(state)', service.state)
                while not page.is_closed():
                    page.wait_for_timeout(100)
                    service.drain(page)
    except Exception as exc:
        if type(exc).__name__ != 'TargetClosedError':
            raise
    finally:
        service.stop.set()
        deadline = time.monotonic() + 90
        while service.busy and time.monotonic() < deadline:
            time.sleep(.1)
        lock.close()


def self_test():
    from playwright.sync_api import sync_playwright
    from datetime import datetime, timedelta
    with sync_playwright() as p:
        with p.chromium.launch(headless=True) as browser:
            page = browser.new_page(viewport={'width': 1120, 'height': 810})
            page.goto(ui_file())
            page.wait_for_function('typeof window.renderState === "function"')
            assert page.locator('input').count() == 3
            assert page.locator('#password').get_attribute('type') == 'password'
            page.locator('[data-help="token"]').click()
            page.wait_for_timeout(650)
            assert page.locator('#help-panel').get_attribute('aria-hidden') == 'false'
            page.locator('#help-close').click()
            page.wait_for_timeout(650)
            page.evaluate('(s) => window.renderState(s)', {'busy': True, 'phase': 'running', 'message': 'Проверка завершена', 'telegram': True, 'crm': True, 'found': 7, 'sent': 2, 'last': '12:30'})
            assert page.locator('#found-value').inner_text() == '7'
            assert page.locator('#start').is_disabled()
            page.screenshot(path=str(DATA / 'preview.png'))
            page.set_content('<table><tr><th>Заявка</th><th>Реклама</th></tr><tr><td>123</td><td>Авито</td></tr></table>')
            assert omni.matches(omni.rows(page))[0]['Заявка'] == '123'
        with p.chromium.launch(headless=False) as browser:
            page = browser.new_page()
            page.set_content('<input type="date"><input type="date"><button>Поиск</button>')
            omni.refresh_dates(page, {'lookback_days': 1})
            today = datetime.now().date()
            assert page.locator('input').nth(0).input_value() == (today - timedelta(days=1)).isoformat()
            assert page.locator('input').nth(1).input_value() == today.isoformat()
    assert omni.telegram_ssl_context().verify_mode != 0
    (DATA / 'self-test.json').write_text(json.dumps({'ok': True, 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': ['glass UI', 'three fields', 'animated help', 'live state', 'chromium headless', 'chromium visible', 'yesterday/today', 'CRM parsing', 'TLS']}), encoding='utf-8')


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        try:
            self_test()
        except Exception as exc:
            (DATA / 'self-test.json').write_text(json.dumps({'ok': False, 'error': type(exc).__name__}), encoding='utf-8')
            raise
    else:
        main()
