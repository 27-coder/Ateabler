from __future__ import annotations

import unittest

from ateable.getting_started_ui import next_step


class NextStepTests(unittest.TestCase):
    def step(self, connection="online", online=0, known=0, verified=True, jobs=0):
        return next_step(
            connection,
            online_computers=online,
            known_computers=known,
            workers_verified=verified,
            jobs_total=jobs,
        )

    def test_connection_takes_priority_over_cached_workers(self):
        offline = self.step("error", online=5, known=5, jobs=20)
        self.assertEqual(offline.target, "settings")
        self.assertIn("before saving new links", offline.detail)
        connecting = self.step("connecting", online=5, known=5)
        self.assertFalse(connecting.enabled)
        self.assertNotEqual(connecting.target, "connect")

    def test_worker_snapshot_must_be_verified(self):
        checking = self.step(online=3, known=3, verified=False)
        self.assertEqual(checking.target, "retry")
        self.assertIn("Checking", checking.title)

    def test_first_computer_and_offline_computers_have_different_actions(self):
        self.assertEqual(self.step().target, "connect")
        offline = self.step(known=2)
        self.assertEqual(offline.target, "devices")
        self.assertIn("offline", offline.detail)

    def test_ready_state_supports_first_and_existing_downloads(self):
        self.assertEqual(self.step(online=1, known=1).target, "download")
        existing = self.step(online=5, known=5, jobs=10)
        self.assertEqual(existing.target, "queue")
        self.assertIn("5 download computers", existing.detail)


if __name__ == "__main__":
    unittest.main()
