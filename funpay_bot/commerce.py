"""Общая машина состояний заказов и взаимодействие с адаптерами поставщиков."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import ui
from .finance import FinancialLedger, money
from .framework import (
    FrameworkPlugin,
    PluginConfigStore,
    ProductAdapter,
    ProviderAdapter,
    SettingsSchema,
    plugin_capability,
)
from .lots import LotProfile
from .metadata import human_time
from .primitives import RetryPolicy
from .ui import ui_button, ui_format_main_screen


class CommerceRuntime:
    """Обрабатывает торговые заказы независимо от конкретного поставщика."""

    DEFAULTS = {}

    def __init__(self, ctx):
        self.ctx = ctx
        self.app = ctx.app
        self.id = ctx.id
        self.log = getattr(
            ctx, "logger", logging.getLogger("FunPayBOT.plugins." + self.id)
        )
        self.path = self.app.cfg.path.parent / "plugins" / self.id / "config.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        defaults = {
            "Plugin": {
                "enabled": "false",
                "category_id": "0",
                "purchase_rate_rub": "0",
                "target_profit_per_unit": "0.05",
                "managed_lots": "{}",
                "auto_refund": "false",
                "require_category_match": "false",
                "autodump": "false",
                "auto_stock": "true",
                "unit_cost_usdt": "0",
                "payout_rate": "0.97",
                "status_check_interval": "15",
            },
            "Provider": self.DEFAULTS,
        }
        self.config_store = PluginConfigStore(self.path, defaults)
        self.config = self.config_store.data
        self.state = self.app.state.d["plugin_state"].setdefault(
            self.id,
            {"orders": {}, "inventory": {"units": "0", "cost": "0"}, "topups": {}},
        )
        self.inputs = {}
        self.tasks = {}
        self.purchase_lock = asyncio.Lock()
        self.last_check = 0
        self.last_lots = 0
        self.last_balance = 0

    def save(self):
        for order in self.state.get("orders", {}).values():
            if (
                not order.get("direct")
                and order.get("event")
                and self.app.claim_order(self.id, order["event"])
            ):
                self.app.record_order_progress(
                    order["order_id"], order.get("phase", "recipient")
                )
        self.app.state.save()

    def save_config(self):
        self.config_store.save()

    def get(self, key, default=""):
        return self.config.get("Plugin", key, fallback=default)

    def flag(self, key):
        return self.config.getboolean("Plugin", key, fallback=False)

    def effective_payout_rate(self):
        return min(
            Decimal(self.get("payout_rate", "0.97")),
            Decimal("1") - self.app.estimated_withdrawal_rate(),
        )

    def provider_get(self, key, default=""):
        return self.config.get("Provider", key, fallback=default)

    async def start(self):
        # Never resume the send phase after a crash. Lookup using the persisted provider reference.
        for order in self.state["orders"].values():
            if order["phase"] == "paying":
                order["phase"] = "verifying" if order.get("reference") else "manual"
            elif order["phase"] == "preparing":
                order["phase"] = "retry_wait"
                order["retry_due"] = time.time() + 30
            elif order["phase"] == "delivery_check" and self.RECOVER_DELIVERY_CHECK:
                order["phase"] = "verifying"
        self.save()
        await self.menu()
        self.log.debug(
            "Плагин запущен: автоматическая выдача=%s, демпер=%s, сохранённых операций=%s",
            self.flag("enabled"),
            self.flag("autodump"),
            len(self.state["orders"]),
        )

    async def stop(self):
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    SETTINGS_SCHEMA = {}
    RECOVER_DELIVERY_CHECK = False
    PRICE_SAMPLE_MIN = 10

    def setting_fields(self, key):
        descriptor = self.SETTINGS_SCHEMA[key]
        return descriptor.get("section", "Plugin"), descriptor["fields"]

    def apply_setting(self, key, text):
        descriptor = self.SETTINGS_SCHEMA[key]
        values = SettingsSchema.validate(descriptor, text)
        section = descriptor.get("section", "Plugin")
        for field, value in values.items():
            self.config[section][field] = value
        self.save_config()
        self.last_lots = 0
        self.state["floor_dirty"] = True
        self.save()

    def statistics_snapshot(self):
        return {
            "title": self.TITLE,
            "unit": getattr(self, "PRODUCT_LABEL", "единиц"),
            "days": self.ui_statistics(),
        }

    def ui_statistics(self):
        stats = copy.deepcopy(self.state.get("historical_statistics", {}))
        for key, row in self.app.state.d["ledger"].items():
            if row["source"] != self.id or not row.get("delivery_accounted"):
                continue
            day = datetime.fromtimestamp(
                row.get("delivered_at", row["at"]), ZoneInfo(ui.UI_TIMEZONE)
            ).strftime("%Y-%m-%d")
            entry = stats.setdefault(day, {"total_profit": 0.0, "quantities_sold": {}})
            credit = (
                money(row["credit"]) if row["status"] != "REFUNDED" else Decimal("0")
            )
            fee = (
                Decimal("0")
                if row.get("kind") == "direct"
                else (
                    money(credit * Decimal(row.get("fee_rate", "0.03")))
                    + money(row.get("fee_correction", 0))
                    if row["status"] != "REFUNDED"
                    else money(row.get("actual_fee", 0))
                )
            )
            entry["total_profit"] += float(credit - money(row.get("cost") or 0) - fee)
            order = self.state["orders"].get(key, {})
            delivered_units = row.get("delivery_units") or (
                int(order["quantity"]) * int(order.get("packs", 1)) if order else 0
            )
            if row["status"] != "REFUNDED" and delivered_units:
                quantity = str(int(delivered_units))
                entry["quantities_sold"][quantity] = (
                    entry["quantities_sold"].get(quantity, 0) + 1
                )
        return stats

    async def menu(self, *, new_message=False):
        ui.UI_TIMEZONE = self.app.cfg.get("General", "timezone", "Europe/Moscow")
        balance = "—"
        configured = True
        try:
            self.validate_credentials()
        except (ValueError, TypeError):
            configured = False
        if configured:
            try:
                value = Decimal(str(await self.provider_balance()))
                self.observe_balance(value)
                balance = f"${value:.4f}"
                self.state["balance_snapshot"] = str(value)
                self.save()
            except Exception as error:
                balance = "ошибка API"
                self.log.exception("Не удалось получить баланс поставщика")
        elif self.state.get("balance_snapshot") is not None:
            balance = f"${Decimal(self.state['balance_snapshot']):.4f}"
        text = ui_format_main_screen(
            getattr(self, "BALANCE_TITLE", "Баланс поставщика"),
            balance,
            self.ui_statistics(),
            getattr(self, "PRODUCT_LABEL", "единиц"),
            {
                "Доступы поставщика": {"running": configured},
                "Выдача": {"running": configured and self.flag("enabled")},
                "Проверка заказов": getattr(self, "runtime", None).health.get(
                    "monitor", {"running": False, "error": "Ожидается первый проход"}
                ),
                "Плавающий демпер": {
                    "running": configured
                    and self.flag("enabled")
                    and self.flag("autodump")
                },
            },
        )
        enabled = self.flag("enabled")
        rows = [
            [
                {
                    "text": "Выдача " + ("включена" if enabled else "выключена"),
                    "callback_data": "toggle",
                }
            ],
            [
                ui_button("Прямая закупка", "direct", "unit"),
                ui_button("Настройки", "settings", "settings"),
            ],
            [ui_button("Закрыть", "close", "stop")],
        ]
        await self.ctx.card(
            text, rows, parse_mode="HTML", new_message=new_message, main_menu=True
        )
        for order in self.state["orders"].values():
            if order["phase"] == "awaiting_sale_amount":
                await self.prompt_direct_sale(order)
        if self.state.get("pending_topups"):
            await self.prompt_topup()

    HOST_ONLY_FIELDS = frozenset(
        {
            "enabled",
            "category_id",
            "lot_kind",
            "target_profit_per_unit",
            "payout_rate",
            "managed_lots",
            "autodump",
            "auto_stock",
        }
    )

    def is_host_only_setting(self, key):
        return bool(
            set(self.SETTINGS_SCHEMA.get(key, {}).get("fields", []))
            & self.HOST_ONLY_FIELDS
        )

    def is_host_only_action(self, action):
        if action in (
            "lots",
            "discover_lots",
            "toggle_dump",
            "toggle_stock",
            "price_lots",
            "floor",
            "activate",
            "deactivate",
            "stock",
        ):
            return True
        if action.startswith(("setting:", "setting_toggle:")):
            return self.is_host_only_setting(action.split(":", 1)[1])
        return False

    def set_delivery_enabled(self, enabled):
        if enabled:
            self.validate_credentials()
            if money(self.get("purchase_rate_rub", "0")) <= 0:
                raise ValueError("Сначала учтите стоимость закупки USDT")
        self.config["Plugin"]["enabled"] = str(bool(enabled)).lower()
        self.save_config()

    async def callback(self, action, user):
        if self.is_host_only_action(action):
            return
        if action == "menu":
            self.inputs.pop(user, None)
            await self.menu()
        elif action == "toggle":
            self.set_delivery_enabled(not self.flag("enabled"))
            await self.menu()
        elif action == "logs":
            await self.app.log_menu(self.id)
        elif action == "close":
            key = "plugin:" + self.id + "/menu"
            message = self.app.state.d["cards"].get(key)
            if message:
                await self.app.tg.call(
                    "delete_message", chat_id=self.app.group, message_id=message
                )
            self.app.state.d["cards"].pop(key, None)
            self.save()
        elif action == "finance":
            r = FinancialLedger(self.app.state).summary(self.id)
            await self.ctx.card(
                f"{self.TITLE} · Финансы\nПоступления: {r['credit']:.2f} ₽\nВыдано по себестоимости: {r['cost']:.2f} ₽\nПрибыль после расчётных расходов на вывод: {r['profit']:.2f} ₽\nФактические выводы и подробная разбивка находятся в главной теме.",
                [[("‹ Назад", "menu")]],
            )
        elif action == "orders":
            text = "📋 Последние операции\n"
            rows = []
            for key, o in sorted(
                self.state["orders"].items(), key=lambda p: p[1]["at"], reverse=True
            )[:15]:
                text += f"#{key} · {o['phase']} · {o['quantity']} · {human_time(self.app.cfg, o['at'])}\n"
                if o["phase"] in ("manual", "failed", "verifying", "delivery_check"):
                    rows.append([(f"#{key} · проверить", "inspect:" + key)])
            await self.ctx.card(text, rows + [[("‹ Назад", "menu")]])
        elif action.startswith("inspect:"):
            key = action.split(":")[1]
            o = self.state["orders"].get(key)
            if not o:
                return
            rows = []
            if o["phase"] == "delivery_check":
                rows.append(
                    [("Выдачу проверил у поставщика", "delivery_verified:" + key)]
                )
            if o["phase"] in ("failed", "manual") and not o.get("payment_intent"):
                rows.append([("Повторить подготовку", "retry:" + key)])
                if not o.get("direct"):
                    rows.append([("Возврат покупателю", "refund:" + key)])
            receipt = self.receipt_url(o)
            if receipt:
                rows.append([{"text": "Открыть транзакцию", "url": receipt}])
            await self.ctx.card(
                f"#{key}\nСостояние: {o['phase']}\nСсылка поставщика: {o.get('reference', 'нет')}\nОшибка: {o.get('error', 'нет')}\nПосле намерения оплаты повторные платежи и возвраты заблокированы; выполняется только проверка.",
                rows + [[("‹ Заказы", "orders")]],
            )
        elif action.startswith("delivery_verified:"):
            key = action.split(":")[1]
            o = self.state["orders"].get(key)
            if o and o["phase"] == "delivery_check":
                o["delivery_verified_by"] = user
                o["delivery_verified_at"] = time.time()
                self.save()
                await self.finalize(o)
            await self.menu()
        elif action.startswith("retry:"):
            o = self.state["orders"].get(action.split(":")[1])
            if o and not o.get("payment_intent"):
                o["phase"] = "ready"
                o.pop("error", None)
                self.save()
            await self.menu()
        elif action.startswith("refund:"):
            key = action.split(":")[1]
            o = self.state["orders"].get(key)
            if o and not o.get("direct") and not o.get("payment_intent"):
                await self.ctx.card(
                    "Вернуть оплату за #"
                    + key
                    + "? После начала оплаты поставщику эта функция запрещена.",
                    [[("Вернуть", "refund_confirm:" + key)], [("Отмена", "orders")]],
                )
        elif action.startswith("refund_confirm:"):
            key = action.split(":")[1]
            o = self.state["orders"].get(key)
            if o and not o.get("direct") and not o.get("payment_intent"):
                verified = await self.app.fp.verify_order(key)
                if verified["status"] == "PAID":
                    ok = await self.app.once(
                        "refund:" + key, lambda: self.app.fp.refund_order(key)
                    )
                    if ok:
                        o["phase"] = "refunded"
                        self.save()
            await self.menu()
        elif action == "direct":
            self.inputs[user] = ("direct_recipient", time.time() + 300)
            await self.ctx.card(
                "Введите " + self.RECIPIENT_PROMPT + ":", [[("Отмена", "menu")]]
            )
        elif action == "topup":
            self.inputs[user] = ("topup", time.time() + 300)
            await self.ctx.card(
                "Пришлите количество USDT и фактическую стоимость закупки в рублях через пробел. Пример: 100 9300.",
                [[("Отмена", "menu")]],
            )
        elif action.startswith("topup_cost:"):
            queue = self.state.get("pending_topups", [])
            if queue and queue[0]["id"] == action.split(":", 1)[1]:
                self.inputs[user] = (action, time.time() + 300)
                await self.prompt_topup()
        elif action.startswith("direct_personal:"):
            await self.account_direct(action.split(":", 1)[1], user, Decimal("0"), True)
        elif action.startswith("direct_sale:"):
            key = action.split(":", 1)[1]
            o = self.state["orders"].get(key)
            if o and o.get("operator") == user and o["phase"] == "awaiting_sale_amount":
                self.inputs[user] = ("direct_sale:" + key, time.time() + 300)
                await self.ctx.card(
                    "За какую сумму была совершена прямая продажа? Введите сумму в рублях. Комиссии к ней применяться не будут.",
                    [[("Покупка себе", "direct_personal:" + key)]],
                )
        elif action == "balance":
            value = await self.provider_balance()
            await self.ctx.say(
                f"Баланс поставщика: {value} USDT\nЗаписанный запас: {self.state['inventory']['units']} USDT\nПополнения учитываются отдельно по фактической рублёвой стоимости."
            )
            await self.menu()
        elif action == "settings":
            rows = [
                [(descriptor["title"], "setting:" + key)]
                for key, descriptor in self.SETTINGS_SCHEMA.items()
                if not self.is_host_only_setting(key)
            ]
            await self.ctx.card(
                "⚙ Настройки " + getattr(self, "PRODUCT_LABEL", ""),
                rows
                + [
                    [("📋 Операции", "orders"), ("💰 Учёт USDT", "topup")],
                    [("Назад", "menu")],
                ],
            )
        elif action.startswith("setting:"):
            key = action.split(":", 1)[1]
            if key not in self.SETTINGS_SCHEMA:
                return
            descriptor = self.SETTINGS_SCHEMA[key]
            section, fields = self.setting_fields(key)
            value = {f: self.config[section].get(f, "") for f in fields}
            secret = descriptor.get("secret", False)
            current = "••••••••" if secret else ("; ".join(value.values()))
            if descriptor.get("kind") == "bool":
                await self.ctx.card(
                    descriptor["title"]
                    + "\n"
                    + descriptor["description"]
                    + "\nСейчас: "
                    + ("включено" if self.flag(fields[0]) else "выключено"),
                    [
                        [("Переключить", "setting_toggle:" + key)],
                        [("Назад", "settings")],
                    ],
                )
            else:
                self.inputs[user] = ("setting:" + key, time.time() + 300)
                hint = (
                    "Введите JSON с полями " + ", ".join(fields)
                    if len(fields) > 1
                    else "Введите новое значение"
                )
                await self.ctx.card(
                    descriptor["title"]
                    + "\n"
                    + descriptor["description"]
                    + "\nСейчас: "
                    + current
                    + "\n"
                    + hint,
                    [[("Отмена", "settings")]],
                )
        elif action.startswith("setting_toggle:"):
            key = action.split(":", 1)[1]
            if (
                key not in self.SETTINGS_SCHEMA
                or self.SETTINGS_SCHEMA[key].get("kind") != "bool"
            ):
                return
            section, fields = self.setting_fields(key)
            field = fields[0]
            enabled = not self.flag(field)
            if field == "enabled" and enabled:
                self.validate_credentials()
            self.config[section][field] = str(enabled).lower()
            self.save_config()
            await self.callback("setting:" + key, user)
        elif action.startswith("direct_confirm:"):
            token = action[15:]
            plan = self.state.get("direct_plans", {}).get(token)
            if plan and plan["user"] == user and plan["expires"] > time.time():
                self.state["direct_plans"].pop(token)
                key = "D" + token
                self.state["orders"][key] = {
                    "order_id": key,
                    "chat": None,
                    "quantity": plan["quantity"],
                    "recipient": plan["recipient"],
                    "packs": 1,
                    "phase": "ready",
                    "at": time.time(),
                    "direct": True,
                    "operator": user,
                    "event": {},
                    "quoted_spent": plan["quoted_spent"],
                }
                self.save()
                await self.menu()

    async def message(self, text, user):
        pending = self.inputs.pop(user, None)
        if not pending or pending[1] < time.time():
            queue = self.state.get("pending_topups", [])
            if queue:
                pending = ("topup_cost:" + queue[0]["id"], time.time() + 300)
            else:
                return
        action = pending[0]
        try:
            if action.startswith("topup_cost:"):
                queue = self.state.get("pending_topups", [])
                if not queue or queue[0]["id"] != action.split(":", 1)[1]:
                    return
                cost = money(text.replace(",", "."))
                if cost <= 0:
                    raise ValueError()
                topup = queue.pop(0)
                units = Decimal(topup["units"])
                inv = self.state["inventory"]
                inv["units"] = str(Decimal(inv["units"]) + units)
                inv["cost"] = str(money(inv["cost"]) + cost)
                self.state["topups"][topup["id"]] = {
                    **topup,
                    "cost": str(cost),
                    "at": time.time(),
                    "operator": user,
                }
                self.config["Plugin"]["purchase_rate_rub"] = str(
                    money(inv["cost"]) / Decimal(inv["units"])
                )
                self.save_config()
                self.save()
                await self.refresh_stock()
                await self.ctx.say(
                    f"Пополнение учтено: {units:.3f} USDT за {cost:.3f} ₽."
                )
            elif action == "topup":
                units_raw, cost_raw = text.split()
                units = Decimal(units_raw.replace(",", "."))
                cost = money(cost_raw.replace(",", "."))
                if not units.is_finite() or units <= 0 or cost <= 0:
                    raise ValueError()
                inv = self.state["inventory"]
                inv["units"] = str(Decimal(inv["units"]) + units)
                inv["cost"] = str(money(inv["cost"]) + cost)
                avg = money(inv["cost"]) / Decimal(inv["units"])
                self.config["Plugin"]["purchase_rate_rub"] = str(avg)
                self.save_config()
                self.save()
            elif action == "direct_recipient":
                recipient = self.normalize_recipient(text.strip())
                if not recipient:
                    raise ValueError()
                self.inputs[user] = ("direct_quantity:" + recipient, time.time() + 300)
                available = getattr(self, "DIRECT_QUANTITIES", "по правилам товара")
                await self.ctx.card(
                    "Получатель: "
                    + recipient
                    + "\nВведите количество. Доступно: "
                    + available,
                    [[("Отмена", "menu")]],
                )
                return
            elif action.startswith("direct_quantity:"):
                recipient = action.split(":", 1)[1]
                quantity = int(text)
                if not self.valid_quantity(quantity):
                    raise ValueError()
                self.validate_credentials()
                quote = {
                    "order_id": "QUOTE" + secrets.token_hex(6),
                    "recipient": recipient,
                    "quantity": quantity,
                    "packs": 1,
                    "direct": True,
                    "event": {},
                }
                await self.provider_prepare(quote)
                spent = Decimal(str(quote["spent_usdt"]))
                balance = Decimal(str(await self.provider_balance()))
                if not spent.is_finite() or spent <= 0 or balance < spent:
                    raise ValueError("Недостаточно средств у поставщика")
                units, _ = self.available_inventory()
                if units < spent:
                    raise ValueError("Сначала учтите закупку USDT")
                token = secrets.token_hex(6)
                self.state.setdefault("direct_plans", {})[token] = {
                    "user": user,
                    "expires": time.time() + 120,
                    "recipient": recipient,
                    "quantity": quantity,
                    "quoted_spent": str(spent),
                }
                self.save()
                await self.ctx.card(
                    f"Подтвердите прямую закупку\nПолучатель: {recipient}\nКоличество: {quantity} "
                    + getattr(self, "PRODUCT_LABEL", "")
                    + f"\nПредварительное списание: {spent} USDT. Изменение цены потребует нового подтверждения.\nПосле выполнения укажите сумму продажи или выберите «Покупка себе».",
                    [[("Купить", "direct_confirm:" + token)], [("Отмена", "menu")]],
                )
                return
            elif action.startswith("direct_sale:"):
                await self.account_direct(
                    action.split(":", 1)[1], user, money(text.replace(",", ".")), False
                )
                return
            elif action.startswith("setting:"):
                self.apply_setting(action.split(":", 1)[1], text)
                await self.callback("settings", user)
                return
        except (ValueError, TypeError, ArithmeticError):
            self.inputs[user] = (action, time.time() + 300)
            await self.ctx.say(
                "Неверный формат значения. Попробуйте ещё раз или нажмите «Отмена»."
            )
            return
        await self.menu()

    def available_inventory(self):
        units = Decimal(self.state["inventory"]["units"])
        cost = money(self.state["inventory"]["cost"])
        for o in self.state["orders"].values():
            if o.get("payment_intent") and not o.get("accounted"):
                units -= Decimal(o.get("spent_usdt", "0"))
                cost -= money(o.get("reserved_cost", "0"))
        return units, cost

    def current_order(self, chat):
        candidates = []
        for p in self.app.plugins.values():
            if plugin_capability(p, "commerce"):
                candidates.extend(
                    (o["at"], p, o)
                    for o in p.state["orders"].values()
                    if o.get("chat") == chat
                    and o["phase"] not in ("done", "refunded", "failed", "cancelled")
                )
        return min(candidates, key=lambda t: t[0])[1:] if candidates else (None, None)

    async def buyer_command(self, e):
        return False

    def recipient_candidates(self, event):
        return ()

    def operation_reference(self, order):
        return order.get("reference", order["order_id"])

    async def send_product_catalog(self, e):
        product = getattr(self, "PRODUCT_LABEL", "товара")
        try:
            offers = await self.app.fp.lots()
            nominals = {}
            for offer in offers:
                if str(offer["category"]) == self.get("category_id"):
                    quantity = self.parse_quantity(offer.get("description", ""))
                    if quantity:
                        nominals.setdefault(quantity, offer["id"])
            text = (
                "📋 Доступные номиналы "
                + product
                + ":\n"
                + "\n".join(
                    f"🔹 {quantity} {product} — https://funpay.com/lots/offer?id={nominals[quantity]}"
                    for quantity in sorted(nominals)
                )
                if nominals
                else "❌ Не найдено активных лотов " + product + "."
            )
        except Exception:
            self.log.warning("Provider %s buyer lot list unavailable", self.id)
            text = (
                "ℹ️ Актуальные предложения "
                + product
                + " доступны в профиле продавца на FunPay."
            )
        await self.ctx.send_buyer(e, text, "lot_list:" + e["id"])
        return True

    async def event(self, e):
        if e["kind"] == "order":
            key = e["order_id"]
            o = self.state["orders"].get(key)
            if o and e["status"] == "REFUNDED":
                o["customer_refunded"] = True
                if not o.get("payment_intent"):
                    o["phase"] = "refunded"
                self.save()
                return
            if key in self.state.get("blocked_order_ids", []):
                return
            if (
                not o
                and e["status"] == "PAID"
                and self.flag("enabled")
                and (
                    not self.flag("require_category_match")
                    or str(e.get("category")) == self.get("category_id")
                )
            ):
                quantity = self.parse_quantity(e.get("description", ""))
                if not quantity:
                    return
                packs = int(e.get("amount") or 1)
                if packs < 1:
                    return
                if not self.app.claim_order(self.id, e):
                    return
                self.state["orders"][key] = {
                    "order_id": key,
                    "chat": e["chat"],
                    "quantity": quantity,
                    "packs": packs,
                    "phase": "recipient",
                    "at": time.time(),
                    "event": copy.deepcopy(e),
                }
                self.app.state.d["ledger"][key]["source"] = self.id
                self.save()
                for candidate in self.recipient_candidates(e):
                    recipient = self.normalize_recipient(candidate)
                    if recipient:
                        self.state["orders"][key].update(
                            recipient=recipient, phase="confirm"
                        )
                        self.save()
                        await self.ctx.send_buyer(
                            e,
                            f"Получатель из заказа: {recipient}. Подтвердите знаком + или пришлите другого.",
                            key + ":prefilled",
                        )
                        break
        elif e["kind"] == "message" and not any(
            e.get(k) for k in ("seller", "system", "by_bot")
        ):
            if await self.buyer_command(e):
                return
            plugin, o = self.current_order(e["chat"])
            if plugin is not self or not o or not self.flag("enabled"):
                return
            raw = e.get("text", "").strip()
            if o["phase"] == "recipient":
                recipient = self.normalize_recipient(raw)
                if not recipient:
                    await self.ctx.send_buyer(
                        e,
                        "Не удалось распознать получателя. Пришлите "
                        + self.RECIPIENT_PROMPT
                        + ".",
                        o["order_id"] + ":invalid:" + e["id"],
                    )
                    return
                o.update(recipient=recipient, phase="confirm")
                self.save()
                await self.ctx.send_buyer(
                    e,
                    f"Получатель: {recipient}. Количество: {o['quantity']} × {o['packs']}. Отправьте + для подтверждения или другого получателя.",
                    o["order_id"] + ":confirm:" + e["id"],
                )
            elif o["phase"] == "confirm":
                if raw.lower() in ("да", "д", "yes", "y", "+", "верно", "подтверждаю"):
                    o["phase"] = "ready"
                    self.save()
                else:
                    recipient = self.normalize_recipient(raw)
                    if recipient:
                        o["recipient"] = recipient
                        self.save()
                        await self.ctx.send_buyer(
                            e,
                            f"Получатель изменён: {recipient}. Подтвердите знаком +.",
                            o["order_id"] + ":confirm:" + e["id"],
                        )

    async def tick(self):
        now = time.time()
        if now - self.last_check < 5:
            return
        self.last_check = now
        if hasattr(self, "runtime"):
            self.runtime.health["monitor"] = {"running": True, "error": None, "at": now}
        for key, o in list(self.state["orders"].items()):
            if (
                o["phase"] == "recipient"
                and self.flag("enabled")
                and not o.get("prompted")
            ):
                plugin, current = self.current_order(o["chat"])
                if plugin is self and current is o:
                    o["prompted"] = True
                    self.save()
                    await self.ctx.send_buyer(
                        o["event"],
                        "Для заказа #"
                        + key
                        + " пришлите "
                        + self.RECIPIENT_PROMPT
                        + ". После этого потребуется подтверждение знаком +.",
                        key + ":request",
                    )
            if o["phase"] == "retry_wait" and now >= o.get("retry_due", 0):
                o["phase"] = "ready"
                self.save()
            if (
                o["phase"] == "ready"
                and (o.get("direct") or self.flag("enabled"))
                and self.app.cfg.flag("SellerStatus", "online")
            ):
                if not self.tasks.get("purchase") or self.tasks["purchase"].done():
                    self.tasks["purchase"] = (
                        self.runtime.start_worker(
                            "purchase", lambda o=o: self.purchase(o)
                        )
                        if hasattr(self, "runtime")
                        else asyncio.create_task(self.purchase(o))
                    )
            if o["phase"] == "verifying" and now - o.get("last_status", 0) >= float(
                self.get("status_check_interval", "15")
            ):
                o["last_status"] = now
                self.save()
                try:
                    status = await self.provider_status(o)
                    if status == "done":
                        await self.finalize(o)
                    elif status == "paid":
                        o["phase"] = "delivery_check"
                        self.save()
                        notice = "Оплата подтверждена. Проверьте выдачу у поставщика в меню плагина. Повторный платёж заблокирован."
                        if key in self.app.state.d.get("order_cards", {}):
                            self.app.order_log(key, notice, "delivery-check")
                            self.save()
                        else:
                            await self.ctx.say("#" + key + ": " + notice)
                    elif status == "failed":
                        self.state["sales_suspended"] = True
                        await self.manage_lots("deactivate")
                        o.update(
                            phase="manual",
                            error="Поставщик сообщил об ошибке после оплаты. Нужна сверка.",
                        )
                        self.save()
                        if key not in self.app.state.d.get("order_cards", {}):
                            await self.ctx.card(
                                "#" + key + ": нужна ручная проверка поставщика.",
                                [
                                    [("Проверить заказ", "inspect:" + key)],
                                    [("Назад", "menu")],
                                ],
                            )
                except Exception:
                    self.log.exception(
                        "Ошибка проверки состояния оплаты/выдачи: заказ=%s", key
                    )
        if self.flag("enabled"):
            try:
                self.validate_credentials()
            except (ValueError, TypeError) as error:
                message = str(error)
                if getattr(self, "credential_error", None) != message:
                    self.log.warning("Плагин ожидает настройки: %s", message)
                self.credential_error = message
                return
            if getattr(self, "credential_error", None):
                self.log.info("Настройки поставщика прошли проверку")
            self.credential_error = None
        if self.flag("enabled") and now - self.last_balance >= 60:
            self.last_balance = now
            try:
                self.validate_credentials()
                before = len(self.state.get("pending_topups", []))
                self.observe_balance(await self.provider_balance())
                if len(self.state.get("pending_topups", [])) > before:
                    await self.prompt_topup()
            except Exception as error:
                self.log.exception("Не удалось получить баланс поставщика")
        if (
            self.flag("enabled")
            and self.app.cfg.flag("SellerStatus", "online")
            and (self.state.get("floor_dirty") or now - self.last_lots >= 1500)
        ):
            self.last_lots = now
            if self.state.get("floor_dirty"):
                result = await self.manage_lots("floor")
                if not result.get("errors"):
                    self.state.pop("floor_dirty", None)
                    self.save()
            if self.flag("autodump"):
                await self.manage_lots("price_lots")
            if self.flag("auto_stock"):
                await self.manage_lots("stock")

    def extra_operation_cost(self):
        return Decimal("0")

    async def purchase(self, o):
        async with self.purchase_lock:
            if (
                o.get("refund_hold")
                or o.get("payment_intent")
                or o.get("accounted")
                or o.get("phase") in ("done", "refunded", "cancelled")
            ):
                self.log.warning(
                    "Повторный запуск закупки заблокирован: заказ=%s", o["order_id"]
                )
                return
            key = o["order_id"]
            self.log.info(
                "Начало закупки: заказ=%s, количество=%s, пакетов=%s, прямая=%s",
                key,
                o["quantity"],
                o.get("packs", 1),
                bool(o.get("direct")),
            )
            try:
                self.validate_credentials()
                if not o.get("direct"):
                    status = await self.app.fp.verify_order(key)
                    if (
                        status["status"] != "PAID"
                        or status["chat"] != o["chat"]
                        or status["currency"] != "RUB"
                    ):
                        o["phase"] = "cancelled"
                        self.save()
                        return
                o["phase"] = "preparing"
                self.save()
                await self.provider_prepare(o)
                self.log.info(
                    "Расчёт поставщика: заказ=%s, стоимость=%s USDT, reference=%s",
                    key,
                    o.get("spent_usdt"),
                    self.operation_reference(o),
                )
                spent = Decimal(str(o["spent_usdt"]))
                if o.get("direct") and spent != Decimal(o["quoted_spent"]):
                    raise ValueError(
                        "Стоимость изменилась. Создайте новую прямую закупку и подтвердите сумму"
                    )
                if Decimal(str(await self.provider_balance())) < spent:
                    raise ValueError("Недостаточно средств у поставщика")
                inv = self.state["inventory"]
                units, available_cost = self.available_inventory()
                if spent <= 0 or units < spent:
                    raise ValueError(
                        "Не хватает учтённого запаса USDT; внесите закупку"
                    )
                rate = available_cost / units
                cost = money(spent * rate) + money(o.get("extra_cost_rub", 0))
                credit = money(o["event"].get("price", 0))
                paid = Decimal(self.paid_units(o["quantity"])) * int(o.get("packs", 1))
                if paid > 0:
                    self.config["Plugin"]["unit_cost_usdt"] = str(spent / paid)
                    self.save_config()
                target = (
                    Decimal(self.get("target_profit_per_unit", "0"))
                    * Decimal(o["quantity"])
                    * int(o.get("packs", 1))
                )
                if (
                    not o.get("direct")
                    and credit * self.effective_payout_rate() - cost < target
                ):
                    self.last_lots = 0
                    self.state["floor_dirty"] = True
                    self.save()
                    await self.manage_lots("floor")
                    raise ValueError(
                        "Закупка не обеспечивает целевую прибыль; защитные цены пересчитаны"
                    )
                o.update(
                    cost=str(cost),
                    reserved_cost=str(money(spent * rate)),
                    purchase_rate=str(rate),
                    phase="paying",
                    payment_intent=True,
                )
                self.save()
                await self.provider_pay(o)
                o["phase"] = "verifying"
                self.save()
            except asyncio.CancelledError:
                if o.get("payment_intent"):
                    o["phase"] = "verifying" if o.get("reference") else "manual"
                else:
                    o["phase"] = "manual"
                self.save()
                raise
            except Exception as e:
                o["phase"] = (
                    "verifying"
                    if o.get("payment_intent") and o.get("reference")
                    else "manual"
                    if o.get("payment_intent")
                    else "failed"
                )
                if not o.get("payment_intent") and RetryPolicy.safe_error(e):
                    attempt = int(o.get("retry_attempt", 0))
                    delay = RetryPolicy.delay(attempt)
                    if delay is not None:
                        o.update(
                            phase="retry_wait",
                            retry_attempt=attempt + 1,
                            retry_due=time.time() + delay,
                        )
                if o["phase"] in ("failed", "manual"):
                    self.state["sales_suspended"] = True
                    try:
                        await self.manage_lots("deactivate")
                    except Exception:
                        self.log.exception(
                            "Не удалось аварийно отключить управляемые лоты"
                        )
                o["error"] = str(e) if isinstance(e, ValueError) else type(e).__name__
                self.save()
                self.log.exception(
                    "Ошибка закупки: заказ=%s, фаза=%s, намерение оплаты=%s",
                    key,
                    o["phase"],
                    bool(o.get("payment_intent")),
                )
                if key in self.app.state.d.get("order_cards", {}):
                    self.app.order_log(
                        key,
                        "⚠ Ошибка закупки. Подробности — в журнале и проверке заказа в меню плагина.",
                    )
                    self.save()
                else:
                    await self.ctx.card(
                        f"⚠ Заказ #{key}: {o['phase']}. {o['error']}.",
                        [[("Проверить заказ", "inspect:" + key)], [("Назад", "menu")]],
                    )
                if o["phase"] in ("failed", "manual") and not o.get("direct"):
                    await self.ctx.send_buyer(
                        o["event"],
                        "При выполнении заказа возникла техническая проблема. Продавец уведомлён и проверит заказ.",
                        key + ":technical_failure",
                    )
                if (
                    o["phase"] == "failed"
                    and not o.get("payment_intent")
                    and self.flag("auto_refund")
                    and not o.get("direct")
                ):
                    verified = await self.app.fp.verify_order(key)
                    if verified["status"] == "PAID" and await self.app.once(
                        "refund:" + key, lambda: self.app.fp.refund_order(key)
                    ):
                        o["phase"] = "refunded"
                        self.save()

    async def finalize(self, o):
        if not o.get("accounted"):
            with self.app.state.transaction():
                inv = self.state["inventory"]
                spent = Decimal(o["spent_usdt"])
                cost = money(o["cost"])
                if self.state.get("balance_observed") is not None:
                    self.state["balance_observed"] = str(
                        max(
                            Decimal("0"),
                            Decimal(self.state["balance_observed"]) - spent,
                        )
                    )
                inv["units"] = str(Decimal(inv["units"]) - spent)
                inv["cost"] = str(
                    money(inv["cost"]) - money(spent * Decimal(o["purchase_rate"]))
                )
                if not o.get("direct"):
                    self.app.state.d["ledger"][o["order_id"]]["fee_rate"] = str(
                        self.app.estimated_withdrawal_rate()
                    )
                    FinancialLedger(self.app.state).delivery(
                        self.id,
                        o["order_id"],
                        cost,
                        spent,
                        o["purchase_rate"],
                        save=False,
                    )
                paid_quantity = Decimal(self.paid_units(o["quantity"])) * int(
                    o.get("packs", 1)
                )
                if paid_quantity > 0:
                    samples = self.state.setdefault(
                        "price_samples", {"spent": "0", "paid": "0", "count": 0}
                    )
                    samples.update(
                        spent=str(Decimal(samples["spent"]) + spent),
                        paid=str(Decimal(samples["paid"]) + paid_quantity),
                        count=samples["count"] + 1,
                    )
                    if samples["count"] >= self.PRICE_SAMPLE_MIN:
                        self.config["Plugin"]["unit_cost_usdt"] = str(
                            spent / paid_quantity
                        )
                        self.save_config()
                        self.state["floor_dirty"] = True
                        self.last_lots = 0
                        self.state["price_samples"] = {
                            "spent": "0",
                            "paid": "0",
                            "count": 0,
                        }
                if not o.get("direct"):
                    self.app.state.d["ledger"][o["order_id"]]["delivery_units"] = int(
                        o["quantity"]
                    ) * int(o.get("packs", 1))
                o["accounted"] = True
                self.save()
            await self.refresh_stock()
        if o.get("direct"):
            if o.get("sale_accounted"):
                return
            o["phase"] = "awaiting_sale_amount"
            self.save()
            await self.prompt_direct_sale(o)
            return
        if not o.get("customer_refunded"):
            await self.ctx.fulfilled(
                o["event"],
                details={
                    "recipient": o.get("recipient", ""),
                    "quantity": int(o["quantity"]) * int(o.get("packs", 1)),
                    "product": getattr(self, "PRODUCT_LABEL", ""),
                    "reference": self.operation_reference(o),
                    "receipt_url": self.receipt_url(o) or "",
                },
            )
        o["phase"] = "done"
        self.save()
        quantity = o["quantity"] * o.get("packs", 1)
        credit = money(o["event"].get("price", 0))
        profit = money(
            credit
            * (
                1
                - Decimal(
                    self.app.state.d["ledger"][o["order_id"]].get("fee_rate", "0.03")
                )
            )
        ) - money(o["cost"])
        owner = (
            f"✅ Доставка подтверждена\nПолучатель: {o['recipient']}\n"
            + getattr(self, "PRODUCT_LABEL", "Товар")
            + f": {quantity}\nЗаказ: {credit:.2f} ₽\nПрибыль: {profit:.2f} ₽\nСебестоимость: {o['cost']} ₽\n"
        )
        receipt = self.receipt_url(o)
        if receipt:
            owner += "Чек: " + receipt + "\n"
        if o.get("customer_refunded"):
            owner += "⚠ Покупателю уже возвращена оплата; расход сохранён в финансах."
        if not o.get("owner_notified"):
            if o["order_id"] in self.app.state.d.get("order_cards", {}):
                self.app.order_log(o["order_id"], owner, "delivery-details")
            else:
                await self.ctx.say(owner)
            o["owner_notified"] = True
            self.save()
        owner_key = "delivery-owner:" + self.id + ":" + o["order_id"]
        self.app.state.d.setdefault("notification_outbox", {}).setdefault(
            owner_key,
            {
                "kind": "main",
                "notification_kind": "delivery",
                "text": f"✅ {self.TITLE}: заказ #{o['order_id']} выполнен. Подтверждение: {self.operation_reference(o)}",
                "status": "retry",
                "due": time.time(),
                "attempts": 0,
            },
        )
        self.save()
        self.log.info(
            "Плагин %s: заказ #%s выполнен · количество %s · расходы %s ₽ · прибыль %s ₽",
            self.id,
            o["order_id"],
            quantity,
            o["cost"],
            profit,
        )

    def receipt_url(self, o):
        return None

    def buyer_receipt(self, o):
        return self.app.completion_message(
            o["event"],
            {
                "recipient": o.get("recipient", ""),
                "reference": self.operation_reference(o),
                "receipt_url": self.receipt_url(o) or "",
            },
        )

    def observe_balance(self, value):
        value = Decimal(str(value))
        if not value.is_finite() or value < 0:
            raise ValueError("Некорректный баланс поставщика")
        # A not-yet-final spend would make the observed delta ambiguous.
        if any(
            o.get("payment_intent") and not o.get("accounted")
            for o in self.state["orders"].values()
        ):
            return
        previous = self.state.get("balance_observed")
        queued = sum(
            (Decimal(t["units"]) for t in self.state.get("pending_topups", [])),
            Decimal("0"),
        )
        baseline = (
            Decimal(previous)
            if previous is not None
            else Decimal(self.state["inventory"]["units"]) + queued
        )
        delta = value - baseline
        if delta > Decimal("0.000001"):
            self.state.setdefault("pending_topups", []).append(
                {"id": secrets.token_hex(6), "units": str(delta), "at": time.time()}
            )
        self.state["balance_observed"] = str(value)
        self.save()

    async def prompt_topup(self):
        queue = self.state.get("pending_topups", [])
        if not queue:
            return
        topup = queue[0]
        await self.ctx.card(
            f"Обнаружено пополнение {Decimal(topup['units']):.6f} USDT.\nВведите затраты на это пополнение в рублях:",
            [[("Указать стоимость", "topup_cost:" + topup["id"])], [("Назад", "menu")]],
        )

    async def prompt_direct_sale(self, o):
        text = (
            f"✅ Прямая закупка выполнена\n\nТовар: {o['quantity']} "
            + getattr(self, "PRODUCT_LABEL", "")
            + f"\nСписано: {o['spent_usdt']} USDT\nПолучатель: {o['recipient']}\n\nЗа какую сумму была совершена прямая продажа?\nВведите сумму в рублях. Комиссии к ней применяться не будут."
        )
        user = o.get("operator")
        if user is not None:
            self.inputs[user] = ("direct_sale:" + o["order_id"], time.time() + 300)
        rows = [
            [("Указать сумму продажи", "direct_sale:" + o["order_id"])],
            [("Покупка себе", "direct_personal:" + o["order_id"])],
        ]
        url = self.receipt_url(o)
        if url:
            rows.append([{"text": "Открыть транзакцию", "url": url}])
        await self.ctx.card(text, rows)

    async def account_direct(self, key, user, revenue, personal):
        o = self.state["orders"].get(key)
        if not o or o.get("operator") != user or o["phase"] != "awaiting_sale_amount":
            return
        revenue = Decimal("0") if personal else money(revenue)
        if revenue < 0:
            raise ValueError("Сумма продажи не может быть отрицательной")
        row = self.app.state.d["ledger"].setdefault(
            key,
            {
                "kind": "direct",
                "source": self.id,
                "credit": str(revenue),
                "cost": o["cost"],
                "spent_usdt": o["spent_usdt"],
                "purchase_rate": o["purchase_rate"],
                "personal": bool(personal),
                "status": "CLOSED",
                "currency": "RUB",
                "delivery_accounted": True,
                "at": o["at"],
                "delivered_at": time.time(),
                "withdrawn": "0",
                "actual_fee": "0",
                "fee_correction": "0",
            },
        )
        row["delivery_units"] = int(o["quantity"]) * int(o.get("packs", 1))
        o.update(
            sale_accounted=True,
            phase="done",
            sale_amount=row["credit"],
            personal=row["personal"],
        )
        self.save()
        self.inputs.pop(user, None)
        kind = "Покупка себе" if row["personal"] else "Прямая продажа"
        await self.ctx.card(
            f"✅ {kind} учтена\n\nТовар: {o['quantity']} "
            + getattr(self, "PRODUCT_LABEL", "")
            + f"\nСебестоимость: {row['cost']} ₽\nСумма продажи: {row['credit']} ₽\nПрибыль: {money(row['credit']) - money(row['cost']):.2f} ₽\nПолучатель: {o['recipient']}",
            [[("Назад", "menu")]],
        )

    async def refresh_stock(self):
        if (
            not self.flag("auto_stock")
            or not self.app.cfg.flag("SellerStatus", "online")
            or not json.loads(self.get("managed_lots", "{}"))
        ):
            return
        try:
            await self.manage_lots("stock")
        except Exception as error:
            self.log.warning(
                "Provider %s stock refresh error=%s", self.id, type(error).__name__
            )

    async def manage_lots(self, action):
        profile = self.lot_profile()
        if not isinstance(profile, LotProfile):
            raise TypeError("Ожидается LotProfile от плагина")
        rate = Decimal(self.get("purchase_rate_rub", "0"))
        unit = Decimal(self.get("unit_cost_usdt", "0"))
        profit = Decimal(self.get("target_profit_per_unit", "0"))
        if action in ("price_lots", "floor", "stock") and (rate <= 0 or unit <= 0):
            raise ValueError("Сначала учтите стоимость пополнения")
        if action in ("price_lots", "floor"):

            def floor_for(spec):
                value = (
                    Decimal(self.paid_units(spec.quantity)) * unit * rate
                    + self.extra_operation_cost()
                    + Decimal(spec.quantity) * profit
                ) / self.effective_payout_rate()
                return value

            result = await self.app.lot_dumper.reprice(
                profile, floor_for, floor_only=action == "floor"
            )
        else:
            result = {"checked": 0, "changed": 0, "errors": 0}
            balance = (
                min(
                    Decimal(str(await self.provider_balance())),
                    self.available_inventory()[0],
                )
                if action == "stock"
                else None
            )
            for spec in profile.entries():
                try:
                    key = spec.lot_id
                    lock_key = (
                        f"chips:{spec.category_id}" if spec.kind == "chips" else key
                    )
                    async with self.app.lot_locks.setdefault(lock_key, asyncio.Lock()):
                        fields = (
                            await self.app.fp.chip_floor(spec.category_id, key)
                            if spec.kind == "chips"
                            else await self.app.fp.lot_floor(int(key))
                        )
                        result["checked"] += 1
                        if str(fields["category"]) != str(spec.category_id):
                            raise ValueError(
                                "Управляемый лот относится к другой категории"
                            )
                        if action == "activate":
                            self.state["sales_suspended"] = False
                            active = True
                        elif action == "deactivate":
                            active = False
                        elif action == "stock":
                            cost = Decimal(self.paid_units(spec.quantity)) * unit
                            stock = int(balance // cost) if cost > 0 else 0
                            active = (
                                bool(fields["active"])
                                and stock > 0
                                and not self.state.get("sales_suspended")
                            )
                            if spec.kind == "chips":
                                await self.app.fp.chip_update(
                                    spec.category_id, key, active=active, amount=stock
                                )
                            else:
                                await self.app.fp.lot_update(
                                    int(key),
                                    active=active,
                                    amount=stock,
                                    deactivate_after_sale=(stock <= 1),
                                )
                            result["changed"] += 1
                            continue
                        else:
                            raise ValueError("Неизвестное действие менеджера лотов")
                        if bool(fields["active"]) != active:
                            if spec.kind == "chips":
                                await self.app.fp.chip_update(
                                    spec.category_id, key, active=active
                                )
                            else:
                                await self.app.fp.lot_update(int(key), active=active)
                            result["changed"] += 1
                except Exception:
                    result["errors"] += 1
                    self.log.exception("Ошибка обработки лота %s", spec.lot_id)
        self.save()
        if result["changed"] or result["errors"]:
            await self.app.notify(
                "lot_manager",
                f"⚙️ {self.TITLE}: {action}. Проверено {result['checked']}, изменено {result['changed']}, ошибок {result['errors']}.",
            )
        return result


class CommercePlugin(FrameworkPlugin, CommerceRuntime):
    """Объединяет поставщика и товар; неторговые расширения используют FrameworkPlugin."""

    PROVIDER = None
    PRODUCT = None
    CATEGORY_ID = 0
    COMMON_SETTINGS_SCHEMA = {
        "STRICT_CATEGORY": {
            "title": "Строгая категория заказа",
            "description": "Принимать заказы только из заданной категории.",
            "fields": ["require_category_match"],
            "kind": "bool",
        },
        "AUTO_REFUND": {
            "title": "Автоматический возврат",
            "description": "Возвращать оплаченный заказ, если безопасно выполнить его не удалось.",
            "fields": ["auto_refund"],
            "kind": "bool",
        },
        "TARGET_PROFIT_PER_UNIT": {
            "title": "Чистый профит за единицу",
            "description": "Желаемый чистый профит в рублях за единицу товара.",
            "fields": ["target_profit_per_unit"],
            "kind": "number",
            "minimum": 0,
        },
        "CATEGORY_ID": {
            "title": "Категория FunPay",
            "description": "ID категории управляемых лотов.",
            "fields": ["category_id"],
            "kind": "integer",
            "minimum": 1,
        },
        "SYSTEM_ENABLED": {
            "title": "Автоматическая выдача",
            "description": "Автоматически выполнять новые заказы.",
            "fields": ["enabled"],
            "kind": "bool",
        },
        "FUNPAY_PAYOUT_RATE": {
            "title": "Коэффициент выплаты",
            "description": "Доля после дополнительной комиссии вывода FunPay.",
            "fields": ["payout_rate"],
            "kind": "number",
            "minimum": 1e-06,
            "maximum": 1,
        },
        "AUTODUMP_ENABLED": {
            "title": "Плавающий демпер",
            "description": "Изменять цены с учётом прибыльного минимума.",
            "fields": ["autodump"],
            "kind": "bool",
        },
    }

    def __init__(self, ctx):
        if not isinstance(self.PROVIDER, type) or not issubclass(
            self.PROVIDER, ProviderAdapter
        ):
            raise TypeError("PROVIDER должен быть классом ProviderAdapter")
        if not isinstance(self.PRODUCT, type) or not issubclass(
            self.PRODUCT, ProductAdapter
        ):
            raise TypeError("PRODUCT должен быть классом ProductAdapter")
        self.SETTINGS_SCHEMA = copy.deepcopy(
            {**self.COMMON_SETTINGS_SCHEMA, **self.SETTINGS_SCHEMA}
        )
        CommerceRuntime.__init__(self, ctx)
        FrameworkPlugin.__init__(self, ctx)
        self.provider = self.runtime.register("provider", self.PROVIDER(self))
        self.product = self.runtime.register("product", self.PRODUCT(self))
        self.runtime.provide("commerce", self)
        self.runtime.provide(
            "statistics", lambda: CommerceRuntime.statistics_snapshot(self)
        )
        self.runtime.provide("lot_profile", self.lot_profile)
        self.runtime.on("start", lambda: CommerceRuntime.start(self))
        self.runtime.on("stop", lambda: CommerceRuntime.stop(self))
        self.runtime.on("event", lambda event: CommerceRuntime.event(self, event))
        self.runtime.on("tick", lambda: CommerceRuntime.tick(self))
        self.runtime.on(
            "message", lambda text, user: CommerceRuntime.message(self, text, user)
        )
        self.runtime.router.on_prefix(
            "", lambda action, user: CommerceRuntime.callback(self, action, user)
        )
        for key, descriptor in self.SETTINGS_SCHEMA.items():
            declared_validator = descriptor.get("validator")

            def validate(values, key=key, declared_validator=declared_validator):
                if declared_validator:
                    declared_validator(values)
                self.provider.validate_setting(key, values)

            descriptor["validator"] = validate
        if self.get("category_id") == "0" and self.CATEGORY_ID:
            self.config["Plugin"]["category_id"] = str(self.CATEGORY_ID)
            self.save_config()
        self.configure_runtime()

    def configure_runtime(self):
        """Подключает дополнительные компоненты, маршруты и фоновые задачи плагина."""

    def validate_credentials(self):
        return self.provider.validate_credentials()

    def normalize_recipient(self, text):
        return self.product.normalize_recipient(text)

    def valid_quantity(self, quantity):
        return self.product.valid_quantity(quantity)

    def paid_units(self, quantity):
        return self.product.paid_units(quantity)

    def parse_quantity(self, text):
        return self.product.parse_quantity(text)

    def recipient_candidates(self, event):
        return self.product.recipient_candidates(event)

    async def buyer_command(self, event):
        return await self.product.buyer_command(event)

    def buyer_receipt(self, order):
        return self.app.completion_message(
            order["event"],
            {
                "recipient": order.get("recipient", ""),
                "reference": self.operation_reference(order),
                "receipt_url": self.receipt_url(order) or "",
            },
        )

    async def provider_balance(self):
        return await self.provider.provider_balance()

    async def provider_prepare(self, order):
        return await self.provider.provider_prepare(order)

    async def provider_pay(self, order):
        return await self.provider.provider_pay(order)

    async def provider_status(self, order):
        return await self.provider.provider_status(order)

    def receipt_url(self, order):
        return self.provider.receipt_url(order)

    def operation_reference(self, order):
        return self.provider.operation_reference(order)

    def extra_operation_cost(self):
        return self.provider.extra_operation_cost()

    def lot_profile(self):
        return LotProfile(
            owner=self.id,
            category_id=int(self.get("category_id")),
            kind=self.get("lot_kind", "common"),
            lots=json.loads(self.get("managed_lots", "{}")),
            parse_quantity=self.parse_quantity,
        )
