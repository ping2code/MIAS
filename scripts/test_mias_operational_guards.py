import sys
import unittest
import tempfile
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import mias_operational_cycle as cycle
import mias_publish_batch as publisher
from market_data.models import Interval
from market_data.calendar import default_calendar

class OperationalGuards(unittest.TestCase):
    def test_weekend_requires_friday_close(self):
        cutoff=datetime.fromisoformat('2026-10-05T05:00:00+00:00')
        expected={'1d':'2026-10-02T00:00:00-04:00','1h':'2026-10-02T15:30:00-04:00','5m':'2026-10-02T15:55:00-04:00'}
        for label, stamp in expected.items():
            self.assertEqual(cycle.expected_start(default_calendar(),cutoff,Interval.parse(label)).isoformat(),stamp)
    def test_incomplete_opening_bar_excluded(self):
        cutoff=datetime.fromisoformat('2026-10-05T13:34:59+00:00')
        self.assertEqual(cycle.expected_start(default_calendar(),cutoff,Interval.M5).isoformat(),'2026-10-02T15:55:00-04:00')
    def test_first_completed_opening_bar(self):
        cutoff=datetime.fromisoformat('2026-10-05T13:35:00+00:00')
        self.assertEqual(cycle.expected_start(default_calendar(),cutoff,Interval.M5).isoformat(),'2026-10-05T09:30:00-04:00')
    def test_persistence_failures_block(self):
        good='event=technical_persistence queued=6 persisted=6 conflict=0 failed=0 dropped_queue_full=0 dropped_shutdown=0 dropped_initializing=0 failed_initializing=0 invalid_row=0 drained=true'
        cycle.check_persistence_log(good)
        for key in ('conflict','failed','dropped_queue_full','dropped_shutdown','dropped_initializing','failed_initializing','invalid_row'):
            with self.assertRaises(ValueError):
                cycle.check_persistence_log(good.replace(key+'=0',key+'=1'))
        for bad in ('',good.replace('persisted=6','persisted=5'),good.replace('drained=true','drained=false')):
            with self.assertRaises(ValueError):
                cycle.check_persistence_log(bad)
    def test_calendar_window(self):
        self.assertTrue(cycle.scheduled_window(datetime.fromisoformat('2026-10-05T12:30:00+00:00')))
        self.assertFalse(cycle.scheduled_window(datetime.fromisoformat('2026-10-04T13:30:00+00:00')))

    def test_cleanup_only_touches_owned_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            owner=Path(directory)/'owner.json'
            with patch.object(publisher,'OWNER',owner), patch.object(publisher,'oc') as oc:
                publisher.cleanup_owned_publisher()
                oc.assert_not_called()
                owner.write_text('{}')
                oc.side_effect=['', '{"spec":{"replicas":0}}']
                publisher.cleanup_owned_publisher()
                self.assertFalse(owner.exists())
                self.assertEqual(oc.call_count,2)

    def test_failed_cleanup_preserves_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            owner=Path(directory)/'owner.json'
            owner.write_text('{}')
            with patch.object(publisher,'OWNER',owner), patch.object(publisher,'oc',side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    publisher.cleanup_owned_publisher()
                self.assertTrue(owner.exists())

if __name__=='__main__':
    unittest.main()
