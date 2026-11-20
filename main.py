
import asyncio
import html
import json
import os
import random
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    ChosenInlineResult,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultCachedPhoto,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaPhoto,
)
from aiogram.exceptions import TelegramBadRequest


# =====================================================
# НАСТРОЙКИ БОТА
# =====================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в переменных окружения")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

GAMES_FILE = "games.json"

MAX_PLAYERS = 5
CARDS_PER_PLAYER = 5
TURNS_PER_PLAYER = 5

RANKS = [
    "6", "7", "8", "9", "10",
    "Валет", "Дама", "Король", "Туз"
]

SUITS = ["черви", "бубны", "трефы", "пики"]

CARDS = [
    f"{rank}|{suit}"
    for suit in SUITS
    for rank in RANKS
]


# =====================================================
# РУБАШКА КАРТЫ
# =====================================================

BACK_IMAGE_FILE_ID = (
    "AgACAgIAAxkBAAOdavzc2_WbtXpPrntJDYHSPXYt4oMA"
    "ArYeaxsnouhLeRqhqh9asgsBAAMCAAN5AAM9BA"
)


# =====================================================
# ВСЕ 36 ЛИЦЕВЫХ СТОРОН КАРТ
# =====================================================

CARD_IMAGE_FILE_IDS = {

    # ЧЕРВИ

    "6|черви":
    "AgACAgIAAxkBAAM6avzIGO3WJSoLxCK_unEVy7pdsf4AAoMeaxsnouhLSexYtUmC_6MBAAMCAAN4AAM9BA",

    "7|черви":
    "AgACAgIAAxkBAANgavzXHTqVYY5jfr1MNUSjKT7iAtkAApoeaxsnouhLCEkTQKIbgv4BAAMCAAN4AAM9BA",

    "8|черви":
    "AgACAgIAAxkBAANiavzXJya4IdunanjUaHAiE9O7IaoAApseaxsnouhLkF1MzqY0rRYBAAMCAAN4AAM9BA",

    "9|черви":
    "AgACAgIAAxkBAANkavzXKgm20YNtI87Ksb31uYXr9CMAApweaxsnouhL7La5DZQcIvQBAAMCAAN4AAM9BA",

    "10|черви":
    "AgACAgIAAxkBAANmavzXLz3G9Ims_nuiVX0-zIdcO6UAAp0eaxsnouhLFbFbRfHnTxwBAAMCAAN4AAM9BA",

    "Валет|черви":
    "AgACAgIAAxkBAANoavzXNIO0T7CT6tRDfKeTqWrAwIcAAp4eaxsnouhLQkDTjFan98cBAAMCAAN4AAM9BA",

    "Дама|черви":
    "AgACAgIAAxkBAANsavzXRY_8lt63hYG4gcc_0IhRsWIAAp8eaxsnouhLajwhcnRZ9RwBAAMCAAN4AAM9BA",

    "Король|черви":
    "AgACAgIAAxkBAANuavzXSt3aExPXvL4NJ3jLwF3f6gsAAqAeaxsnouhLjuny34kj4jIBAAMCAAN4AAM9BA",

    "Туз|черви":
    "AgACAgIAAxkBAANwavzXUZFpkZuAaqq-fJfAAAGQiEcMAAKhHmsbJ6LoS_ZEn46LLtikAQADAgADeAADPQQ",


    # БУБНЫ

    "6|бубны":
    "AgACAgIAAxkBAANEavzWWTw4uZx3FOhk2tl67lRgmS0AApEeaxsnouhLtOlfQiank9cBAAMCAAN4AAM9BA",

    "7|бубны":
    "AgACAgIAAxkBAANFavzWWVoSsbhYQ9EvEt_bDy_Yf5MAApIeaxsnouhLFysAARvOSlanAQADAgADeAADPQQ",

    "8|бубны":
    "AgACAgIAAxkBAANHavzWWfjjo20TN1sv23nCcryPma0AApQeaxsnouhLOSt2441PYxwBAAMCAAN4AAM9BA",

    "9|бубны":
    "AgACAgIAAxkBAANIavzWWetshtija2Cw8doRAywUx_MAApUeaxsnouhLZ_nwrT3RvtcBAAMCAAN4AAM9BA",

    "10|бубны":
    "AgACAgIAAxkBAANKavzWWbpFWX6p79pK_FIGuEva-44AApceaxsnouhLOhmBTTJuTOEBAAMCAAN4AAM9BA",

    "Валет|бубны":
    "AgACAgIAAxkBAANLavzWWXTg_0PALzzd_uyQyHDvm-cAApgeaxsnouhLiBnaBtvN5XgBAAMCAAN4AAM9BA",

    "Дама|бубны":
    "AgACAgIAAxkBAANMavzWWdJSLsygQTXEkmlbZ9f3ctwAApkeaxsnouhLG7tHE270rN0BAAMCAAN4AAM9BA",

    "Король|бубны":
    "AgACAgIAAxkBAANJavzWWQ_q2rGqSSKU6JlkqN_86nsAApYeaxsnouhLaiPynp5oHLcBAAMCAAN4AAM9BA",

    "Туз|бубны":
    "AgACAgIAAxkBAANGavzWWei9TTNlCS56lJSlo6ds9YEAApMeaxsnouhLfjHADOQAAehIAQADAgADeAADPQQ",


    # ТРЕФЫ

    "6|трефы":
    "AgACAgIAAxkBAANzavzbOk6pmXe7hip5fHUimK0ivaQAAqMeaxsnouhLA1Xubv_w2OsBAAMCAAN4AAM9BA",

    "7|трефы":
    "AgACAgIAAxkBAAN1avzbP-kVfaBuAAEq3qI2KYMcnnObAAKkHmsbJ6LoSzmivfWpjFiUAQADAgADeAADPQQ",

    "8|трефы":
    "AgACAgIAAxkBAAN3avzbQbutzLnkEKTVWEXQjILReKEAAqUeaxsnouhLU-7BzOClLQEBAAMCAAN4AAM9BA",

    "9|трефы":
    "AgACAgIAAxkBAAN5avzbQlvot7Ja7noOVoZUjqlnnPgAAqYeaxsnouhLr8mgkgj2JYYBAAMCAAN4AAM9BA",

    "10|трефы":
    "AgACAgIAAxkBAAN7avzbRVtYFtnGCPJXxt2ItA62bDEAAqceaxsnouhLMWbUkNCoTo4BAAMCAAN4AAM9BA",

    "Валет|трефы":
    "AgACAgIAAxkBAAN9avzbSH1i4t-EOxDojx6d6YmUl3AAAqgeaxsnouhLHCYt1AnyCCUBAAMCAAN4AAM9BA",

    "Дама|трефы":
    "AgACAgIAAxkBAAN_avzbS4EqdP7LauJGRTn_ss0jfusAAqkeaxsnouhL-poRZaicYUYBAAMCAAN5AAM9BA",

    "Король|трефы":
    "AgACAgIAAxkBAAOBavzbTu5UYJHsbo18xISGB5MPsVEAAqoeaxsnouhL5Gy1TM-PMe8BAAMCAAN4AAM9BA",

    "Туз|трефы":
    "AgACAgIAAxkBAAODavzbUHFL9iMgsl1Z4Iau5NAXigcAAqseaxsnouhLK_iJTHVmmxUBAAMCAAN4AAM9BA",


    # ПИКИ

    "6|пики":
    "AgACAgIAAxkBAAOFavzcTXb31EHSOXA8gBe3XOxd-WQAAq0eaxsnouhLo02YedCqpegBAAMCAAN4AAM9BA",

    "7|пики":
    "AgACAgIAAxkBAAOHavzcUWyaMJ_doD9hKJGbn-hs1IAAAq4eaxsnouhLp5f84horQlABAAMCAAN4AAM9BA",

    "8|пики":
    "AgACAgIAAxkBAAOJavzcVc_m9vyCnY_gO4pIs-37ZtoAAq8eaxsnouhLsgMkwjYo7aIBAAMCAAN4AAM9BA",

    "9|пики":
    "AgACAgIAAxkBAAONavzcYoHiEgGCcWCbJS2LEPwrdvMAArIeaxsnouhL_Ox7yGchXwEBAAMCAAN4AAM9BA",

    "10|пики":
    "AgACAgIAAxkBAAOMavzcYjLYwKBgiQs3keFomdxK-jYAArEeaxsnouhLqHfktN_DbFIBAAMCAAN4AAM9BA",

    "Валет|пики":
    "AgACAgIAAxkBAAOVavzcgQb3FIE_J3lpQ_iDqRxdysIAArMeaxsnouhLzEdLcBjtdBQBAAMCAAN5AAM9BA",

    "Дама|пики":
    "AgACAgIAAxkBAAOXavzchKTRu6De3TiMx_F5xWE1a_AAArQeaxsnouhLSIpxjWjUe0kBAAMCAAN4AAM9BA",

    "Король|пики":
    "AgACAgIAAxkBAAOLavzcYmql7duR_dWW3-MJiSAyfpMAArAeaxsnouhLueAbOZOtqSYBAAMCAAN4AAM9BA",

    "Туз|пики":
    "AgACAgIAAxkBAAObavzcjXHZke7es4USssoCLmjMYcgAArUeaxsnouhLnh5q6P2c6cMBAAMCAAN4AAM9BA",
}


