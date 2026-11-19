"""Shared group preferences; only the settings panel is delivered privately."""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from html import escape
import json
import logging
from pathlib import Path
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from public_turn import apply_edit

LOG = logging.getLogger("monkeys_lab")
PANEL_TTL = 60 * 60


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class CardModes:
    inline: bool = True
    ephemeral: bool = True
    revision: int = 0


@dataclass(frozen=True)
class PersonalMode:
    mode: str = "group"
    revision: int = 0


PERSONAL_MODES = {"group": "Как в группе", "inline": "Скинуть карту · инлайн",
                  "ephemeral": "Мои карты · эфемерно", "both": "Оба способа"}


class GroupSettings:
    def __init__(self, path: Path):
        self.path = path
        self.groups: dict[int, CardModes] = {}
        self.users: dict[int, PersonalMode] = {}
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                values = saved["groups"]
                for chat, value in values.items():
                    modes = CardModes(**value)
                    if (type(modes.inline) is not bool or type(modes.ephemeral) is not bool
                            or type(modes.revision) is not int or modes.revision < 0
                            or not (modes.inline or modes.ephemeral)):
                        raise ValueError("Invalid card modes")
                    self.groups[int(chat)] = modes
                for user, value in saved.get("users", {}).items():
                    preference = PersonalMode(**value)
                    if (preference.mode not in PERSONAL_MODES or type(preference.revision) is not int
                            or preference.revision < 0):
                        raise ValueError("Invalid personal mode")
                    self.users[int(user)] = preference
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise SettingsError("Не удалось прочитать настройки групп.") from error

    def get(self, chat_id: int) -> CardModes:
        return self.groups.get(chat_id, CardModes())

    def personal(self, user_id: int) -> PersonalMode:
        return self.users.get(user_id, PersonalMode())

    def effective(self, chat_id: int, user_id: int) -> CardModes:
        group = self.get(chat_id)
        mode = self.personal(user_id).mode
        inline = group.inline and mode != "ephemeral"
        ephemeral = group.ephemeral and mode != "inline"
        # A personal preference cannot leave a player without a permitted button.
        return CardModes(inline, ephemeral, group.revision) if inline or ephemeral else group

    def _save(self, groups: dict[int, CardModes], users: dict[int, PersonalMode]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"groups": {str(chat): asdict(modes) for chat, modes in groups.items()},
                                             "users": {str(user): asdict(mode) for user, mode in users.items()}},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as error:
            raise SettingsError("Не удалось сохранить настройки. Повторите действие.") from error

    def set_personal(self, user_id: int, mode: str, revision: int) -> PersonalMode:
        current = self.personal(user_id)
        if mode not in PERSONAL_MODES:
            raise SettingsError("Неизвестный способ выбора.")
        if revision != current.revision:
            raise SettingsError("Настройки уже изменились. Панель обновлена.")
        if mode == current.mode:
            return current
        updated = PersonalMode(mode, current.revision + 1)
        users = dict(self.users)
        users[user_id] = updated
        self._save(self.groups, users)
        self.users = users
        return updated

    def set_mode(self, chat_id: int, mode: str, enabled: bool, revision: int) -> CardModes:
        current = self.get(chat_id)
        if mode not in {"inline", "ephemeral"} or type(enabled) is not bool:
            raise SettingsError("Неизвестная настройка.")
        if revision != current.revision:
            raise SettingsError("Настройки уже изменились. Панель обновлена.")
        if getattr(current, mode) == enabled:
            return current
        updated = replace(current, **{mode: enabled}, revision=current.revision + 1)
        if not (updated.inline or updated.ephemeral):
            raise SettingsError("Оставьте включенным хотя бы один способ выбора карт.")
        saved = dict(self.groups)
        saved[chat_id] = updated
        self._save(saved, self.users)
        self.groups = saved
        return updated


@dataclass
class SettingsPanel:
    token: str
    chat_id: int
    owner_id: int
    title: str
    created_at: float
    message_id: int | None = None
    personal: bool = False


