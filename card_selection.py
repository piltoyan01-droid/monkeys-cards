"""Experiment 2: card selection rules and the private Telegram view."""

from dataclasses import dataclass, replace
from enum import StrEnum

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

DECK = tuple(f"{rank} {suit}" for suit in ("♠", "♥", "♦", "♣")
             for rank in ("6", "7", "8", "9", "10", "J", "Q", "K", "A"))


class Phase(StrEnum):
    HAND = "hand"
    DECLARE = "declare"
    REVIEW = "review"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class ConfirmedMove:
    real_card: str
    declared_card: str
    is_truth: bool


@dataclass(frozen=True)
class Selection:
    phase: Phase = Phase.HAND
    real_index: int | None = None
    declared_index: int | None = None
    confirmed: ConfirmedMove | None = None


class SelectionError(ValueError):
    """A safe explanation of an invalid action, without private card data."""


def advance_selection(state: Selection, cards: tuple[str, ...], action: str,
                      value: int | None = None) -> Selection:
    """Pure transition: no Telegram calls or mutation. Caller commits after editing."""
    if action == "select" and state.phase == Phase.HAND:
        if value is None or not 0 <= value < len(cards):
            raise SelectionError("Выберите настоящую карту из своей руки.")
        return Selection(phase=Phase.DECLARE, real_index=value)
    if action == "claim" and state.phase == Phase.DECLARE:
        if value is None or not 0 <= value < len(DECK):
            raise SelectionError("Выберите заявляемую карту из колоды.")
        return replace(state, phase=Phase.REVIEW, declared_index=value)
    if action == "change_real" and state.phase in {Phase.DECLARE, Phase.REVIEW}:
        return Selection()
    if action == "change_claim" and state.phase == Phase.REVIEW:
        return replace(state, phase=Phase.DECLARE, declared_index=None)
    if action == "cancel" and state.phase in {Phase.DECLARE, Phase.REVIEW}:
        return Selection()
    if action == "confirm" and state.phase == Phase.REVIEW:
        if state.real_index is None or state.declared_index is None:
            raise SelectionError("Сначала выберите настоящую и заявляемую карты.")
        real = cards[state.real_index]
        declared = DECK[state.declared_index]
        return replace(state, phase=Phase.CONFIRMED,
                       confirmed=ConfirmedMove(real, declared, real == declared))
    if action == "new" and state.phase == Phase.CONFIRMED:
        return Selection()
    raise SelectionError("Эта кнопка недоступна на текущем шаге. Используйте текущее окно.")


def selection_text(state: Selection, cards: tuple[str, ...], experiment: int = 2) -> str:
    title = f"MONKEYS CARDS LAB · эксперимент {experiment}\n"
    if state.phase == Phase.HAND:
        return (title + "Шаг 1 из 3. Выберите настоящую карту из своей руки:\n\n"
                + "\n".join(f"{i + 1}. {card}" for i, card in enumerate(cards)))
    if state.phase == Phase.CONFIRMED:
        move = state.confirmed
        assert move is not None
        if experiment == 3:
            return (title + "Ход подтвержден.\n\n"
                    f"Настоящая карта: {move.real_card}\nЗаявленная карта: {move.declared_card}\n\n")
        verdict = "Правда: карты совпали." if move.is_truth else "Блеф: карты не совпали."
        return (title + "Тестовый ход подтвержден.\n\n"
                f"Настоящая карта: {move.real_card}\nЗаявленная карта: {move.declared_card}\n\n"
                f"{verdict}\n\nРезультат показан только вам.\n"
                "Можно начать новый тестовый ход с теми же пятью картами.")
    assert state.real_index is not None
    real = cards[state.real_index]
    if state.phase == Phase.DECLARE:
        return (title + f"Настоящая карта: {real}\n\n"
                "Шаг 2 из 3. Что заявляете? Выберите любую из 36 карт.\n"
                "Можно заявить и ту карту, которой нет в вашей руке.")
    assert state.declared_index is not None
    return (title + "Шаг 3 из 3. Проверьте выбор перед подтверждением:\n\n"
            f"Настоящая карта: {real}\nЗаявляемая карта: {DECK[state.declared_index]}\n\n"
            "Нажмите «Подтвердить ход» или измените выбор.")


def selection_keyboard(state: Selection, cards: tuple[str, ...], nonce: str,
                       version: int, experiment: int = 2) -> InlineKeyboardMarkup:
    def button(text: str, action: str, value: int | None = None) -> InlineKeyboardButton:
        data = f"lab{experiment}:{action}:{nonce}:{version}"
        if value is not None:
            data += f":{value}"
        return InlineKeyboardButton(text=text, callback_data=data)

    if state.phase == Phase.HAND:
        rows = [[button(card, "select", i)] for i, card in enumerate(cards)]
    elif state.phase == Phase.DECLARE:
        # Nine ranks, four suits per row. Every card in the deck is selectable.
        rows = [[button(DECK[rank + suit * 9], "claim", rank + suit * 9)
                 for suit in range(4)] for rank in range(9)]
        rows += [[button("Изменить настоящую карту", "change_real")],
                 [button("Отмена", "cancel")]]
    elif state.phase == Phase.REVIEW:
        rows = [[button("Подтвердить ход", "confirm")],
                [button("Изменить заявление", "change_claim")],
                [button("Изменить настоящую карту", "change_real")],
                [button("Отмена", "cancel")]]
    else:
        rows = [[button("Новый тестовый ход", "new")]]
        if experiment == 3:
            rows.insert(0, [button("Обновить / повторить отправку", "status")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