# Проверяем, что все карты имеют изображения
if len(CARD_IMAGE_FILE_IDS) != 36:
    raise RuntimeError(
        f"В словаре карт найдено {len(CARD_IMAGE_FILE_IDS)} ID вместо 36"
    )


# =====================================================
# ХРАНЕНИЕ ИГР
# =====================================================

games = {}


def load_games():
    global games

    if not os.path.exists(GAMES_FILE):
        games = {}
        return

    try:
        with open(GAMES_FILE, "r", encoding="utf-8") as file:
            games = json.load(file)
    except Exception:
        games = {}


def save_games():
    temp_file = GAMES_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(games, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_file, GAMES_FILE)


def card_name(card):
    rank, suit = card.split("|")
    return f"{rank} {suit}"


def player_name(user):
    return html.escape(
        user.full_name or user.username or str(user.id)
    )


def get_game(chat_id):
    return games.get(str(chat_id))


def current_author(game):
    index = game["turns_done"] % len(game["players"])
    return game["players"][index]


def next_player(game):
    index = game["turns_done"] % len(game["players"])
    next_index = (index + 1) % len(game["players"])
    return game["players"][next_index]


def player_by_id(game, user_id):
    for player in game["players"]:
        if player["id"] == user_id:
            return player
    return None


def find_active_game_for_author(user_id):
    for chat_id, game in games.items():
        if game.get("status") != "active":
            continue

        if game.get("pending") is not None:
            continue

        if game.get("turns_done", 0) >= game.get("total_turns", 0):
            continue

        author = current_author(game)

        if author["id"] == user_id:
            return chat_id, game

    return None, None


