"""Get real face stickers from a LAB-only set, without chat messages or hosting."""

import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, InputSticker

from card_selection import DECK
from public_turn import PublicLab, card_filename

LOG = logging.getLogger("monkeys_lab")


def parse_declaration(text: str) -> str | None:
    """Accept =6♠, =6пик, =пики6, =A♥ and full Russian rank names."""
    compact = "".join(text.removeprefix("=").lower().replace("\ufe0f", "").split())
    ranks = {rank.lower(): rank for rank in ("6", "7", "8", "9", "10", "J", "Q", "K", "A")}
    ranks.update({"валет": "J", "дама": "Q", "король": "K", "туз": "A"})
    suits = {"♠": "♠", "пики": "♠", "пик": "♠", "пика": "♠", "s": "♠",
             "♥": "♥", "❤": "♥", "черви": "♥", "червей": "♥", "червы": "♥", "h": "♥",
             "♦": "♦", "бубны": "♦", "бубен": "♦", "бубна": "♦", "d": "♦",
             "♣": "♣", "трефы": "♣", "треф": "♣", "крести": "♣", "c": "♣"}
    for suffix, suit in sorted(suits.items(), key=lambda item: len(item[0]), reverse=True):
        rank_text = None
        if compact.endswith(suffix):
            rank_text = compact[:-len(suffix)]
        elif compact.startswith(suffix):
            rank_text = compact[len(suffix):]
        if rank_text in ranks:
            card = f"{ranks[rank_text]} {suit}"
            return card if card in DECK else None
    return None


class InlineFaces:
    def __init__(self):
        self.ids: dict[str, str] = {}

    async def prepare(self, bot: Bot, public: PublicLab, owner_id: int, cards: tuple[str, ...],
                      allow_create: bool = True) -> bool:
        # Only IDs returned as Sticker objects are suitable here. uploadStickerFile
        # returns an upload for createNewStickerSet, not a finished inline sticker.
        for card in cards:
            known = public.sticker_ids.get(card_filename(card))
            if known and card not in self.ids:
                self.ids[card] = known
        if all(card in self.ids for card in cards):
            public.sticker_ids.update({card_filename(card): identifier for card, identifier in self.ids.items()})
            return True
        try:
            async with asyncio.timeout(55 if allow_create else 8):
                me = await bot.get_me()
                name = f"lab_faces_v1_by_{me.username}"
                manifest_path = public.asset_dir.parent / "inline_stickers.json"
                try:
                    pack = await bot.get_sticker_set(name)
                except TelegramBadRequest as error:
                    if not allow_create or ("stickerset_invalid" not in error.message.lower()
                                            and "not found" not in error.message.lower()):
                        raise
                    LOG.info("inline_face_set_creating")
                    await bot.create_new_sticker_set(user_id=owner_id, name=name,
                        title="Monkeys Cards · карты", sticker_type="regular",
                        stickers=[InputSticker(sticker=FSInputFile(public.asset_dir / card_filename(card)),
                                               format="static", emoji_list=["🃏"]) for card in DECK],
                        request_timeout=45)
                    pack = await bot.get_sticker_set(name)
                if len(pack.stickers) != len(DECK) or any(
                        sticker.is_animated or sticker.is_video or not sticker.file_id for sticker in pack.stickers):
                    raise ValueError("Unexpected LAB sticker set content")
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if (manifest.get("bot_id") != me.id or manifest.get("set_name") != name
                            or set(manifest.get("cards", {})) != set(DECK)):
                        raise ValueError("LAB sticker manifest does not match this bot")
                    by_unique = {sticker.file_unique_id: sticker.file_id for sticker in pack.stickers}
                    mapped = {card: by_unique[unique] for card, unique in manifest["cards"].items()}
                else:
                    # This deterministic set is created once above in DECK order.
                    mapped = {card: sticker.file_id for card, sticker in zip(DECK, pack.stickers)}
                    manifest = {"bot_id": me.id, "set_name": name,
                                "cards": {card: sticker.file_unique_id for card, sticker in zip(DECK, pack.stickers)}}
                    manifest_path.parent.mkdir(parents=True, exist_ok=True)
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                self.ids.update(mapped)
                public.sticker_ids.update({card_filename(card): identifier for card, identifier in self.ids.items()})
                LOG.info("inline_face_set_ready cards=36")
        except (TelegramAPIError, TimeoutError, ValueError, KeyError, OSError, TypeError) as error:
            reason = "STICKER_SET_PREPARATION_FAILED"
            if isinstance(error, TelegramAPIError):
                message = error.message.lower()
                if "access to the user" in message:
                    reason = "OWNER_ACCESS_DENIED"
                elif "user" in message:
                    reason = "STICKER_SET_OWNER_INVALID"
                elif "stickerset_invalid" in message or "not found" in message:
                    reason = "STICKER_SET_NOT_PREPARED"
                elif "file" in message and ("invalid" in message or "wrong" in message):
                    reason = "MEDIA_REFERENCE_INVALID"
            LOG.warning("inline_faces_prepare_failed error=%s reason=%s", type(error).__name__, reason)
            return False
        return all(card in self.ids for card in cards)
