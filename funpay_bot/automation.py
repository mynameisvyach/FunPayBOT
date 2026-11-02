"""Службы автоматизации."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .finance import FinancialLedger, RMTHubStatistics, WithdrawalCosts, money
from .logging_setup import plugin_logger
from .metadata import LOG, human_time


class TicketClassifier:
    # Deliberately lexical: no external service and no claim of semantic understanding.
    patterns = (
        r"не\s+(?:работ\w*|могу\w*\s+(?:зайти|войти|вход|авториз\w*)|(?:за|во)ход\w*|(?:за|во)йти|приш\w*|приход\w*|дош\w*|получ\w*|начисл\w*|отправ\w*|зачисл\w*|запуска\w*|открыва\w*)",
        r"не\s+туда|не\s+тому|не\s+тот\s+(?:аккаунт|получатель)|ошиб\w*|неверн\w*|неправильн\w*",
        r"проблем\w*|претензи\w*|возврат\w*|верни\w*\s+(?:деньг\w*|средств\w*)|обман\w*|скам\w*|заблок\w*|нерабоч\w*",
        r"doesn.?t\s+work|not\s+(?:working|received|arrived)|can.?t\s+(?:log\s*in|login|access)|wrong\s+(?:account|recipient)|error|refund|scam",
    )

    @classmethod
    def reasons(cls, messages):
        result = []
        for message in messages:
            if (
                message.get("seller")
                or message.get("system")
                or message.get("employee")
            ):
                continue
            text = " ".join(
                str(message.get("text", "")).casefold().replace("ё", "е").split()
            )
            for pattern in cls.patterns:
                match = re.search(pattern, text)
                if match:
                    result.append(
                        {
                            "message_id": message.get("id"),
                            "match": match.group(0),
                            "text": str(message.get("text", ""))[:600],
                        }
                    )
                    break
        return result


class ReleaseFeatures:
    async def rmthub_statistics(self, refresh=False):
        if not self.cfg.flag("Statistics", "rmthub_enabled"):
            await self.card(
                "main",
                "menu",
                "RMTHub выключен в конфиге ядра.",
                [[("Назад", "profile")]],
            )
            return
        account = getattr(self.fp, "account", None)
        identifier = getattr(account, "id", None)
        username = getattr(account, "username", None)
        if not identifier or not username:
            await self.card(
                "main",
                "menu",
                "Для статистики RMTHub нужен подключённый аккаунт FunPay.",
                [[("Назад", "profile")]],
            )
            return
        cached = self.state.d.get("rmthub")
        error = None
        if cached and str(cached.get("data", {}).get("user", {}).get("id")) != str(
            identifier
        ):
            cached = None
        lifetime = max(
            60, self.cfg.number("Statistics", "rmthub_cache_minutes", 60) * 60
        )
        if not cached or time.time() - cached["cached_at"] > lifetime or refresh:
            if time.time() < self.state.d.get("rmthub_retry_after", 0):
                error = "Повторный запрос RMTHub пока отложен."
            else:
                try:
                    cached = await RMTHubStatistics.fetch(username, identifier)
                    self.state.d["rmthub"] = cached
                    self.state.d["rmthub_retry_after"] = time.time() + 60
                    self.state.save()
                    LOG.info(
                        "RMTHub: получена статистика публичных отзывов, account_id=%s",
                        identifier,
                    )
                except Exception:
                    error = "Не удалось обновить RMTHub; проверьте журнал."
                    self.state.d["rmthub_retry_after"] = time.time() + 60
                    self.state.save()
                    LOG.exception(
                        "Не удалось получить статистику RMTHub: account_id=%s",
                        identifier,
                    )
        text = "🌐 RMTHub · статистика по отзывам\n"
        if error:
            text += "⚠ " + error + "\n"
        if cached:
            data = cached["data"]
            stats = data["stats"]
            text += f"Профиль: {username}\nОтзывов: {stats.get('totalReviews', 0)}\nОценка оборота по отзывам: {stats.get('totalAmount', 0)} USD-экв.\nСреднее на отзыв: {stats.get('averagePerReview', 0)} USD-экв.\nИгр: {stats.get('gamesPlayed', 0)}\nПолучено: {human_time(self.cfg, cached['cached_at'])}"
            fetched = cached.get("meta", {}).get("fetchedAt")
            if fetched:
                try:
                    text += "\nСнимок источника: " + human_time(
                        self.cfg,
                        datetime.fromisoformat(
                            fetched.replace("Z", "+00:00")
                        ).timestamp(),
                    )
                except (ValueError, TypeError):
                    pass
            periods = stats.get("byTimePeriod", {})
            if periods:
                text += "\n\nПо периодам:"
                for name, row in list(periods.items())[:6]:
                    title = {
                        "thisMonth": "Этот месяц",
                        "lastMonth": "Прошлый месяц",
                    }.get(name, name)
                    text += f"\n{title}: {row.get('amount', 0)} USD-экв. · отзывов {row.get('count', 0)}"
            games = stats.get("byGame", {})
            if games:
                text += "\n\nПо играм:"
                for name, row in list(games.items())[:10]:
                    text += f"\n{name}: {row.get('amount', 0)} USD-экв. · отзывов {row.get('count', 0)}"
            text += "\n\nЭто публичная оценка по отзывам, а не все продажи и не чистая прибыль. В финансовый журнал она не добавляется."
        await self.card(
            "main",
            "menu",
            text,
            [
                [("Обновить", "rmthub_refresh")],
                [
                    {
                        "text": "Открыть профиль RMTHub",
                        "url": "https://rmthub.com/ru/funpay/" + str(identifier),
                    }
                ],
                [("Назад", "profile")],
            ],
        )

    async def finances(self, source=None):
        ledger = FinancialLedger(self.state)
        total = ledger.summary(source)
        balance = self.state.d.get("balance", {})
        text = "💰 Финансы" + (" · " + source if source else "")
        text += f"\nБаланс FunPay: {balance.get('total', 'не обновлён')} ₽\nДоступно к выводу: {balance.get('available', 'не обновлено')} ₽"
        if balance.get("at"):
            text += "\nПроверен: " + human_time(self.cfg, balance["at"])
        text += f"\n\nПодтверждённые поступления: {total['credit']:.2f} ₽\nНе подтверждено: {total['pending']:.2f} ₽\nСебестоимость выданного: {total['cost']:.2f} ₽\nРасчётная комиссия вывода: {total['estimated_fee']:.2f} ₽\nПоправка фактической комиссии: {total['correction']:.2f} ₽\nПрибыль с учтённой себестоимостью: {total['profit']:.2f} ₽\nВыведено по учёту: {total['withdrawn']:.2f} ₽\nРасходы на вывод по учёту: {total['actual_fee']:.2f} ₽\nКомиссия способа (для USDT — расчёт тарифа): {total['actual_service_fee']:.2f} ₽\nИз них разница курсов: {total['actual_fx_loss']:.2f} ₽\nВозвращённые поступления: {total['refunds']:.2f} ₽"
        if self.cfg.get("AutoWithdraw", "method_kind", "card") == "usdt_trc20":
            text += "\n\nUSDT: комиссия способа и курсовая разница учтены раздельно; рублёвая оценка получения фиксируется по рыночному ориентиру в момент расчёта. До расчёта вывода 6% — только базовая оценка, курсовая разница может быть неизвестна."
        historical_profit = sum(
            (
                money(day.get("total_profit", 0))
                for key, plugin in self.state.d["plugin_state"].items()
                if source is None or key == source
                for day in plugin.get("historical_statistics", {}).values()
            ),
            Decimal("0"),
        )
        if historical_profit:
            text += f"\n\nИсторический итог: {historical_profit:.2f} ₽. Это готовая историческая прибыль; комиссия из неё повторно не удерживается. Новые операции показаны выше."
        if total["unknown_cost"]:
            text += f"\n⚠ Себестоимость неизвестна у {total['unknown_cost']} заказов: итог прибыли неполный."
        if total["foreign"]:
            text += f"\nЗаказы в других валютах: {total['foreign']}, в рублёвый итог не включены."
        if source is None:
            for name in sorted({r["source"] for r in self.state.d["ledger"].values()}):
                row = ledger.summary(name)
                text += (
                    f"\n\n{self.source_name(name)}: поступления {row['credit']:.2f} ₽ · затраты {row['cost']:.2f} ₽ · результат {row['profit']:.2f} ₽"
                    + (" (неполный)" if row["unknown_cost"] else "")
                )
        text += f"\n\nПрямые продажи: {total['direct_credit']:.2f} ₽\nСебестоимость прямых закупок: {total['direct_cost']:.2f} ₽\nРезультат прямых закупок без комиссии FunPay: {total['direct_profit']:.2f} ₽"
        personal = total["direct_personal"] + sum(
            (
                money(v["cost"])
                for key, p in self.state.d["plugin_state"].items()
                if source is None or key == source
                for v in p.get("personal_expenses", {}).values()
            ),
            Decimal("0"),
        )
        text += f"\n\nЛичные закупки вне выручки FunPay: {personal:.2f} ₽"
        text += "\n\nКомиссия категории уже учтена в сумме продавцу. Расчётные 3% не списываются повторно. Вывод — движение денег, не новая продажа. Старые продажи не импортируются."
        sources = sorted({r["source"] for r in self.state.d["ledger"].values()})
        rows = [
            [(self.source_name(n), "finance_source:" + n)]
            for n in sources
            if n != source
        ]
        rows += [
            [("🔄 Баланс", "balance"), ("💸 Вывод", "withdraw_menu")],
            [
                ("📋 Операции вывода", "withdraw_history"),
                ("📥 Экспорт учёта", "ledger_export"),
            ],
            [("⏳ Задержка / уведомления", "finance_settings")],
            [("‹ Главное", "menu:main")],
        ]
        await self.card("main", "menu", text, rows)

    def source_name(self, value):
        plugin = self.plugins.get(value)
        return (
            getattr(plugin, "TITLE", None)
            or getattr(plugin, "NAME", None)
            or self.state.d.get("plugin_metadata", {}).get(value, {}).get("title")
            or ("Прочие продажи" if value == "other" else value)
        )

    async def refresh_balance(self):
        balance = await self.fp.balance(self.cfg)
        self.state.d["balance"] = {**balance, "at": time.time()}
        self.state.save()
        return balance

    async def withdraw_menu(self):
        c = self.cfg
        kind = c.get("AutoWithdraw", "method_kind", "card")
        percentage, fee_min, threshold = WithdrawalCosts.terms(kind)
        text = f"💸 Вывод средств\nАвтовывод: {'включён' if c.flag('AutoWithdraw') else 'выключен'}\nСпособ: {c.get('AutoWithdraw', 'method') or 'не задан'}\nРеквизиты: {'заданы локально' if c.get('AutoWithdraw', 'wallet') else 'не заданы'}\nМинимальная сумма: {c.get('AutoWithdraw', 'minimum')} ₽\nОставлять на балансе: {c.get('AutoWithdraw', 'reserve')} ₽\n\nВыводится доступный баланс минус остаток. Фактическая сумма получения берётся из предварительного расчёта FunPay. Операции с неизвестным результатом блокируют новые выводы до проверки."
        text += f"\n\nТариф для расчёта: {percentage * 100:.0f}%, не менее {fee_min:.0f} ₽.\nБез повышения процента минимальной комиссией: от {threshold:.0f} ₽.\nДля USDT разница курсов считается отдельно. Итог подтверждается предварительным расчётом FunPay."
        await self.card(
            "main",
            "menu",
            text,
            [
                [
                    (
                        ("🟢" if c.flag("AutoWithdraw") else "⚪") + " Автовывод",
                        "toggle:AutoWithdraw:enabled",
                    )
                ],
                [("Способы и сохранённые реквизиты", "withdraw_methods")],
                [
                    ("Порог", "edit:AutoWithdraw:minimum"),
                    ("Остаток", "edit:AutoWithdraw:reserve"),
                ],
                [("Сумма ручного вывода", "withdraw_amount")],
                [("Рассчитать вывод сейчас", "withdraw_preview")],
                [("‹ Финансы", "menu:finance")],
            ],
        )

    async def preview_withdrawal(self, user=None, automatic=False):
        if any(
            o["status"] in ("pending", "unknown")
            for o in self.state.d["withdrawals"].values()
        ):
            if not automatic:
                await self.say(
                    "main",
                    "Есть неподтверждённый вывод. Проверьте операции вывода и результат на FunPay.",
                )
            return
        balance = await self.refresh_balance()
        debit = money(self.eligible_balance(balance)) - money(
            self.cfg.get("AutoWithdraw", "reserve", "0")
        )
        if (
            not automatic
            and self.cfg.get("AutoWithdraw", "manual_amount", "all") != "all"
        ):
            requested = money(self.cfg.get("AutoWithdraw", "manual_amount"))
            if requested <= 0 or requested > debit:
                await self.say(
                    "main",
                    "Указанная сумма превышает доступный вывод или равна нулю. Измените сумму в меню вывода.",
                )
                return
            debit = requested
        if (
            debit <= 0
            or automatic
            and debit < money(self.cfg.get("AutoWithdraw", "minimum", "1000"))
        ):
            return
        cfg = self.cfg
        if not cfg.get("AutoWithdraw", "method") or not cfg.get(
            "AutoWithdraw", "wallet"
        ):
            if automatic:
                return
            await self.say(
                "main",
                "Выберите способ и реквизиты в разделе вывода. Новые реквизиты сначала добавьте на сайте FunPay.",
            )
            return
        quote = await self.fp.withdraw_quote(cfg, debit)
        if "amount_int" in quote and money(quote["amount_int"]) != debit:
            raise ValueError("FunPay изменил сумму списания; пересчитайте вывод")
        spec = quote.get("_spec", {"kind": "card", "currency": "RUB"})
        kind = spec["kind"]
        percentage, minimum, threshold = WithdrawalCosts.terms(kind)
        cfg.set("AutoWithdraw", "method_kind", kind)
        if automatic and debit < threshold:
            return
        receive = Decimal(str(quote["amount_ext"]))
        market = None
        if kind == "usdt_trc20":
            market = await self.get_market_rates()
        costs = WithdrawalCosts.calculate(
            debit, receive, kind, market["usdt_rub"] if market else None
        )
        plan = {
            "id": secrets.token_hex(8),
            "user": user,
            "expires": time.time() + 120,
            "debit": str(debit),
            "receive": str(receive),
            "method": cfg.get("AutoWithdraw", "method"),
            "kind": kind,
            "wallet_hash": hashlib.sha256(
                cfg.get("AutoWithdraw", "wallet").encode()
            ).hexdigest(),
            "status": "quoted",
            "at": time.time(),
            **costs,
        }
        if market:
            plan.update(market_at=market["at"], market_source=market["source"])
        self.state.d["withdrawals"][plan["id"]] = plan
        self.state.d["withdrawal_reference"] = copy.deepcopy(plan)
        self.state.save()
        if automatic:
            await self.execute_withdrawal(plan["id"], None)
            return
        text = f"💸 Подтвердить вывод?\nСписать: {debit:.2f} ₽\nПолучить: {receive} {plan['receive_currency']}\nКомиссия способа: {plan['service_fee']} ₽"
        if kind == "usdt_trc20":
            text += f"\nКурс по предварительному расчёту: {Decimal(plan['funpay_rate']):.4f} ₽/USDT\nРыночный ориентир: {plan['market_rate']} ₽/USDT\nРазница курсов: {plan['fx_loss']} ₽\nВсего расходов относительно рынка: {plan['fee']} ₽ ({Decimal(plan['total_cost_percent']):.2f}%)\nКомиссия {percentage * 100:.0f}%/минимум {minimum:.0f} ₽ взята из настроенного тарифа; разложение курса расчётное."
        if debit < threshold:
            text += f"\n⚠ Ниже {threshold:.0f} ₽ минимальная комиссия повышает фактический процент."
        text += "\nДействительно 2 минуты."
        await self.card(
            "main",
            "menu",
            text,
            [
                [("Подтвердить", "withdraw_confirm:" + plan["id"])],
                [("Отмена", "withdraw_menu")],
            ],
        )

    async def execute_withdrawal(self, key, user):
        async with self.cash_lock:
            op = self.state.d["withdrawals"].get(key)
            if (
                not op
                or op["status"] != "quoted"
                or op["user"] != user
                or time.time() > op["expires"]
            ):
                return
            if not self.cfg.flag("SellerStatus", "online"):
                await self.say("main", "Включите онлайн перед выводом.")
                return
            if (
                op["method"] != self.cfg.get("AutoWithdraw", "method")
                or op["wallet_hash"]
                != hashlib.sha256(
                    self.cfg.get("AutoWithdraw", "wallet").encode()
                ).hexdigest()
            ):
                raise ValueError("Реквизиты изменились. Пересчитайте вывод.")
            if any(
                v["status"] in ("pending", "unknown")
                for k, v in self.state.d["withdrawals"].items()
                if k != key
            ):
                return
            balance = await self.refresh_balance()
            if money(self.eligible_balance(balance)) - money(op["debit"]) < money(
                self.cfg.get("AutoWithdraw", "reserve", "0")
            ):
                await self.say("main", "Баланс изменился, пересчитайте вывод.")
                return
            # Freeze intent before the irreversible request; never repeat on timeout/restart.
            op["status"] = "pending"
            self.state.save()
            try:
                result = await self.fp.withdraw_submit(self.cfg, op["receive"])
                actual_debit = money(result["amount_int"])
                actual_receive = Decimal(str(result.get("amount_ext", op["receive"])))
                if actual_debit != money(op["debit"]) or actual_receive != Decimal(
                    op["receive"]
                ):
                    raise ValueError(
                        "Фактические суммы отличаются от расчёта; нужна сверка"
                    )
                op.update(
                    status="done", debit=str(actual_debit), receive=str(actual_receive)
                )
                FinancialLedger(self.state).allocate_withdrawal(op)
                await self.say(
                    "main",
                    f"💸 Заявка на вывод принята FunPay\nСписано: {op['debit']} ₽\nК получению: {op['receive']} {op.get('receive_currency', 'RUB')}\nРасходы на вывод по учёту: {op['fee']} ₽\n{human_time(self.cfg)}\nЗачисление на внешний счёт проверяется отдельно.",
                )
                self.state.d.pop("balance", None)
            except Exception as e:
                op["status"] = "unknown"
                LOG.error("Withdrawal outcome unknown: %s", type(e).__name__)
                await self.say(
                    "main",
                    f"⚠️ Вывод {key}: результат неизвестен. Повтор заблокирован. Проверьте историю на FunPay.",
                )
            self.state.save()

    async def withdrawal_history(self):
        rows = sorted(
            self.state.d["withdrawals"].values(), key=lambda x: x["at"], reverse=True
        )[:20]
        text = "📋 Выводы\n" + "\n".join(
            f"{o['id']} · {o['status']} · {o['debit']} ₽ · {human_time(self.cfg, o['at'])}"
            for o in rows
        )
        buttons = [
            [(f"Сверить {o['id'][:6]}", "reconcile:" + o["id"])]
            for o in rows
            if o["status"] in ("pending", "unknown")
        ]
        await self.card(
            "main",
            "menu",
            text or "Выводов нет",
            buttons + [[("‹ Финансы", "menu:finance")]],
        )

    async def release_callback(self, data, user):
        if data in ("rmthub", "rmthub_refresh"):
            await self.rmthub_statistics(data == "rmthub_refresh")
        elif data == "balance":
            await self.refresh_balance()
            await self.finances()
        elif data.startswith("finance_source:"):
            await self.finances(data.split(":", 1)[1])
        elif data == "withdraw_menu":
            await self.withdraw_menu()
        elif data == "withdraw_preview":
            await self.preview_withdrawal(user)
        elif data.startswith("withdraw_confirm:"):
            await self.execute_withdrawal(data.split(":")[1], user)
        elif data == "withdraw_history":
            await self.withdrawal_history()
        elif data.startswith("reconcile:"):
            key = data.split(":")[1]
            await self.card(
                "main",
                "menu",
                f"Вывод {key}\nСначала сверьте историю FunPay и внешний счёт. Какой результат подтверждён?",
                [
                    [("Заявка есть, суммы совпали", "reconcile_done:" + key)],
                    [("Указать фактические суммы", "reconcile_amounts:" + key)],
                    [("Точно не было списания", "reconcile_absent:" + key)],
                    [("Оставить на проверке", "withdraw_history")],
                ],
            )
        elif data.startswith("reconcile_amounts:"):
            key = data.split(":")[1]
            if self.cash_lock.locked():
                await self.say(
                    "main", "Запрос вывода ещё выполняется. Дождитесь результата."
                )
                return True
            self.pending[user] = ("_withdraw", key, time.time() + 300)
            op = self.state.d["withdrawals"].get(key, {})
            unit = op.get("receive_currency", "RUB")
            await self.card(
                "main",
                "menu",
                f"После сверки FunPay пришлите списанную сумму в RUB и полученную сумму в {unit} через пробел. Отмена: /menu.",
            )
        elif data.startswith(("reconcile_done:", "reconcile_absent:")):
            kind, key = data.split(":")
            op = self.state.d["withdrawals"].get(key)
            if op and op["status"] in ("pending", "unknown"):
                op["status"] = "done" if kind == "reconcile_done" else "absent"
                op["reconciled_by"] = user
                op["reconciled_at"] = time.time()
                if op["status"] == "done":
                    FinancialLedger(self.state).allocate_withdrawal(op)
                self.state.save()
            await self.withdrawal_history()
        elif data == "ledger_export":
            body = json.dumps(
                {
                    "ledger": self.state.d["ledger"],
                    "withdrawals": self.state.d["withdrawals"],
                },
                ensure_ascii=False,
                indent=2,
            ).encode()
            await self.tg.call(
                "send_document",
                chat_id=self.group,
                message_thread_id=self.state.d["topics"]["main"],
                document=("finance.json", body),
            )
        elif data == "withdraw_methods":
            methods = await self.fp.withdraw_methods()
            self.withdraw_choices = {}
            rows = []
            for i, (method, name, wallet) in enumerate(methods):
                token = str(i)
                self.withdraw_choices[token] = (method, wallet)
                rows.append([(name + " · …" + wallet[-4:], "withdraw_method:" + token)])
            await self.card(
                "main",
                "menu",
                "Выберите способ и сохранённые реквизиты: карта, СБП или USDT TRC20.\nЕсли список пуст — добавьте их на сайте FunPay.",
                rows + [[("‹ Назад", "withdraw_menu")]],
            )
        elif data.startswith("withdraw_method:"):
            choice = getattr(self, "withdraw_choices", {}).get(data.split(":")[1])
            if choice:
                self.cfg.set("AutoWithdraw", "method", choice[0])
                self.cfg.set("AutoWithdraw", "wallet", choice[1])
                spec = getattr(self.fp, "withdrawal_options", {}).get(
                    choice[0], {"kind": "card"}
                )
                self.cfg.set("AutoWithdraw", "method_kind", spec["kind"])
            await self.withdraw_menu()
        elif data == "profile":
            result = await self.fp.profile()
            if self.cfg.flag("Finance", "hold_enabled"):
                due = [
                    row["confirmed_at"] + 48 * 3600
                    for row in self.state.d["ledger"].values()
                    if row.get("status") == "CLOSED"
                    and row.get("currency") == "RUB"
                    and row.get("kind") != "direct"
                    and row.get("confirmed_at")
                    and row["confirmed_at"] + 48 * 3600 > time.time()
                ]
                result += "\n\nЛокальная задержка: 48 часов."
                if due:
                    result += (
                        " Ближайшее известное освобождение: "
                        + human_time(self.cfg, min(due))
                        + ". Фактический доступ проверяется на FunPay."
                    )
            else:
                result += "\n\nЛокальный таймер задержки выключен; доступность определяет FunPay."
            await self.card(
                "main",
                "menu",
                result,
                [
                    [("🌐 RMTHub · статистика", "rmthub")],
                    [("⚖ Ограничения", "rating")],
                    [("‹ Главное", "menu:main")],
                ],
            )
        elif data == "rating":
            await self.card(
                "main", "menu", await self.fp.rating(), [[("‹ Профиль", "profile")]]
            )
        elif data == "graphs":
            await self.graph()
        elif data == "logs_menu":
            rows = [
                [(title, "logpick:" + source)]
                for source, (title, path) in self.log_sources().items()
            ]
            await self.card(
                "main",
                "menu",
                "📋 Логи\nВыберите журнал ядра или плагина.",
                rows + [[("‹ Главное", "menu:main")]],
            )
        elif data.startswith(("logpick:", "logfile:", "logerrors:", "logsearch:")):
            action, source = data.split(":", 1)
            if source not in self.log_sources():
                return True
            if action == "logpick":
                await self.log_menu(source)
            elif action == "logfile":
                title, path = self.log_sources()[source]
                await self.file_to_main(
                    "core.log" if source == "core" else source + ".log", path
                )
            elif action == "logerrors":
                await self.search_logs("ERROR", source)
            else:
                self.pending[user] = ("_logs", source, time.time() + 300)
                await self.card(
                    "main",
                    "menu",
                    "Пришлите строку для поиска в выбранном журнале.",
                    [[("‹ Назад", "logpick:" + source)]],
                )
        elif data in ("errors", "log_search"):
            if data == "log_search":
                self.pending[user] = ("_logs", "core", time.time() + 300)
                await self.card(
                    "main",
                    "menu",
                    "Пришлите строку для поиска в журнале.",
                    [[("‹ Назад", "logpick:core")]],
                )
            else:
                await self.search_logs("ERROR")
        else:
            return False
        return True

    def log_sources(self):
        sources = {"core": ("⚙️ Общий журнал", self.state.path.parent / "log.log")}
        directory = self.state.path.parent / "plugins"
        names = set(self.plugins)
        if directory.exists():
            names.update(path.name for path in directory.iterdir() if path.is_dir())
        for name in sorted(names):
            if name == "core" or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
                continue
            plugin = self.plugins.get(name)
            title = (
                getattr(plugin, "TITLE", None)
                or getattr(plugin, "NAME", None)
                or self.source_name(name)
            )
            if name == "lot_tools":
                title = "🧰 Инструменты лотов"
            sources[name] = (title, directory / name / "log.log")
        return sources

    async def log_menu(self, source):
        if source not in self.log_sources():
            return
        title, path = self.log_sources()[source]
        await self.card(
            "main",
            "menu",
            "📋 " + title,
            [
                [
                    ("Скачать", "logfile:" + source),
                    ("Последние ошибки", "logerrors:" + source),
                ],
                [("Поиск", "logsearch:" + source)],
                [("‹ Все журналы", "logs_menu")],
            ],
        )

    async def search_logs(self, query, source="core"):
        if source not in self.log_sources():
            return
        title, path = self.log_sources()[source]
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        result = "\n".join(
            line for line in lines if query.casefold() in line.casefold()
        )[-3400:]
        await self.card(
            "main",
            "menu",
            "📋 " + title + " · результаты\n" + (result or "Совпадений нет"),
            [[("‹ Журнал", "logpick:" + source)]],
        )

    async def graph(self):
        await self.report()

    async def release_tick(self):
        now = time.time()
        if now >= self.state.d.get("plugins_scan_due", 0):
            self.state.d["plugins_scan_due"] = now + 15
            await self.reload_plugins()
        day = datetime.now(ZoneInfo(self.cfg.get("General", "timezone"))).strftime(
            "%Y-%m-%d"
        )
        for name, operation in [
            ("tickets", self.ticket_tick),
            ("finance_notices", self.finance_notification_tick),
            ("receipt_outbox", self.notification_outbox_tick),
        ]:
            try:
                await operation(now)
            except Exception:
                LOG.exception(
                    "Ошибка фоновой службы %s; остальные службы продолжают работу", name
                )
        if (
            self.cfg.flag("SellerStatus", "online")
            and self.cfg.flag("AutoWithdraw")
            and now >= self.state.d.get("withdraw_due", 0)
        ):
            self.state.d["withdraw_due"] = now + max(
                60, self.cfg.number("AutoWithdraw", "interval_seconds", 300)
            )
            self.state.save()
            await self.preview_withdrawal(automatic=True)
        for name, plugin in list(self.plugins.items()):
            if hasattr(plugin, "tick"):
                try:
                    await plugin.tick()
                except Exception as error:
                    if hasattr(plugin, "runtime"):
                        plugin.runtime.health["monitor"] = {
                            "running": False,
                            "error": type(error).__name__,
                            "at": time.time(),
                        }
                    plugin_logger(self.cfg, self.state.path.parent, name).exception(
                        "Ошибка планировщика плагина"
                    )
        if self.cfg.flag("RatingRestrictions") and now >= self.state.d.get(
            "rating_due", 0
        ):
            self.state.d["rating_due"] = now + 300
            result = await self.fp.rating()
            if self.state.d.get("rating_last") not in (None, result):
                await self.say("main", "⚠️ Ограничения рейтинга изменились\n" + result)
            self.state.d["rating_last"] = result
            self.state.save()
