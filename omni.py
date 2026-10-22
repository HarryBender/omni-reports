import argparse
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import secrets
import sqlite3
import socket
import ssl
import time
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
URL = 'https://omni.business-controller.one/orders/quality_reports?not_in_work=true&status=0'
CONFIG = ROOT / 'config.json'


def telegram_ssl_context():
    try:
        import truststore
        import certifi
    except ImportError:
        raise RuntimeError('Нужны SSL-зависимости: запустите update.cmd в папке скрипта') from None
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    return context


def telegram(token, method, **payload):
    token = str(token or '').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise RuntimeError('Неверный формат токена. В config.json нужен токен от BotFather вида 123456789:ABC..., без слова bot, пробелов и ссылки')
    context = telegram_ssl_context()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40, context=context) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Telegram HTTP {exc.code}; проверьте токен, chat_id и доступность бота') from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            detail = f'SSL: проверка сертификата не пройдена (код {getattr(reason, "verify_code", "неизвестен")}). Проверьте дату Windows и HTTPS-проверку антивируса/прокси'
        elif isinstance(reason, socket.gaierror):
            detail = 'DNS: Windows не смогла определить адрес api.telegram.org'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            detail = 'Истекло время подключения к api.telegram.org:443'
        else:
            detail = f'Сбой соединения ({type(reason).__name__}, код {getattr(reason, "winerror", None) or getattr(reason, "errno", None)})'
        raise RuntimeError(detail) from None
    except TimeoutError:
        raise RuntimeError('Telegram: истекло время ожидания ответа от api.telegram.org:443') from None
    except Exception as exc:
        raise RuntimeError(f'Telegram: ошибка {type(exc).__name__}') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram отклонил запрос')
    return result['result']


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')


def rows(page):
    return page.evaluate('''() => {
      const norm = s => s.replace(/\\s+/g, ' ').trim();
      for (const table of document.querySelectorAll('table')) {
        const trs = [...table.querySelectorAll('tr')];
        const hi = trs.findIndex(tr => {
          const h = [...tr.cells].map(c => norm(c.innerText));
          return h.includes('Заявка') && h.includes('Реклама');
        });
        if (hi < 0) continue;
        const headers = [...trs[hi].cells].map(c => norm(c.innerText));
        return trs.slice(hi+1).filter(tr => tr.cells.length >= headers.length).map(tr => {
          const obj = Object.fromEntries(headers.map((h,i) => [h, norm(tr.cells[i].innerText)]));
          const a = tr.cells[headers.indexOf('Заявка')].querySelector('a[href]');
          obj.link = a ? a.href : '';
          return obj;
        });
      }
      return null;
    }''')


def matches(items):
    return [r for r in items if re.fullmatch(r'\d+', r.get('Заявка', ''))
            and 'авито' in r.get('Реклама', '').casefold()]


def date_fields(page):
    return page.evaluate('''() => [...document.querySelectorAll('input')].map((e,i) => ({
      index:i, value:e.value, type:e.type
    })).filter(e => e.type === 'date' || /^\\d{2}\\s*\\.\\s*\\d{2}\\s*\\.\\s*\\d{4}$/.test(e.value)
       || /^\\d{4}-\\d{2}-\\d{2}$/.test(e.value))''')


def refresh_dates(page, cfg):
    fields = date_fields(page)
    if len(fields) != 2:
        raise RuntimeError('Не удалось однозначно найти два поля дат. Уведомления остановлены; запустите setup.cmd')
    today = datetime.now().date()
    for field, day in zip(fields, [today-timedelta(days=cfg.get('lookback_days', 1)), today]):
        value = day.isoformat() if field['type'] == 'date' or '-' in field['value'] else day.strftime('%d.%m.%Y')
        loc = page.locator('input').nth(field['index'])
        loc.fill(value)
        loc.dispatch_event('change')
    button = page.get_by_role('button', name='Поиск', exact=True)
    if button.count() != 1:
        raise RuntimeError('Не найдена единственная кнопка Поиск')
    button.click()
    page.wait_for_load_state('networkidle', timeout=30000)


def collect(page, cfg):
    page.goto(cfg['url'], wait_until='networkidle', timeout=60000)
    refresh_dates(page, cfg)
    result, signatures = {}, set()
    for _ in range(100):
        batch = rows(page)
        if batch is None:
            raise RuntimeError('Таблица заявок отсутствует. Возможно, закончилась сессия: запустите setup.cmd')
        signature = tuple(r.get('Заявка', '') for r in batch)
        if signature in signatures:
            raise RuntimeError('Пагинация не переключает заявки; нужна настройка next_selector')
        signatures.add(signature)
        for row in matches(batch):
            result[row['Заявка']] = row
        next_button = page.locator(cfg['next_selector'])
        enabled = []
        for i in range(next_button.count()):
            loc = next_button.nth(i)
            disabled = loc.evaluate("e => !!e.closest('.disabled,[aria-disabled=\"true\"]') || e.hasAttribute('disabled')")
            if loc.is_visible() and not disabled:
                enabled.append(loc)
        if not enabled:
            return list(result.values())
        if len(enabled) != 1:
            raise RuntimeError('Неоднозначная пагинация; нужна настройка next_selector')
        enabled[0].click()
        page.wait_for_load_state('networkidle', timeout=30000)
    raise RuntimeError('Более 100 страниц: проверка прервана')


