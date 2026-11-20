import tempfile
import unittest
from pathlib import Path

from player_stats import PlayerStats


class PlayerStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PlayerStats(Path(self.temp.name) / "stats.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_match_is_recorded_once(self):
        result = [(1, "Alice", True), (2, "Bob", False)]
        self.assertTrue(self.store.record_match("match-1", -100, result))
        self.assertFalse(self.store.record_match("match-1", -100, result))
        self.assertEqual(self.store.get(1).played, 1)
        self.assertEqual(self.store.get(1).wins, 1)
        self.assertEqual(self.store.get(2).losses, 1)

    def test_tied_players_both_get_a_win(self):
        self.store.record_match("match-1", -100, [(1, "Alice", True), (2, "Bob", True)])
        self.assertEqual(self.store.get(1).wins, 1)
        self.assertEqual(self.store.get(2).wins, 1)
        self.assertEqual(self.store.get(1).losses, 0)

    def test_sherlock_after_two_wins_and_only_once(self):
        self.store.record_match("match-1", -100, [(1, "Alice", True)])
        self.assertEqual(self.store.pending_achievements(), [])
        self.store.record_match("match-2", -100, [(1, "Alice", True)])
        pending = self.store.pending_achievements()
        self.assertEqual([(item.user_id, item.key) for item in pending], [(1, "sherlock")])
        self.store.mark_achievements_announced(pending)
        self.assertEqual(self.store.pending_achievements(), [])
        self.store.record_match("match-3", -100, [(1, "Alice", False)])
        self.store.record_match("match-4", -100, [(1, "Alice", True)])
        self.store.record_match("match-5", -100, [(1, "Alice", True)])
        self.assertEqual(self.store.pending_achievements(), [])

    def test_skipped_match_does_not_change_win_streak(self):
        self.store.record_match("match-1", -100, [(1, "Alice", True)])
        # A match without this player is equivalent to a skipped match.
        self.store.record_match("match-2", -100, [(2, "Bob", True)])
        self.store.record_match("match-3", -100, [(1, "Alice", True)])
        self.assertEqual(len(self.store.pending_achievements()), 1)

    def test_achievement_is_announced_only_in_origin_group(self):
        self.store.record_match("match-1", -100, [(1, "Alice", True)])
        self.store.record_match("match-2", -200, [(1, "Alice", True)])
        pending = self.store.pending_achievements()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].chat_id, -200)


if __name__ == "__main__":
    unittest.main()