# =====================================================
# КЛАВИАТУРЫ
# =====================================================

def lobby_keyboard(chat_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🐒 Войти в игру",
                    callback_data=f"join:{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎴 Начать игру",
                    callback_data=f"begin:{chat_id}"
                )
            ]
        ]
    )


def vote_keyboard(token):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ВЕРЮ ❤️",
                    callback_data=f"vote:{token}:yes"
                ),
                InlineKeyboardButton(
                    text="НЕ ВЕРЮ 🐒",
                    callback_data=f"vote:{token}:no"
                )
            ]
        ]
    )


# =====================================================
# /START
# =====================================================

@dp.message(CommandStart())
async def start_command(message: Message):
    await message.answer(
        "🐒 <b>Добро пожаловать в MONKEYS CARDS!</b>\n\n"
        "Это игра в правду и блеф с карточной колодой.\n\n"
        "Все игровые действия происходят в общей группе.\n\n"
        "Чтобы начать игру, используйте команду /game."
    )


# =====================================================
# /GAME — СОЗДАНИЕ ЛОББИ
# =====================================================

@dp.message(Command("game"))
async def create_game(message: Message):

    if message.chat.type == "private":
        await message.answer(
            "🐒 Создавать игру нужно в общей группе."
        )
        return

    chat_id = str(message.chat.id)

    if chat_id in games and games[chat_id].get("status") in (
        "lobby", "active"
    ):
        await message.answer(
            "В этой группе уже есть активная игра."
        )
        return

    creator = {
        "id": message.from_user.id,
        "name": player_name(message.from_user)
    }

    games[chat_id] = {
        "status": "lobby",
        "creator_id": message.from_user.id,
        "players": [creator],
        "hands": {},
        "turns_done": 0,
        "turns_by_player": {},
        "total_turns": 0,
        "pending": None,
        "choices": {}
    }

    save_games()

    await message.answer(
        "🐒 <b>MONKEYS CARDS — новая игра!</b>\n\n"
        f"Создатель: {creator['name']}\n\n"
        "Игроки, нажимайте кнопку входа.\n"
        "Минимум 2 игрока, максимум 5.",
        reply_markup=lobby_keyboard(chat_id)
    )


# =====================================================
# ВХОД В ИГРУ
# =====================================================

