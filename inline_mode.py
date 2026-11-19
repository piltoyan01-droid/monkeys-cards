"""Experiment 4: private numbered selection; only final claims reach the group.

A normal panel callback binds chat_instance. Selection feedback commits a move;
voting callbacks verify the group and can recover missing selection feedback.
"""

from dataclasses import dataclass, field
import logging
import random
import secrets
import time
from collections.abc import Awaitable
from typing import Callable

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, ChosenInlineResult, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle, InlineQueryResultCachedSticker, InputTextMessageContent, Message,
)

from card_selection import ConfirmedMove, DECK
from public_turn import PublicLab, PublicTurn, PublicTurnError, NO_PREVIEW, apply_edit, claim_text, display_name, remember_profile
from inline_faces import InlineFaces, parse_declaration

LOG = logging.getLogger("monkeys_lab")
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
INLINE_TTL = 30 * 60


class InlineFlowError(ValueError):
    """Only fixed safe explanations are passed to users."""


@dataclass
class InlinePanel:
    token: str
    chat_id: int
    thread_id: int | None
    message_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    chat_instance: str | None = None
    match_id: str | None = None
    match_turn: int | None = None


@dataclass
class InlineSession:
    token: str
    panel: InlinePanel
    user_id: int
    user_name: str
    cards: tuple[str, ...] = field(repr=False)
    created_at: float = field(default_factory=time.monotonic)
    phase: str = "choosing"
    active_candidate: str | None = None
    public_turn_id: str | None = None
    claim_tokens: dict[tuple[int, int], str] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        now = time.monotonic()
        return now - self.created_at > INLINE_TTL or now - self.panel.created_at > INLINE_TTL


@dataclass
class InlineProposal:
    token: str
    session_token: str
    real_index: int = field(repr=False)
    declared_index: int = field(repr=False)
    inline_message_id: str | None = None
    feedback_seen: bool = False


def inline_button(text: str, query: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, switch_inline_query_current_chat=query)


