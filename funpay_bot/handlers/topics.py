"""Topics application handlers."""

from __future__ import annotations

import asyncio
import re
import time

from ..metadata import LOG


class TopicsMixin:
    async def forum_icon(self, emoji):
        normalize = lambda value: value.replace("\ufe0f", "").replace("\ufe0e", "")
        if self.topic_icons is None:
            if time.time() < self.topic_icons_retry_at:
                return None
            try:
                stickers = await self.tg.call("get_forum_topic_icon_stickers")
                icons = {}
                for sticker in stickers if isinstance(stickers, list) else []:
                    get = (
                        sticker.get
                        if isinstance(sticker, dict)
                        else lambda key, default=None: getattr(sticker, key, default)
                    )
                    if get("emoji") and get("custom_emoji_id"):
                        icons[normalize(get("emoji"))] = get("custom_emoji_id")
                if not icons:
                    self.topic_icons_retry_at = time.time() + 60
                    return None
                self.topic_icons = icons
            except Exception:
                LOG.exception("Не удалось получить разрешённые значки тем Telegram")
                self.topic_icons_retry_at = time.time() + 60
                return None
        emoji = normalize(emoji)
        aliases = {
            "⚙": ("🤖", "💡"),
            "🤖": ("🤖", "💡"),
            "⭐": ("⭐", "🌟"),
            "🎮": ("🎮", "🕹"),
            "🧰": ("📝", "🏗"),
            "📝": ("📝", "🏗"),
            "💬": ("💬", "🗨"),
            "📦": ("🛍", "🛒"),
            "🛍": ("🛍", "🛒"),
            "❗": ("❗", "‼", "❕"),
        }
        return next(
            (
                self.topic_icons[value]
                for value in (*aliases.get(emoji, (emoji,)), "📝")
                if value in self.topic_icons
            ),
            None,
        )

    async def topic(self, key, title=None, *, icon_emoji=None):
        async with self.topic_locks.setdefault(key, asyncio.Lock()):
            raw_title = title or key
            title = re.sub(r"^[^\w]+", "", raw_title).strip()[:128] or key
            emoji = icon_emoji
            if key.startswith("chat:"):
                support = (
                    self.state.d.get("chat_meta", {})
                    .get(key[5:], {})
                    .get("support_chat")
                )
                emoji = emoji or ("❗" if support else "💬")
                if support:
                    title = "‼️ " + title[:125]
            elif key == "main":
                emoji = emoji or "🤖"
            elif key == "orders":
                emoji = emoji or "🛍"
            elif key.startswith("plugin:"):
                emoji = emoji or raw_title.split(" ", 1)[0]
            icon = await self.forum_icon(emoji) if emoji else None
            icons = {"icon_custom_emoji_id": icon} if icon else {}
            titles = self.state.d.setdefault("topic_titles", {})
            applied = self.state.d.setdefault("topic_icon_ids", {})
            if key not in self.state.d["topics"]:
                LOG.info(
                    "Создание темы Telegram: привязка=%s, группа=%s", key, self.group
                )
                try:
                    t = await self.tg.call(
                        "create_forum_topic", chat_id=self.group, name=title, **icons
                    )
                except Exception:
                    LOG.exception("Не удалось создать тему Telegram: привязка=%s", key)
                    raise
                self.state.d["topics"][key] = t["message_thread_id"]
                titles[key] = title
                if icon:
                    applied[key] = icon
                self.state.save()
                LOG.info(
                    "Тема Telegram создана: привязка=%s, topic_id=%s",
                    key,
                    t["message_thread_id"],
                )
            elif titles.get(key) != title or icon and applied.get(key) != icon:
                await self.tg.call(
                    "edit_forum_topic",
                    chat_id=self.group,
                    message_thread_id=self.state.d["topics"][key],
                    name=title,
                    **icons,
                )
                titles[key] = title
                if icon:
                    applied[key] = icon
                self.state.save()
            return self.state.d["topics"][key]

    async def say(self, topic, text, **kw):
        thread = self.state.d["topics"][topic]
        for start in range(0, len(text), 3800):
            await self.tg.call(
                "send_message",
                chat_id=self.group,
                message_thread_id=thread,
                text=text[start : start + 3800],
                **kw,
            )

    def register_ui_message(self, message_id, topic, key):
        self.state.d.setdefault("ui_messages", {})[str(message_id)] = {
            "topic": topic,
            "key": key,
        }

    async def close_ui_message(self, message, topic, user):
        mid = message.get("message_id")
        owned = self.state.d.get("ui_messages", {}).get(str(mid))
        if not owned or owned["topic"] != topic:
            return
        await self.tg.call("delete_message", chat_id=self.group, message_id=mid)
        cardkey = topic + "/" + owned["key"]
        if self.state.d["cards"].get(cardkey) == mid:
            self.state.d["cards"].pop(cardkey, None)
            self.pending.pop(user, None)
            if topic.startswith("plugin:"):
                plugin = self.plugins.get(topic[7:])
                if hasattr(plugin, "inputs"):
                    plugin.inputs.pop(user, None)
        self.state.d["ui_messages"].pop(str(mid), None)
        self.state.save()

    async def card(
        self, topic, key, text, rows=(), *, new_message=False, parse_mode=None
    ):
        if topic == "main" and key == "menu":
            self.tools_screen_active = False
        async with self.card_locks.setdefault(topic + "/" + key, asyncio.Lock()):
            cardkey = topic + "/" + key
            base = [
                [
                    dict(cell)
                    if isinstance(cell, dict)
                    else {"text": cell[0], "callback_data": cell[1]}
                    for cell in row
                ]
                for row in rows
            ]
            is_menu = (bool(base) or key == "menu") and key != "controls"
            keep_close = topic.startswith("plugin:") and any(
                cell.get("callback_data", "").split(":")[-1] == "close"
                for row in base
                for cell in row
            )

            def markup(fresh):
                converted = []
                closed = False
                for row in base:
                    line = []
                    for cell in row:
                        cell = dict(cell)
                        if cell.get("callback_data", "").split(":")[-1] == "close":
                            if not fresh and not keep_close:
                                continue
                            cell["callback_data"] = "ui:close"
                            closed = True
                        line.append(cell)
                    if line:
                        converted.append(line)
                if fresh and is_menu and not closed:
                    converted.append([{"text": "Закрыть", "callback_data": "ui:close"}])
                return {"inline_keyboard": converted}

            args = dict(chat_id=self.group, text=text[:3800])
            if parse_mode:
                args["parse_mode"] = parse_mode
            old = None if new_message else self.state.d["cards"].get(cardkey)
            if old:
                try:
                    await self.tg.call(
                        "edit_message_text",
                        message_id=old,
                        reply_markup=markup(False),
                        **args,
                    )
                    if keep_close:
                        self.register_ui_message(old, topic, key)
                        self.state.save()
                    return
                except Exception as error:
                    if "message to edit not found" not in str(error).lower():
                        raise
            message = await self.tg.call(
                "send_message",
                message_thread_id=self.state.d["topics"][topic],
                reply_markup=markup(True),
                disable_notification=(
                    topic == "orders"
                    and not self.cfg.flag("Notifications", "new_order")
                ),
                **args,
            )
            self.state.d["cards"][cardkey] = message["message_id"]
            if is_menu:
                self.register_ui_message(message["message_id"], topic, key)
            self.state.save()
