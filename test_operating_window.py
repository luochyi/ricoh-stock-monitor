from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import monitor
import watch_order
from health import Health
from operating_window import QuietHours
from test_heartbeat_window import stamp


class OperatingWindowTests(unittest.TestCase):
    def test_unsent_order_at_boundary_can_be_rechecked_next_day(self):
        import checkout
        from test_checkout import FakeAPI
        class BoundaryAPI(FakeAPI):
            def call(self,path,body=None):
                if path.startswith('/order/create/'):
                    raise QuietHours('已到 18:00，尚未发送请求')
                return super().call(path,body)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'order.json';api=BoundaryAPI()
            candidate={'id':42,'storeName':checkout.TARGET,'stock':1}
            with self.assertRaises(QuietHours):checkout.attempt(api,candidate,path)
            self.assertEqual(api.created,[])
            self.assertEqual(monitor.read_json(path)['phase'],'ready')
            next_day=FakeAPI();checkout.attempt(next_day,candidate,path)
            self.assertEqual(len(next_day.created),1)

    def test_watch_resumes_at_eight_after_night_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json'
            monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf',
                'source':{'mode':'ricoh_refurbished_list','scope':'all'},'state_file':'stock.json'})
            clock=[stamp(17,7,59,59)]
            def sleep(seconds):
                if clock[0]<stamp(17,8):clock[0]=stamp(17,8)
                else:raise KeyboardInterrupt()
            with patch('sys.argv',['watch','--config',str(config),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',side_effect=lambda:clock[0]), \
                 patch.object(watch_order.time,'sleep',side_effect=sleep), \
                 patch.object(monitor,'source_fetch',return_value={'code':0,'data':[]}) as fetch, \
                 patch.object(watch_order.checkout,'preflight'), \
                 patch.object(monitor,'send_dingtalk') as send:
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            fetch.assert_called_once();send.assert_called_once()
            self.assertIn('运行正常',send.call_args.args[0])

    def test_night_loop_does_not_query_order_or_notify(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json'
            monitor.save_state(config,{'purchase_rule':'gr_iiix_or_hdf','favorites':{'enabled':True},
                'source':{'mode':'ricoh_refurbished_list','scope':'all'},'state_file':'stock.json'})
            with patch('sys.argv',['watch','--config',str(config),'--credentials','unused']), \
                 patch.object(monitor,'webhook_config'), \
                 patch.object(watch_order.time,'time',return_value=stamp(16,20)), \
                 patch.object(monitor,'source_fetch',return_value={'code':0,'data':[]}) as fetch, \
                 patch.object(monitor,'cycle'), \
                 patch.object(watch_order.FavoritesWatch,'cycle') as favorite_cycle, \
                 patch.object(watch_order.checkout,'preflight') as preflight, \
                 patch.object(watch_order.checkout,'attempt') as order, \
                 patch.object(monitor,'send_dingtalk') as send, \
                 patch.object(watch_order.time,'sleep',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):watch_order.main()
            fetch.assert_not_called();preflight.assert_not_called()
            order.assert_not_called();send.assert_not_called();favorite_cycle.assert_not_called()

    def test_all_network_requests_blocked_at_eighteen(self):
        urls=['https://shop.ricn-mall.com/app-api/product/products',
              'https://shop.ricn-mall.com/app-api/order/create/key',
              'https://oapi.dingtalk.com/robot/send?access_token=test']
        for url in urls:
            with self.subTest(url=url), \
                 patch.object(watch_order.time,'time',return_value=stamp(16,18)), \
                 patch.object(monitor.urllib.request,'build_opener') as opener:
                with self.assertRaises(QuietHours):monitor.request_json(url)
                opener.assert_not_called()

    def test_night_fault_and_recovery_queue_is_not_delivered(self):
        for kind in ['heartbeat','fault','recovery']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                h=Health(Path(directory)/'health.json');sent=[]
                h.state['pending']={'kind':kind,'message':'昨日状态'};h.save()
                h.flush(sent.append,stamp(16,20))
                self.assertEqual(sent,[])
                self.assertIsNone(h.state['pending'])

    def test_overnight_restart_is_not_reported_as_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'health.json';h=Health(path);sent=[]
            h.success(stamp(16,17,59),False);h.flush(sent.append,stamp(16,17,59))
            h=Health(path)
            h.success(stamp(17,8),False);h.flush(sent.append,stamp(17,8))
            self.assertEqual(len(sent),2)
            self.assertIn('运行正常',sent[-1]);self.assertNotIn('中断',sent[-1])


if __name__=='__main__':unittest.main()