@dp.callback_query(F.data.startswith("join:"))
async def join_game(callback: CallbackQuery):

    chat_id = callback.data.split(":", 1)[1]
    game = get_game(chat_id)

    if not game or game["status"] != "lobby":
        await callback.answer(
            "Набор игроков уже закрыт.",
            show_alert=True
        )
        return

    user = callback.from_user

    if player_by_id(game, user.id):
        await callback.answer(
            "Ты уже участвуешь!",
            show_alert=True
        )
        return

    if len(game["players"]) >= MAX_PLAYERS:
        await callback.answer(
            "Все места уже заняты.",
            show_alert=True
        )
        return

    game["players"].append({
        "id": user.id,
        "name": player_name(user)
    })

    save_games()

    players_text = "\n".join(
        f"• {player['name']}"
        for player in game["players"]
    )

    await callback.message.edit_text(
        "🐒 <b>MONKEYS CARDS — набор игроков</b>\n\n"
        f"{players_text}\n\n"
        f"Игроков: {len(game['players'])}/{MAX_PLAYERS}\n\n"
        "Можно начинать игру!",
        reply_markup=lobby_keyboard(chat_id)
    )

    await callback.answer("Ты в игре! ❤️")


# =====================================================
# НАЧАЛО ИГРЫ И РАЗДАЧА
# =====================================================

@dp.callback_query(F.data.startswith("begin:"))
async def begin_game(callback: CallbackQuery):

    chat_id = callback.data.split(":", 1)[1]
    game = get_game(chat_id)

    if not game or game["status"] != "lobby":
        await callback.answer(
            "Игра уже началась или не найдена.",
            show_alert=True
        )
        return

    if callback.from_user.id != game["creator_id"]:
        await callback.answer(
            "Начать игру может только создатель.",
            show_alert=True
        )
        return

    if len(game["players"]) < 2:
        await callback.answer(
            "Нужно минимум 2 игрока.",
            show_alert=True
        )
        return

    deck = CARDS.copy()
    random.shuffle(deck)

    game["hands"] = {}

    for player in game["players"]:
        user_id = str(player["id"])

        game["hands"][user_id] = [
            deck.pop()
            for _ in range(CARDS_PER_PLAYER)
        ]

    game["turns_done"] = 0
    game["turns_by_player"] = {
        str(player["id"]): 0
        for player in game["players"]
    }

    game["total_turns"] = (
        len(game["players"]) * TURNS_PER_PLAYER
    )

    game["pending"] = None
    game["choices"] = {}
    game["status"] = "active"

    save_games()

    await callback.message.edit_text(
        "🎴 <b>ИГРА НАЧАЛАСЬ!</b>\n\n"
        f"Игроков: {len(game['players'])}\n"
        f"Каждый получил по {CARDS_PER_PLAYER} карт.\n\n"
        f"Всего ходов: {game['total_turns']}\n\n"
        "Первый игрок может делать ход!\n\n"
        "Для выбора карты напишите в чате:\n"
        "<code>@monkeysmafcardsbot + ваш факт</code>\n\n"
        "Знак + означает правду, знак - означает блеф."
    )

    await callback.answer("Раздача завершена! ❤️")


# =====================================================
# INLINE MODE — ВЫБОР КАРТЫ
# =====================================================

@dp.inline_query()
async def inline_cards(inline_query: InlineQuery):

    query = inline_query.query.strip()

    if not query or query[0] not in ("+", "-"):
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="help",
                    title="Как сделать ход?",
                    description="Напиши + факт или - факт",
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            "Чтобы сделать ход, введи:\n"
                            "@monkeysmafcardsbot + факт\n"
                            "или\n"
                            "@monkeysmafcardsbot - факт"
                        )
                    )
                )
            ],
            cache_time=0,
            is_personal=True
        )
        return

    is_truth = query[0] == "+"
    fact = query[1:].strip()

    if not fact:
        await inline_query.answer(
            results=[],
            cache_time=0,
            is_personal=True
        )
        return

    chat_id, game = find_active_game_for_author(
        inline_query.from_user.id
    )

    if not game:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="not_your_turn",
                    title="Сейчас не твой ход",
                    description="Дождись своей очереди в MONKEYS CARDS",
                    input_message_content=InputTextMessageContent(
                        message_text="Сейчас не твой ход 🐒"
                    )
                )
            ],
            cache_time=0,
            is_personal=True
        )
        return

    author = current_author(game)

    hand = game["hands"].get(str(author["id"]), [])

    if not hand:
        await inline_query.answer(
            results=[],
            cache_time=0,
            is_personal=True
        )
        return

    voter = next_player(game)

    results = []

    for card in hand:

        token = uuid.uuid4().hex[:12]

        game["choices"][token] = {
            "card": card,
            "is_truth": is_truth,
            "fact": fact,
            "author_id": author["id"],
            "chat_id": chat_id
        }

        results.append(
            InlineQueryResultCachedPhoto(
                id=token,
                photo_file_id=BACK_IMAGE_FILE_ID,
                title=f"Выбрать: {card_name(card)}",
                description="Закрытая карта — выбери её для хода",
                caption=(
                    "🐒 <b>MONKEYS CARDS</b>\n\n"
                    f"Ход {game['turns_done'] + 1} "
                    f"из {game['total_turns']}\n\n"
                    f"Игрок: {author['name']}\n"
                    f"Факт: {html.escape(fact)}\n\n"
                    f"Голосует: {voter['name']}"
                ),
                reply_markup=vote_keyboard(token),
                parse_mode=ParseMode.HTML
            )
        )

    save_games()

    await inline_query.answer(
        results=results,
        cache_time=0,
        is_personal=True
    )


