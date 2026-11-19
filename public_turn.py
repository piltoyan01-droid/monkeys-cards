"""Experiment 3: one public test turn per group, no database or full game."""

from dataclasses import dataclass, field
from collections.abc import Awaitable
import logging
from html import escape
from pathlib import Path
import secrets
import time
from typing import Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, RichTextTextMention, User

from card_selection import ConfirmedMove
from match_engine import MatchError, MatchManager, TURNS_PER_PLAYER

LOG = logging.getLogger("monkeys_lab")
TURN_TTL = 30 * 60
SUIT_FILES = {"♠": "spades", "♥": "hearts", "♦": "diamonds", "♣": "clubs"}
KNOWN_PROFILES: dict[int, User] = {}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def apply_edit(request: Awaitable[object]) -> None:
    """A retry may find the intended text/keyboard already applied by Telegram."""
    try:
        await request
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            raise


def card_filename(card: str) -> str:
    rank, suit = card.split()
    return f"{SUIT_FILES[suit]}_{rank}.webp"


def display_name(name: str) -> str:
    return " ".join(name.split())[:80]


def profile_link(user_id: int, name: str) -> str:
    """HTML mention by stable Telegram ID; names remain literal display text."""
    user = KNOWN_PROFILES.get(user_id)
    url = f"https://t.me/{user.username}" if user and user.username else f"tg://user?id={int(user_id)}"
    return f'<a href="{escape(url, quote=True)}">{escape(display_name(name))}</a>'


def remember_profile(user: User) -> None:
    KNOWN_PROFILES[user.id] = user


def rich_profile(user_id: int, name: str) -> RichTextTextMention:
    user = KNOWN_PROFILES.get(user_id) or User(id=user_id, is_bot=False, first_name=display_name(name))
    return RichTextTextMention(text=display_name(name), user=user)


