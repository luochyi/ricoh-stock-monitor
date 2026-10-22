import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import monitor
import watch_order


class WatchHealthTests(unittest.TestCase):
    def test_hidden_favorite_notifies_without_entering_purchase_flow(self):
        from test_favorites import FavoriteAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json';sent=[]
            monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf','favorites':{'enabled':True},
                'source':{'mode':'ricoh_refurbished_list','scope':'all'},'state_file':'stock.json'})
            api=FavoriteAPI();api.pages[1][0]['storeName']='官翻品 GR IIIx'
            api.details[130]['storeInfo']['storeName']='官翻品 GR IIIx'
            with patch('sys.argv',['watch','--config',str(config),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=1000), \
                 patch.object(watch_order.checkout,'API',return_value=api), \
                 patch.object(watch_order.checkout,'preflight'), \
                 patch.object(watch_order.checkout,'attempt') as purchase, \
                 patch.object(monitor,'source_fetch',return_value={'code':0,'data':[]}), \
                 patch.object(monitor,'send_dingtalk',side_effect=sent.append), \
                 patch.object(watch_order.time,'sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            purchase.assert_not_called()
            self.assertTrue(any('GR IIIx' in message and '12' in message and '未上架' in message for message in sent))
            self.assertTrue(any('收藏/关注详情' in message and '运行正常' in message for message in sent))

    def test_favorite_failure_does_not_block_listing_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json';sent=[]
            monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf','favorites':{'enabled':True},
                'source':{'mode':'ricoh_refurbished_list','scope':'all'},'state_file':'stock.json'})
            with patch('sys.argv',['watch','--config',str(config),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=1000), \
                 patch.object(watch_order.FavoritesWatch,'cycle',side_effect=monitor.MonitorError('收藏异常')), \
                 patch.object(watch_order.checkout,'preflight'), \
                 patch.object(monitor,'source_fetch',return_value={'code':0,'data':[{'id':130,'storeName':'官翻品 GR IV','stock':1}]}), \
                 patch.object(monitor,'send_dingtalk',side_effect=sent.append), \
                 patch.object(watch_order.time,'sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            self.assertTrue(any('首次发现有货' in message for message in sent))
            self.assertFalse(any('运行正常' in message for message in sent))

    def test_hdf_order_uses_actual_name_and_unlimited_price_in_notification(self):
        from test_checkout import FakeAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json';sent=[]
            monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf',
                'source':{'mode':'ricoh_refurbished_list','scope':'all'},'state_file':'stock.json'})
            name='官翻品 GR III HDF'
            data={'code':0,'data':[{'id':42,'storeName':name,'stock':1}]}
            api=FakeAPI();api.detail['storeInfo']['storeName']=name
            api.detail['productValue']['默认']['price']=20000
            api.preview['cartInfo'][0]['productInfo']['storeName']=name
            api.preview['cartInfo'][0]['truePrice']=20000
            api.preview['priceGroup']={'totalPrice':20010,'storePostage':10}
            with patch('sys.argv',['watch_order','--config',str(config),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=1000), \
                 patch.object(monitor,'source_fetch',return_value=data), \
                 patch.object(watch_order.checkout,'API',return_value=api), \
                 patch.object(monitor,'send_dingtalk',side_effect=sent.append), \
                 patch.object(watch_order.time,'sleep',side_effect=[None,KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            self.assertEqual(len(api.created),1)
            orders=[message for message in sent if '创建订单接口已返回成功' in message]
            self.assertEqual(len(orders),1)
            self.assertIn(name,orders[0]);self.assertIn('20010',orders[0])
            self.assertTrue(any('不设价格上限' in message for message in sent))

    def test_startup_login_failure_notifies_instead_of_silent_exit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json'
            p.write_text(json.dumps({'purchase_rule':'gr_iiix_or_hdf','source':{'mode':'ricoh_refurbished_list','scope':'all'},'state_file':'stock.json','interval_seconds':60}),encoding='utf-8')
            sent=[]
            with patch('sys.argv',['watch_order','--config',str(p),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=1000), \
                 patch.object(monitor,'source_fetch',return_value={'code':0,'data':[]}), \
                 patch.object(monitor,'cycle') as cycle, \
                 patch.object(watch_order.checkout,'preflight',side_effect=monitor.MonitorError('登录失效')), \
                 patch.object(monitor,'send_dingtalk',side_effect=sent.append), \
                 patch.object(watch_order.time,'sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            self.assertEqual(len(sent),1)
            self.assertIn('故障',sent[0])
            self.assertIn('登录失效',sent[0])
            self.assertNotIn('运行正常',sent[0])
            state=json.loads((Path(d)/'health-state.json').read_text(encoding='utf-8'))
            self.assertIsNone(state['last_report'])
            cycle.assert_called_once()

    def test_unmatched_model_is_not_purchased_and_monitor_continues_after_order(self):
        for phase in ('ready','submitted','submitting'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as d:
                root=Path(d); config=root/'config.json'
                monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf',
                    'source':{'mode':'ricoh_refurbished_list','scope':'all'},
                    'state_file':'stock.json','interval_seconds':60})
                monitor.save_state(root/'purchase-state.json',{'phase':phase,'total':'6749'})
                data={'code':0,'data':[{'id':130,'storeName':'官翻品 GR IV','stock':2}]}
                sent=[]
                with patch('sys.argv',['watch_order','--config',str(config),'--credentials','unused']), \
                     patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=1000), \
                     patch.object(monitor,'source_fetch',return_value=data), \
                     patch.object(watch_order.checkout,'preflight'), \
                     patch.object(watch_order.checkout,'attempt') as attempt, \
                     patch.object(monitor,'send_dingtalk',side_effect=sent.append), \
                     patch.object(watch_order.time,'sleep',side_effect=[None,KeyboardInterrupt]):
                    with self.assertRaises(KeyboardInterrupt):watch_order.main()
                attempt.assert_not_called()
                self.assertEqual(monitor.read_json(root/'stock.json')['items']['130']['stock'],2)
                self.assertEqual(monitor.read_json(root/'purchase-state.json')['phase'],phase)
                self.assertEqual(sum('首次发现有货' in text for text in sent),1)
                if phase!='ready':
                    self.assertTrue(any('停止自动下单' in text for text in sent))
                    self.assertFalse(any('下单登录、第一条地址和默认发票检查通过' in text for text in sent))

    def test_candidate_purchase_does_not_change_other_model_notification(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); config=root/'config.json'
            monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf',
                'source':{'mode':'ricoh_refurbished_list','scope':'all'},
                'state_file':'stock.json'})
            target={'id':2,'storeName':'官翻品 GR IIIx','stock':1}
            data={'code':0,'data':[{'id':1,'storeName':'官翻品 GR IV','stock':3},target]}
            sent=[]
            def purchase(api,candidate,path):
                result={'phase':'submitted','total':'6749'}
                monitor.save_state(path,result)
                return result
            with patch('sys.argv',['watch_order','--config',str(config),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=1000), \
                 patch.object(monitor,'source_fetch',return_value=data), \
                 patch.object(watch_order.checkout,'preflight'), \
                 patch.object(watch_order.checkout,'attempt',side_effect=purchase) as attempt, \
                 patch.object(monitor,'send_dingtalk',side_effect=sent.append), \
                 patch.object(watch_order.time,'sleep',side_effect=[None,KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            self.assertEqual(attempt.call_count,1)
            self.assertEqual(attempt.call_args.args[1],target)
            self.assertTrue(any('官翻品 GR IV' in text for text in sent))

if __name__=='__main__':unittest.main()
