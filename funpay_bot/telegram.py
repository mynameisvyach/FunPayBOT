"""Общий транспорт Telegram."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time


class Telegram:
    """Управляет общей очередью исходящих запросов для тем, карточек, плагинов и чатов."""

    def __init__(self, cfg):
        from aiogram import Bot
        from aiogram.client.session.aiohttp import AiohttpSession

        proxy = cfg.get("Telegram", "proxy") or None
        self.bot = Bot(
            cfg.get("Telegram", "token"), session=AiohttpSession(proxy=proxy)
        )
        self.gate = asyncio.Lock()
        self.next_send = 0.0

    async def call(self, method, **kw):
        from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
        from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

        callback_tokens = []
        if kw.get("reply_markup"):
            raw = kw["reply_markup"]
            raw = raw.model_dump() if hasattr(raw, "model_dump") else raw
            raw = {
                **raw,
                "inline_keyboard": [
                    [dict(cell) for cell in row] for row in raw["inline_keyboard"]
                ],
            }
            encoder = getattr(self, "callback_encoder", None)
            if encoder:
                for row in raw["inline_keyboard"]:
                    for cell in row:
                        if cell.get("callback_data"):
                            cell["callback_data"] = encoder(cell["callback_data"])
                            callback_tokens.append(cell["callback_data"])
            kw["reply_markup"] = InlineKeyboardMarkup.model_validate(raw)
        if isinstance(kw.get("photo"), tuple):
            name, body = kw["photo"]
            kw["photo"] = BufferedInputFile(body, filename=name)
        if isinstance(kw.get("document"), tuple):
            name, body = kw["document"]
            kw["document"] = BufferedInputFile(body, filename=name)
        # No retry of ambiguous network errors for mutating operations.
        async with (
            contextlib.AsyncExitStack()
            if method in ("answer_callback_query", "edit_message_text")
            else self.gate
        ):
            paced = method not in (
                "edit_message_text",
                "answer_callback_query",
                "get_updates",
                "get_chat_member",
                "get_chat",
            )
            for attempt in range(3):
                if paced:
                    await asyncio.sleep(max(0, self.next_send - time.monotonic()))
                try:
                    value = await getattr(self.bot, method)(**kw)
                    if paced:
                        self.next_send = time.monotonic() + 3.1
                    value = (
                        value.model_dump() if hasattr(value, "model_dump") else value
                    )
                    recorder = getattr(self, "callback_recorder", None)
                    if recorder and callback_tokens:
                        mid = (
                            value.get("message_id")
                            if isinstance(value, dict)
                            else kw.get("message_id")
                        )
                        if mid:
                            recorder(mid, callback_tokens)
                    return value
                except TelegramRetryAfter as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(e.retry_after + 0.2)
                except TelegramBadRequest as e:
                    if attempt < 2 and any(
                        word in str(e).casefold()
                        for word in (
                            "custom emoji",
                            "custom_emoji",
                            "emoji_not_allowed",
                            "entity customemoji",
                        )
                    ):
                        if kw.get("reply_markup"):
                            raw = kw["reply_markup"].model_dump(exclude_none=True)
                            for row in raw.get("inline_keyboard", []):
                                for cell in row:
                                    cell.pop("icon_custom_emoji_id", None)
                            kw["reply_markup"] = InlineKeyboardMarkup.model_validate(
                                raw
                            )
                        for field in ("text", "caption"):
                            if isinstance(kw.get(field), str):
                                kw[field] = re.sub(
                                    r"<tg-emoji[^>]*>(.*?)</tg-emoji>",
                                    r"\1",
                                    kw[field],
                                    flags=re.S,
                                )
                        continue
                    if (
                        method == "edit_forum_topic"
                        and "TOPIC_NOT_MODIFIED" in str(e).upper()
                    ):
                        if paced:
                            self.next_send = time.monotonic() + 3.1
                        return True
                    if (
                        method == "edit_message_text"
                        and "message is not modified" in str(e).lower()
                    ):
                        recorder = getattr(self, "callback_recorder", None)
                        if recorder and callback_tokens:
                            recorder(kw["message_id"], callback_tokens)
                        return {"message_id": kw["message_id"]}
                    raise