# =====================================================
# ПОДТВЕРЖДЕНИЕ ВЫБРАННОЙ КАРТЫ
# =====================================================

@dp.chosen_inline_result()
async def chosen_card(chosen: ChosenInlineResult):

    token = chosen.result_id

    for chat_id, game in games.items():

        if game.get("status") != "active":
            continue

        choice = game.get("choices", {}).get(token)

        if not choice:
            continue

        if choice["author_id"] != chosen.from_user.id:
            return

        if game.get("pending") is not None:
            return

        author = current_author(game)

        if author["id"] != chosen.from_user.id:
            return

        user_id = str(author["id"])
        card = choice["card"]

        if card not in game["hands"].get(user_id, []):
            return

        # Убираем карту из руки автора
        game["hands"][user_id].remove(card)

        game["pending"] = {
            "token": token,
            "card": card,
            "is_truth": choice["is_truth"],
            "fact": choice["fact"],
            "author_id": author["id"],
            "voter_id": next_player(game)["id"],
            "inline_message_id": chosen.inline_message_id
        }

        # Удаляем использованный вариант
        game["choices"].pop(token, None)

        save_games()
        return


# =====================================================
# ГОЛОСОВАНИЕ
# =====================================================

@dp.callback_query(F.data.startswith("vote:"))
async def vote_card(callback: CallbackQuery):

    parts = callback.data.split(":")

    if len(parts) != 3:
        await callback.answer("Ошибка голосования.")
        return

    token = parts[1]
    vote = parts[2]

    if vote not in ("yes", "no"):
        await callback.answer("Некорректный ответ.")
        return

    found_game = None
    found_chat_id = None

    for chat_id, game in games.items():
        pending = game.get("pending")

        if pending and pending.get("token") == token:
            found_game = game
            found_chat_id = chat_id
            break

    if not found_game:
        await callback.answer(
            "Этот ход уже завершён.",
            show_alert=True
        )
        return

    game = found_game
    pending = game["pending"]

    if callback.from_user.id != pending["voter_id"]:
        await callback.answer(
            "Голосовать может только следующий игрок!",
            show_alert=True
        )
        return

    voter_id = pending["voter_id"]
    author_id = pending["author_id"]

    voter_guessed = (
        (vote == "yes" and pending["is_truth"])
        or
        (vote == "no" and not pending["is_truth"])
    )

    card = pending["card"]

    if voter_guessed:
        # Правильно угадал — карта возвращается автору
        receiver_id = author_id
        result_text = "❤️ Игрок угадал! Карта возвращается автору."
    else:
        # Ошибся — карта переходит голосующему
        receiver_id = voter_id
        result_text = "🐒 Игрок ошибся! Карта переходит ему."

    game["hands"][str(receiver_id)].append(card)

    author = player_by_id(game, author_id)
    voter = player_by_id(game, voter_id)

    game["turns_by_player"][str(author_id)] += 1
    game["turns_done"] += 1

    fact = html.escape(pending["fact"])
    card_title = html.escape(card_name(card))

    if pending["is_truth"]:
        truth_text = "Это была ПРАВДА ❤️"
    else:
        truth_text = "Это был БЛЕФ 🐒"

    caption = (
        "🎴 <b>MONKEYS CARDS — карта открыта!</b>\n\n"
        f"Факт: {fact}\n\n"
        f"Автор: {author['name']}\n"
        f"Голосовал: {voter['name']}\n\n"
        f"Ответ: {truth_text}\n"
        f"{result_text}\n\n"
        f"Открытая карта: <b>{card_title}</b>"
    )

    save_games()

    # Открываем лицо карты только после голосования
    inline_message_id = pending.get("inline_message_id")

    try:
        if inline_message_id:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaPhoto(
                    media=CARD_IMAGE_FILE_IDS[card],
                    caption=caption,
                    parse_mode=ParseMode.HTML
                ),
                reply_markup=None
            )
        else:
            await bot.send_photo(
                chat_id=int(found_chat_id),
                photo=CARD_IMAGE_FILE_IDS[card],
                caption=caption
            )

    except TelegramBadRequest:
        # Если Telegram не разрешил изменить inline-сообщение,
        # отправляем открытую карту отдельным сообщением
        await bot.send_photo(
            chat_id=int(found_chat_id),
            photo=CARD_IMAGE_FILE_IDS[card],
            caption=caption
        )

    game["pending"] = None
    game["choices"] = {}

    save_games()

    await callback.answer("Голос принят! ❤️")

    if game["turns_done"] >= game["total_turns"]:
        await finish_game(found_chat_id)
        return

    await bot.send_message(
        chat_id=int(found_chat_id),
        text=(
            f"🎴 <b>Ход {game['turns_done'] + 1} "
            f"из {game['total_turns']}</b>\n\n"
            f"Следующий автор: {current_author(game)['name']}\n\n"
            "Для хода используй:\n"
            "<code>@monkeysmafcardsbot + факт</code>\n"
            "или\n"
            "<code>@monkeysmafcardsbot - факт</code>"
        )
    )


