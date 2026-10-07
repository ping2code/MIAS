"""The artifact-index refresh never overlaps with itself, even when one refresh takes longer than the interval.

The Refresher runs one daemon thread whose loop is ``while not stop.wait(interval): refresh()``: the next wait starts
only after the previous refresh returns, so the effective period is refresh time + interval. ArtifactStore.refresh is
also serialized by a lock, so an extra caller cannot run a second build concurrently either."""
import threading
import time
import unittest
from unittest.mock import patch

from api.artifacts import Refresher
from artifact_store.service import ArtifactStore


class SlowStore:
    """Records how many refreshes run at once; each takes longer than the refresh interval."""

    def __init__(self, seconds):
        self.seconds, self.active, self.max_active, self.calls = seconds, 0, 0, 0
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
        return True


class RefreshConcurrencyTests(unittest.TestCase):
    def test_a_refresh_longer_than_the_interval_never_overlaps(self):
        store = SlowStore(0.15)
        refresher = Refresher(store, interval=0.01)
        refresher.start()                      # one synchronous startup refresh, then the background thread
        time.sleep(1.0)
        refresher.stop()
        self.assertGreaterEqual(store.calls, 4)
        self.assertEqual(store.max_active, 1)

    def test_store_refresh_is_serialized(self):
        store = ArtifactStore("/nonexistent-mias-root-for-test")
        active, peak = [0], [0]
        guard = threading.Lock()

        def slow_build(root):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with guard:
                active[0] -= 1
            raise RuntimeError("stop")

        with patch("artifact_store.service.build_index", side_effect=slow_build):
            threads = [threading.Thread(target=lambda: self.assertRaises(RuntimeError, store.refresh)) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak[0], 1)


if __name__ == "__main__":
    unittest.main()
