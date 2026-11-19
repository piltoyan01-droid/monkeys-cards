"""Four independent Telegram group experiments, including an inline fallback."""

import asyncio
from dataclasses import dataclass, field, replace
import logging
import random
import secrets
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError, TelegramConflictError,
    TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, EphemeralMessageParameters, ErrorEvent,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import ConfigurationError, ROOT, load_settings
from card_selection import (
    DECK, Phase, Selection, SelectionError, advance_selection,
    selection_keyboard, selection_text,
)
from public_turn import PublicLab, PublicTurn, PublicTurnError, remember_profile
from inline_mode import InlineLab
from match_engine import MatchError, MatchManager
from game_flow import GameFlow
from group_settings import GroupSettings, SettingsUI

LOG = logging.getLogger("monkeys_lab")
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
SESSION_TTL = 30 * 60
RECEIPT_TIMEOUT = 30
router = Router()
matches = MatchManager()
public_lab = PublicLab(ROOT / "assets" / "stickers", matches)


@dataclass
class HandView:
    chat_id: int
    user_id: int
    nonce: str
    cards: tuple[str, ...]
    created_at: float
    ephemeral_id: int | None = None
    selected: int | None = None
    revision: int = 0
    seen: bool = False
    experiment: int = 1
    selection: Selection = field(default_factory=Selection)
    thread_id: int | None = None
    public_turn_id: str | None = None
    match_id: str | None = None
    match_turn: int | None = None


# One current view per group/user. Memory is discarded on restart.
views: dict[tuple[int, int], HandView] = {}
receipt_tasks: set[asyncio.Task] = set()


def current_cards(chat_id: int, user_id: int) -> tuple[str, ...] | None:
    game = matches.ongoing(chat_id)
    if game and game.status == "active":
        return tuple(game.hands[user_id]) if user_id in game.hands else None
    view = views.get((chat_id, user_id))
    return view.cards if view and time.monotonic() - view.created_at <= SESSION_TTL else None


inline_lab = InlineLab(lambda: public_lab, current_cards)


async def close_private_view(bot: Bot, chat_id: int, user_id: int) -> None:
    view = views.pop((chat_id, user_id), None)
    if view and view.ephemeral_id is not None:
        try:
            await bot.delete_ephemeral_message(chat_id=chat_id, receiver_user_id=user_id,
                                               ephemeral_message_id=view.ephemeral_id)
        except TelegramAPIError as error:
            LOG.warning("private_view_close_failed error=%s", type(error).__name__)


group_settings = GroupSettings(ROOT / "data" / "group_settings.json")
game_flow = GameFlow(matches, lambda: public_lab, inline_lab, close_private_view,
                     group_settings.get, group_settings.effective)
settings_ui = SettingsUI(group_settings, game_flow.refresh_modes, game_flow.refresh_personal)
public_lab.after_game_turn = game_flow.after_turn


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Мои карты", callback_data="lab:open")
    ]])


