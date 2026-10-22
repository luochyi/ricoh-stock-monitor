import tempfile
import unittest
from pathlib import Path
from health import Health


class HealthTests(unittest.TestCase):
    def test_restart_does_not_send_early_report(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'health.json';sent=[]
            h=Health(path);h.success(1000,False);h.flush(sent.append,1000)
            self.assertEqual(len(sent),1)
            h=Health(path);h.success(1060,False);h.flush(sent.append,1060)
            self.assertEqual(len(sent),1)

    def test_continuous_success_reports_only_each_hour(self):
        with tempfile.TemporaryDirectory() as d:
            sent=[];h=Health(Path(d)/'s.json')
            for now in range(1000,4601,60):
                h.success(now,False);h.flush(sent.append,now)
                if now==2800:h=Health(Path(d)/'s.json')
            self.assertEqual(len(sent),2)
            self.assertIn('运行正常',sent[-1])
            self.assertIn('无货',sent[-1])

    def test_failures_alert_once_and_recovery_once(self):
        with tempfile.TemporaryDirectory() as d:
            h=Health(Path(d)/'s.json');sent=[]
            for now in (1000,1060,1120,1180):
                h.failure(now,'库存接口连接失败');h.flush(sent.append,now)
            self.assertEqual(len(sent),1)
            self.assertIn('故障',sent[0])
            h.success(1240,False);h.flush(sent.append,1240)
            h.success(1300,False);h.flush(sent.append,1300)
            self.assertEqual(len(sent),2)
            self.assertIn('恢复',sent[1])

    def test_offline_gap_reported_on_resume(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'s.json';sent=[]
            h=Health(path);h.success(1000,False);h.flush(sent.append,1000)
            h=Health(path);h.success(1600,False);h.flush(sent.append,1600)
            self.assertIn('中断',sent[-1])
            self.assertIn('恢复',sent[-1])

    def test_failed_delivery_not_marked_sent_or_repeatedly_queued(self):
        with tempfile.TemporaryDirectory() as d:
            h=Health(Path(d)/'s.json')
            def fail(message):raise RuntimeError('模拟断网')
            h.success(1000,False)
            with self.assertRaises(RuntimeError):h.flush(fail,1000)
            h.success(1060,False);sent=[];h.flush(sent.append,1060)
            self.assertEqual(len(sent),1)
            self.assertEqual(h.state['last_report'],1060)

    def test_unsent_recovery_is_not_overwritten_by_normal_report(self):
        with tempfile.TemporaryDirectory() as d:
            h=Health(Path(d)/'s.json');h.failure(1000,'登录失效',immediate=True)
            h.success(1060,False)
            h.success(1120,False)
            sent=[];h.flush(sent.append,1120)
            self.assertIn('恢复',sent[0])

    def test_fault_replaces_unsent_healthy_report(self):
        with tempfile.TemporaryDirectory() as d:
            h=Health(Path(d)/'s.json');h.success(1000,False)
            h.failure(1060,'登录失效',immediate=True)
            sent=[];h.flush(sent.append,1060)
            self.assertEqual(len(sent),1)
            self.assertIn('登录失效',sent[0])
            self.assertNotIn('运行正常',sent[0])

if __name__=='__main__':unittest.main()