# =====================================================
# /STATA — СТАТИСТИКА
# =====================================================

@dp.message(Command("stata"))
async def show_stats(message: Message):

    game = get_game(str(message.chat.id))

    if not game or game["status"] != "active":
        await message.answer("Сейчас нет активной игры.")
        return

    lines = ["📊 <b>MONKEYS CARDS — статистика</b>\n"]

    for player in game["players"]:
        count = len(
            game["hands"].get(str(player["id"]), [])
        )

        lines.append(
            f"{player['name']} — <b>{count}</b> карт"
        )

    lines.append(
        f"\nХод: {game['turns_done']} из {game['total_turns']}"
    )

    await message.answer("\n".join(lines))


# =====================================================
# ЗАВЕРШЕНИЕ ИГРЫ
# =====================================================

async def finish_game(chat_id):

    game = get_game(str(chat_id))

    if not game:
        return

    results = []

    for player in game["players"]:
        count = len(
            game["hands"].get(str(player["id"]), [])
        )

        results.append((count, player))

    results.sort(key=lambda item: item[0])

    best_score = results[0][0]

    winners = [
        player
        for count, player in results
        if count == best_score
    ]

    lines = ["🏆 <b>MONKEYS CARDS — ИГРА ОКОНЧЕНА!</b>\n"]

    for position, (count, player) in enumerate(results, start=1):
        lines.append(
            f"{position}. {player['name']} — {count} карт"
        )

    lines.append("")

    if len(winners) == 1:
        lines.append(
            f"👑 Победитель: {winners[0]['name']}!"
        )
    else:
        names = ", ".join(
            player["name"]
            for player in winners
        )
        lines.append(
            f"🤝 Ничья! Победители: {names}"
        )

    await bot.send_message(
        chat_id=int(chat_id),
        text="\n".join(lines)
    )

    games.pop(str(chat_id), None)
    save_games()


# =====================================================
# /STOP — ОСТАНОВКА ИГРЫ АДМИНИСТРАТОРОМ
# =====================================================

@dp.message(Command("stop"))
async def stop_game(message: Message):

    if message.chat.type == "private":
        return

    member = await bot.get_chat_member(
        message.chat.id,
        message.from_user.id
    )

    if member.status not in ("creator", "administrator"):
        await message.answer(
            "Остановить игру может только администратор группы."
        )
        return

    chat_id = str(message.chat.id)

    if chat_id not in games:
        await message.answer("Активной игры нет.")
        return

    games.pop(chat_id, None)
    save_games()

    await message.answer(
        "🛑 Игра MONKEYS CARDS остановлена."
    )


# =====================================================
# ЗАПУСК
# =====================================================

async def main():

    load_games()

    print("MONKEYS CARDS запущен!")
    print("Карт в колоде:", len(CARD_IMAGE_FILE_IDS))

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    asyncio.run(main())