class InlineLab:
    def __init__(self, public_provider: Callable[[], PublicLab],
                 cards_provider: Callable[[int, int], tuple[str, ...] | None]):
        self.public_provider = public_provider
        self.cards_provider = cards_provider
        self.panels: dict[str, InlinePanel] = {}
        self.sessions: dict[str, InlineSession] = {}
        self.users: dict[tuple[int, int | None, int], str] = {}
        self.proposals: dict[str, InlineProposal] = {}
        self.hands: dict[tuple[int, int], tuple[tuple[str, ...], float]] = {}
        self.faces = InlineFaces()
        self.return_modes: Callable[[Bot, InlineSession], Awaitable[None]] | None = None
        self.mode_opened: Callable[[Bot, InlineSession], Awaitable[None]] | None = None
        self.mode_enabled: Callable[[int, int], bool] = lambda chat, user: True
        self.router = Router(name="inline_lab")
        self.router.message.register(self.show_panel, Command("lab4"), F.chat.type.in_(GROUPS))
        self.router.message.register(self.on_group_choice, F.via_bot, F.chat.type.in_(GROUPS))
        self.router.inline_query.register(self.answer_query)
        self.router.chosen_inline_result.register(self.on_chosen)
        self.router.callback_query.register(self.on_callback, F.data.startswith("lab4:"))

    def clear(self) -> None:
        for mapping in (self.panels, self.sessions, self.users, self.proposals, self.hands):
            mapping.clear()
        self.faces.ids.clear()

    def prune(self) -> None:
        now = time.monotonic()
        for token, session in list(self.sessions.items()):
            if session.expired or session.phase == "cancelled":
                del self.sessions[token]
        for token, proposal in list(self.proposals.items()):
            if proposal.session_token not in self.sessions:
                del self.proposals[token]
        for key, token in list(self.users.items()):
            if token not in self.sessions:
                del self.users[key]
        for token, panel in list(self.panels.items()):
            if now - panel.created_at > INLINE_TTL:
                del self.panels[token]
        for key, (_, created_at) in list(self.hands.items()):
            if now - created_at > INLINE_TTL:
                del self.hands[key]

    def existing_cards(self, chat_id: int, user_id: int) -> tuple[str, ...] | None:
        hand = self.hands.get((chat_id, user_id))
        return hand[0] if hand and time.monotonic() - hand[1] <= INLINE_TTL else None

    def cancel_selection(self, chat_id: int, user_id: int, match_id: str, match_turn: int) -> None:
        for session in self.sessions.values():
            if (session.panel.chat_id == chat_id and session.user_id == user_id
                    and session.panel.match_id == match_id and session.panel.match_turn == match_turn
                    and session.phase == "choosing"):
                session.phase = "cancelled"

    def get_session(self, panel: InlinePanel, user_id: int, user_name: str) -> InlineSession:
        key = (panel.chat_id, panel.thread_id, user_id)
        previous = self.sessions.get(self.users.get(key, ""))
        if (previous and not previous.expired and previous.phase != "cancelled"
                and previous.panel.match_id == panel.match_id and previous.panel.match_turn == panel.match_turn):
            return previous
        cards = (self.cards_provider(panel.chat_id, user_id)
                 or self.existing_cards(panel.chat_id, user_id)
                 or tuple(random.sample(DECK, 5)))
        self.hands[(panel.chat_id, user_id)] = (cards, time.monotonic())
        session = InlineSession(secrets.token_urlsafe(9), panel, user_id, display_name(user_name), cards)
        self.sessions[session.token] = session
        self.users[key] = session.token
        return session

    def get_proposal(self, token: str, user_id: int) -> tuple[InlineProposal, InlineSession]:
        proposal = self.proposals.get(token)
        session = self.sessions.get(proposal.session_token) if proposal else None
        if (proposal is None or session is None or session.expired
                or session.phase == "cancelled" or session.user_id != user_id):
            raise InlineFlowError("Выбор недоступен или устарел. Откройте /lab4 в исходной группе.")
        return proposal, session

    async def prepare_faces(self, bot: Bot, user_id: int, cards: tuple[str, ...], allow_create: bool = True) -> bool:
        return await self.faces.prepare(bot, self.public_provider(), user_id, cards, allow_create=allow_create)

    def proposal_for(self, session: InlineSession, real_index: int, declared_index: int) -> InlineProposal:
        key = (real_index, declared_index)
        token = session.claim_tokens.get(key)
        if token is None:
            proposal = InlineProposal(secrets.token_urlsafe(9), session.token, real_index, declared_index)
            token = proposal.token
            session.claim_tokens[key] = token
            self.proposals[token] = proposal
        return self.proposals[token]

    def final_content(self, session: InlineSession, card: str) -> InputTextMessageContent:
        matches = self.public_provider().matches
        turn = PublicTurn("preview", session.panel.chat_id, session.panel.thread_id, session.user_id,
                          session.user_name, ConfirmedMove("", card, False), experiment=4)
        if session.panel.match_id and matches:
            game = matches.find(session.panel.chat_id, session.panel.match_id)
            turn.match_id, turn.match_turn = game.token, game.turns_done
            turn.total_match_turns = game.total_turns
            turn.match_round = game.current_round
            turn.expected_voter_id, turn.expected_voter_name = game.voter.user_id, game.voter.name
        return InputTextMessageContent(message_text=claim_text(turn), parse_mode="HTML", link_preview_options=NO_PREVIEW)

    def selection_keyboard(self, token: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=label, callback_data=f"lab4:vote:{token}:{vote}")
            for label, vote in (("Верю", "yes"), ("Не верю", "no"))]])

    def build_results(self, query: InlineQuery) -> list[InlineQueryResultArticle | InlineQueryResultCachedSticker]:
        self.prune()
        if query.chat_type not in GROUPS or query.offset:
            return []
        parts = query.query.split(maxsplit=2)
        if len(parts) not in {2, 3} or parts[0] != "lab4":
            return []
        panel = self.panels.get(parts[1])
        if panel is None or panel.chat_instance is None:
            return []
        if panel.match_id and not self.mode_enabled(panel.chat_id, query.from_user.id):
            return []
        try:
            self.public_provider().validate_selection(panel.chat_id, query.from_user.id,
                                                       panel.match_id, panel.match_turn)
        except PublicTurnError:
            return []
        suffix = parts[2].strip() if len(parts) == 3 else ""
        session = self.get_session(panel, query.from_user.id, query.from_user.full_name)
        remember_profile(query.from_user)
        if session.phase != "choosing":
            return []
        if not suffix or suffix.startswith("="):
            declared = parse_declaration(suffix) if suffix else None
            if suffix and declared is None:
                return []
            if any(card not in self.faces.ids for card in session.cards):
                return []
            results = []
            for index, real in enumerate(session.cards):
                statement = declared or real
                proposal = self.proposal_for(session, index, DECK.index(statement))
                results.append(InlineQueryResultCachedSticker(
                    id=proposal.token, sticker_file_id=self.faces.ids[real],
                    # Required override: the face is preview-only, never the sent content.
                    input_message_content=self.final_content(session, statement),
                    reply_markup=self.selection_keyboard(proposal.token),
                ))
            return results
        # Preserve the already-tested numbered text mode for existing queries.
        number = suffix
        if not number.isascii() or not number.isdigit() or len(number) > 2 or number.startswith("0"):
            return []
        real_index = int(number) - 1
        if not 0 <= real_index < len(session.cards):
            return []
        hand = " ".join(f"{i + 1}:{card.replace(' ', '')}" for i, card in enumerate(session.cards))
        results = []
        for index, card in enumerate(DECK):
            token = self.proposal_for(session, real_index, index).token
            results.append(InlineQueryResultArticle(
                id=token, title=f"Играю {session.cards[real_index]} → заявляю {card}",
                description=f"Карт: {len(session.cards)}. Рука {hand}. Номер: 1–{len(session.cards)}.",
                input_message_content=self.final_content(session, card),
                reply_markup=self.selection_keyboard(token),
            ))
        return results

    async def show_panel(self, message: Message, bot: Bot) -> None:
        self.prune()
        panel = InlinePanel(secrets.token_urlsafe(9), message.chat.id, message.message_thread_id)
        self.panels[panel.token] = panel
        sent = await bot.send_message(
            chat_id=panel.chat_id, message_thread_id=panel.thread_id,
            text="MONKEYS CARDS LAB · эксперимент 4\n"
                 "Нажмите «Начать inline», затем «Скинуть карту».\n"
                 "В превью появятся лицевые карты вашей руки. "
                 "Без дополнений выбранная карта заявляется правдиво. "
                 "Для другого заявления допишите, например, =6пик, затем нажмите настоящую карту.\n"
                 "Нажатие карты сразу отправляет ход с кнопками «Верю / Не верю».",
            reply_markup=self.panel_keyboard(panel),
        )
        panel.message_id = sent.message_id

    async def answer_query(self, query: InlineQuery) -> None:
        results = []
        opened_session = None
        try:
            # Warm-up normally happens before the turn panel is shown. Lazy upload
            # also covers another user's standalone preview and a restarted cache.
            parts = query.query.split(maxsplit=2)
            if query.chat_type in GROUPS and not query.offset and len(parts) in {2, 3} and parts[0] == "lab4":
                panel = self.panels.get(parts[1])
                suffix = parts[2].strip() if len(parts) == 3 else ""
                if panel and panel.chat_instance and (not panel.match_id or self.mode_enabled(panel.chat_id, query.from_user.id)):
                    self.public_provider().validate_selection(panel.chat_id, query.from_user.id,
                                                               panel.match_id, panel.match_turn)
                    session = self.get_session(panel, query.from_user.id, query.from_user.full_name)
                    if not session.expired and session.phase == "choosing":
                        opened_session = session
                        if not suffix or suffix.startswith("="):
                            await self.prepare_faces(query.bot, session.user_id, session.cards, allow_create=False)
            results = self.build_results(query)
        except PublicTurnError:
            pass
        try:
            await query.answer(results=results, cache_time=0, is_personal=True, next_offset="")
        except TelegramAPIError as error:
            LOG.warning("inline_query_answer_failed error=%s", type(error).__name__)
        if opened_session and opened_session.panel.match_id and self.mode_opened:
            try:
                await self.mode_opened(query.bot, opened_session)
            except (TelegramAPIError, PublicTurnError, ValueError) as error:
                LOG.warning("inline_mode_panel_failed error=%s", type(error).__name__)

    def commit_selection(self, proposal: InlineProposal, session: InlineSession, inline_id: str | None,
                         source_message_id: int | None = None) -> PublicTurn:
        if (not inline_id and source_message_id is None) or not session.panel.chat_instance:
            raise InlineFlowError("Нужен итоговый inline-ход из группы.")
        if inline_id and proposal.inline_message_id and proposal.inline_message_id != inline_id:
            raise InlineFlowError("Этот выбор уже связан с другим сообщением.")
        if session.active_candidate and session.active_candidate != proposal.token:
            raise InlineFlowError("Этот вариант больше не активен.")
        if session.phase == "confirmed":
            turn = self.public_provider().turns.get(session.public_turn_id or "")
            if turn is None:
                raise InlineFlowError("Этот ход недоступен.")
            if source_message_id is not None:
                if turn.claim_id is not None and turn.claim_id != source_message_id:
                    raise InlineFlowError("Ход уже связан с другим сообщением группы.")
                turn.claim_id = source_message_id
                turn.inline_origin_verified = True
            if inline_id and proposal.inline_message_id is None:
                proposal.inline_message_id = inline_id
                turn.inline_claim_id = inline_id
            return turn
        if session.phase != "choosing":
            raise InlineFlowError("Выбор устарел.")
        if session.panel.match_id and not self.mode_enabled(session.panel.chat_id, session.user_id):
            raise InlineFlowError("Инлайн-режим выключен в настройках.")
        self.public_provider().validate_selection(session.panel.chat_id, session.user_id,
                                                   session.panel.match_id, session.panel.match_turn)
        real, declared = session.cards[proposal.real_index], DECK[proposal.declared_index]
        turn = self.public_provider().create_turn(session.panel.chat_id, session.panel.thread_id,
            session.user_id, session.user_name, ConfirmedMove(real, declared, real == declared), experiment=4,
            match_id=session.panel.match_id, match_turn=session.panel.match_turn)
        session.phase, session.active_candidate = "confirmed", proposal.token
        proposal.inline_message_id = inline_id
        session.public_turn_id = turn.token
        turn.inline_claim_id, turn.chat_instance, turn.control_token = inline_id, session.panel.chat_instance, proposal.token
        # The selected result already contains the final text and vote buttons.
        # Feedback has no chat_instance; defer inline edits until a verified vote.
        turn.inline_vote_ready = True
        turn.claim_id = source_message_id
        turn.inline_origin_verified = source_message_id is not None
        return turn

    async def on_group_choice(self, message: Message, bot: Bot) -> None:
        # Messages sent via this bot are an independent delivery path from
        # chosen_inline_result. They give the actual source group immediately.
        if (message.via_bot is None or message.via_bot.id != bot.id or message.from_user is None
                or message.from_user.is_bot or message.forward_origin is not None or message.reply_markup is None):
            return
        tokens = set()
        for row in message.reply_markup.inline_keyboard:
            for button in row:
                parts = (button.callback_data or "").split(":")
                if len(parts) == 4 and parts[:2] == ["lab4", "vote"]:
                    tokens.add(parts[2])
        if len(tokens) != 1:
            return
        try:
            proposal, session = self.get_proposal(tokens.pop(), message.from_user.id)
            if message.chat.id != session.panel.chat_id or message.message_thread_id != session.panel.thread_id:
                raise InlineFlowError("Выбор получен из другого чата или темы.")
            remember_profile(message.from_user)
            self.commit_selection(proposal, session, proposal.inline_message_id, message.message_id)
            await self.resume_public(bot, proposal, session)
            LOG.info("inline_group_choice_processed")
        except (InlineFlowError, PublicTurnError) as error:
            LOG.warning("inline_group_choice_rejected error=%s", type(error).__name__)

    async def on_chosen(self, chosen: ChosenInlineResult, bot: Bot | None = None) -> None:
        try:
            proposal, session = self.get_proposal(chosen.result_id, chosen.from_user.id)
        except InlineFlowError:
            return
        proposal.feedback_seen = True
        remember_profile(chosen.from_user)
        if bot is None:
            try:
                bot = chosen.bot
            except RuntimeError:
                pass
        # Telegram feedback has no chat ID. The issued query binds the author and
        # game; the vote later verifies chat_instance. Never expose the real face
        # in this inline message, including if its query was manually copied.
        if (not chosen.inline_message_id or bot is None
                or chosen.query.split(maxsplit=2)[:2] != ["lab4", session.panel.token]):
            return
        try:
            self.commit_selection(proposal, session, chosen.inline_message_id)
            await self.resume_public(bot, proposal, session)
        except (InlineFlowError, PublicTurnError) as error:
            LOG.warning("inline_choice_rejected error=%s", type(error).__name__)

    async def vote_chosen(self, callback: CallbackQuery, bot: Bot, token: str, vote: str) -> None:
        proposal = self.proposals.get(token)
        session = self.sessions.get(proposal.session_token) if proposal else None
        try:
            if (session is None or session.expired or session.phase == "cancelled"
                    or callback.from_user.is_bot or callback.from_user.id == session.user_id or vote not in {"yes", "no"}):
                raise InlineFlowError("На этот ход должен ответить другой участник.")
            self.require_inline_message(callback, proposal, session)
            remember_profile(callback.from_user)
            if session.panel.match_id:
                matches = self.public_provider().matches
                if session.phase == "choosing":
                    game = matches.validate_selection(session.panel.chat_id, session.user_id,
                                                       session.panel.match_id, session.panel.match_turn)
                else:
                    game = matches.validate_voter(session.panel.chat_id, session.panel.match_id,
                                                  session.public_turn_id, callback.from_user.id)
                if game.voter.user_id != callback.from_user.id:
                    raise InlineFlowError("На этот ход отвечает назначенный участник.")
            turn = self.commit_selection(proposal, session, callback.inline_message_id)
            turn.inline_origin_verified = True
            await self.public_provider().resume(bot, turn)
            self.public_provider().accept_vote(turn, callback.from_user.id, callback.from_user.full_name,
                                               callback.from_user.is_bot, vote)
            await self.answer(callback, "Ответ принят. Открываю карту.")
            await self.public_provider().resume(bot, turn)
        except (InlineFlowError, PublicTurnError, ValueError) as error:
            await self.answer(callback, str(error), True)
        except TelegramAPIError as error:
            LOG.warning("inline_vote_delivery_failed error=%s", type(error).__name__)
            await self.answer(callback, "Не все сообщения отправлены. Повторите кнопку.", True)

    async def answer(self, callback: CallbackQuery, text: str, alert: bool = False) -> None:
        try:
            await callback.answer(text=text, show_alert=alert, cache_time=0)
        except TelegramAPIError as error:
            LOG.warning("inline_callback_answer_failed error=%s", type(error).__name__)

    def panel_keyboard(self, panel: InlinePanel) -> InlineKeyboardMarkup:
        start = (inline_button("Скинуть карту", f"lab4 {panel.token}") if panel.chat_instance
                 else InlineKeyboardButton(text="Начать inline", callback_data=f"lab4:enable:{panel.token}"))
        return InlineKeyboardMarkup(inline_keyboard=[[start], [
            InlineKeyboardButton(text="Сбросить мой inline-выбор", callback_data=f"lab4:reset:{panel.token}")]])

    def claim_keyboard(self, token: str, committed: bool = False) -> InlineKeyboardMarkup:
        actions = (("Обновить / повторить отправку", "status"),) if committed else (
            ("Подтвердить ход", "confirm"), ("Отмена", "cancel"))
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=text, callback_data=f"lab4:{action}:{token}")
        ] for text, action in actions])

    def require_inline_message(self, callback: CallbackQuery, proposal: InlineProposal,
                               session: InlineSession) -> None:
        if (not callback.inline_message_id or not session.panel.chat_instance
                or callback.chat_instance != session.panel.chat_instance):
            raise InlineFlowError("Нажмите кнопку итогового заявления в исходной группе.")
        if proposal.inline_message_id and callback.inline_message_id != proposal.inline_message_id:
            raise InlineFlowError("Этот ход уже связан с другим сообщением.")

    def require_no_pending_turn(self, session: InlineSession) -> None:
        turn = self.public_provider().turns.get(session.public_turn_id or "")
        if turn and not turn.expired and not turn.finished:
            raise InlineFlowError("Сначала завершите опубликованный ход ответом другого участника.")

    async def resume_public(self, bot: Bot, proposal: InlineProposal, session: InlineSession) -> None:
        turn = self.public_provider().turns.get(session.public_turn_id or "")
        if turn is None or turn.expired:
            raise InlineFlowError("Ход устарел. Нажмите «Новый inline-ход».")
        try:
            await self.public_provider().resume(bot, turn)
        except TelegramAPIError as error:
            LOG.warning("inline_public_delivery_failed chat=%s error=%s",
                        session.panel.chat_id, type(error).__name__)
            if not turn.inline_origin_verified or not proposal.inline_message_id:
                return  # The original vote buttons can retry from the verified chat.
            # Edit the existing final statement, never send a separate status post.
            try:
                await apply_edit(bot.edit_message_reply_markup(
                    inline_message_id=proposal.inline_message_id,
                    reply_markup=self.claim_keyboard(proposal.token, committed=True),
                ))
            except TelegramAPIError as edit_error:
                LOG.warning("inline_retry_button_failed error=%s", type(edit_error).__name__)

    async def on_callback(self, callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) == 4 and parts[1] == "vote":
            await self.vote_chosen(callback, bot, parts[2], parts[3])
            return
        if len(parts) != 3:
            await self.answer(callback, "Неизвестная кнопка.", True)
            return
        _, action, token = parts
        try:
            if action in {"reset", "enable"}:
                await self.panel_action(callback, bot, token, action)
                return
            proposal, session = self.get_proposal(token, callback.from_user.id)
            self.require_inline_message(callback, proposal, session)
            if session.active_candidate and session.active_candidate != proposal.token:
                raise InlineFlowError("Этот вариант больше не активен.")
            if action == "confirm":
                if session.phase == "confirmed":
                    await self.answer(callback, "Этот ход уже подтвержден.")
                    await self.resume_public(bot, proposal, session)
                    return
                if session.phase != "choosing":
                    raise InlineFlowError("Начните новый выбор через /lab4.")
                if session.panel.match_id and not self.mode_enabled(session.panel.chat_id, session.user_id):
                    raise InlineFlowError("Инлайн-режим выключен в настройках группы.")
                self.public_provider().validate_selection(session.panel.chat_id, session.user_id,
                                                           session.panel.match_id, session.panel.match_turn)
                real, declared = session.cards[proposal.real_index], DECK[proposal.declared_index]
                turn = self.public_provider().create_turn(
                    session.panel.chat_id, session.panel.thread_id, session.user_id, session.user_name,
                    ConfirmedMove(real, declared, real == declared), experiment=4,
                    match_id=session.panel.match_id, match_turn=session.panel.match_turn,
                )
                # Commit exactly once, before any Telegram publication or edit.
                session.phase = "confirmed"
                session.active_candidate = proposal.token
                proposal.inline_message_id = callback.inline_message_id
                session.public_turn_id = turn.token
                turn.inline_claim_id = callback.inline_message_id
                turn.chat_instance = session.panel.chat_instance
                turn.control_token = proposal.token
                LOG.info("inline_move_confirmed chat=%s user=%s feedback_seen=%s",
                         session.panel.chat_id, session.user_id, proposal.feedback_seen)
                await self.answer(callback, "Inline-ход подтвержден. Публикую в группу.")
                await self.resume_public(bot, proposal, session)
            elif action == "status" and session.phase == "confirmed":
                await self.answer(callback, "Обновляю состояние хода.")
                await self.resume_public(bot, proposal, session)
            elif ((action == "cancel" and session.phase == "choosing")
                  or (action == "new" and session.phase == "confirmed")):
                if session.panel.match_id:
                    if action == "new":
                        raise InlineFlowError("Дождитесь сообщения бота с очередным ходом партии.")
                    self.public_provider().validate_selection(session.panel.chat_id, session.user_id,
                                                               session.panel.match_id, session.panel.match_turn)
                self.require_no_pending_turn(session)
                restart = InlineKeyboardMarkup(inline_keyboard=[[
                    inline_button("Скинуть карту", f"lab4 {session.panel.token}")]])
                if action == "cancel":
                    await apply_edit(bot.edit_message_text(
                        inline_message_id=callback.inline_message_id,
                        text="Выбор отменен.", reply_markup=restart))
                    session.phase = "cancelled"
                    if session.panel.match_id and self.return_modes:
                        await self.return_modes(bot, session)
                else:
                    await apply_edit(bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id,
                                                                   reply_markup=restart))
                    session.phase = "cancelled"
                await self.answer(callback, "Можно выбрать карты заново.")
            else:
                raise InlineFlowError("Эта кнопка недоступна на текущем шаге.")
        except (InlineFlowError, PublicTurnError) as error:
            await self.answer(callback, str(error), True)
        except TelegramAPIError as error:
            LOG.warning("inline_action_failed error=%s", type(error).__name__)
            await self.answer(callback, "Telegram не выполнил действие. Повторите кнопку или откройте /lab4.", True)

    async def panel_action(self, callback: CallbackQuery, bot: Bot, token: str, action: str) -> None:
        panel = self.panels.get(token)
        message = callback.message
        if (panel is None or time.monotonic() - panel.created_at > INLINE_TTL
                or not isinstance(message, Message) or message.chat.type not in GROUPS
                or message.ephemeral_message_id is not None
                or message.chat.id != panel.chat_id or message.message_id != panel.message_id
                or (panel.chat_instance is not None and callback.chat_instance != panel.chat_instance)):
            raise InlineFlowError("Откройте новую панель через /lab4 в группе.")
        if action == "enable":
            if panel.chat_instance is None:
                panel.chat_instance = callback.chat_instance
            session = self.get_session(panel, callback.from_user.id, callback.from_user.full_name)
            await self.answer(callback, "Подготавливаю лицевые карты для inline.")
            await self.prepare_faces(bot, session.user_id, session.cards)
            await apply_edit(bot.edit_message_reply_markup(chat_id=panel.chat_id, message_id=panel.message_id,
                                                           reply_markup=self.panel_keyboard(panel)))
            return
        session = self.sessions.get(self.users.get((panel.chat_id, panel.thread_id, callback.from_user.id), ""))
        if session:
            self.require_no_pending_turn(session)
            session.phase = "cancelled"
        await self.answer(callback, "Ваш inline-выбор сброшен. Нажмите «Мои карты через inline».")
