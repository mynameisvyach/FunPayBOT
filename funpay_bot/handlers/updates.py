"""Updates application handlers."""

from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal

from ..finance import FinancialLedger, WithdrawalCosts, money
from ..framework import (
    plugin_capability,
)
from ..metadata import LOG, TITLES
from ..telegram import DemoTelegram


class UpdatesMixin:
    def allowed(self, message, user_id, *, callback=False):
        chat = message.get("chat", {})
        if chat.get("id") != self.group or chat.get("type") != "supergroup":
            return False
        if callback:
            return user_id == self.cfg.owner_id
        sender_chat = message.get("sender_chat")
        if sender_chat:
            return (
                sender_chat.get("id") == self.group
                and sender_chat.get("type") == "supergroup"
                and not message.get("is_automatic_forward")
            )
        return user_id == self.cfg.owner_id

    def route(self, message):
        thread = message.get("message_thread_id")
        return next(
            (key for key, value in self.state.d["topics"].items() if value == thread),
            None,
        )

    async def ingest_update(self, update):
        await self.update(update.model_dump(exclude_none=True, by_alias=True))

    async def update(self, update):
        call = update.get("callback_query")
        message = (call or {}).get("message") if call else update.get("message")
        if not message:
            return
        sender = (call or message).get("from", {})
        anonymous = not call and bool(message.get("sender_chat"))
        if sender.get("is_bot") and not anonymous:
            return
        if not self.allowed(message, sender.get("id"), callback=bool(call)):
            return
        if anonymous:
            LOG.info("Управление от имени настроенной группы: %s", self.group)
        # The only personal control session belongs to the owner. Anonymous group
        # messages may fill its forms; callbacks still require the real owner ID.
        sender = {"id": self.cfg.owner_id}
        topic = self.route(message)
        if not call:
            command = message.get("text", "").split(" ", 1)[0].split("@", 1)[0]
            name = command[1:] if command.startswith("/") else ""
            if name in ("backup", "proxy", "golden_key", "restart"):
                await self.service_command(name, sender["id"])
                return
            if name == "lot_tools":
                await self.central_lot_menu("lot_tools")
                return
            if name in self.plugins:
                plugin = self.plugins[name]
                if hasattr(plugin, "menu"):
                    await plugin.menu(new_message=True)
                else:
                    self.state.d["cards"].pop("plugin:" + name + "/menu", None)
                    await plugin.callback("menu", sender["id"])
                return
        if not topic:
            return
        user = sender["id"]
        if not call and topic == "main" and await self.service_input(message, user):
            return
        if call:
            await self.tg.call("answer_callback_query", callback_query_id=call["id"])
            data = call.get("data", "")
            if await self.service_callback(data, user, topic, message):
                return
            if topic == "orders" and data.startswith("ord:"):
                try:
                    await self.order_callback(data, user, message)
                except Exception:
                    LOG.exception("Ошибка действия карточки заказа: %s", data)
                    key = data.split(":")[1]
                    if message.get("message_id") == self.state.d["cards"].get(
                        "orders/order:" + key
                    ):
                        self.order_log(
                            key,
                            "⚠ Действие не подтверждено. Проверьте заказ на FunPay и журнал.",
                        )
                        self.state.save()
                        await self.render_order(key)
                return
            if data == "ui:close":
                await self.close_ui_message(message, topic, user)
                return
            if data.startswith("reportpage:") and topic == "main":
                try:
                    _, token, page = data.split(":")
                    page = int(page)
                except ValueError:
                    return
                report = self.state.d.get("reports", {}).get(token)
                if not report:
                    return
                mid = message.get("message_id")
                if mid not in (
                    report["message_id"],
                    self.state.d["cards"].get("main/menu"),
                ):
                    return
                await self.report_details(
                    token, page, new_message=(mid == report["message_id"])
                )
                return
            if data.split(":", 1)[0] in self.plugins:
                name, action = data.split(":", 1)
                if topic == "plugin:" + name and name in self.plugins:
                    if message.get("message_id") != self.state.d["cards"].get(
                        topic + "/menu"
                    ):
                        return
                    if action == "close":
                        if message.get("message_id") != self.state.d["cards"].get(
                            topic + "/menu"
                        ):
                            return
                        self.register_ui_message(message["message_id"], topic, "menu")
                        await self.close_ui_message(message, topic, user)
                        return

                    if not plugin_capability(self.plugins[name], "commerce"):
                        logging.getLogger("unified.plugins." + name).info(
                            "Действие меню: %s; администратор=%s",
                            action.split(":", 1)[0],
                            user,
                        )
                    try:
                        await self.plugins[name].callback(action, user)
                    except Exception as error:
                        logging.getLogger("unified.plugins." + name).exception(
                            "Ошибка меню плагина: %s", action
                        )
                        await self.say(
                            topic,
                            str(error)
                            if isinstance(error, ValueError)
                            else "Не удалось выполнить действие. Подробности в журнале.",
                        )
                return
            if topic.startswith("chat:"):
                chat = topic[5:]
                if data.startswith("template:"):
                    name = data[9:]
                    if message.get("message_id") != self.state.d["cards"].get(
                        topic + "/controls"
                    ):
                        return
                    entry = self.manual_templates().get(name)
                    if entry:
                        await self.send_buyer(
                            {"chat": chat}, entry["text"], "template:" + call["id"]
                        )
                return
            if topic != "main":
                return
            # Only active menu buttons can mutate settings; reject old forwarded cards.
            if message.get("message_id") != self.state.d["cards"].get("main/menu"):
                return
            self.pending.pop(user, None)
            try:
                await self.callback(data, user)
            except Exception as error:
                LOG.exception("Ошибка действия главного меню: %s", data)
                await self.say(
                    "main",
                    str(error)
                    if isinstance(error, ValueError)
                    else "Не удалось выполнить действие. Проверьте журнал и подключение.",
                )
        else:
            text = message.get("text", "")
            if topic == "main":
                command = text.split("@", 1)[0].split(" ", 1)[0]
                if command == "/debug":
                    await self.toggle_debug()
                elif command == "/menu":
                    self.pending.pop(user, None)
                    await self.menu(new_message=True)
                elif user in self.pending:
                    section, key, expires = self.pending.pop(user)
                    if time.time() > expires:
                        await self.say(
                            "main", "Время ввода истекло. Откройте параметр снова."
                        )
                        return
                    if not text:
                        return
                    if section == "_automation":
                        await self.automation_input(key, text, user)
                        return
                    if section == "_logs":
                        await self.search_logs(text, "core" if key == "query" else key)
                        return
                    if (section, key) == ("SellerStatus", "command"):
                        value = text.strip()
                        if not value.isprintable() or not re.fullmatch(
                            r"\S{1,64}", value
                        ):
                            self.pending[user] = (section, key, expires)
                            await self.card(
                                "main",
                                "menu",
                                "Введите одну команду без пробелов, до 64 символов. Например: #status или !seen.",
                                [[("‹ Отмена", "menu:status")]],
                            )
                            return
                        self.cfg.set(section, key, value)
                        await self.menu("status")
                        return
                    if section == "_withdraw":
                        try:
                            debit_raw, receive_raw = text.split()
                            debit = money(debit_raw.replace(",", "."))
                            receive = Decimal(receive_raw.replace(",", "."))
                            if debit <= 0 or not receive.is_finite() or receive <= 0:
                                raise ValueError()
                            async with self.cash_lock:
                                op = self.state.d["withdrawals"].get(key)
                                if op and op["status"] in ("pending", "unknown"):
                                    costs = WithdrawalCosts.calculate(
                                        debit,
                                        receive,
                                        op.get("kind", "card"),
                                        op.get("market_rate"),
                                    )
                                    op.update(
                                        status="done",
                                        debit=str(debit),
                                        receive=str(receive),
                                        reconciled_by=user,
                                        reconciled_at=time.time(),
                                        **costs,
                                    )
                                    FinancialLedger(self.state).allocate_withdrawal(op)
                                    self.state.save()
                        except (ValueError, ArithmeticError):
                            await self.say(
                                "main",
                                "Нужны две корректные суммы в указанных валютах. Откройте сверку снова.",
                            )
                            return
                        await self.withdrawal_history()
                        return
                    old = self.cfg.get(section, key)
                    try:
                        value = self.validate_edit(old, text)
                        self.cfg.set(section, key, value)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        await self.say(
                            "main",
                            "Неверный формат. Для списков и словарей нужен JSON; число не может быть отрицательным.",
                        )
                        return
                    if section == "AUTORESPONSE":
                        await self.refresh_all_chat_controls()
                    if section == "AutoWithdraw":
                        await self.withdraw_menu()
                    else:
                        await self.section(
                            section
                        ) if section in TITLES else await self.menu("status")
            elif topic.startswith("plugin:"):
                name = topic[7:]
                if (
                    name in self.plugins
                    and text.split("@", 1)[0].split(" ", 1)[0] == "/menu"
                ):
                    plugin = self.plugins[name]
                    if hasattr(plugin, "menu"):
                        await plugin.menu(new_message=True)
                    else:
                        self.state.d["cards"].pop(topic + "/menu", None)
                        await plugin.callback("menu", user)
                    return
                if (
                    name in self.plugins
                    and message.get("document")
                    and hasattr(self.plugins[name], "document")
                ):
                    doc = message["document"]
                    if doc.get("file_size", 0) > (
                        40_000_000 if name == "lot_tools" else 1_000_000
                    ):
                        await self.say(topic, "Файл слишком большой для этого плагина.")
                        return
                    if isinstance(self.tg, DemoTelegram):
                        return
                    body = await self.tg.bot.download(doc["file_id"])
                    await self.plugins[name].document(
                        doc.get("file_name", ""), body.read(), user
                    )
                elif name in self.plugins and hasattr(self.plugins[name], "message"):
                    await self.plugins[name].message(text, user)
            elif topic.startswith("chat:"):
                if not text.startswith("/"):
                    await self.telegram_to_funpay(message, topic)

    @staticmethod
    def validate_edit(old, text):
        if old.lower() in ("true", "false"):
            if text.lower() not in ("true", "false"):
                raise ValueError()
            return text.lower()
        if old.startswith(("{", "[")):
            value = json.loads(text)
            if type(value) != type(json.loads(old)):
                raise ValueError()
            return json.dumps(value, ensure_ascii=False)
        if re.fullmatch(r"\d+(\.\d+)?", old):
            if not re.fullmatch(r"\d+(\.\d+)?", text):
                raise ValueError()
        return text

    async def callback(self, data, user):
        if await self.management_callback(data, user):
            return
        if await self.automation_callback(data, user):
            return
        if await self.release_callback(data, user):
            return
        if data.startswith("menu:"):
            self.pending.pop(user, None)
            await self.menu(data[5:])
        elif data.startswith("section:"):
            await self.section(data[8:])
        elif data.startswith("toggle:"):
            _, section, key = data.split(":")
            if section == "AUTORESPONSE":
                return
            if section not in set(TITLES) | {
                "SellerStatus",
                "AutoRaise",
                "AutoWithdraw",
            }:
                return
            if key not in ("enabled", "online"):
                return
            if (
                section == "AutoWithdraw"
                and not self.cfg.flag(section, key)
                and not self.cfg.get("AutoWithdraw", "wallet")
            ):
                raise ValueError(
                    "Сначала выберите способ и сохранённые реквизиты в разделе вывода"
                )
            self.cfg.set(section, key, not self.cfg.flag(section, key))
            if section == "AutoWithdraw":
                await self.withdraw_menu()
            elif section == "SellerStatus" and key == "enabled":
                await self.menu("status")
            else:
                await self.section(section) if section in TITLES else await self.menu()
        elif data.startswith("edit:"):
            _, section, key = data.split(":")
            if (section, key) not in (
                ("AutoWithdraw", "minimum"),
                ("AutoWithdraw", "reserve"),
                ("SellerStatus", "command"),
            ):
                return
            self.pending[user] = (section, key, time.time() + 300)
            if (section, key) == ("SellerStatus", "command"):
                await self.card(
                    "main",
                    "menu",
                    "✏ Команда статуса продавца\nСейчас: "
                    + self.cfg.get(section, key)
                    + "\nПришлите новую команду без пробелов, до 64 символов. Например: !seen.",
                    [[("‹ Отмена", "menu:status")]],
                )
                return
            await self.card(
                "main",
                "menu",
                f"✏ {section} / {key}\nПришлите новое значение в эту тему. Для отмены — /menu.\n\nСейчас:\n{self.cfg.get(section, key)}",
            )
        elif data == "demo" and self.fp.mode == "demo":
            await self.handle_event(
                {
                    "kind": "message",
                    "id": "demo:" + str(time.time_ns()),
                    "chat": "101",
                    "username": "DemoBuyer",
                    "text": "!help",
                }
            )
        elif data == "reload_plugins":
            await self.reload_plugins()
            await self.menu("plugins")
        elif data == "rates":
            await self.rates()
        elif data == "report":
            await self.report()
        elif data == "logs":
            await self.file_to_main("log.log", self.state.path.parent / "log.log")
