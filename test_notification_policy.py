"""普通 GR4 静默策略覆盖列表、收藏和重试队列。"""
import tempfile
import unittest
from pathlib import Path
import monitor
import favorites
from test_all_models import SOURCE, row
from test_favorites import FavoriteAPI

POLICY = {'mute_plain_gr4': True}
CONFIG = {'source': SOURCE, 'notification_policy': POLICY}


def event(name, sku='130'):
    return {'name': name, 'id': sku, 'kind': '补货', 'before': 0, 'stock': 1, 'url': ''}


class NotificationPolicyTests(unittest.TestCase):
    def test_model_aliases_keep_hdf_and_other_models(self):
        for name in ('官翻品 GR IV', '官翻品 gr4', '官翻品 ＧＲ Ⅳ',
                     '[收藏/关注] 官翻品 GR IV（规格：默认；未上架）'):
            with self.subTest(name=name):
                self.assertFalse(monitor.stock_notification_enabled(name, POLICY))
        for name in ('官翻品 GR IV HDF', '官翻品 GR4hdf', '官翻品 GR Ⅳ ＨＤＦ',
                     '官翻品 GR IIIx', '官翻品 GR III HDF', '官翻品 GR III'):
            with self.subTest(name=name):
                self.assertTrue(monitor.stock_notification_enabled(name, POLICY))
        self.assertTrue(monitor.stock_notification_enabled('官翻品 GR IV', {}))

    def test_list_silences_all_plain_gr4_changes_but_keeps_stock_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'; sent = []
            for stock in (1, 0, 2, 3, 0):
                data = {'code': 0, 'data': [row(130, '官翻品 GR IV', stock),
                    row(140, '官翻品 GR IV HDF', stock)]}
                monitor.cycle(CONFIG, path, lambda: data, sent.append)
                self.assertEqual(monitor.read_json(path)['items']['130']['stock'], stock)
            self.assertEqual(len(sent), 5)
            self.assertTrue(all('GR IV HDF' in text and 'SKU：130' not in text for text in sent))
            monitor.cycle(CONFIG, path, lambda: {'code': 0, 'data': []}, sent.append)
            monitor.cycle(CONFIG, path, lambda: data, sent.append)
            self.assertEqual(len(sent), 6)
            self.assertNotIn('SKU：130', sent[-1])

    def test_mixed_pending_keeps_hdf_and_other_models_even_after_send_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            monitor.save_state(path, {'version': 1, 'initialized': True, 'items': {},
                'pending': [monitor.format_message([event('官翻品 GR IV'),
                    event('官翻品 GR IV HDF', '140'), event('官翻品 GR IIIx', '65')])]})
            def fail(text):
                self.assertNotIn('SKU：130', text)
                raise monitor.MonitorError('测试发送失败')
            with self.assertRaises(monitor.MonitorError):
                monitor.cycle(CONFIG, path, lambda: {'code': 0, 'data': []}, fail)
            self.assertNotIn('SKU：130', monitor.read_json(path)['pending'][0])
            sent = []
            monitor.cycle(CONFIG, path, lambda: {'code': 0, 'data': []}, sent.append)
            self.assertEqual(len(sent), 1)
            self.assertIn('GR IV HDF', sent[0]); self.assertIn('GR IIIx', sent[0])
            self.assertEqual(monitor.read_json(path)['pending'], [])

    def test_plain_only_pending_dropped_before_fetch_and_other_messages_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'; sent = []
            monitor.save_state(path, {'version': 1, 'initialized': True, 'items': {},
                'pending': [monitor.format_message([event('官翻品 GR IV')]), '旧待发消息']})
            def fail(): raise monitor.MonitorError('测试查询失败')
            with self.assertRaises(monitor.MonitorError):
                monitor.cycle(CONFIG, path, fail, sent.append)
            self.assertEqual(sent, ['旧待发消息'])
            self.assertEqual(monitor.read_json(path)['pending'], [])

    def test_favorite_policy_applies_without_affecting_hdf(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FavoriteAPI(); sent = []
            watch = favorites.FavoritesWatch(api, Path(directory), {}, notification_policy=POLICY)
            watch.cycle(sent.append, 1000)
            self.assertEqual(sent, [])
            self.assertEqual(monitor.read_json(watch.state_path)['items']['130:sku130']['stock'], 12)
            api.details[130]['storeInfo']['storeName'] = '官翻品 GR IV HDF'
            api.details[130]['productValue']['默认']['stock'] = 13
            watch.cycle(sent.append, 1030)
            self.assertEqual(len(sent), 1); self.assertIn('GR IV HDF', sent[0])


if __name__ == '__main__':
    unittest.main()