def hand_keyboard(view: HandView) -> InlineKeyboardMarkup:
    if view.experiment in {2, 3}:
        keyboard = selection_keyboard(view.selection, view.cards, view.nonce, view.revision, view.experiment)
        if view.match_id and view.selection.phase == Phase.CONFIRMED:
            keyboard.inline_keyboard = [row for row in keyboard.inline_keyboard
                                         if not any(":new:" in (button.callback_data or "") for button in row)]
        elif view.match_id and game_flow.can_choose_mode(view.chat_id, view.user_id):
            keyboard.inline_keyboard.append([InlineKeyboardButton(
                text="Назад к выбору режима", callback_data=f"lab3:back:{view.nonce}:{view.revision}")])
        return keyboard
    rows = [[InlineKeyboardButton(
        text=card, callback_data=f"lab:select:{view.nonce}:{i}"
    )] for i, card in enumerate(view.cards)]
    rows.append([InlineKeyboardButton(
        text="Вижу карты", callback_data=f"lab:seen:{view.nonce}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hand_text(view: HandView) -> str:
    if view.experiment in {2, 3}:
        text = selection_text(view.selection, view.cards, view.experiment)
        if view.experiment == 3 and view.selection.phase == Phase.CONFIRMED:
            text += public_lab.status_text(view.public_turn_id)
        return text
    text = "MONKEYS CARDS · эксперимент 1\nВаши пять тестовых карт:\n\n"
    text += "\n".join(f"{i + 1}. {card}" for i, card in enumerate(view.cards))
    text += "\n\nНажмите карту: изменится только это персональное сообщение."
    if view.selected is not None:
        text += f"\nВыбрана: {view.cards[view.selected]} (тест, без розыгрыша)."
    if view.seen:
        text += "\nВы подтвердили отображение карт."
    if view.revision:
        text += f"\nОбновление: {view.revision}."
    return text


def failure_message(error: Exception) -> str:
    if isinstance(error, TelegramRetryAfter):
        return f"Лимит Telegram. Подождите {error.retry_after} сек. и нажмите «Мои карты»."
    if isinstance(error, TelegramNetworkError):
        return "Нет связи с Telegram. Проверьте интернет и повторите «Мои карты»."
    if isinstance(error, TelegramForbiddenError):
        return "Telegram запретил действие. Проверьте права бота в тестовой группе."
    return "Telegram не выполнил действие. Обновите клиент и снова нажмите «Мои карты»."


async def answer(callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=alert, cache_time=0)
    except TelegramAPIError as error:
        LOG.warning("callback_answer_failed error=%s", type(error).__name__)


async def watch_receipt(view: HandView) -> None:
    await asyncio.sleep(RECEIPT_TIMEOUT)
    if views.get((view.chat_id, view.user_id)) is view and not view.seen:
        # Absence of a click is NOT evidence of failed delivery.
        LOG.info("delivery_unconfirmed chat=%s user=%s (нет нажатия кнопок за 30 сек.)",
                 view.chat_id, view.user_id)


@router.message(Command("start", "help"), F.chat.type.in_(GROUPS))
async def welcome(message: Message) -> None:
    await message.answer(
        "🐒 <b>Добро пожаловать в MONKEYS CARDS!</b>\n\n"
        "Это игра в правду и блеф с карточной колодой.\n"
        "Все игровые действия происходят в общей группе.\n\n"
        "Чтобы начать игру, используйте команду /game.",
        parse_mode="HTML",
    )


@router.message(Command("lab"), F.chat.type.in_(GROUPS))
async def show_panel(message: Message) -> None:
    await message.answer(
        "MONKEYS CARDS · эксперимент 1\n"
        "Нажмите «Мои карты». Бот попробует показать пять карт персонально внутри группы.\n"
        "Если карты не появились: обновите Telegram и нажмите еще раз.\n"
        "Для проверки нужен второй участник с другим аккаунтом.",
        reply_markup=panel_keyboard(),
    )


@router.message(Command("lab2"), F.chat.type.in_(GROUPS))
async def show_selection_panel(message: Message) -> None:
    await message.answer(
        "MONKEYS CARDS · эксперимент 2\n"
        "Нажмите «Мои карты», выберите настоящую карту, заявите любую из 36 карт "
        "и подтвердите тестовый ход.\nВсе шаги и результат видны только вам внутри группы.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Мои карты", callback_data="lab2:open")
        ]]),
    )


@router.message(Command("lab3"), F.chat.type.in_(GROUPS))
async def show_public_panel(message: Message) -> None:
    await message.answer(
        "MONKEYS CARDS · эксперимент 3\n"
        "Нажмите «Мои карты», выберите настоящую и заявляемую карты, подтвердите ход.\n"
        "Бот опубликует рубашку и заявление. Первый ответ другого участника откроет карту.\n"
        "Угадали — карта возвращается автору. Ошиблись — карта достается вам.\n"
        "В группе проверяем по одному ходу за раз.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Мои карты", callback_data="lab3:open")
        ]]),
    )


