import json
import tempfile
import unittest
from pathlib import Path
import service_runner as s


class ServiceTests(unittest.TestCase):
    def test_unexpected_exit_is_restartable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(s.service_exit_code(1,Path(d)),1)
            self.assertEqual(s.service_exit_code(-15,Path(d)),1)
            self.assertEqual(s.service_exit_code(0,Path(d)),1)

    def test_completed_and_uncertain_orders_still_restart_stock_monitor(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for phase in ['submitted','submitting']:
                (root/'purchase-state.json').write_text(json.dumps({'phase':phase}),encoding='utf-8')
                (root/'purchase-notices.json').write_text(json.dumps({'pending':[]}),encoding='utf-8')
                self.assertEqual(s.service_exit_code(0,root),1)
                self.assertEqual(s.service_exit_code(1,root),1)

    def test_pending_notice_restarts_to_retry_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'purchase-state.json').write_text(json.dumps({'phase':'submitted'}),encoding='utf-8')
            (root/'purchase-notices.json').write_text(json.dumps({'pending':['通知']}),encoding='utf-8')
            self.assertEqual(s.service_exit_code(1,root),1)

    def test_missing_or_corrupt_notice_record_needs_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'purchase-state.json').write_text(json.dumps({'phase':'submitting'}),encoding='utf-8')
            self.assertEqual(s.service_exit_code(1,root),1)
            (root/'purchase-notices.json').write_text('{',encoding='utf-8')
            self.assertEqual(s.service_exit_code(1,root),1)

if __name__=='__main__':unittest.main()
