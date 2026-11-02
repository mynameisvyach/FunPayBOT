"""Переписка FunPay, заготовки и автоматические ответы."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import re
import time

from ..finance import FinancialLedger
from ..logging_setup import Redactor, action_logger
from ..metadata import LOG, human_time
from ..templates import render_template


class ChatsMixin:
    def greeting_provider(self):
        """Находит работающий плагин, взявший общение и приветствия на себя."""
        for plugin in self.plugins.values():
            runtime = getattr(plugin, "runtime", None)
            service = getattr(runtime, "capabilities", {}).get("greetings")
            if runtime and runtime.started and service and service.handles_greetings():
                return service
        return None

    async def once(self, key, operation, *, notify=True):
        if key in self.state.d["actions"]:
            return False
        # Reserve before external mutation. Ambiguous outcomes require inspection, never replay.
        self.state.d["actions"][key] = {"status": "pending", "at": time.time()}
        self.state.save()
        try:
            result = await operation()
            mid = getattr(result, "id", None)
            chat = getattr(result, "chat_id", None)
            if mid is not None and chat is not None:
                outgoing = self.state.d.setdefault("outgoing_messages", {})
                outgoing[f"m:{chat}:{mid}"] = {
                    "action": key,
                    "mirrored": key.startswith("tg:"),
                }
                if key.startswith(("tg:", "template:", "owner:")):
                    self.state.d.setdefault("chat_meta", {}).setdefault(str(chat), {})[
                        "manual_action"
                    ] = key
                while len(outgoing) > 5000:
                    outgoing.pop(next(iter(outgoing)))
        except Exception as e:
            reason, rejected = self.send_error(e)
            self.state.d["actions"][key].update(
                status="failed" if rejected else "unknown", reason=reason
            )
            self.state.save()
            self.counts["errors"] += 1
            action_logger(key).error(
                "Действие %s: %s · ошибка %s",
                key,
                "отклонено" if not notify and rejected else "результат неизвестен",
                type(e).__name__,
            )
            if notify:
                await self.say(
                    "main",
                    f"⚠️ Не удалось подтвердить действие {key}. Автоповтор отключён; проверьте результат на FunPay.",
                )
            return False
        self.state.d["actions"][key]["status"] = "done"
        self.state.save()
        if key.startswith("tg:"):
            LOG.info("Сообщение отправлено покупателю из Telegram")
        return True

    def send_error(self, error):
        name = type(error).__name__
        status = getattr(error, "status_code", None)
        detail = getattr(error, "error_message", None)
        rejected = (
            (bool(detail) and name in ("MessageNotDeliveredError", "ImageUploadError"))
            or name in ("UnauthorizedError", "AccountNotInitiatedError")
            or status in (400, 401, 403, 404, 429)
        )
        if detail:
            reason = "FunPay: " + str(detail)
        elif name in ("TimeoutError", "ReadTimeout", "ConnectTimeout"):
            reason = "истекло время ожидания ответа FunPay"
        elif name in ("ConnectionError", "ClientConnectionError"):
            reason = "соединение с FunPay прервано"
        elif name == "UnauthorizedError" or status == 401:
            reason = "авторизация FunPay истекла"
        elif name == "AccountNotInitiatedError":
            reason = "подключение к аккаунту FunPay ещё не готово"
        elif status:
            reason = "FunPay вернул HTTP " + str(status)
        else:
            reason = "ошибка " + name + "; подробности в журнале"
        record = logging.LogRecord("FunPayBOT", logging.INFO, "", 0, reason, (), None)
        reason = Redactor(self.cfg).format(record).partition("FunPayBOT: ")[2]
        return " ".join(reason.split())[:350], bool(rejected)

    async def send_receipt(self, topic, message, text):
        key = str(message["message_id"])
        receipts = self.state.d.setdefault("send_receipts", {})
        old = receipts.get(key)
        if old:
            await self.telegram_call(
                "edit_message_text", chat_id=self.group, message_id=old, text=text
            )
        else:
            receipt = await self.telegram_call(
                "send_message",
                chat_id=self.group,
                message_thread_id=self.state.d["topics"][topic],
                text=text,
                reply_parameters={
                    "message_id": message["message_id"],
                    "allow_sending_without_reply": True,
                },
            )
            receipts[key] = receipt["message_id"]
            while len(receipts) > 5000:
                receipts.pop(next(iter(receipts)))
            self.state.save()

    def sending_status(self, key):
        action = self.state.d["actions"].get(key, {})
        if action.get("status") == "done":
            return "✅ Отправлено"
        reason = action.get("reason", "нет подтверждения FunPay")
        if action.get("status") == "failed":
            return "❌ Не отправлено (" + reason + ")"
        return (
            "⚠️ Не удалось подтвердить отправку ("
            + reason
            + "). Проверьте чат FunPay перед повтором."
        )

    async def telegram_to_funpay(self, message, topic):
        chat = topic[5:]
        key = f"tg:{self.group}:{message['message_id']}"
        self.record_human_activity("Telegram")
        if not self.cfg.flag("SellerStatus", "online"):
            await self.send_receipt(
                topic, message, "❌ Не отправлено (бот в режиме офлайн)"
            )
            return
        text = message.get("text", "")
        photo = message.get("photo")
        doc = message.get("document", {})
        image_document = doc.get("mime_type") in (
            "image/png",
            "image/jpeg",
            "image/webp",
        )
        if not text and not photo and not image_document:
            await self.send_receipt(
                topic,
                message,
                "❌ Не отправлено (этот тип сообщения не поддерживается; отправьте текст или изображение)",
            )
            return
        if photo or image_document:
            if key not in self.state.d["actions"]:
                try:
                    file_id = photo[-1]["file_id"] if photo else doc["file_id"]
                    data = await self.tg.bot.download(file_id)
                    content = data.read()
                except Exception:
                    LOG.exception(
                        "Не удалось скачать изображение Telegram для отправки в FunPay"
                    )
                    await self.send_receipt(
                        topic,
                        message,
                        "❌ Не отправлено (не удалось загрузить изображение из Telegram)",
                    )
                    return
                await self.once(key, lambda: self.fp.image(chat, content), notify=False)
            if self.state.d["actions"].get(key, {}).get("status") != "done":
                await self.send_receipt(topic, message, self.sending_status(key))
                return
            if message.get("caption"):
                await self.once(
                    key + ":caption",
                    lambda: self.fp.send(chat, message["caption"]),
                    notify=False,
                )
                status = self.sending_status(key + ":caption")
                if status != "✅ Отправлено":
                    status = "📷 Изображение отправлено. Подпись: " + status
            else:
                status = "✅ Отправлено"
        else:
            await self.once(key, lambda: self.fp.send(chat, text), notify=False)
            status = self.sending_status(key)
        await self.send_receipt(topic, message, status)

    @staticmethod
    def nick_link(name, chat):
        from urllib.parse import quote

        return (
            '<a href="https://funpay.com/chat/?node='
            + quote(str(chat), safe="")
            + '">'
            + html.escape(str(name))
            + "</a>"
        )

    async def chat_message(self, topic, author, body, *, notify=True):
        prefix = author + ": "
        budget = 3800 - len(prefix)
        chunk = ""
        for char in str(body):
            escaped = html.escape(char)
            if len(chunk) + len(escaped) > budget:
                await self.telegram_call(
                    "send_message",
                    chat_id=self.group,
                    message_thread_id=self.state.d["topics"][topic],
                    text=prefix + chunk,
                    parse_mode="HTML",
                    disable_notification=not notify,
                    link_preview_options={"is_disabled": True},
                )
                chunk = ""
            chunk += escaped
        await self.telegram_call(
            "send_message",
            chat_id=self.group,
            message_thread_id=self.state.d["topics"][topic],
            text=prefix + chunk,
            parse_mode="HTML",
            disable_notification=not notify,
            link_preview_options={"is_disabled": True},
        )
        if (
            topic.startswith("chat:")
            and topic + "/controls" not in self.state.d["cards"]
        ):
            await self.refresh_chat_controls(topic[5:])

    def render(self, template, event):
        """Дополняет контекст сохранёнными данными чата и именно указанного заказа."""
        meta = self.state.d.get("chat_meta", {}).get(str(event.get("chat", "")), {})
        order_id = str(event.get("order_id", "") or "").lstrip("#")
        saved = self.state.d.get("order_cards", {}).get(order_id, {}).get("event", {})
        if not saved:
            saved = self.state.d.get("orders", {}).get(order_id, {})
        values = {**meta, **saved, **event}
        # В сообщении имя — автор, в заказе — покупатель, независимо от чата.
        if event.get("kind") == "message":
            values["template_username"] = event.get("author") or event.get(
                "username", "покупатель"
            )
        return render_template(
            template, values, self.cfg.get("General", "timezone", "Europe/Moscow")
        )

    def response_rules(self):
        rules = {}
        for key, value in self.cfg.data("AUTORESPONSE", "commands", {}).items():
            if not isinstance(value, dict):
                continue
            rule = {str(k).upper(): v for k, v in value.items()}
            rule.setdefault("TRIGGER_AUTHOR", "BUYER")
            rule.setdefault("ENABLED", True)
            if str(rule.get("TRIGGER_AUTHOR", "BUYER")).upper() == "BUYER":
                rules[str(key).replace("\n", "").strip().casefold()] = rule
        return rules

    def manual_templates(self):
        items = self.cfg.data("ChatTemplates", "items", {})
        if not isinstance(items, dict):
            return {}
        return {
            token: {"label": str(item.get("title") or token), "text": item["text"]}
            for token, item in items.items()
            if isinstance(item, dict) and item.get("enabled", True) and item.get("text")
        }

    def seller_name(self):
        return str(
            getattr(getattr(self.fp, "account", None), "username", None) or "Продавец"
        )

    def chat_label(self, chat):
        meta = self.state.d.get("chat_meta", {}).get(str(chat), {})
        name = meta.get("username") or "Пользователь"
        suffix = str(meta["buyer_id"]) if meta.get("buyer_id") else "чат " + str(chat)
        return ("‼️ " if meta.get("support_chat") else "") + f"{name} ({suffix})"

    def origin_icon(self, action):
        if action.startswith("plugin:"):
            name = action.split(":", 2)[1]
            plugin = self.plugins.get(name)
            title = getattr(plugin, "TITLE", getattr(plugin, "NAME", ""))
            return title.split(" ", 1)[0] if title and not title[0].isalnum() else "🧩"
        return "👤" if action.startswith(("template:", "owner:", "tg:")) else "🤖"

    async def refresh_chat_controls(self, chat, *, force=False):
        key = "chat:" + str(chat)
        if key not in self.state.d["topics"]:
            return
        templates = self.manual_templates()
        revision = hashlib.sha256(
            json.dumps(
                {"templates": templates, "layout": "seller-templates-v2"},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        existing = self.state.d["cards"].get(key + "/controls")
        if (
            force
            or self.state.d.get("controls_revision", {}).get(str(chat)) != revision
            or not existing
        ):
            rows = [
                [(entry["label"], "template:" + token)]
                for token, entry in templates.items()
            ]
            await self.card(
                key,
                "controls",
                "Заготовки продавца:",
                rows,
            )
            self.state.d.setdefault("controls_revision", {})[str(chat)] = revision
        current = self.state.d["cards"][key + "/controls"]
        if self.state.d.get("controls_pinned", {}).get(str(chat)) != current:
            try:
                await self.telegram_call(
                    "pin_chat_message",
                    chat_id=self.group,
                    message_id=current,
                    disable_notification=True,
                )
                self.state.d.setdefault("controls_pinned", {})[str(chat)] = current
            except Exception:
                LOG.exception("Не удалось закрепить заготовки чата %s", chat)
        self.state.save()

    async def refresh_all_chat_controls(self):
        for key in list(self.state.d["topics"]):
            if key.startswith("chat:"):
                meta = self.state.d.get("chat_meta", {}).get(key[5:], {})
                previous = self.state.d.get("topic_titles", {}).get(key, "")
                if meta.get("username"):
                    await self.topic(key, self.chat_label(key[5:]))
                elif previous:
                    await self.topic(key, previous)
                await self.refresh_chat_controls(key[5:], force=True)

    async def send_buyer(self, event, text, action, *, watermark=True, rendered=False):
        if not text or not self.cfg.flag("SellerStatus", "online"):
            return
        meta = self.state.d.get("chat_meta", {}).get(str(event["chat"]), {})
        event = {**meta, **event}
        if not rendered:
            text = self.render(text, event)
        if watermark and self.cfg.flag("Messages", "watermark"):
            watermark = self.render(
                self.cfg.get("Messages", "watermark_text"), event
            ).rstrip()
            if watermark:
                if self.greeting_provider():
                    watermark = watermark.replace("🤖 FunPay BOT", "👤 ИИ-помощник")
                text = watermark + "\n" + text
        ok = await self.once(action, lambda: self.fp.send(event["chat"], text))
        if ok:
            action_logger(action).info(
                "Сообщение отправлено покупателю · %s",
                meta.get("username") or "чат " + str(event["chat"]),
                extra={"plugin_id": action.split(":", 2)[1]}
                if action.startswith("plugin:")
                else {},
            )
            await self.chat_message(
                "chat:" + str(event["chat"]),
                self.nick_link(self.seller_name(), event["chat"])
                + " ("
                + self.origin_icon(action)
                + ")",
                text,
            )
            for row in self.state.d.get("outgoing_messages", {}).values():
                if row["action"] == action:
                    row["mirrored"] = True
            self.state.save()

    async def handle_event(self, e):
        if e["kind"] == "baseline":
            self.baseline.add(str(e["chat"]))
            LOG.debug(
                "Начальный снимок чата %s: старая история не отправляется", e["chat"]
            )
            return
        if e.get("initial"):
            return
        e = dict(e)
        e["chat"] = str(e["chat"])
        if self.state.event_seen(e):
            self.counts["duplicates"] += 1
            LOG.debug("Повторное событие пропущено: %s", e["id"])
            return
        if e["kind"] == "order":
            previous_status = (
                self.state.d.get("ledger", {})
                .get(e["order_id"], {})
                .get("handled_status")
            )
            if previous_status == e.get("status") or (
                previous_status == "REFUNDED"
                or previous_status == "CLOSED"
                and e.get("status") == "PAID"
            ):
                self.state.finish_event(e)
                self.state.save()
                return
        self.counts["events"] += 1
        self.last_event = e["kind"] + " · " + e["id"]
        key = "chat:" + e["chat"]
        meta = self.state.d.setdefault("chat_meta", {}).setdefault(e["chat"], {})
        if e.get("username"):
            meta["username"] = e["username"]
        if e.get("chat_name"):
            meta["chat_name"] = e["chat_name"]
        if e.get("kind") == "message" and not any(
            e.get(flag) for flag in ("seller", "system", "by_bot", "employee")
        ):
            meta["message_text"] = e.get("message_text", e.get("text", ""))
        if e.get("support_chat"):
            meta["support_chat"] = True
        if e.get("buyer_id"):
            meta["buyer_id"] = e["buyer_id"]
        await self.topic(key, self.chat_label(e["chat"]))
        await self.refresh_chat_controls(e["chat"])
        if e["kind"] == "message":
            outgoing = self.state.d.get("outgoing_messages", {}).get(e["id"], {})
            if e.get("employee"):
                author = (
                    self.nick_link(e.get("author") or "Поддержка FunPay", e["chat"])
                    + " 🆘"
                )
            elif e.get("system"):
                author = "FunPay 🔵"
            elif e.get("seller"):
                icon = (
                    self.origin_icon(outgoing["action"])
                    if outgoing
                    else "🤖"
                    if e.get("by_bot")
                    else "👤"
                )
                author = (
                    self.nick_link(self.seller_name(), e["chat"]) + " (" + icon + ")"
                )
            else:
                author = "💬 " + self.nick_link(
                    e.get("author") or meta.get("username") or "Пользователь", e["chat"]
                )
            body = e.get("text", "") or ("📷 Изображение" if e.get("photo") else "")
            # Exact echoes already present in Telegram are deduplicated by message ID.
            # Never discard a message merely because it is by_bot/system/employee.
            if not outgoing.get("mirrored"):
                await self.chat_message(
                    key,
                    author,
                    body,
                    notify=self.cfg.flag("Notifications", "order_status")
                    if e.get("system")
                    else True,
                )
                if e.get("photo"):
                    await self.telegram_call(
                        "send_photo",
                        chat_id=self.group,
                        message_thread_id=self.state.d["topics"][key],
                        photo=e["photo"],
                    )
            if (
                e.get("seller")
                and not e.get("by_bot")
                and not e.get("system")
                and not outgoing
            ):
                meta["manual_action"] = e["id"]
                self.record_human_activity("FunPay")
            if not e.get("system") and not e.get("by_bot"):
                await self.automate_message(e)
        elif e["kind"] == "order":
            FinancialLedger(self.state).observe(e)
            archived = e["order_id"] not in self.state.d.get(
                "order_cards", {}
            ) and self.state.d["cards"].get("orders/order:" + e["order_id"])
            if not archived:
                self.record_order_status(e)
            elif e["status"] in ("CLOSED", "REFUNDED"):
                action = "archive:" + e["order_id"] + ":" + e["status"]

                async def append_history():
                    await self.telegram_call(
                        "send_message",
                        chat_id=self.group,
                        message_thread_id=self.state.d["topics"]["orders"],
                        reply_to_message_id=archived,
                        text=f"{self.ORDER_STATUSES[e['status']]} · #{e['order_id']}",
                        allow_sending_without_reply=True,
                    )

                await self.once(action, append_history)
            self.state.d.get("completed_order_stats", {}).pop(e["order_id"], None)
            self.state.d["orders"][e["order_id"]] = {**e, "updated_at": time.time()}
            if e["status"] == "PAID":
                await self.automate_message(e)
            if e["status"] == "CLOSED":
                self.state.d["ledger"][e["order_id"]].setdefault(
                    "confirmed_at", time.time()
                )
                if self.cfg.flag("CONFIRMATIONREPLY"):
                    await self.send_buyer(
                        e,
                        self.cfg.get("CONFIRMATIONREPLY", "text"),
                        "confirm:" + e["order_id"],
                    )
                self.schedule("REVIEWREMINDER", e)
            if e["status"] in ("CLOSED", "REFUNDED"):
                self.state.d["jobs"].pop("CONFIRMREMINDER:" + e["order_id"], None)
            if e["status"] == "REFUNDED":
                self.state.d["jobs"].pop("REVIEWREMINDER:" + e["order_id"], None)
        elif e["kind"] == "review":
            if e["order_id"] in self.state.d.get("order_cards", {}):
                self.order_log(
                    e["order_id"],
                    "🗑 Отзыв удалён"
                    if e.get("rating") == 6
                    else (
                        "📝 Отзыв изменён" if e.get("changed") else "⭐ Получен отзыв"
                    )
                    + f": {e.get('rating', '—')}",
                    "review:" + e["id"],
                )
            self.state.d["jobs"].pop("REVIEWREMINDER:" + e["order_id"], None)
            for section in ("REVIEWREPLY", "REVIEWCHATREPLY"):
                if (
                    e.get("changed")
                    and section == "REVIEWCHATREPLY"
                    and not self.cfg.flag(section, "on_feedback_changed")
                ):
                    continue
                rule = self.cfg.data(section, str(e.get("rating", 0)))
                if self.cfg.flag(section) and rule.get("ENABLE"):
                    text = rule.get("TEXT", "")
                    if section == "REVIEWREPLY":
                        if e.get("rating", 0) not in range(1, 6):
                            continue
                        await self.once(
                            section + ":" + e["id"],
                            lambda: self.fp.review(e["order_id"], self.render(text, e)),
                        )
                    else:
                        await self.send_buyer(e, text, section + ":" + e["id"])
        for name, plugin in list(self.plugins.items()):
            if hasattr(plugin, "event"):
                try:
                    await plugin.event(copy.deepcopy(e))
                except Exception as ex:
                    self.counts["errors"] += 1
                    logging.getLogger("FunPayBOT.plugins." + name).exception(
                        "Ошибка обработки события: %s", e["id"]
                    )
        if e["kind"] == "review":
            await self.notify(
                "review", f"⭐ Отзыв по заказу #{e['order_id']}: {e.get('rating', '—')}"
            )
        self.state.finish_event(e)
        if e["kind"] == "order":
            self.state.d["ledger"][e["order_id"]]["handled_status"] = e["status"]
        self.state.save()
        await self.flush_order_cards()
        self.state.cleanup_completed()
        self.state.save()
        self.log_funpay_event(e)

    def record_human_activity(self, source):
        self.state.d["last_human"] = time.time()
        self.state.save()

    def log_funpay_event(self, event):
        kind = event["kind"]
        if kind == "order":
            title = {
                "PAID": "Новый заказ",
                "CLOSED": "Заказ подтверждён",
                "REFUNDED": "Возврат по заказу",
            }.get(event.get("status"), "Изменение заказа")
            LOG.info(
                "%s: #%s · %s %s",
                title,
                event.get("order_id", "—"),
                event.get("price", "—"),
                event.get("currency", ""),
                extra={"event_source": "FunPay"},
            )
        elif kind == "review":
            LOG.info(
                "%s: заказ #%s",
                "Отзыв удалён" if event.get("rating") == 6 else "Новый отзыв",
                event.get("order_id", "—"),
                extra={"event_source": "FunPay"},
            )
        elif kind == "message":
            if event.get("by_bot") or event.get("id") in self.state.d.get(
                "outgoing_messages", {}
            ):
                return
            label = (
                "Сообщение поддержки"
                if event.get("employee")
                else "Системное сообщение FunPay"
                if event.get("system")
                else "Сообщение продавца на FunPay"
                if event.get("seller")
                else "Новое сообщение"
            )
            LOG.info(
                "%s · %s",
                label,
                event.get("username") or event.get("author") or "чат покупателя",
                extra={"event_source": "FunPay"},
            )

    async def automate_message(self, e):
        if not self.cfg.flag("SellerStatus", "online"):
            return
        text = e.get("text", "").replace("\n", "").strip().casefold()
        owner = bool(e.get("seller"))
        if (
            not owner
            and e.get("text", "").strip().casefold()
            == self.cfg.get("SellerStatus", "command", "#status").strip().casefold()
        ):
            if self.cfg.flag("SellerStatus"):
                stamp = self.state.d["last_human"]
                status = self.render(
                    self.cfg.get(
                        "SellerStatus", "active_text" if stamp else "unknown_text"
                    ),
                    {
                        **e,
                        "last_activity": human_time(self.cfg, stamp) if stamp else "",
                        "activity_source": "",
                    },
                )
                await self.send_buyer(
                    e, status, "status:" + e["id"], watermark=False, rendered=True
                )
            return
        if not owner:
            rule = self.response_rules().get(text, {})
            target = str(rule.get("TRIGGER_AUTHOR", "BUYER")).upper()
            if rule.get("ENABLED") and target == "BUYER":
                await self.send_buyer(
                    e,
                    rule.get("RESPONSE", ""),
                    ("owner:" if owner else "response:") + e["id"],
                )
                if rule.get("NOTIFY", True):
                    notice = self.render(
                        self.cfg.get("AUTORESPONSE", "notification_text"),
                        e,
                    )
                    if notice:
                        await self.notify("autoresponse", notice)
                return
        if owner:
            return
        if self.cfg.flag("AUTOGREETINGS") and self.greeting_provider() is None:
            if (
                self.cfg.flag("AUTOGREETINGS", "only_new_chats")
                and e["chat"] in self.baseline
            ):
                return
            previous = self.state.d["greetings"].get(e["chat"], 0)
            cooldown = self.cfg.number("AUTOGREETINGS", "cooldown_hours", 48) * 3600
            if time.time() - previous >= cooldown:
                template = self.cfg.get("AUTOGREETINGS", "default_text")
                if not e.get("description"):
                    known = [
                        order
                        for order in self.state.d["orders"].values()
                        if str(order.get("chat")) == str(e["chat"])
                        and order.get("description")
                    ]
                    if known:
                        e = {
                            **e,
                            "description": max(
                                known, key=lambda item: item.get("updated_at", 0)
                            )["description"],
                        }
                for pattern, value in self.cfg.data(
                    "AUTOGREETINGS", "lot_greetings"
                ).items():
                    if e.get("description") and re.fullmatch(
                        re.escape(pattern).replace(r"\{\}", r".*"), e["description"]
                    ):
                        template = value
                        break
                self.state.d["greetings"][e["chat"]] = time.time()
                self.state.save()
                await self.send_buyer(e, template, "greeting:" + e["id"])
