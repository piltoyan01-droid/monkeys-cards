"""Original lobby/turn/score rules, kept in memory for the separate LAB bot."""

from dataclasses import dataclass, field
import random
import secrets

from card_selection import ConfirmedMove, DECK

MIN_PLAYERS = 2
MAX_PLAYERS = 5
CARDS_PER_PLAYER = 5
TURNS_PER_PLAYER = 5


class MatchError(ValueError):
    """Fixed, safe explanations of game rule violations."""


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str


@dataclass(frozen=True)
class PendingMove:
    token: str
    author_id: int
    voter_id: int
    move: ConfirmedMove = field(repr=False)


@dataclass(frozen=True)
class TurnSlot:
    author_id: int
    round_number: int


@dataclass(frozen=True)
class LeaveOutcome:
    player: Player
    cancelled_move: bool = False
    creator_changed: bool = False


@dataclass
class Match:
    token: str
    chat_id: int
    thread_id: int | None
    creator_id: int
    players: list[Player]
    status: str = "lobby"
    hands: dict[int, list[str]] = field(default_factory=dict, repr=False)
    turns_done: int = 0
    turns_by_player: dict[int, int] = field(default_factory=dict)
    pending: PendingMove | None = field(default=None, repr=False)
    applied: set[str] = field(default_factory=set)
    chat_instance: str | None = None
    lobby_id: int | None = None
    announcements: dict[int, int] = field(default_factory=dict)
    final_message_id: int | None = None
    remaining_turns: list[TurnSlot] = field(default_factory=list, repr=False)

    @property
    def total_turns(self) -> int:
        return len(self.players) * TURNS_PER_PLAYER if self.status == "lobby" else self.turns_done + len(self.remaining_turns)

    @property
    def current_round(self) -> int:
        return self.remaining_turns[0].round_number if self.remaining_turns else TURNS_PER_PLAYER

    @property
    def author(self) -> Player:
        author_id = self.remaining_turns[0].author_id if self.remaining_turns else self.players[0].user_id
        return next(player for player in self.players if player.user_id == author_id)

    @property
    def voter(self) -> Player:
        index = self.players.index(self.author)
        return self.players[(index + 1) % len(self.players)]