def turn_label(index: int, total_turns: int, round_number: int | None = None) -> str:
    players = max(1, total_turns // TURNS_PER_PLAYER)
    current_round = round_number if round_number is not None else index // players + 1
    return f"Круг {current_round}/{TURNS_PER_PLAYER} · Ход {index + 1}/{total_turns}"


def turn_heading(index: int, total_turns: int, round_number: int | None = None) -> str:
    return f"<b>{turn_label(index, total_turns, round_number)}</b>"


@dataclass(frozen=True)
class VoteOutcome:
    is_truth: bool
    guessed: bool
    receiver_id: int
    receiver_name: str


def resolve_vote(move: ConfirmedMove, vote: str, author_id: int, author_name: str,
                 voter_id: int, voter_name: str) -> VoteOutcome:
    if vote not in {"yes", "no"}:
        raise PublicTurnError("Неизвестный ответ.")
    truth = move.real_card == move.declared_card
    guessed = (vote == "yes") == truth
    return VoteOutcome(truth, guessed, author_id if guessed else voter_id,
                       author_name if guessed else voter_name)


class PublicTurnError(ValueError):
    """A fixed user-facing explanation without secret data."""


@dataclass
class PublicTurn:
    token: str
    chat_id: int
    thread_id: int | None
    author_id: int
    author_name: str
    move: ConfirmedMove = field(repr=False)
    created_at: float = field(default_factory=time.monotonic)
    back_id: int | None = None
    claim_id: int | None = None
    face_id: int | None = None
    result_id: int | None = None
    voter_id: int | None = None
    voter_name: str = ""
    vote: str | None = None
    keyboard_removed: bool = False
    delivery_failed: bool = False
    experiment: int = 3
    inline_claim_id: str | None = None
    chat_instance: str | None = None
    control_token: str | None = None
    inline_vote_ready: bool = False
    inline_origin_verified: bool = True
    inline_finished_updated: bool = False
    match_id: str | None = None
    match_turn: int | None = None
    expected_voter_id: int | None = None
    expected_voter_name: str = ""
    total_match_turns: int = 0
    match_round: int | None = None
    match_applied: bool = False
    announcement_done: bool = False

    @property
    def expired(self) -> bool:
        return self.match_id is None and time.monotonic() - self.created_at > TURN_TTL

    @property
    def finished(self) -> bool:
        return self.result_id is not None

    @property
    def outcome(self) -> VoteOutcome | None:
        if self.vote is None or self.voter_id is None:
            return None
        return resolve_vote(self.move, self.vote, self.author_id, self.author_name,
                            self.voter_id, self.voter_name)


def vote_keyboard(turn: PublicTurn) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="Верю", callback_data=f"lab3:vote:{turn.token}:yes"),
        InlineKeyboardButton(text="Не верю", callback_data=f"lab3:vote:{turn.token}:no"),
    ]]
    if turn.control_token:
        rows += inline_controls(turn).inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_controls(turn: PublicTurn) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Обновить / повторить отправку",
                                   callback_data=f"lab4:status:{turn.control_token}")]]
    if turn.finished and turn.match_id is None:
        rows.append([InlineKeyboardButton(text="Новый inline-ход", callback_data=f"lab4:new:{turn.control_token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def claim_text(turn: PublicTurn) -> str:
    # The real card and truth value must never appear in this public message.
    author = profile_link(turn.author_id, turn.author_name)
    if turn.match_id:
        voter = profile_link(turn.expected_voter_id, turn.expected_voter_name)
        return (f"{turn_heading(turn.match_turn or 0, turn.total_match_turns, turn.match_round)}\n\n"
                f"Автор: {author}\nОтвечает: {voter}\n"
                f"\nЗаявленная карта: {turn.move.declared_card}\n\n"
                f"{voter}, веришь?")
    return (f"MONKEYS CARDS LAB · эксперимент {turn.experiment}\n"
            f"Автор: {author}\nЗаявленная карта: {turn.move.declared_card}\n\n"
            "Принимается первый ответ другого участника.\n"
            f"Угадаете — карту заберет {author}. Ошибетесь — оставите карту себе.")


def result_text(turn: PublicTurn) -> str:
    outcome = turn.outcome
    if outcome is None:
        raise PublicTurnError("Сначала нужен ответ другого участника.")
    truth = "ПРАВДА: карты совпали." if outcome.is_truth else "БЛЕФ: карты не совпали."
    answer = "Верю" if turn.vote == "yes" else "Не верю"
    author = profile_link(turn.author_id, turn.author_name)
    voter = profile_link(turn.voter_id, turn.voter_name)
    receiver = profile_link(outcome.receiver_id, outcome.receiver_name)
    result = (f"{voter} угадывает. Карта осталась у {receiver}." if outcome.guessed else
              f"{voter} ошибается, карта переходит этому игроку.")
    return ("<b>Карта открыта!</b>\n\n"
            f"Автор: {author}\nОтвечал: {voter}\nОтвет: {answer}\n\n"
            f"Заявленная карта: {turn.move.declared_card}\nНастоящая карта: {turn.move.real_card}\n\n"
            f"{truth}\n{result}")


class PublicLab:
    def __init__(self, asset_dir: Path, matches: MatchManager | None = None):
        self.asset_dir = asset_dir
        self.matches = matches
        self.after_game_turn: Callable[[Bot, PublicTurn], Awaitable[None]] | None = None
        self.turns: dict[str, PublicTurn] = {}
        self.sticker_ids: dict[str, str] = {}

    def active_turn(self, chat_id: int) -> PublicTurn | None:
        for turn in self.turns.values():
            if turn.chat_id != chat_id or turn.finished or turn.expired:
                continue
            if turn.match_id and self.matches:
                game = self.matches.ongoing(chat_id)
                if game is None or game.token != turn.match_id:
                    continue
            return turn
        return None

    def validate_selection(self, chat_id: int, user_id: int, match_id: str | None = None,
                           match_turn: int | None = None, allow_pending: bool = False) -> None:
        if self.matches:
            try:
                self.matches.validate_selection(chat_id, user_id, match_id, match_turn, allow_pending)
            except MatchError as error:
                raise PublicTurnError(str(error)) from None

    def validate_managed_turn(self, turn: PublicTurn) -> None:
        if turn.match_id and self.matches:
            try:
                game = self.matches.find(turn.chat_id, turn.match_id)
                if turn.match_applied:
                    if game.status not in {"active", "finished"}:
                        raise MatchError("Партия остановлена.")
                elif (game.status != "active" or game.pending is None
                      or game.pending.token != turn.token):
                    raise MatchError("Этот ход партии уже недоступен.")
            except MatchError as error:
                raise PublicTurnError(str(error)) from None

    def create_turn(self, chat_id: int, thread_id: int | None, author_id: int,
                    author_name: str, move: ConfirmedMove, experiment: int = 3,
                    match_id: str | None = None, match_turn: int | None = None) -> PublicTurn:
        self.validate_selection(chat_id, author_id, match_id, match_turn)
        if self.active_turn(chat_id):
            raise PublicTurnError("В группе уже есть незавершенный ход. Сначала ответьте на него.")
        for token, old in list(self.turns.items()):
            if old.expired or (old.finished and time.monotonic() - old.created_at > TURN_TTL):
                del self.turns[token]
        turn = PublicTurn(secrets.token_urlsafe(9), chat_id, thread_id, author_id,
                          display_name(author_name), move, experiment=experiment)
        if match_id is not None and self.matches:
            try:
                game = self.matches.reserve(chat_id, match_id, match_turn, author_id, turn.token, move)
            except MatchError as error:
                raise PublicTurnError(str(error)) from None
            turn.match_id = game.token
            turn.match_turn = game.turns_done
            turn.expected_voter_id = game.voter.user_id
            turn.expected_voter_name = game.voter.name
            turn.total_match_turns = game.total_turns
            turn.match_round = game.current_round
        self.turns[turn.token] = turn
        return turn

    async def send_sticker(self, bot: Bot, turn: PublicTurn, filename: str):
        sticker = self.sticker_ids.get(filename) or FSInputFile(self.asset_dir / filename)
        sent = await bot.send_sticker(chat_id=turn.chat_id, message_thread_id=turn.thread_id,
                                      sticker=sticker)
        if sent.sticker and isinstance(sent.sticker.file_id, str):
            self.sticker_ids[filename] = sent.sticker.file_id
        return sent

    async def publish(self, bot: Bot, turn: PublicTurn) -> None:
        self.validate_managed_turn(turn)
        if turn.expired:
            raise PublicTurnError("Тестовый ход устарел. Создайте новый через /lab3.")
        if turn.back_id is None:
            sent = await self.send_sticker(bot, turn, "card_back.webp")
            turn.back_id = sent.message_id
        if turn.inline_claim_id:
            if not turn.inline_vote_ready:
                # Reuse the final inline statement. Do not send a duplicate claim.
                await apply_edit(bot.edit_message_text(inline_message_id=turn.inline_claim_id,
                                                       text=claim_text(turn), parse_mode="HTML", link_preview_options=NO_PREVIEW,
                                                       reply_markup=vote_keyboard(turn)))
                turn.inline_vote_ready = True
        elif turn.claim_id is None:
            sent = await bot.send_message(chat_id=turn.chat_id, message_thread_id=turn.thread_id,
                                           text=claim_text(turn), parse_mode="HTML", link_preview_options=NO_PREVIEW,
                                           reply_markup=vote_keyboard(turn))
            turn.claim_id = sent.message_id

    def accept_vote(self, turn: PublicTurn, voter_id: int, voter_name: str,
                    is_bot: bool, vote: str) -> None:
        if turn.expired or turn.finished:
            raise PublicTurnError("Этот ход уже завершен или устарел.")
        if turn.claim_id is None and not (turn.inline_claim_id and turn.inline_vote_ready):
            raise PublicTurnError("Публикация не завершена. Автору нужно повторить отправку.")
        if is_bot or voter_id == turn.author_id:
            raise PublicTurnError("На ход должен ответить другой участник, не его автор.")
        if vote not in {"yes", "no"}:
            raise PublicTurnError("Неизвестный ответ.")
        if turn.match_id and self.matches:
            try:
                self.matches.validate_voter(turn.chat_id, turn.match_id, turn.token, voter_id)
            except MatchError as error:
                raise PublicTurnError(str(error)) from None
        if turn.vote is not None:
            if turn.voter_id != voter_id or turn.vote != vote:
                raise PublicTurnError("Первый ответ уже принят. Изменить его нельзя.")
            return  # An explicit retry resumes delivery, never changes the vote.
        turn.voter_id = voter_id
        turn.voter_name = display_name(voter_name)
        turn.vote = vote

    async def remove_keyboard(self, bot: Bot, turn: PublicTurn) -> None:
        if (turn.claim_id is None and turn.inline_claim_id is None) or turn.keyboard_removed:
            return
        try:
            if turn.inline_claim_id:
                await apply_edit(bot.edit_message_reply_markup(inline_message_id=turn.inline_claim_id,
                                                               reply_markup=inline_controls(turn)))
            else:
                await apply_edit(bot.edit_message_reply_markup(chat_id=turn.chat_id, message_id=turn.claim_id,
                                                               reply_markup=None))
            turn.keyboard_removed = True
        except TelegramAPIError as error:
            # Even if the client still shows buttons, the first response is locked.
            LOG.warning("vote_keyboard_close_failed error=%s", type(error).__name__)

    async def reveal(self, bot: Bot, turn: PublicTurn) -> None:
        self.validate_managed_turn(turn)
        if turn.expired:
            raise PublicTurnError("Тестовый ход устарел. Создайте новый через /lab3.")
        if turn.outcome is None:
            raise PublicTurnError("Сначала нужен ответ другого участника.")
        await self.remove_keyboard(bot, turn)
        if turn.face_id is None:
            sent = await self.send_sticker(bot, turn, card_filename(turn.move.real_card))
            turn.face_id = sent.message_id
        if turn.result_id is None:
            sent = await bot.send_message(chat_id=turn.chat_id, message_thread_id=turn.thread_id,
                                           text=result_text(turn), parse_mode="HTML", link_preview_options=NO_PREVIEW)
            turn.result_id = sent.message_id
        if turn.match_id and self.matches:
            if not turn.match_applied:
                try:
                    self.matches.complete(turn.chat_id, turn.match_id, turn.token, turn.voter_id, turn.vote)
                except MatchError as error:
                    raise PublicTurnError(str(error)) from None
                turn.match_applied = True
            if self.after_game_turn and not turn.announcement_done:
                await self.after_game_turn(bot, turn)
                turn.announcement_done = True
        if turn.match_id and turn.keyboard_removed:
            turn.inline_finished_updated = True
        if turn.inline_claim_id and not turn.inline_finished_updated:
            try:
                await apply_edit(bot.edit_message_reply_markup(inline_message_id=turn.inline_claim_id,
                                                               reply_markup=inline_controls(turn)))
                turn.inline_finished_updated = True
                turn.keyboard_removed = True
            except TelegramAPIError as error:
                LOG.warning("inline_finished_controls_failed error=%s", type(error).__name__)

    async def resume(self, bot: Bot, turn: PublicTurn) -> None:
        try:
            if turn.vote is None:
                await self.publish(bot, turn)
            else:
                await self.reveal(bot, turn)
        except TelegramAPIError:
            turn.delivery_failed = True
            raise
        turn.delivery_failed = False

    def status_text(self, token: str | None) -> str:
        turn = self.turns.get(token or "")
        if turn is None or turn.expired:
            return "Ход устарел. Можно начать новый тестовый ход."
        if turn.delivery_failed:
            return ("Ошибка отправки. Часть сообщений могла появиться в группе.\n"
                    "Нажмите «Обновить / повторить отправку». При сетевом таймауте возможен дубль последнего сообщения.")
        if turn.finished:
            return "Ход завершен. Карта и результат проверки опубликованы в группе."
        if turn.vote is not None:
            return "Первый ответ принят. Бот отправляет открытую карту и результат в группу."
        if turn.claim_id is not None or turn.inline_vote_ready:
            return "Ход опубликован. Ожидается ответ другого участника в группе."
        return "Публикация не завершена. Нажмите «Обновить / повторить отправку»."