class SettingsUI:
    def __init__(self, store: GroupSettings, on_change: Callable[[Bot, int], Awaitable[None]],
                 on_personal_change: Callable[[Bot, int], Awaitable[None]] | None = None):
        self.store = store
        self.on_change = on_change
        self.on_personal_change = on_personal_change
        self.panels: dict[str, SettingsPanel] = {}
        self.router = Router(name="group_settings")
        self.router.message.register(self.command, Command("settings"))
        self.router.message.register(self.personal_command, Command("mysettings"))
        self.router.message.register(self.start, Command("start"), F.chat.type == ChatType.PRIVATE)
        self.router.callback_query.register(self.change, F.data.startswith("prefs:"))

    def text(self, panel: SettingsPanel) -> str:
        if panel.personal:
            selected = PERSONAL_MODES[self.store.personal(panel.owner_id).mode]
            return (f"<b>Мои настройки</b>\n\nВыбрано: {selected}.\n\n"
                    "Выберите удобный способ выбора карт. Эти настройки действуют только для вас.\n\n"
                    "Общие настройки группы определяют доступные способы. "
                    "Если ваш способ в группе выключен, будет показан доступный.")
        modes = self.store.get(panel.chat_id)
        state = lambda enabled: "включено" if enabled else "выключено"
        return (f"<b>Настройки игры</b>\nГруппа: {escape(panel.title)}\n\n"
                f"<b>Скинуть карту</b> — инлайн-режим: {state(modes.inline)}.\n"
                f"<b>Мои карты</b> — эфемерное отображение: {state(modes.ephemeral)}.\n\n"
                "Настройки общие для этой группы.\n"
                "Нажмите кнопку, чтобы включить или выключить способ. "
                "Хотя бы один должен оставаться включенным.")

    def keyboard(self, panel: SettingsPanel) -> InlineKeyboardMarkup:
        if panel.personal:
            preference = self.store.personal(panel.owner_id)
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"{'✅ ' if mode == preference.mode else ''}{label}",
                callback_data=f"prefs:{panel.token}:personal:{preference.revision}:{mode}")]
                for mode, label in PERSONAL_MODES.items()])
        modes = self.store.get(panel.chat_id)
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{'✅' if getattr(modes, mode) else '❌'} {label}",
                callback_data=f"prefs:{panel.token}:{mode}:{modes.revision}:{int(not getattr(modes, mode))}")
        ] for mode, label in (("inline", "Скинуть карту"), ("ephemeral", "Мои карты"))])

    async def send(self, bot: Bot, panel: SettingsPanel) -> None:
        sent = await bot.send_message(chat_id=panel.owner_id, text=self.text(panel), parse_mode="HTML",
                                      reply_markup=self.keyboard(panel))
        panel.message_id = sent.message_id

    async def command(self, message: Message, bot: Bot) -> None:
        await self.open_panel(message, bot, personal=False)

    async def personal_command(self, message: Message, bot: Bot) -> None:
        await self.open_panel(message, bot, personal=True)

    async def open_panel(self, message: Message, bot: Bot, personal: bool) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        if message.chat.type == ChatType.PRIVATE and not personal:
            await message.answer("Для настроек отправьте /settings в нужной группе. Панель придет сюда.")
            return
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}:
            return
        now = time.monotonic()
        self.panels = {token: panel for token, panel in self.panels.items() if now - panel.created_at < PANEL_TTL}
        panel = SettingsPanel(secrets.token_urlsafe(9), message.chat.id, message.from_user.id,
                              message.chat.title or "Группа", now, personal=personal)
        self.panels[panel.token] = panel
        try:
            await self.send(bot, panel)
        except (TelegramForbiddenError, TelegramBadRequest) as error:
            if isinstance(error, TelegramBadRequest) and "chat not found" not in error.message.lower():
                LOG.warning("settings_private_send_failed error=%s", type(error).__name__)
                await message.answer("Не удалось отправить настройки. Повторите /settings.")
                return
            me = await bot.get_me()
            await message.answer("Откройте личный чат с ботом и нажмите «Начать», чтобы получить настройки.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="Открыть настройки", url=f"https://t.me/{me.username}?start=settings_{panel.token}")]]))
        except TelegramAPIError as error:
            LOG.warning("settings_private_send_failed error=%s", type(error).__name__)
            await message.answer("Не удалось отправить настройки. Повторите /settings.")
        else:
            if message.chat.type == ChatType.PRIVATE:
                return
            await message.answer("Настройки отправлены вам в личные сообщения.")

    async def start(self, message: Message, bot: Bot) -> None:
        parts = (message.text or "").split(maxsplit=1)
        token = parts[1].removeprefix("settings_") if len(parts) == 2 and parts[1].startswith("settings_") else ""
        panel = self.panels.get(token)
        if (panel is None or message.from_user is None or panel.owner_id != message.from_user.id
                or time.monotonic() - panel.created_at >= PANEL_TTL):
            await message.answer("🐒 Добро пожаловать в MONKEYS CARDS! Игра проходит в группе: /game. Личные настройки: /mysettings. Общие настройки: /settings в нужной группе.")
            return
        await self.send(bot, panel)

    async def change(self, callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        panel = self.panels.get(parts[1]) if len(parts) == 5 else None
        message = callback.message
        if (panel is None or panel.owner_id != callback.from_user.id or not isinstance(message, Message)
                or message.chat.type != ChatType.PRIVATE or message.chat.id != panel.owner_id
                or message.message_id != panel.message_id or time.monotonic() - panel.created_at >= PANEL_TTL
                or not parts[3].isascii() or not parts[3].isdigit()
                or (panel.personal and (parts[2] != "personal" or parts[4] not in PERSONAL_MODES))
                or (not panel.personal and parts[4] not in {"0", "1"})):
            await callback.answer("Панель недоступна. Вызовите /settings в группе заново.", show_alert=True)
            return
        try:
            if panel.personal:
                self.store.set_personal(panel.owner_id, parts[4], int(parts[3]))
            else:
                self.store.set_mode(panel.chat_id, parts[2], parts[4] == "1", int(parts[3]))
        except SettingsError as error:
            await callback.answer(str(error), show_alert=True)
        else:
            await callback.answer("Личные настройки сохранены." if panel.personal else "Настройки группы сохранены.")
            try:
                if panel.personal:
                    if self.on_personal_change:
                        await self.on_personal_change(bot, panel.owner_id)
                else:
                    await self.on_change(bot, panel.chat_id)
            except TelegramAPIError as error:
                LOG.warning("settings_turn_refresh_failed error=%s", type(error).__name__)
        await apply_edit(bot.edit_message_text(chat_id=panel.owner_id, message_id=panel.message_id,
                         text=self.text(panel), parse_mode="HTML", reply_markup=self.keyboard(panel)))
