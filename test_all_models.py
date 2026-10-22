import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import monitor
import checkout


SOURCE = {'mode': 'ricoh_refurbished_list', 'scope': 'all',
          'complete_inventory_confirmed': True,
          'url': 'https://shop.ricn-mall.com/app-api/product/products?keyword=&limit=2&page=1&sid=9'}


def row(product_id, name, stock):
    return {'id': product_id, 'storeName': name, 'stock': stock}


class AllModelsTests(unittest.TestCase):
    def test_legacy_state_migrates_without_dropping_pending_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'; messages = []
            monitor.save_state(path, {'version':1,'initialized':True,'pending':['旧待发消息'],
                'items':{'ricoh-refurbished-sid9':{'name':'官翻品 GR IIIx','stock':False,'url':''}}})
            data = {'code':0,'data':[row(130,'官翻品 GR IV',1)]}
            monitor.cycle({'source':SOURCE},path,lambda:data,messages.append)
            self.assertEqual(messages[0],'旧待发消息')
            self.assertIn('首次发现有货',messages[1])
            self.assertEqual(list(monitor.read_json(path)['items']),['130'])

    def test_other_model_restock_notifies_but_is_never_purchased(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            messages = []
            for stock in (0, 2, 2):
                data = {'code': 0, 'data': [row(130, '官翻品 GR IV', stock)]}
                monitor.cycle({'source': SOURCE}, path, lambda: data, messages.append)
                self.assertIsNone(checkout.find_candidate(data))
            self.assertEqual(len(messages), 1)
            self.assertIn('官翻品 GR IV', messages[0])
            self.assertIn('补货', messages[0])
            self.assertIn('0 → 2', messages[0])

    def test_reappearance_notifies_without_inventing_zero_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'; messages = []
            data = {'code': 0, 'data': [row(130, '官翻品 GR IV', 1)]}
            monitor.cycle({'source': SOURCE}, path, lambda: data, messages.append)
            monitor.cycle({'source': SOURCE}, path, lambda: {'code': 0, 'data': []}, messages.append)
            state = monitor.read_json(path)
            self.assertEqual(state['items']['130']['stock'], 1)
            monitor.cycle({'source': SOURCE}, path, lambda: data, messages.append)
            self.assertEqual(len(messages), 2)
            self.assertIn('重新上架', messages[-1])

    def test_pagination_fetches_every_page_and_signs_each_url(self):
        first = [row(1, '官翻品 GR III', 0), row(2, checkout.TARGET, 1)]
        with patch.object(monitor, 'request_json', side_effect=[
                {'code': 0, 'data': first},
                {'code': 0, 'data': [row(3, '官翻品 GR IV', 2)]},
                {'code': 0, 'data': []}]) as fetch:
            result = monitor.source_fetch({'source': SOURCE})
        self.assertEqual(len(result['data']), 3)
        self.assertEqual(checkout.find_candidate(result)['id'], 2)
        for index, call in enumerate(fetch.call_args_list, 1):
            url, method, body, headers = call.args
            self.assertEqual(parse_qs(urlsplit(url).query)['page'], [str(index)])
            self.assertEqual(headers, monitor.ricoh_headers(
                url, headers['X-Request-Timestamp'], headers['X-Request-Nonce']))

    def test_bad_or_repeated_later_page_rejects_partial_inventory(self):
        first = {'code': 0, 'data': [row(1, '官翻品 GR III', 0), row(2, checkout.TARGET, 1)]}
        for second in ({'code': 401, 'data': []}, first):
            with patch.object(monitor, 'request_json', side_effect=[first, second]):
                with self.assertRaises(monitor.MonitorError):
                    monitor.source_fetch({'source': SOURCE})

    def test_all_scope_rejects_filtered_url_and_invalid_stock(self):
        with self.assertRaises(monitor.MonitorError):
            monitor.source_fetch({'source': dict(SOURCE, url=SOURCE['url'].replace('keyword=', 'keyword=GR'))})
        for invalid in (None, True, -1):
            with self.assertRaises(monitor.MonitorError):
                monitor.normalize({'code': 0, 'data': [row(1, '官翻品 GR IV', invalid)]}, SOURCE)


if __name__ == '__main__':
    unittest.main()
