import unittest

from card_selection import ConfirmedMove
from match_engine import Match, MatchManager, PendingMove, Player, TurnSlot


class SkipEmptyTurnsTests(unittest.TestCase):
    def setUp(self):
        self.manager = MatchManager()
        self.players = [Player(1, "A"), Player(2, "B"), Player(3, "C")]

    def make_match(self, hands, slots, pending=None, status="active"):
        return Match(
            token="test",
            chat_id=-100,
            thread_id=None,
            creator_id=1,
            players=self.players,
            status=status,
            hands=hands,
            turns_by_player={1: 0, 2: 0, 3: 0},
            pending=pending,
            remaining_turns=slots,
        )

    def test_skips_empty_author_and_stops_at_first_nonempty_hand(self):
        game = self.make_match(
            hands={1: [], 2: ["6 ♠"], 3: ["7 ♥"]},
            slots=[TurnSlot(1, 1), TurnSlot(2, 1), TurnSlot(3, 1)],
        )

        skipped = self.manager.skip_empty_turns(game)

        self.assertEqual(skipped, [TurnSlot(1, 1)])
        self.assertEqual(game.turns_done, 1)
        self.assertEqual(game.turns_by_player[1], 1)
        self.assertEqual(game.remaining_turns[0], TurnSlot(2, 1))
        self.assertEqual(game.status, "active")

    def test_skips_consecutive_empty_slots_and_counts_each_one(self):
        game = self.make_match(
            hands={1: [], 2: [], 3: ["7 ♥"]},
            slots=[TurnSlot(1, 1), TurnSlot(2, 1), TurnSlot(3, 1)],
        )

        skipped = self.manager.skip_empty_turns(game)

        self.assertEqual(skipped, [TurnSlot(1, 1), TurnSlot(2, 1)])
        self.assertEqual(game.turns_done, 2)
        self.assertEqual(game.turns_by_player, {1: 1, 2: 1, 3: 0})
        self.assertEqual(game.remaining_turns, [TurnSlot(3, 1)])

    def test_finishes_when_all_remaining_hands_are_empty(self):
        game = self.make_match(
            hands={1: [], 2: [], 3: []},
            slots=[TurnSlot(1, 2), TurnSlot(2, 2), TurnSlot(3, 2)],
        )

        skipped = self.manager.skip_empty_turns(game)

        self.assertEqual(len(skipped), 3)
        self.assertEqual(game.turns_done, 3)
        self.assertEqual(game.remaining_turns, [])
        self.assertEqual(game.status, "finished")

    def test_does_not_skip_while_a_move_is_pending(self):
        move = ConfirmedMove("6 ♠", "6 ♠", True)
        pending = PendingMove("pending", 1, 2, move)
        game = self.make_match(
            hands={1: [], 2: ["7 ♥"], 3: []},
            slots=[TurnSlot(1, 1), TurnSlot(2, 1)],
            pending=pending,
        )

        skipped = self.manager.skip_empty_turns(game)

        self.assertEqual(skipped, [])
        self.assertEqual(game.turns_done, 0)
        self.assertEqual(len(game.remaining_turns), 2)

    def test_does_not_skip_outside_active_game(self):
        game = self.make_match(
            hands={1: [], 2: ["7 ♥"], 3: []},
            slots=[TurnSlot(1, 1)],
            status="lobby",
        )

        self.assertEqual(self.manager.skip_empty_turns(game), [])
        self.assertEqual(game.turns_done, 0)
        self.assertEqual(len(game.remaining_turns), 1)


if __name__ == "__main__":
    unittest.main()
