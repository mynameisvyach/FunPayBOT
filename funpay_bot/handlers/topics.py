"""Создание, восстановление и оформление тем Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time

from ..metadata import LOG


class TopicsMixin:
    async def remove_own_pin_notice(self, message):
        """Удаляет только служебное уведомление о закреплении, сохраняя закреплённое сообщение."""
        chat = message.get("chat", {})
        if chat.get("id") != self.group or chat.get("type") != "supergroup":
            return
        if not message.get("pinned_message"):
            return
        bot_id = self.state.d.get("identity", {}).get("bot_id") or getattr(
            getattr(self.tg, "bot", None), "id", None
        )
        if not bot_id or message.get("from", {}).get("id") != bot_id:
            return
        message_id = message.get("message_id")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            return
        try:
            await self.tg.call(
                "delete_message", chat_id=self.group, message_id=message_id
            )
        except Exception:
            if not getattr(self, "pin_notice_delete_warning", False):
                self.pin_notice_delete_warning = True
                LOG.warning(
                    "Не удалось удалить служебное сообщение о закреплении. Проверьте право удаления сообщений у бота."
                )

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

    @staticmethod
    def is_missing_topic(error):
        if type(error).__name__ != "TelegramBadRequest":
            return False
        text = str(error).casefold()
        return any(
            value in text
            for value in (
                "message thread not found",
                "message_thread_not_found",
                "topic_id_invalid",
                "topic_deleted",
                "topic not found",
                "topic was deleted",
            )
        )

    def forget_chat_topic(self, key):
        """Удаляет только привязки интерфейса Telegram, сохраняя данные FunPay и платежей."""
        if not key.startswith("chat:"):
            raise ValueError("Автовосстановление доступно только для тем покупателей")
        old_thread = self.state.d["topics"].pop(key, None)
        if old_thread is not None:
            self.__dict__.setdefault("_retired_chat_threads", {})[old_thread] = key
        self.state.d.setdefault("topic_titles", {}).pop(key, None)
        self.state.d.setdefault("topic_icon_ids", {}).pop(key, None)
        for card in list(self.state.d["cards"]):
            if card.startswith(key + "/"):
                del self.state.d["cards"][card]
        for mid, info in list(self.state.d.get("ui_messages", {}).items()):
            if info.get("topic") == key:
                del self.state.d["ui_messages"][mid]
        for field in ("controls_revision", "controls_pinned"):
            self.state.d.setdefault(field, {}).pop(key[5:], None)
        self.state.save()

    async def telegram_call(self, method, **kw):
        """Повторяет явно отклонённую отправку один раз после восстановления удалённой темы покупателя."""
        old_thread = kw.get("message_thread_id")
        key = next(
            (
                name
                for name, thread in self.state.d["topics"].items()
                if name.startswith("chat:") and thread == old_thread
            ),
            None,
        )
        if key is None:
            key = getattr(self, "_retired_chat_threads", {}).get(old_thread)
        try:
            return await self.tg.call(method, **kw)
        except Exception as error:
            if (
                not key
                or kw.get("chat_id") != self.group
                or method not in ("send_message", "send_photo", "send_document")
                or not self.is_missing_topic(error)
            ):
                raise
            new_thread = await self.topic(
                key, self.chat_label(key[5:]), _failed_thread_id=old_thread
            )
            kw["message_thread_id"] = new_thread
            return await self.tg.call(method, **kw)

    async def topic(self, key, title=None, *, icon_emoji=None, _failed_thread_id=None):
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
            if (
                _failed_thread_id is not None
                and key.startswith("chat:")
                and self.state.d["topics"].get(key) == _failed_thread_id
            ):
                self.forget_chat_topic(key)
                LOG.info(
                    "Восстановление удалённой темы покупателя; сохранены заказы и история"
                )
            titles = self.state.d.setdefault("topic_titles", {})
            applied = self.state.d.setdefault("topic_icon_ids", {})

            async def create_topic():
                LOG.debug(
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
                LOG.debug(
                    "Тема Telegram создана: привязка=%s, topic_id=%s",
                    key,
                    t["message_thread_id"],
                )

            if key not in self.state.d["topics"]:
                await create_topic()
            elif titles.get(key) != title or icon and applied.get(key) != icon:
                try:
                    await self.tg.call(
                        "edit_forum_topic",
                        chat_id=self.group,
                        message_thread_id=self.state.d["topics"][key],
                        name=title,
                        **icons,
                    )
                except Exception as error:
                    if not key.startswith("chat:") or not self.is_missing_topic(error):
                        raise
                    self.forget_chat_topic(key)
                    await create_topic()
                else:
                    titles[key] = title
                    if icon:
                        applied[key] = icon
                    self.state.save()
            return self.state.d["topics"][key]

    async def say(self, topic, text, **kw):
        thread = self.state.d["topics"][topic]
        for start in range(0, len(text), 3800):
            await self.telegram_call(
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
        await self.telegram_call("delete_message", chat_id=self.group, message_id=mid)
        cardkey = topic + "/" + owned["key"]
        if self.state.d["cards"].get(cardkey) == mid:
            self.state.d["cards"].pop(cardkey, None)
            self.pending.pop(user, None)
            if topic.startswith("plugin:"):
                plugin = self.plugins.get(topic[7:])
                if hasattr(plugin, "inputs"):
                    plugin.inputs.pop(user, None)
        if topic.startswith("plugin:") and owned["key"] == "menu":
            self.state.d.setdefault("plugin_menu_closed", {})[topic] = True
        self.state.d["ui_messages"].pop(str(mid), None)
        self.state.d.get("message_callbacks", {}).pop(str(mid), None)
        self.state.save()

    def reopen_plugin_menu(self, name):
        """Разрешает показать закрытое меню только после явного запроса владельца."""
        self.state.d.get("plugin_menu_closed", {}).pop("plugin:" + name, None)
        self.state.save()

    def encode_callback(self, data):
        """Передаёт callback в нижнем регистре, сохраняя регистр идентификаторов внутри данных."""
        data = str(data)
        if data == data.lower() and len(data.encode("utf-8")) <= 64:
            return data
        prefix = data.split(":", 1)[0].lower()
        if not re.fullmatch(r"[a-z0-9_]{1,24}", prefix):
            prefix = "cb"
        token = prefix + ":~" + hashlib.sha256(data.encode()).hexdigest()[:24]
        registry = self.state.d.setdefault("callback_registry", {})
        if token in registry and registry[token] != data:
            raise ValueError("Коллизия идентификаторов кнопок")
        registry[token] = data
        return token

    def record_message_callbacks(self, message_id, tokens):
        """Сохраняет ссылки кнопок обычных сообщений и карточек после подтверждённой отправки."""
        self.state.d.setdefault("message_callbacks", {})[str(message_id)] = list(tokens)
        self.state.save()

    def decode_callback(self, data):
        """Восстанавливает исходные параметры после проверки отправителя Telegram."""
        return self.state.d.get("callback_registry", {}).get(str(data), str(data))

    async def pin_plugin_menu(self, topic, message_id):
        """Закрепляет единственное меню плагина один раз на текущее сообщение."""
        pinned = self.state.d.setdefault("plugin_menu_pins", {})
        if pinned.get(topic) == message_id:
            return
        try:
            await self.tg.call(
                "pin_chat_message",
                chat_id=self.group,
                message_id=message_id,
                disable_notification=True,
            )
        except Exception:
            LOG.warning("Не удалось закрепить меню плагина %s", topic)
            return
        pinned[topic] = message_id
        self.state.save()

    async def card(
        self,
        topic,
        key,
        text,
        rows=(),
        *,
        new_message=False,
        parse_mode=None,
        plugin_main=False,
    ):
        if topic.startswith("plugin:") and key == "menu":
            if (
                self.state.d.get("plugin_menu_closed", {}).get(topic)
                and not new_message
            ):
                return
            if new_message:
                self.state.d.setdefault("plugin_menu_closed", {}).pop(topic, None)
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
            for row in base:
                for cell in row:
                    if "callback_data" in cell:
                        cell["callback_data"] = self.encode_callback(
                            cell["callback_data"]
                        )
            callback_tokens = [
                cell["callback_data"]
                for row in base
                for cell in row
                if "callback_data" in cell
            ]
            is_menu = (bool(base) or key == "menu") and key not in ("controls", "rates")
            keep_close = (
                topic.startswith("plugin:")
                and plugin_main
                and any(
                    cell.get("callback_data", "").split(":")[-1] == "close"
                    for row in base
                    for cell in row
                )
            )

            def markup(fresh):
                converted = []
                closed = False
                for row in base:
                    line = []
                    for cell in row:
                        cell = dict(cell)
                        if cell.get("callback_data", "").split(":")[-1] == "close":
                            if topic.startswith("plugin:") and not plugin_main:
                                continue
                            if not fresh and not keep_close:
                                continue
                            cell["callback_data"] = "ui:close"
                            closed = True
                        line.append(cell)
                    if line:
                        converted.append(line)
                if (
                    is_menu
                    and not closed
                    and (
                        (topic.startswith("plugin:") and plugin_main)
                        or (fresh and not topic.startswith("plugin:"))
                    )
                ):
                    converted.append([{"text": "Закрыть", "callback_data": "ui:close"}])
                return {"inline_keyboard": converted}

            args = dict(chat_id=self.group, text=text[:3800])
            if parse_mode:
                args["parse_mode"] = parse_mode
            previous = (
                self.state.d["cards"].get(cardkey)
                if new_message and topic.startswith("plugin:") and key == "menu"
                else None
            )
            old = None if new_message else self.state.d["cards"].get(cardkey)
            if old:
                try:
                    await self.telegram_call(
                        "edit_message_text",
                        message_id=old,
                        reply_markup=markup(False),
                        **args,
                    )
                    self.state.d.setdefault("card_callbacks", {})[cardkey] = (
                        callback_tokens
                    )
                    if keep_close:
                        self.register_ui_message(old, topic, key)
                    self.state.save()
                    if topic.startswith("plugin:") and key == "menu":
                        await self.pin_plugin_menu(topic, old)
                    return
                except Exception as error:
                    if topic.startswith("chat:") and self.is_missing_topic(error):
                        await self.topic(
                            topic,
                            self.chat_label(topic[5:]),
                            _failed_thread_id=self.state.d["topics"].get(topic),
                        )
                    elif "message to edit not found" not in str(error).lower():
                        raise
            message = await self.telegram_call(
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
            self.state.d.setdefault("card_callbacks", {})[cardkey] = callback_tokens
            if is_menu:
                self.register_ui_message(message["message_id"], topic, key)
            self.state.save()
            if previous and previous != message["message_id"]:
                try:
                    await self.telegram_call(
                        "delete_message", chat_id=self.group, message_id=previous
                    )
                except Exception as error:
                    if "message to delete not found" not in str(error).lower():
                        LOG.warning(
                            "Новое меню отправлено, но прежнее удалить не удалось: %s",
                            type(error).__name__,
                        )
                else:
                    self.state.d.get("ui_messages", {}).pop(str(previous), None)
                    self.state.d.get("message_callbacks", {}).pop(str(previous), None)
                self.state.save()
            if topic.startswith("plugin:") and key == "menu":
                await self.pin_plugin_menu(topic, message["message_id"])