def message(row, url):
    return (f"Авито — заявка №{row['Заявка']}\n"
            f"Создана: {row.get('Создан', '—')}\n"
            f"Отдел: {row.get('Отдел', '—')}\n"
            f"Реклама: {row['Реклама'][:2000]}\n"
            f"{row.get('link') or url}")[:4000]


def deliver(db, items, cfg, sender):
    for row in items:
        key = row['Заявка']
        if db.execute('SELECT 1 FROM sent WHERE id=?', (key,)).fetchone():
            continue
        sender(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'],
               text=message(row, cfg['url']), link_preview_options={'is_disabled': True})
        db.execute('INSERT INTO sent VALUES (?, ?)', (key, datetime.now().isoformat()))
        db.commit()
        logging.info('Отправлена заявка %s', key)
        time.sleep(1.1)


def setup(p):
    cfg = json.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else {}
    token = getpass.getpass('Токен Telegram-бота (ввод скрыт; Enter — сохранить текущий): ').strip() or cfg.get('token')
    if not token:
        raise RuntimeError('Нужен токен бота')
    bot = telegram(token, 'getMe')
    print(f"Бот: @{bot['username']}")
    if not cfg.get('chat_id'):
        code = '/start ' + secrets.token_hex(6)
        print(f'Отправьте боту в ЛИЧКУ точное сообщение: {code}')
        input('После отправки нажмите Enter: ')
        updates = telegram(token, 'getUpdates', timeout=0, limit=100)
        chats = {u['message']['chat']['id'] for u in updates
                 if u.get('message', {}).get('text') == code
                 and u['message']['chat']['type'] == 'private'}
        if len(chats) != 1:
            raise RuntimeError('Сообщение с кодом не найдено. Повторите setup.cmd. При webhook или другом получателе getUpdates укажите chat_id вручную в config.json')
        cfg['chat_id'] = chats.pop()
    cfg.update(token=token)
    cfg.setdefault('url', URL)
    cfg.setdefault('interval_seconds', 60)
    cfg.setdefault('lookback_days', 1)
    cfg.setdefault('next_selector', 'a[rel="next"], .pagination li.next a, .pagination a:has-text("Следующая"), .pagination a:has-text("»"), .pagination a:has-text("›")')
    with p.chromium.launch_persistent_context(str(ROOT/'profile'), headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg['url'], wait_until='domcontentloaded')
        print('Войдите в CRM. Откройте список заявок, выставьте нужные отделы/менеджера/статусы и нажмите Поиск.')
        input('Когда таблица появилась, нажмите Enter здесь: ')
        if rows(page) is None:
            raise RuntimeError('Таблица с колонками Заявка и Реклама не найдена')
        cfg['url'] = page.url
        save_config(cfg)
        found = collect(page, cfg)
        print(f'Проверка: найдено заявок с Авито: {len(found)}. Фильтры сохранены в URL.')
    telegram(token, 'sendMessage', chat_id=cfg['chat_id'], text='Omni: подключение настроено. Для мониторинга запустите start.cmd.')
    print('Готово. Запустите start.cmd. При первом запуске отправятся все найденные заявки.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['setup', 'run', 'check'])
    args = parser.parse_args()
    handler = RotatingFileHandler(ROOT/'monitor.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[handler, logging.StreamHandler()])
    # Windows releases this lock automatically if the process exits or crashes.
    import msvcrt
    lock = open(ROOT/'monitor.lock', 'a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise RuntimeError('Уже работает другой экземпляр. Остановите его перед настройкой') from None
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        if args.command == 'setup':
            setup(p)
            return
        if not CONFIG.exists():
            raise RuntimeError('Сначала запустите setup.cmd')
        cfg = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
        db = sqlite3.connect(ROOT/'sent.sqlite3')
        db.execute('CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)')
        last_alert = 0
        while True:
            try:
                with p.chromium.launch_persistent_context(str(ROOT/'profile'), headless=True) as context:
                    page = context.pages[0] if context.pages else context.new_page()
                    while True:
                        items = collect(page, cfg)
                        logging.info('Найдено Авито: %s', len(items))
                        if args.command == 'check':
                            for item in items:
                                print(message(item, cfg['url']))
                            return
                        deliver(db, items, cfg, telegram)
                        time.sleep(max(30, int(cfg['interval_seconds'])))
            except Exception as exc:
                # Do not print exception text that could contain the bot token.
                detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                logging.error('Проверка не завершена: %s', detail)
                if args.command == 'check':
                    raise RuntimeError(detail) from None
                if time.time()-last_alert > 3600:
                    try:
                        telegram(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'], text='Omni: ошибка проверки. Проверьте monitor.log на дедике; при потере входа запустите setup.cmd.')
                        last_alert = time.time()
                    except Exception:
                        pass
                time.sleep(60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Остановлено')
    except Exception as exc:
        print('Ошибка:', str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise SystemExit(1)
