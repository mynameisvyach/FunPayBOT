"""Карточки заказов, их история и действия продавца."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time

from ..framework import (
    plugin_capability,
)
from ..metadata import LOG, human_time


class OrdersMixin:
    ORDER_PHASES = {
        "recipient": "Ожидает получателя",
        "confirm": "Ожидает подтверждения получателя",
        "ready": "В очереди на выполнение",
        "preparing": "Подготовка закупки",
        "paying": "Отправка оплаты поставщику",
        "verifying": "Проверка выдачи поставщиком",
        "delivery_check": "Нужна проверка выдачи",
        "manual": "Нужна ручная проверка",
        "failed": "Ошибка выполнения",
        "done": "Выполнен",
        "refunded": "Средства возвращены",
        "cancelled": "Выполнение отменено",
    }

    ORDER_STATUSES = {
        "PAID": "🆕 Заказ открыт · оплачен",
        "CLOSED": "✅ Заказ подтверждён",
        "REFUNDED": "↩ Средства возвращены",
    }

    def order_record(self, event):
        records = self.state.d.setdefault("order_cards", {})
        key = event["order_id"]
        row = records.setdefault(
            key,
            {
                "event": {},
                "history": [],
                "revision": 0,
                "status": None,
                "phase": "manual_wait",
            },
        )
        row["event"].update({k: v for k, v in event.items() if v is not None})
        return row

    def order_log(self, key, text, identity=None):
        row = self.state.d.get("order_cards", {}).get(key)
        if not row:
            return
        if identity and any(entry.get("id") == identity for entry in row["history"]):
            return
        row["history"].append(
            {"at": time.time(), "text": str(text)[:500], "id": identity}
        )
        row["revision"] += 1

    def record_order_status(self, event):
        row = self.order_record(event)
        status = event["status"]
        if row["status"] == status:
            return
        row["status"] = status
        row.pop("refund_confirmation", None)
        if status == "REFUNDED":
            row.pop("refund_pending", None)
            row.update(phase="refunded", progress_text=None)
        self.order_log(
            event["order_id"],
            self.ORDER_STATUSES.get(status, "Статус FunPay: " + status),
        )

    def claim_order(self, plugin_id, event):
        row = self.state.d.get("order_cards", {}).get(event["order_id"])
        if not row:
            return (
                True  # Existing recovery operations do not backfill Telegram history.
            )
        if (
            row.get("refund_pending")
            or row["status"] == "REFUNDED"
            or row.get("fulfilled", {}).get("source") == "manual"
            or row.get("plugin") not in (None, plugin_id)
        ):
            return False
        row["event"].update(
            {key: event[key] for key in ("category", "description") if event.get(key)}
        )
        if not row.get("plugin"):
            row["plugin"] = plugin_id
            title = (
                self.state.d.get("plugin_metadata", {})
                .get(plugin_id, {})
                .get("title", plugin_id)
            )
            self.order_log(event["order_id"], "🧩 Принят плагином " + title)
        return True

    def record_order_progress(self, key, phase, description=None):
        row = self.state.d.get("order_cards", {}).get(key)
        if not row or (
            row.get("phase") == phase and row.get("progress_text") == description
        ):
            return
        row.update(
            phase=str(phase),
            progress_text=str(description)[:300] if description else None,
        )
        self.order_log(
            key,
            "⚙ " + (row["progress_text"] or self.ORDER_PHASES.get(phase, str(phase))),
        )

    def order_pages(self, row):
        e = row["event"]
        plugin = row.get("plugin")
        title = (
            self.state.d.get("plugin_metadata", {}).get(plugin, {}).get("title", plugin)
            if plugin
            else "Без плагина · вручную"
        )
        phase = (
            "Средства возвращены"
            if row["status"] == "REFUNDED"
            else "Выполнен · подтверждён покупателем"
            if row["status"] == "CLOSED"
            else "Выполнен вручную"
            if row.get("fulfilled", {}).get("source") == "manual"
            else row.get("progress_text")
            or self.ORDER_PHASES.get(row["phase"], "Ожидает ручного выполнения")
        )
        head = (
            f"📦 Заказ #{e['order_id']}\nПокупатель: {str(e.get('username', 'Покупатель'))[:100]}"
            f"\nТовар: {str(e.get('description', 'Не указан'))[:450]}\nКоличество: {str(e.get('amount', 1))[:20]}"
            f"\nСумма: {str(e.get('price', '—'))[:30]} {str(e.get('currency', 'RUB'))[:10]}"
            f"\nКатегория: {str(e.get('category') or 'Не передана')[:40]}\nПлагин: {str(title)[:100]}"
            f"\nПроцесс: {phase}\n\nИстория заказа:\n"
        )
        pages = []
        body = ""
        for entry in row["history"]:
            line = human_time(self.cfg, entry["at"]) + " · " + entry["text"] + "\n"
            if body and len((head + body + line).encode("utf-16-le")) // 2 > 3400:
                pages.append(head + body)
                body = ""
            body += line
        pages.append(head + body)
        return pages

    async def render_order(self, key, page=None):
        async with self.card_locks.setdefault("orders/order:" + key, asyncio.Lock()):
            row = self.state.d.get("order_cards", {}).get(key)
            if not row:
                return
            revision = row["revision"]
            pages = self.order_pages(row)
            page = (
                len(pages) - 1
                if page is None
                else max(0, min(int(page), len(pages) - 1))
            )
            text = pages[page]
            buttons = []
            terminal = row["status"] in ("CLOSED", "REFUNDED")
            e = row["event"]
            if len(pages) > 1 and not terminal:
                text += f"\nИстория: {page + 1}/{len(pages)}"
                buttons.append(
                    [
                        {
                            "text": "‹ Раньше",
                            "callback_data": f"ord:{key}:page:{max(0, page - 1)}",
                        },
                        {
                            "text": "Позже ›",
                            "callback_data": f"ord:{key}:page:{min(len(pages) - 1, page + 1)}",
                        },
                    ]
                )
            request = row.get("refund_confirmation", {})
            if request and request.get("expires", 0) > time.time():
                text += "\n\nВернуть покупателю всю оплату? Действие отменить нельзя."
                if row.get("fulfilled"):
                    text += " Товар уже отмечен выполненным."
                buttons.append(
                    [
                        {
                            "text": "Подтвердить возврат",
                            "callback_data": f"ord:{key}:confirm:{request['token']}",
                        },
                        {"text": "Отмена", "callback_data": f"ord:{key}:cancel"},
                    ]
                )
            elif row["status"] == "PAID":
                if (
                    not row.get("refund_pending")
                    and not row.get("plugin")
                    and not row.get("fulfilled")
                ):
                    buttons.append(
                        [{"text": "✅ Выполнено", "callback_data": f"ord:{key}:done"}]
                    )
                if not self.state.d["actions"].get("refund:" + key):
                    buttons.append(
                        [
                            {
                                "text": "↩ Вернуть средства",
                                "callback_data": f"ord:{key}:refund",
                            }
                        ]
                    )
            thread = self.state.d["topics"].get("chat:" + str(e.get("chat")))
            links = [
                {
                    "text": "Открыть заказ",
                    "url": "https://funpay.com/orders/" + key + "/",
                }
            ]
            if thread:
                links.insert(
                    0,
                    {
                        "text": "Перейти в чат",
                        "url": f"https://t.me/c/{str(self.group).removeprefix('-100')}/{thread}",
                    },
                )
            buttons.append(links)
            markup = {"inline_keyboard": buttons}
            cardkey = "orders/order:" + key
            mid = self.state.d["cards"].get(cardkey)
            signature = hashlib.sha256(
                json.dumps([text, markup], ensure_ascii=False).encode()
            ).hexdigest()
            if mid and row.get("render_signature") == signature:
                return
            if mid:
                try:
                    await self.tg.call(
                        "edit_message_text",
                        chat_id=self.group,
                        message_id=mid,
                        text=text,
                        reply_markup=markup,
                    )
                except Exception as error:
                    if "message is not modified" not in str(error).lower():
                        raise
            else:

                async def create():
                    message = await self.tg.call(
                        "send_message",
                        chat_id=self.group,
                        message_thread_id=self.state.d["topics"]["orders"],
                        text=text,
                        reply_markup=markup,
                    )
                    self.state.d["cards"][cardkey] = message["message_id"]
                    self.state.save()

                if not await self.once("ordercard:" + key, create, notify=False):
                    return
            if terminal and len(pages) > 1:
                for index, history in enumerate(pages[:-1]):

                    async def send_history(history=history):
                        await self.tg.call(
                            "send_message",
                            chat_id=self.group,
                            message_thread_id=self.state.d["topics"]["orders"],
                            reply_to_message_id=self.state.d["cards"][cardkey],
                            text=history,
                            allow_sending_without_reply=True,
                        )

                    action = f"archive:{key}:history:{index}"
                    if action not in self.state.d["actions"]:
                        await self.once(action, send_history, notify=False)
                    if self.state.d["actions"].get(action, {}).get("status") != "done":
                        return
            row.update(rendered_revision=revision, render_signature=signature)
            self.state.save()

    async def flush_order_cards(self):
        for key, row in list(self.state.d.get("order_cards", {}).items()):
            if row.get("rendered_revision") != row["revision"]:
                try:
                    await self.render_order(key)
                except Exception:
                    LOG.exception("Не удалось обновить карточку заказа #%s", key)

    async def order_callback(self, data, user, message):
        parts = data.split(":")
        key = parts[1] if len(parts) > 2 else ""
        action = parts[2] if len(parts) > 2 else ""
        row = self.state.d.get("order_cards", {}).get(key)
        if not row or message.get("message_id") != self.state.d["cards"].get(
            "orders/order:" + key
        ):
            return
        async with self.lock:
            if action == "page":
                if len(parts) == 4 and parts[3].isdigit():
                    await self.render_order(key, int(parts[3]))
                return
            if action == "cancel":
                row.pop("refund_confirmation", None)
                row["revision"] += 1
            elif (
                action == "done"
                and not row.get("refund_pending")
                and not row.get("plugin")
                and not row.get("fulfilled")
            ):
                current = await self.fp.verify_order(key)
                if current["chat"] != row["event"]["chat"]:
                    raise ValueError("Чат заказа не совпадает")
                row["event"].update(
                    {
                        name: current[name]
                        for name in (
                            "order_desc",
                            "order_params",
                            "category_name",
                            "category_fullname",
                            "game",
                        )
                        if name in current
                    }
                )
                if current["status"] != row["status"]:
                    await self.handle_event(
                        {
                            **row["event"],
                            "kind": "order",
                            "status": current["status"],
                            "id": "verify:" + key + ":" + str(time.time_ns()),
                        }
                    )
                if current["status"] in ("PAID", "CLOSED"):
                    await self.fulfilled(row["event"], manual_user=user)
            elif (
                action == "refund"
                and row["status"] in ("PAID", "CLOSED")
                and not self.state.d["actions"].get("refund:" + key)
            ):
                row["refund_confirmation"] = {
                    "user": user,
                    "token": secrets.token_hex(5),
                    "expires": time.time() + 120,
                }
                row["revision"] += 1
            elif action == "confirm" and len(parts) == 4:
                request = row.pop("refund_confirmation", {})
                row["revision"] += 1
                if (
                    request.get("user") == user
                    and request.get("token") == parts[3]
                    and request.get("expires", 0) > time.time()
                ):
                    plugin = self.plugins.get(row.get("plugin"))
                    commerce = plugin_capability(plugin, "commerce") if plugin else None
                    if commerce and not hasattr(commerce, "purchase_lock"):
                        commerce = None
                    lock = commerce.purchase_lock if commerce else asyncio.Lock()
                    async with lock:
                        await self.refund_from_order(key, user, commerce)
            self.state.save()
            await self.render_order(key)

    async def refund_from_order(self, key, user, commerce=None):
        row = self.state.d["order_cards"][key]
        e = row["event"]
        operation = (
            self.state.d["plugin_state"]
            .get(row.get("plugin"), {})
            .get("orders", {})
            .get(key)
        )
        if (
            operation
            and operation.get("payment_intent")
            and operation.get("phase") not in ("done", "refunded")
        ):
            self.order_log(
                key,
                "⚠ Возврат не отправлен: сначала проверьте незавершённую оплату/выдачу плагина.",
            )
            return
        current = await self.fp.verify_order(key)
        if current["chat"] != e["chat"]:
            raise ValueError("Чат заказа не совпадает")
        if current["status"] != row["status"]:
            await self.handle_event(
                {
                    **e,
                    "kind": "order",
                    "status": current["status"],
                    "id": "verify:" + key + ":" + str(time.time_ns()),
                }
            )
        if current["status"] not in ("PAID", "CLOSED"):
            return
        if operation:
            operation.update(refund_hold=True, phase="manual")
            if commerce:
                commerce.save()
        row["refund_pending"] = True
        self.order_log(key, f"↩ Администратор {user} отправляет возврат")
        self.state.save()
        ok = await self.once(
            "refund:" + key, lambda: self.fp.refund_order(key), notify=False
        )
        if not ok:
            self.order_log(
                key,
                "⚠ Возврат не подтверждён. Проверьте заказ на FunPay; повторная отправка заблокирована.",
            )
            return
        await self.handle_event(
            {**e, "kind": "order", "id": "refund:telegram:" + key, "status": "REFUNDED"}
        )
        self.order_log(
            key, f"↩ Возврат выполнен администратором {user}", "refund-admin:" + key
        )
