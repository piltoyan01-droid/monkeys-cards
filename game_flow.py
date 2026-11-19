"""Group lobby, scheduled author/responder, next-turn panels and final scoring."""

import logging
import secrets
import time
from collections.abc import Awaitable
from typing import Callable

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, Message,
                           InputRichBlockParagraph, InputRichBlockDetails, RichTextBold)

from inline_mode import INLINE_TTL, InlineLab, InlinePanel, InlineSession
from match_engine import Match, MatchError, MatchManager, MAX_PLAYERS, TURNS_PER_PLAYER
from public_turn import PublicLab, PublicTurn, NO_PREVIEW, apply_edit, profile_link, result_text, rich_profile, remember_profile, turn_label
from group_settings import CardModes

LOG = logging.getLogger("monkeys_lab")
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


class GameFlow:
    def __init__(self, matches: MatchManager, public_provider: Callable[[], PublicLab], inline: InlineLab,
                 close_private: Callable[[Bot, int, int], Awaitable[None]] | None = None,
                 modes: Callable[[int], CardModes] = lambda chat: CardModes(),
                 effective_modes: Callable[[int, int], CardModes] | None = None):
        self.matches = matches
        self.public_provider = public_provider
        self.inline = inline
        self.close_private = close_private
        self.modes = modes
        self.player_modes = effective_modes or (lambda chat, user: self.modes(chat))
        # Inline card selection is reserved for standalone experiments. A real
        # match always uses the private ephemeral hand and ordinary group voting.
        self.inline.mode_enabled = lambda chat, user: (
            self.matches.ongoing(chat) is None and self.player_modes(chat, user).inline
        )
        self.inline.return_modes = self.back_from_inline
        self.inline.mode_opened = self.inline_opened
        self.selected_modes: dict[tuple[str, int], str] = {}
        self.router = Router(name="registered_games")
        self.router.message.register(self.create_game, Command("game"), F.chat.type.in_(GROUPS))
        self.router.message.register(self.stats, Command("stata"), F.chat.type.in_(GROUPS))
        self.router.message.register(self.stop, Command("stop"), F.chat.type.in_(GROUPS))
        self.router.message.register(self.leave, Command("leave"))
        self.router.callback_query.register(self.back_action, F.data.startswith("game:back:"))
        self.router.callback_query.register(self.lobby_action, F.data.startswith("game:") & ~F.data.startswith("game:back:"))

    def turn_keyboard(self, game: Match, panel: InlinePanel, mode: str | None = None) -> InlineKeyboardMarkup:
        ephemeral = InlineKeyboardButton(text="Мои карты",
                                         callback_data=f"lab3:game:{game.token}:{game.turns_done}")
        inline = InlineKeyboardButton(text="Скинуть карту", switch_inline_query_current_chat=f"lab4 {panel.token}")
        # The live match uses only the existing ephemeral private hand.
        # Inline-mode cards and votes belong to standalone experiments.
        return InlineKeyboardMarkup(inline_keyboard=[[ephemeral]])

    def can_choose_mode(self, chat_id: int, user_id: int) -> bool:
        # No mode switch during a registered match: selection stays ephemeral.
        return False

    async def show_mode(self, bot: Bot, game: Match, user_id: int, mode: str) -> None:
        self.matches.validate_selection(game.chat_id, user_id, game.token, game.turns_done)
        if not getattr(self.player_modes(game.chat_id, user_id), mode):
            raise MatchError("Этот способ выбора выключен в настройках группы.")
        key = (game.token, game.turns_done)
        if self.selected_modes.get(key) == mode:
            return
        panel = next(panel for panel in self.inline.panels.values()
                     if panel.match_id == game.token and panel.match_turn == game.turns_done)
        await apply_edit(bot.edit_message_reply_markup(chat_id=game.chat_id,
                         message_id=game.announcements[game.turns_done],
                         reply_markup=self.turn_keyboard(game, panel, mode)))
        self.selected_modes[key] = mode

    async def inline_opened(self, bot: Bot, session: InlineSession) -> None:
        game = self.matches.validate_selection(session.panel.chat_id, session.user_id,
                                               session.panel.match_id, session.panel.match_turn)
        assert game is not None
        await self.show_mode(bot, game, session.user_id, "inline")

    async def return_to_modes(self, bot: Bot, game: Match, user_id: int) -> None:
        self.matches.validate_selection(game.chat_id, user_id, game.token, game.turns_done)
        self.inline.cancel_selection(game.chat_id, user_id, game.token, game.turns_done)
        if self.close_private:
            await self.close_private(bot, game.chat_id, user_id)
        panel = next((panel for panel in self.inline.panels.values()
                      if panel.match_id == game.token and panel.match_turn == game.turns_done
                      and time.monotonic() - panel.created_at <= INLINE_TTL), None)
        if panel is None:
            panel = InlinePanel(secrets.token_urlsafe(9), game.chat_id, game.thread_id,
                                chat_instance=game.chat_instance, match_id=game.token, match_turn=game.turns_done)
            self.inline.panels[panel.token] = panel
        panel.message_id = game.announcements[game.turns_done]
        await apply_edit(bot.edit_message_reply_markup(chat_id=game.chat_id, message_id=panel.message_id,
                                                       reply_markup=self.turn_keyboard(game, panel)))
        self.selected_modes.pop((game.token, game.turns_done), None)

    async def refresh_modes(self, bot: Bot, chat_id: int) -> None:
        game = self.matches.ongoing(chat_id)
        if (game and game.status == "active" and game.pending is None
                and game.turns_done in game.announcements):
            if self.player_modes(chat_id, game.author.user_id).inline:
                await self.inline.prepare_faces(bot, game.author.user_id, tuple(game.hands[game.author.user_id]))
            await self.return_to_modes(bot, game, game.author.user_id)

    async def refresh_personal(self, bot: Bot, user_id: int) -> None:
        for game in list(self.matches.games.values()):
            if game.status == "active" and game.author.user_id == user_id:
                await self.refresh_modes(bot, game.chat_id)

    async def back_from_inline(self, bot: Bot, session: InlineSession) -> None:
        game = self.matches.validate_selection(session.panel.chat_id, session.user_id,
                                               session.panel.match_id, session.panel.match_turn)
        assert game is not None
        await self.return_to_modes(bot, game, session.user_id)

    async def back_action(self, callback: CallbackQuery, bot: Bot) -> None:
        message = callback.message
        parts = (callback.data or "").split(":")
        try:
            if (not isinstance(message, Message) or message.chat.type not in GROUPS or len(parts) != 4
                    or not parts[3].isascii() or not parts[3].isdigit()):
                raise MatchError("Используйте актуальную панель хода в группе.")
            game = self.matches.validate_selection(message.chat.id, callback.from_user.id, parts[2], int(parts[3]))
            assert game is not None
            if (message.message_id != game.announcements[game.turns_done]
                    or callback.chat_instance != game.chat_instance):
                raise MatchError("Это кнопка из другого сообщения или чата.")
            await self.return_to_modes(bot, game, callback.from_user.id)
            await self.answer(callback, "Выберите эфемерный или inline-режим в панели хода.")
        except MatchError as error:
            await self.answer(callback, str(error), True)
        except TelegramAPIError as error:
            LOG.warning("game_return_modes_failed error=%s", type(error).__name__)
            await self.answer(callback, "Не удалось обновить панель. Повторите кнопку.", True)

    def lobby_keyboard(self, game: Match) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Войти в игру", callback_data=f"game:join:{game.token}")
        ], [InlineKeyboardButton(text="Начать игру", callback_data=f"game:begin:{game.token}")]])

    def lobby_text(self, game: Match) -> str:
        players = "\n".join(f"{i + 1}. {profile_link(p.user_id, p.name)}" for i, p in enumerate(game.players))
        return (f"Регистрация в игру\n\n{players}\n\n"
                f"Игроков: {len(game.players)}/{MAX_PLAYERS}. Для старта нужны минимум 2.\n"
                f"По 5 карт и {TURNS_PER_PLAYER} ходов каждому.\n\n"
                "Начать игру может ее создатель.")

    async def answer(self, callback: CallbackQuery, text: str, alert: bool = False) -> None:
        try:
            await callback.answer(text=text, show_alert=alert, cache_time=0)
        except TelegramAPIError as error:
            LOG.warning("game_callback_answer_failed error=%s", type(error).__name__)

    async def create_game(self, message: Message, bot: Bot) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        remember_profile(message.from_user)
        current = self.matches.ongoing(message.chat.id)
        if (current and current.status == "active" and current.pending is None
                and current.turns_done not in current.announcements
                and any(player.user_id == message.from_user.id for player in current.players)):
            await self.announce_turn(bot, current)
            return
        try:
            if self.public_provider().active_turn(message.chat.id):
                raise MatchError("Сначала завершите уже опубликованный ход в группе.")
            game = self.matches.create(message.chat.id, message.message_thread_id,
                                       message.from_user.id, message.from_user.full_name)
        except MatchError as error:
            await message.answer(str(error))
            return
        sent = await bot.send_message(chat_id=game.chat_id, message_thread_id=game.thread_id,
                                      text=self.lobby_text(game), parse_mode="HTML", link_preview_options=NO_PREVIEW,
                                      reply_markup=self.lobby_keyboard(game))
        game.lobby_id = sent.message_id

    async def lobby_action(self, callback: CallbackQuery, bot: Bot) -> None:
        message = callback.message
        parts = (callback.data or "").split(":")
        if not isinstance(message, Message) or message.chat.type not in GROUPS or len(parts) != 3:
            await self.answer(callback, "Откройте /game в общей группе.", True)
            return
        try:
            game = self.matches.find(message.chat.id, parts[2])
            if message.message_id != game.lobby_id:
                raise MatchError("Это устаревшее сообщение регистрации.")
            remember_profile(callback.from_user)
            if parts[1] == "join":
                self.matches.join(game, callback.from_user.id, callback.from_user.full_name)
                await apply_edit(bot.edit_message_text(chat_id=game.chat_id, message_id=game.lobby_id,
                                                        text=self.lobby_text(game), parse_mode="HTML", link_preview_options=NO_PREVIEW,
                                                        reply_markup=self.lobby_keyboard(game)))
                await self.answer(callback, "Вы зарегистрированы.")
            elif parts[1] == "begin":
                # A repeated creator click may retry a failed first announcement.
                if game.status != "active":
                    self.matches.start(game, callback.from_user.id, callback.chat_instance)
                elif callback.from_user.id != game.creator_id:
                    raise MatchError("Начать игру может только ее создатель.")
                await self.answer(callback, "Игра началась.")
                await apply_edit(bot.edit_message_text(
                    chat_id=game.chat_id, message_id=game.lobby_id,
                    text=f"<b>Игра началась!</b>\n\nИгроков: {len(game.players)}.\n"
                         f"Каждому выдано по 5 карт. Всего ходов: {game.total_turns}.",
                    parse_mode="HTML", link_preview_options=NO_PREVIEW, reply_markup=None))
                await self.announce_turn(bot, game)
            else:
                raise MatchError("Неизвестная кнопка регистрации.")
        except MatchError as error:
            await self.answer(callback, str(error), True)
        except TelegramAPIError as error:
            LOG.warning("game_lobby_delivery_failed error=%s", type(error).__name__)
            await self.answer(callback, "Не удалось обновить сообщения. Повторите действие.", True)

    async def announce_turn(self, bot: Bot, game: Match) -> None:
        if game.status != "active" or game.pending is not None or game.turns_done in game.announcements:
            return
        self.matches.skip_empty_turns(game)
        if game.status == "finished":
            await self.finish_game(bot, game)
            return
        panel = InlinePanel(secrets.token_urlsafe(9), game.chat_id, game.thread_id,
                            chat_instance=game.chat_instance, match_id=game.token, match_turn=game.turns_done)
        self.inline.panels[panel.token] = panel
        inline_enabled = False
        blocks = [InputRichBlockParagraph(text=RichTextBold(text=turn_label(game.turns_done, game.total_turns, game.current_round))),
                  InputRichBlockParagraph(text=["Ходит: ", rich_profile(game.author.user_id, game.author.name),
                                               "\nОтвечает: ", rich_profile(game.voter.user_id, game.voter.name)])]
        if inline_enabled:
            blocks.append(InputRichBlockDetails(summary="Как заявить другую карту", is_open=False, blocks=[
                InputRichBlockParagraph(text="— Поставьте пробел, допишите номинал и масть карты, которую хотите заявить.\n"
                                             "Пример: =6пики, =даматреф, =туз червей"),
                InputRichBlockParagraph(text="— Затем нажмите на настоящую. Без текстовых дополнений отправится настоящая карта.")]))
        blocks.append(InputRichBlockParagraph(text=[rich_profile(game.author.user_id, game.author.name), ", выбирай карту."]))
        keyboard = self.turn_keyboard(game, panel)
        sent = await bot.send_rich_message(chat_id=game.chat_id, message_thread_id=game.thread_id,
                                           rich_message=InputRichMessage(blocks=blocks), reply_markup=keyboard)
        panel.message_id = sent.message_id
        game.announcements[game.turns_done] = sent.message_id

    async def after_turn(self, bot: Bot, turn: PublicTurn) -> None:
        game = self.matches.find(turn.chat_id, turn.match_id)
        if game.status == "active":
            await self.announce_turn(bot, game)
        elif game.status == "finished":
            await self.finish_game(bot, game)

    async def finish_game(self, bot: Bot, game: Match) -> None:
        if game.status == "finished" and game.final_message_id is None:
            ranking = self.matches.ranking(game)
            best = ranking[0][1]
            winners = [(player, count) for player, count in ranking if count == best]
            others = [(player, count) for player, count in ranking if count != best]
            title = "Победитель:" if len(winners) == 1 else "Победители (ничья):"
            lines = ["🏆 <b>Партия завершена!</b>", "",
                     f"Ходов сыграно: {game.turns_done}/{game.total_turns}", "", f"<b>{title}</b>"]
            lines += [f"{i + 1}. {profile_link(player.user_id, player.name)} — {count} карт"
                      for i, (player, count) in enumerate(winners)]
            if others:
                lines += ["", "<b>Остальные игроки:</b>"]
                lines += [f"{i + 1 + len(winners)}. {profile_link(player.user_id, player.name)} — {count} карт"
                          for i, (player, count) in enumerate(others)]
            lines += ["", "Новая партия: /game."]
            sent = await bot.send_message(chat_id=game.chat_id, message_thread_id=game.thread_id,
                                          text="\n".join(lines), parse_mode="HTML", link_preview_options=NO_PREVIEW)
            game.final_message_id = sent.message_id

    async def stats(self, message: Message) -> None:
        game = self.matches.games.get(message.chat.id)
        if game is None or game.status == "stopped":
            await message.answer("Партии нет. Создать регистрацию: /game.")
            return
        if game.status == "lobby":
            await message.answer(self.lobby_text(game), parse_mode="HTML", link_preview_options=NO_PREVIEW)
            return
        counts = "\n".join(f"{profile_link(p.user_id, p.name)} — {len(game.hands[p.user_id])} карт, "
                           f"ходов: {game.turns_by_player[p.user_id]}/{TURNS_PER_PLAYER}" for p in game.players)
        pending = "\nОдна разыгрываемая карта находится на столе." if game.pending else ""
        await message.answer(f"MONKEYS CARDS · статистика\n{counts}\n\n"
                             f"Завершено ходов: {game.turns_done}/{game.total_turns}.{pending}", parse_mode="HTML",
                             link_preview_options=NO_PREVIEW)

    async def leave(self, message: Message, bot: Bot) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        if message.chat.type not in GROUPS:
            await message.answer("Отправьте /leave в группе с игрой.")
            return
        game = self.matches.ongoing(message.chat.id)
        if game is None:
            await message.answer("В группе нет регистрации или активной партии.")
            return
        if not any(player.user_id == message.from_user.id for player in game.players):
            await message.answer("Вы не участвуете в этой игре.")
            return
        remember_profile(message.from_user)
        public = self.public_provider()
        old_turn = public.turns.get(game.pending.token) if game.pending else None
        missing_result = None
        # A decision already accepted before /leave remains counted, even if its
        # Telegram publication was interrupted. Never undo an answered move.
        if old_turn and old_turn.vote is not None and not old_turn.match_applied:
            self.matches.complete(game.chat_id, game.token, old_turn.token, old_turn.voter_id, old_turn.vote)
            old_turn.match_applied = True
            if old_turn.result_id is None:
                missing_result = result_text(old_turn)
        if game.status == "finished":
            if missing_result:
                sent = await message.answer(missing_result, parse_mode="HTML", link_preview_options=NO_PREVIEW)
                old_turn.result_id = sent.message_id
            await self.finish_game(bot, game)
            await message.answer("Партия уже завершена.")
            return
        old_panel_id = game.announcements.get(game.turns_done)
        was_lobby = game.status == "lobby"
        outcome = self.matches.leave(game, message.from_user.id)
        lines = [f"{profile_link(outcome.player.user_id, outcome.player.name)} выходит из игры."]
        if missing_result:
            lines = [missing_result, ""] + lines
        if outcome.cancelled_move:
            lines.append("Текущий ход отменен.")
        if was_lobby and outcome.creator_changed:
            creator = next(player for player in game.players if player.user_id == game.creator_id)
            lines.append(f"Начать игру теперь может {profile_link(creator.user_id, creator.name)}.")
        if game.status == "stopped":
            lines.append("Регистрация закрыта. Новая игра: /game." if was_lobby else
                         "Осталось меньше двух игроков. Партия остановлена без победителя. Новая игра: /game.")
        elif not was_lobby and game.status == "active":
            lines.append("Партия продолжается.")
        sent = await message.answer("\n".join(lines), parse_mode="HTML", link_preview_options=NO_PREVIEW)
        if missing_result:
            old_turn.result_id = sent.message_id
        if self.close_private:
            await self.close_private(bot, game.chat_id, message.from_user.id)
            if game.status == "active" and game.pending is None:
                await self.close_private(bot, game.chat_id, game.author.user_id)
        if was_lobby:
            await apply_edit(bot.edit_message_text(chat_id=game.chat_id, message_id=game.lobby_id,
                text=self.lobby_text(game) if game.status == "lobby" else "Регистрация закрыта. Новая игра: /game.",
                parse_mode="HTML", link_preview_options=NO_PREVIEW,
                reply_markup=self.lobby_keyboard(game) if game.status == "lobby" else None))
            return
        if old_turn and (outcome.cancelled_move or old_turn.vote is not None):
            try:
                target = ({"inline_message_id": old_turn.inline_claim_id} if old_turn.inline_claim_id else
                          {"chat_id": old_turn.chat_id, "message_id": old_turn.claim_id})
                if old_turn.inline_claim_id or old_turn.claim_id:
                    await apply_edit(bot.edit_message_reply_markup(**target, reply_markup=None))
            except TelegramAPIError as error:
                LOG.warning("leave_old_vote_keyboard_failed error=%s", type(error).__name__)
        if old_panel_id and game.pending is None:
            try:
                await apply_edit(bot.edit_message_reply_markup(chat_id=game.chat_id, message_id=old_panel_id, reply_markup=None))
            except TelegramAPIError as error:
                LOG.warning("leave_old_turn_keyboard_failed error=%s", type(error).__name__)
        if game.status == "active" and game.pending is None:
            await self.announce_turn(bot, game)
        elif game.status == "finished":
            await self.finish_game(bot, game)

    async def stop(self, message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            await message.answer("Остановить партию может только администратор группы.")
            return
        game = self.matches.ongoing(message.chat.id)
        if game is None:
            await message.answer("Активной партии или регистрации нет.")
            return
        self.matches.stop(game)
        await message.answer("Партия MONKEYS CARDS остановлена. Новая регистрация: /game.")
