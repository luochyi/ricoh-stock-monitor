import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import monitor as m


CFG = {
    'source': {
        'items_path': 'data.items',
        'success': {'path': 'code', 'equals': 0},
        'fields': {'id': 'sku', 'name': 'title', 'stock': 'qty', 'url': 'url'},
    }
}


def payload(qty=0):
    return {'code': 0, 'data': {'items': [
        {'sku': 'A', 'title': '官翻相机', 'qty': qty, 'url': ''}
    ]}}


class MonitorTests(unittest.TestCase):
    def test_official_empty_to_products_is_restock(self):
        cfg = {'source': {'mode': 'ricoh_refurbished_list'}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 's.json'
            messages = []
            m.cycle(cfg, path, lambda: {'code': 0, 'data': []}, messages.append)
            self.assertEqual(messages, [])
            m.cycle(cfg, path, lambda: {'code': 0, 'data': [{'storeName': '官翻品 GR IIIx', 'stock': 1}]}, messages.append)
            self.assertEqual(len(messages), 1)
            self.assertIn('补货', messages[0])
            m.cycle(cfg, path, lambda: {'code': 0, 'data': [{'storeName': '官翻品 GR IIIx', 'stock': 1}]}, messages.append)
            self.assertEqual(len(messages), 1)

    def test_official_expired_signature_is_not_empty_stock(self):
        source = {'mode': 'ricoh_refurbished_list'}
        for response in [{'code': 904, 'data': None}, {'code': 902, 'data': None},
                         {'code': 0, 'data': None}, {'code': 0}, {'code': 0, 'data': {}}]:
            with self.subTest(response=response), self.assertRaises(m.MonitorError):
                m.normalize(response, source)

    def test_ricoh_signature_matches_original_client(self):
        url = 'https://shop.ricn-mall.com/app-api/product/products?couponId=&keyword=&limit=10&news=&page=1&sid=9&type='
        headers = m.ricoh_headers(url, timestamp='1700000000000', nonce='1700000000000abcd1234')
        self.assertEqual(headers['X-Request-Sign'], 'd3cbe7fca7e5a269e55f3e48faf527086676ffd6ab6449772106119b5e68fa11')
        self.assertEqual(headers['X-Request-Timestamp'], '1700000000000')
        self.assertNotEqual(m.ricoh_headers(url)['X-Request-Nonce'], m.ricoh_headers(url)['X-Request-Nonce'])

    def test_ricoh_signing_restricted_to_inventory(self):
        with self.assertRaises(m.MonitorError):
            m.ricoh_headers('https://example.com/app-api/product/products?sid=9')
        with self.assertRaises(m.MonitorError):
            m.ricoh_headers('https://shop.ricn-mall.com/app-api/orders/create')

    def test_target_requires_exact_model_and_real_stock(self):
        source = {'mode': 'ricoh_refurbished_list', 'target_name': '官翻品 GR IIIx'}
        for rows, expected in [([], False), ([{'storeName':'官翻品 GR IV','stock':5}],False),
                               ([{'storeName':'官翻品 GR IIIx','stock':0}],False),
                               ([{'storeName':'官翻品 GR IIIx','stock':1}],True)]:
            with self.subTest(rows=rows):
                self.assertEqual(next(iter(m.normalize({'code':0,'data':rows},source).values()))['stock'],expected)
        with self.assertRaises(m.MonitorError):
            m.normalize({'code':0,'data':[{'storeName':'官翻品 GR IIIx','stock':None}]},source)

    def test_stock_changes_and_no_duplicate(self):
        old = m.normalize(payload(), CFG['source'])
        now = m.normalize(payload(2), CFG['source'])
        self.assertEqual(m.changes(old, now)[0]['kind'], '补货')
        self.assertEqual(m.changes(now, now), [])
        self.assertEqual(m.changes(now, old)[0]['kind'], '售罄')
        self.assertEqual(m.changes(now, m.normalize(payload(3), CFG['source']))[0]['kind'], '库存变化')

    def test_new_product_and_missing_item(self):
        now = m.normalize(payload(2), CFG['source'])
        self.assertEqual(m.changes({}, now)[0]['kind'], '新品上架')
        self.assertEqual(m.changes(now, {}), [])

    def test_invalid_responses_never_become_zero(self):
        bad = [{'code': 401, 'data': {'items': []}}, {'code': 0},
               {'code': 0, 'data': {'items': []}}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(m.MonitorError):
                m.normalize(value, CFG['source'])
        for qty in (None, '', -1, 1.5, True, 'unknown'):
            with self.subTest(qty=qty), self.assertRaises(m.MonitorError):
                m.normalize(payload(qty), CFG['source'])

    def test_duplicate_sku_rejected(self):
        data = payload()
        data['data']['items'] *= 2
        with self.assertRaises(m.MonitorError):
            m.normalize(data, CFG['source'])

    def test_boolean_stock(self):
        source = dict(CFG['source'], stock_mode='available')
        self.assertTrue(m.normalize(payload(True), source)['A']['stock'])
        with self.assertRaises(m.MonitorError):
            m.normalize(payload(2), source)

    def test_notification_failure_keeps_pending_and_retry_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            m.cycle(CFG, path, lambda: payload(), lambda text: None)
            def fail(text):
                raise m.MonitorError('模拟发送失败')
            with self.assertRaises(m.MonitorError):
                m.cycle(CFG, path, lambda: payload(2), fail)
            self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['pending']), 1)
            received = []
            m.cycle(CFG, path, lambda: payload(2), received.append)
            m.cycle(CFG, path, lambda: payload(2), received.append)
            self.assertEqual(len(received), 1)
            self.assertIn('补货', received[0])

    def test_invalid_fetch_preserves_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            m.cycle(CFG, path, lambda: payload(2), lambda text: None)
            before = path.read_bytes()
            with self.assertRaises(m.MonitorError):
                m.cycle(CFG, path, lambda: {'code': 401}, lambda text: None)
            self.assertEqual(path.read_bytes(), before)

    def test_first_run_available_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            received = []
            m.cycle(CFG, Path(tmp) / 's.json', lambda: payload(2), received.append)
            self.assertEqual(len(received), 1)
            self.assertIn('首次发现有货', received[0])

    def test_bad_state_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 's.json'
            path.write_text('{}', encoding='utf-8')
            with self.assertRaises(m.MonitorError):
                m.cycle(CFG, path, lambda: payload(), lambda text: None)

    def test_signature_vector(self):
        from urllib.parse import parse_qs, urlsplit
        url = m.signed_url('https://oapi.dingtalk.com/robot/send?access_token=x', 'SECabc', 1700000000000)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['timestamp'], ['1700000000000'])
        self.assertEqual(query['access_token'], ['x'])
        # 固定输入的签名应随密钥改变，且保留 Base64 字符。
        self.assertEqual(len(query['sign'][0]), 44)
        self.assertNotEqual(url, m.signed_url('https://oapi.dingtalk.com/robot/send?access_token=x', 'SECxyz', 1700000000000))


class HTTPTests(unittest.TestCase):
    @patch('monitor.require_active')
    def test_json_fetch_and_dingtalk_business_error(self, allowed):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload(3)).encode('utf-8'))
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"errcode":0}' if self.path == '/ok' else b'{"errcode":310000}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:%s/' % server.server_port
            self.assertEqual(m.request_json(url)['data']['items'][0]['qty'], 3)
            # 只替换外部地址，实际执行 HTTP 请求和响应校验。
            with patch.object(m, 'signed_url', return_value=url), patch.dict('os.environ', {
                'DINGTALK_WEBHOOK': 'https://oapi.dingtalk.com/robot/send?access_token=test'
            }):
                with self.assertRaises(m.MonitorError):
                    m.send_dingtalk('理光库存测试')
            self.assertEqual(captured[0]['text']['content'], '理光库存测试')
            with patch.object(m, 'signed_url', return_value=url+'ok'), patch.object(m.time, 'sleep'), patch.dict('os.environ', {
                'DINGTALK_WEBHOOK': 'https://oapi.dingtalk.com/robot/send?access_token=test'
            }):
                m.send_dingtalk('理光库存补货模拟验证')
            self.assertEqual(captured[1]['text']['content'], '理光库存补货模拟验证')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
