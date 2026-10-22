import sqlite3
import unittest
import urllib.error
import socket
from unittest.mock import patch
import omni


class MonitorTests(unittest.TestCase):
    def test_invalid_token_does_not_leak(self):
        with self.assertRaises(RuntimeError) as error:
            omni.telegram('SECRET invalid token', 'getMe')
        self.assertNotIn('SECRET', str(error.exception))

    @patch('omni.telegram_ssl_context')
    @patch('omni.urllib.request.urlopen')
    def test_dns_error_is_identified_without_token(self, request, context):
        request.side_effect = urllib.error.URLError(socket.gaierror(11001, 'failed'))
        with self.assertRaises(RuntimeError) as error:
            omni.telegram('123:SECRET', 'getMe')
        self.assertIn('DNS', str(error.exception))
        self.assertNotIn('SECRET', str(error.exception))

    @patch('omni.telegram_ssl_context')
    @patch('omni.urllib.request.urlopen')
    def test_certificate_errors_are_not_bypassed(self, request, context):
        import ssl
        request.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate failed'))
        with self.assertRaisesRegex(RuntimeError, 'SSL'):
            omni.telegram('123:SECRET', 'getMe')
        self.assertEqual(request.call_count, 1)
        self.assertIs(request.call_args.kwargs['context'], context.return_value)

    def test_filter_uses_advertising_only(self):
        items = [
            {'Заявка': '1', 'Реклама': 'АВИТО партнер'},
            {'Заявка': '2', 'Реклама': 'Наклейка', 'Отдел': 'Авито'},
            {'Заявка': '3', 'Реклама': 'Авито Домсервис'},
            {'Заявка': 'Итого', 'Реклама': 'Авито'},
        ]
        self.assertEqual([r['Заявка'] for r in omni.matches(items)], ['1', '3'])

    @patch('omni.time.sleep')
    def test_delivery_deduplicates_and_retries_failures(self, _):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE sent (id TEXT PRIMARY KEY, sent_at TEXT)')
        cfg = {'token': 'test', 'chat_id': 1, 'url': 'https://example.com'}
        items = [{'Заявка': '123', 'Реклама': 'Авито'}]
        def fail(*args, **kwargs):
            raise RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            omni.deliver(db, items, cfg, fail)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sent').fetchone()[0], 0)
        calls = []
        omni.deliver(db, items, cfg, lambda *a, **kw: calls.append(kw))
        omni.deliver(db, items, cfg, lambda *a, **kw: calls.append(kw))
        self.assertEqual(len(calls), 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sent').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