class MatchManager:
    def __init__(self):
        self.games: dict[int, Match] = {}

    def ongoing(self, chat_id: int) -> Match | None:
        game = self.games.get(chat_id)
        return game if game and game.status in {"lobby", "active"} else None

    def find(self, chat_id: int, token: str) -> Match:
        game = self.games.get(chat_id)
        if game is None or game.token != token:
            raise MatchError("Эта партия уже недоступна. Создайте новую через /game.")
        return game

    def create(self, chat_id: int, thread_id: int | None, creator_id: int, name: str) -> Match:
        if self.ongoing(chat_id):
            raise MatchError("В группе уже идет набор или партия. Используйте ее текущие кнопки.")
        game = Match(secrets.token_urlsafe(8), chat_id, thread_id, creator_id,
                     [Player(creator_id, " ".join(name.split())[:80])])
        self.games[chat_id] = game
        return game

    def join(self, game: Match, user_id: int, name: str) -> None:
        if game.status != "lobby":
            raise MatchError("Регистрация уже закрыта.")
        if any(player.user_id == user_id for player in game.players):
            raise MatchError("Вы уже зарегистрированы.")
        if len(game.players) >= MAX_PLAYERS:
            raise MatchError("Все 5 мест уже заняты.")
        game.players.append(Player(user_id, " ".join(name.split())[:80]))

    def start(self, game: Match, user_id: int, chat_instance: str) -> None:
        if user_id != game.creator_id:
            raise MatchError("Начать партию может только ее создатель.")
        if game.status != "lobby":
            raise MatchError("Партия уже началась или завершена.")
        if len(game.players) < MIN_PLAYERS:
            raise MatchError("Для начала нужны минимум 2 игрока.")
        deck = list(DECK)
        random.shuffle(deck)
        game.hands = {player.user_id: [deck.pop() for _ in range(CARDS_PER_PLAYER)]
                      for player in game.players}
        game.turns_by_player = {player.user_id: 0 for player in game.players}
        game.remaining_turns = [TurnSlot(player.user_id, round_number)
                                for round_number in range(1, TURNS_PER_PLAYER + 1) for player in game.players]
        game.chat_instance = chat_instance
        game.status = "active"

    def skip_empty_turns(self, game: Match) -> list[TurnSlot]:
        """Consume scheduled turns for players whose hands are empty."""
        skipped: list[TurnSlot] = []
        if game.status != "active" or game.pending is not None:
            return skipped
        while game.remaining_turns:
            slot = game.remaining_turns[0]
            if game.hands.get(slot.author_id):
                break
            game.remaining_turns.pop(0)
            game.turns_by_player[slot.author_id] = game.turns_by_player.get(slot.author_id, 0) + 1
            game.turns_done += 1
            skipped.append(slot)
        if not game.remaining_turns:
            game.status = "finished"
        return skipped

    def validate_selection(self, chat_id: int, user_id: int, match_id: str | None,
                           match_turn: int | None, allow_pending: bool = False) -> Match | None:
        game = self.ongoing(chat_id)
        if match_id is None:
            if game:
                raise MatchError("Выбирайте карты кнопками актуального хода этой партии.")
            return None
        game = self.find(chat_id, match_id)
        if game.status != "active":
            raise MatchError("Партия еще не началась или уже завершена.")
        if match_turn != game.turns_done:
            raise MatchError("Это кнопка предыдущего хода. Используйте актуальное сообщение бота.")
        if user_id != game.author.user_id:
            raise MatchError(f"Сейчас ходит {game.author.name}. Дождитесь своей очереди.")
        if game.pending is not None and not allow_pending:
            raise MatchError(f"Ход уже подтвержден. Сейчас отвечает {game.voter.name}.")
        return game

    def reserve(self, chat_id: int, match_id: str, match_turn: int, user_id: int,
                token: str, move: ConfirmedMove) -> Match:
        game = self.validate_selection(chat_id, user_id, match_id, match_turn)
        assert game is not None
        if move.real_card not in game.hands[user_id] or move.declared_card not in DECK:
            raise MatchError("Эта карта уже недоступна. Откройте свою актуальную руку.")
        game.hands[user_id].remove(move.real_card)
        game.pending = PendingMove(token, user_id, game.voter.user_id, move)
        return game

    def validate_voter(self, chat_id: int, match_id: str, token: str, user_id: int) -> Match:
        game = self.find(chat_id, match_id)
        if game.status != "active" or game.pending is None or game.pending.token != token:
            raise MatchError("Этот ход партии уже недоступен.")
        if user_id != game.pending.voter_id:
            raise MatchError(f"На этот ход отвечает только {game.voter.name}.")
        return game

    def complete(self, chat_id: int, match_id: str, token: str, voter_id: int, vote: str) -> Match:
        game = self.find(chat_id, match_id)
        if token in game.applied:
            return game
        game = self.validate_voter(chat_id, match_id, token, voter_id)
        pending = game.pending
        assert pending is not None
        if vote not in {"yes", "no"}:
            raise MatchError("Неизвестный ответ.")
        truth = pending.move.real_card == pending.move.declared_card
        guessed = (vote == "yes") == truth
        receiver = pending.author_id if guessed else pending.voter_id
        game.hands[receiver].append(pending.move.real_card)
        game.turns_by_player[pending.author_id] += 1
        game.turns_done += 1
        game.remaining_turns.pop(0)
        game.pending = None
        game.applied.add(token)
        if not game.remaining_turns:
            game.status = "finished"
        return game

    def leave(self, game: Match, user_id: int) -> LeaveOutcome:
        if game.status not in {"lobby", "active"}:
            raise MatchError("Эта партия уже завершена или остановлена.")
        player = next((player for player in game.players if player.user_id == user_id), None)
        if player is None:
            raise MatchError("Вы не участвуете в этой игре.")
        cancelled = bool(game.pending and user_id in {game.pending.author_id, game.pending.voter_id})
        if cancelled:
            pending = game.pending
            if pending.author_id != user_id:
                game.hands[pending.author_id].append(pending.move.real_card)
            game.pending = None
        game.players.remove(player)
        game.hands.pop(user_id, None)
        game.turns_by_player.pop(user_id, None)
        game.remaining_turns = [slot for slot in game.remaining_turns if slot.author_id != user_id]
        creator_changed = user_id == game.creator_id and bool(game.players)
        if creator_changed:
            game.creator_id = game.players[0].user_id
        if game.status == "lobby":
            if not game.players:
                game.status = "stopped"
        elif len(game.players) < MIN_PLAYERS:
            game.status = "stopped"
        elif not game.remaining_turns:
            game.status = "finished"
        # Preserve an unrelated pending move; invalidate drafts if a new panel is needed.
        if game.status != "lobby" and game.pending is None:
            game.token = secrets.token_urlsafe(8)
            game.announcements.pop(game.turns_done, None)
        return LeaveOutcome(player, cancelled, creator_changed)

    def stop(self, game: Match) -> None:
        game.status = "stopped"

    def ranking(self, game: Match) -> list[tuple[Player, int]]:
        return sorted(((player, len(game.hands.get(player.user_id, []))) for player in game.players),
                      key=lambda item: item[1])