@router.callback_query(F.data.startswith("lab3:game:"))
@router.callback_query(F.data == "lab3:open")
@router.callback_query(F.data == "lab2:open")
@router.callback_query(F.data == "lab:open")
async def open_hand(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type not in GROUPS:
        await answer(callback, "Эксперимент доступен только в общей группе.", True)
        return
    match_id = None
    match_turn = None
    game = None
    if (callback.data or "").startswith("lab3:game:"):
        parts = callback.data.split(":")
        try:
            if len(parts) != 4 or not parts[3].isascii() or not parts[3].isdigit():
                raise PublicTurnError("Некорректная кнопка хода партии.")
            match_id, match_turn = parts[2], int(parts[3])
            public_lab.validate_selection(message.chat.id, callback.from_user.id, match_id, match_turn, True)
            game = matches.find(message.chat.id, match_id)
            if game.pending is None and not game_flow.player_modes(game.chat_id, callback.from_user.id).ephemeral:
                raise PublicTurnError("Эфемерное отображение выключено в настройках группы.")
            if game.announcements.get(match_turn) != message.message_id:
                raise PublicTurnError("Откройте карты кнопкой в актуальном сообщении хода.")
        except (PublicTurnError, MatchError) as error:
            await answer(callback, str(error), True)
            return
        experiment = 3
    else:
        experiment = {"lab:open": 1, "lab2:open": 2, "lab3:open": 3}[callback.data]
        if matches.ongoing(message.chat.id):
            await answer(callback, "Сейчас идет партия. Используйте две кнопки выбора в ее актуальном ходе.", True)
            return
    now = time.monotonic()
    for key, old in list(views.items()):
        if now - old.created_at > SESSION_TTL:
            del views[key]
    key = (message.chat.id, callback.from_user.id)
    previous = views.get(key)
    cards = (tuple(game.hands[callback.from_user.id]) if game else
             previous.cards if previous else inline_lab.existing_cards(*key) or tuple(random.sample(DECK, 5)))
    same_context = (previous and previous.experiment == experiment and previous.cards == cards
                    and previous.match_id == match_id and previous.match_turn == match_turn)
    if same_context and now - previous.created_at < 2:
        await answer(callback, "Подождите пару секунд перед повторным нажатием.")
        return
    view = HandView(
        chat_id=message.chat.id, user_id=callback.from_user.id,
        nonce=secrets.token_urlsafe(6),
        cards=cards,
        created_at=now,
        experiment=experiment,
        selection=previous.selection if same_context else Selection(),
        thread_id=message.message_thread_id,
        public_turn_id=previous.public_turn_id if same_context else None,
        match_id=match_id, match_turn=match_turn,
    )
    if experiment == 3:
        pending = public_lab.active_turn(message.chat.id)
        if pending and pending.author_id == callback.from_user.id:
            # Recover the author's pending turn even after switching experiments.
            view.selection = Selection(phase=Phase.CONFIRMED, confirmed=pending.move)
            view.public_turn_id = pending.token
    # Put the view in memory before the request, so rapid callbacks can find it.
    views[key] = view
    try:
        sent = await bot.send_message(
            chat_id=view.chat_id, message_thread_id=message.message_thread_id,
            text=hand_text(view), reply_markup=hand_keyboard(view),
            ephemeral_message_parameters=EphemeralMessageParameters(
                receiver_user_id=view.user_id, callback_query_id=callback.id,
            ),
        )
        if sent.ephemeral_message_id is None:
            raise RuntimeError("Expected an ephemeral message identifier")
        view.ephemeral_id = sent.ephemeral_message_id
    except (TelegramAPIError, RuntimeError) as error:
        if views.get(key) is view:
            if previous:
                views[key] = previous
            else:
                views.pop(key, None)
        LOG.warning("ephemeral_send_failed chat=%s user=%s error=%s",
                    view.chat_id, view.user_id, type(error).__name__)
        # Never fall back to a public message or DM containing the hand.
        await answer(callback, failure_message(error), True)
        return
    LOG.info("ephemeral_api_accepted chat=%s user=%s (отображение еще не подтверждено)",
             view.chat_id, view.user_id)
    task = asyncio.create_task(watch_receipt(view))
    receipt_tasks.add(task)
    task.add_done_callback(receipt_tasks.discard)
    if game and view.selection.phase != Phase.CONFIRMED:
        try:
            await game_flow.show_mode(bot, game, view.user_id, "ephemeral")
        except (TelegramAPIError, MatchError) as error:
            LOG.warning("ephemeral_mode_panel_failed error=%s", type(error).__name__)
    await answer(callback)


@router.callback_query(F.data.startswith("lab:"))
async def update_hand(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type not in GROUPS:
        await answer(callback, "Эксперимент доступен только в общей группе.", True)
        return
    parts = (callback.data or "").split(":")
    view = views.get((message.chat.id, callback.from_user.id))
    if (view is None or view.experiment != 1 or len(parts) < 3 or parts[2] != view.nonce
            or view.ephemeral_id is None
            or message.ephemeral_message_id != view.ephemeral_id
            or (message.receiver_user is not None
                and message.receiver_user.id != callback.from_user.id)
            or time.monotonic() - view.created_at > SESSION_TTL):
        await answer(callback, "Это окно устарело. Нажмите «Мои карты» в общей панели.", True)
        return
    selected = view.selected
    if parts[1] == "select" and len(parts) == 4 and parts[3].isdigit():
        selected = int(parts[3])
        if not 0 <= selected < len(view.cards):
            await answer(callback, "Неизвестная карта.", True)
            return
    elif not (parts[1] == "seen" and len(parts) == 3):
        await answer(callback, "Неизвестная кнопка.", True)
        return
    # A callback from this window confirms that the client displayed it.
    view.seen = True
    view.selected = selected
    view.revision += 1
    LOG.info("client_interaction_confirmed chat=%s user=%s action=%s",
             view.chat_id, view.user_id, parts[1])
    try:
        await bot.edit_ephemeral_message_text(
            chat_id=view.chat_id, receiver_user_id=view.user_id,
            ephemeral_message_id=view.ephemeral_id,
            text=hand_text(view), reply_markup=hand_keyboard(view),
        )
    except TelegramAPIError as error:
        LOG.warning("ephemeral_edit_failed chat=%s user=%s error=%s",
                    view.chat_id, view.user_id, type(error).__name__)
        await answer(callback, failure_message(error), True)
        return
    LOG.info("ephemeral_edit_api_accepted chat=%s user=%s", view.chat_id, view.user_id)
    await answer(callback, "Обновление запрошено. Проверьте текст окна.")


async def refresh_public_view(callback: CallbackQuery, bot: Bot, view: HandView,
                              turn: PublicTurn) -> None:
    try:
        await public_lab.resume(bot, turn)
    except (TelegramAPIError, PublicTurnError) as error:
        LOG.warning("public_delivery_failed chat=%s error=%s", view.chat_id, type(error).__name__)
    try:
        await bot.edit_ephemeral_message_text(
            chat_id=view.chat_id, receiver_user_id=view.user_id,
            ephemeral_message_id=view.ephemeral_id, text=hand_text(view), reply_markup=hand_keyboard(view),
        )
    except TelegramAPIError as error:
        # A committed public turn must never become editable because a private edit failed.
        LOG.warning("public_status_edit_failed chat=%s error=%s", view.chat_id, type(error).__name__)


@router.callback_query(F.data.startswith("lab3:") & ~F.data.startswith("lab3:vote:"))
@router.callback_query(F.data.startswith("lab2:"))
async def update_selection(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type not in GROUPS:
        await answer(callback, "Эксперимент доступен только в общей группе.", True)
        return
    key = (message.chat.id, callback.from_user.id)
    view = views.get(key)
    parts = (callback.data or "").split(":")
    expected_experiment = 3 if (callback.data or "").startswith("lab3:") else 2
    if (view is None or view.experiment != expected_experiment or len(parts) not in {4, 5}
            or parts[2] != view.nonce or view.ephemeral_id is None
            or message.ephemeral_message_id != view.ephemeral_id
            or (message.receiver_user is not None
                and message.receiver_user.id != callback.from_user.id)
            or time.monotonic() - view.created_at > SESSION_TTL):
        await answer(callback, "Это окно устарело. Нажмите «Мои карты» в панели своего эксперимента.", True)
        return
    if parts[3] != str(view.revision):
        await answer(callback, "Эта кнопка от предыдущего шага. Используйте текущее окно.", True)
        return
    action = parts[1]
    if view.match_id and action not in {"status", "back"} and not game_flow.player_modes(view.chat_id, view.user_id).ephemeral:
        await answer(callback, "Эфемерное отображение выключено в настройках группы.", True)
        return
    if view.match_id or matches.ongoing(view.chat_id):
        try:
            if action == "status" and view.match_id:
                turn = public_lab.turns.get(view.public_turn_id or "")
                if turn is None or turn.match_id != view.match_id:
                    raise PublicTurnError("Этот ход партии недоступен.")
                public_lab.validate_managed_turn(turn)
            elif action == "new" and view.match_id:
                raise PublicTurnError("Следующего автора объявляет бот. Используйте актуальную панель хода.")
            else:
                public_lab.validate_selection(view.chat_id, view.user_id, view.match_id, view.match_turn)
        except PublicTurnError as error:
            await answer(callback, str(error), True)
            return
    value = None
    if action in {"select", "claim"}:
        if len(parts) != 5 or not parts[4].isascii() or not parts[4].isdigit() or len(parts[4]) > 2:
            await answer(callback, "Неизвестная карта.", True)
            return
        value = int(parts[4])
    elif len(parts) != 4:
        await answer(callback, "Неизвестная кнопка.", True)
        return
    if action == "back" and view.match_id:
        try:
            game = matches.find(view.chat_id, view.match_id)
            await game_flow.return_to_modes(bot, game, view.user_id)
            await answer(callback, "Выберите удобный режим в панели хода.")
        except MatchError as error:
            await answer(callback, str(error), True)
        return
    if view.experiment == 3 and action == "status":
        turn = public_lab.turns.get(view.public_turn_id or "")
        if turn is None or turn.expired:
            await answer(callback, "Ход устарел. Нажмите «Новый тестовый ход».", True)
            return
        view.seen = True
        candidate = replace(view, revision=view.revision + 1)
        views[key] = candidate
        await answer(callback, "Обновляю состояние хода.")
        await refresh_public_view(callback, bot, candidate, turn)
        return
    if view.experiment == 3 and action == "new":
        turn = public_lab.turns.get(view.public_turn_id or "")
        if turn and not turn.expired and not turn.finished:
            await answer(callback, "Сначала завершите текущий ход: нужен ответ другого участника.", True)
            return
    try:
        selection = advance_selection(view.selection, view.cards, action, value)
    except SelectionError as error:
        await answer(callback, str(error), True)
        return
    view.seen = True
    candidate = replace(view, selection=selection, revision=view.revision + 1)
    if view.experiment == 3 and action == "confirm":
        assert selection.confirmed is not None
        try:
            turn = public_lab.create_turn(view.chat_id, view.thread_id, view.user_id,
                                          callback.from_user.full_name, selection.confirmed,
                                          match_id=view.match_id, match_turn=view.match_turn)
        except PublicTurnError as error:
            await answer(callback, str(error), True)
            return
        candidate.public_turn_id = turn.token
        # Commit the immutable choice BEFORE publishing anything to the group.
        views[key] = candidate
        await answer(callback, "Ход подтвержден. Публикую в группу.")
        await refresh_public_view(callback, bot, candidate, turn)
        return
    if action == "new":
        candidate.public_turn_id = None
    LOG.info("client_interaction_confirmed chat=%s user=%s action=%s experiment=%s",
             view.chat_id, view.user_id, action, view.experiment)
    try:
        await bot.edit_ephemeral_message_text(
            chat_id=candidate.chat_id, receiver_user_id=candidate.user_id,
            ephemeral_message_id=candidate.ephemeral_id,
            text=hand_text(candidate), reply_markup=hand_keyboard(candidate),
        )
    except TelegramAPIError as error:
        # Keep the last committed choice. Reopening can safely retry this step.
        LOG.warning("ephemeral_edit_failed chat=%s user=%s error=%s experiment=%s",
                    view.chat_id, view.user_id, type(error).__name__, view.experiment)
        await answer(callback, failure_message(error), True)
        return
    views[key] = candidate
    # No card names, claims, or truth values are written to logs or public chat.
    LOG.info("selection_step_accepted chat=%s user=%s phase=%s",
             view.chat_id, view.user_id, selection.phase.value)
    await answer(callback, "Тестовый ход подтвержден." if action == "confirm" else "")


@router.callback_query(F.data.startswith("lab3:vote:"))
async def vote_public(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await answer(callback, "Ответ доступен только в исходном сообщении группы.", True)
        return
    turn = public_lab.turns.get(parts[2])
    if turn is None:
        await answer(callback, "Этот ход недоступен. Создайте новый через /lab3.", True)
        return
    if turn.inline_claim_id:
        valid_origin = (callback.inline_message_id == turn.inline_claim_id
                        and turn.chat_instance is not None and callback.chat_instance == turn.chat_instance)
    else:
        valid_origin = (isinstance(message, Message) and message.chat.type in GROUPS
                        and message.ephemeral_message_id is None
                        and message.chat.id == turn.chat_id and message.message_id == turn.claim_id)
    if not valid_origin:
        await answer(callback, "Ответ доступен только в исходном сообщении группы.", True)
        return
    try:
        public_lab.accept_vote(turn, callback.from_user.id, callback.from_user.full_name,
                               callback.from_user.is_bot, parts[3])
        remember_profile(callback.from_user)
    except PublicTurnError as error:
        await answer(callback, str(error), True)
        return
    # The decision is fixed before network requests. Other clicks cannot replace it.
    await answer(callback, "Ответ принят. Открываю карту.")
    try:
        await public_lab.resume(bot, turn)
    except (TelegramAPIError, PublicTurnError) as error:
        LOG.warning("public_reveal_failed chat=%s error=%s", turn.chat_id, type(error).__name__)
        await answer(callback, "Не все сообщения отправлены. Автор может нажать «Обновить / повторить отправку».", True)
        return
    LOG.info("public_turn_finished chat=%s", turn.chat_id)


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    # Do not log the update or exception text: they can contain private data.
    LOG.error("update_failed error=%s", type(event.exception).__name__)
    if event.update.callback_query:
        await answer(event.update.callback_query, "Ошибка обработки. Нажмите «Мои карты» снова.", True)
    return True


async def check_identity(bot: Bot, expected_username: str) -> str:
    me = await bot.get_me()
    if (me.username or "").casefold() != expected_username.casefold():
        raise ConfigurationError("Токен принадлежит другому username. Проверьте основной бот и BOT_USERNAME.")
    webhook = await bot.get_webhook_info()
    if webhook.url:
        # Never delete another bot's webhook automatically.
        raise ConfigurationError("У бота настроен webhook. Сначала проверьте конфигурацию существующего сервиса.")
    return me.username or expected_username


async def main() -> None:
    settings = load_settings()
    async with Bot(token=settings.token, default=DefaultBotProperties(link_preview_is_disabled=True)) as bot:
        username = await check_identity(bot, settings.username)
        LOG.info("Готово: @%s. Polling запущен. Партия с регистрацией: /game@%s.",
                 username, username)
        dp = Dispatcher()
        dp.include_router(settings_ui.router)
        dp.include_router(router)
        dp.include_router(game_flow.router)
        dp.include_router(inline_lab.router)
        try:
            # Sequential handling avoids concurrent edits and duplicated hand opens.
            await dp.start_polling(
                bot, allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"],
                handle_as_tasks=False,
            )
        finally:
            for task in list(receipt_tasks):
                task.cancel()
            await asyncio.gather(*receipt_tasks, return_exceptions=True)
            views.clear()
            public_lab.turns.clear()
            public_lab.sticker_ids.clear()
            inline_lab.clear()
            matches.games.clear()


class SafeFrameworkHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # aiogram retries getUpdates failures internally. Expose only an allowlisted
        # error class, never its text, URL, traceback or update contents.
        if record.name != "aiogram.dispatcher":
            return
        kind = "TelegramAPIError"
        if isinstance(record.args, tuple) and record.args:
            candidate = record.args[0]
            if isinstance(candidate, str) and candidate in {
                "TelegramConflictError", "TelegramNetworkError",
                "TelegramRetryAfter", "TelegramUnauthorizedError",
            }:
                kind = candidate
        LOG.warning("polling_issue error=%s. Проверьте интернет и отсутствие второй копии бота.", kind)


def configure_logging() -> None:
    # Framework tracebacks can contain HTTP URLs with tokens and full updates.
    # Only our fixed diagnostic messages and exception class names are logged.
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(SafeFrameworkHandler())
    root.setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except ConfigurationError as error:
        # Only our configuration validation errors contain user-facing text.
        LOG.error("%s", error)
    except TelegramUnauthorizedError:
        LOG.error("Токен отклонен Telegram. Проверьте BOT_TOKEN существующего процесса.")
    except TelegramConflictError:
        LOG.error("Этот бот уже запущен в другом месте. Оставьте один процесс бота.")
    except TelegramNetworkError:
        LOG.error("Нет соединения с Telegram. Проверьте интернет и повторите запуск.")
    except Exception as error:
        LOG.error("Запуск остановлен: %s. Токен и подробности запроса скрыты.", type(error).__name__)
