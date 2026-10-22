from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from health import Health


def stamp(day, hour, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second,
                    tzinfo=timezone(timedelta(hours=8))).timestamp()


class HeartbeatWindowTests(unittest.TestCase):
    def test_beijing_time_window_boundaries(self):
        for hour, minute, second, expected in (
                (7,59,59,0), (8,0,0,1), (17,59,59,1), (18,0,0,0), (23,0,0,0)):
            with self.subTest(hour=hour), tempfile.TemporaryDirectory() as directory:
                h=Health(Path(directory)/'health.json');sent=[]
                now=stamp(16,hour,minute,second)
                h.success(now,False);h.flush(sent.append,now)
                self.assertEqual(len(sent),expected)

    def test_normal_monitor_continues_overnight_and_reports_at_eight(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'health.json';h=Health(path);sent=[]
            for now in range(int(stamp(16,17)),int(stamp(17,9))+1,30):
                if now==stamp(16,23):h=Health(path)
                h.success(now,False,interval=30)
                h.flush(lambda message:sent.append((now,message)),now)
            self.assertEqual([time for time,_ in sent],[stamp(16,17),stamp(17,8),stamp(17,9)])
            self.assertTrue(all('运行正常' in message for _,message in sent))
            self.assertEqual(h.state['last_success'],stamp(17,9))

    def test_failed_heartbeat_is_not_retried_after_eighteen(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'health.json';h=Health(path)
            h.success(stamp(16,17,59,59),False)
            def fail(message):raise RuntimeError('模拟发送失败')
            with self.assertRaises(RuntimeError):h.flush(fail,stamp(16,17,59,59))
            h=Health(path);sent=[]
            h.flush(sent.append,stamp(16,18))
            self.assertEqual(sent,[])
            self.assertIsNone(h.state['pending'])
            self.assertIsNone(h.state['last_report'])

    def test_fault_and_recovery_notifications_are_silent_at_night(self):
        with tempfile.TemporaryDirectory() as directory:
            h=Health(Path(directory)/'health.json');sent=[]
            h.failure(stamp(16,20),'登录失效',immediate=True)
            h.flush(sent.append,stamp(16,20))
            h.success(stamp(16,20,1),False);h.flush(sent.append,stamp(16,20,1))
            self.assertEqual(sent,[])


if __name__=='__main__':unittest.main()
