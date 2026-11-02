"""Финансовый учёт, комиссии и статистика."""

from __future__ import annotations

import asyncio
import time
from decimal import ROUND_HALF_UP, Decimal


def money(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Сумма должна быть конечным числом")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class WithdrawalCosts:
    """Рассчитывает справочные комиссии; итоговые условия определяет предварительный расчёт площадки."""

    @staticmethod
    def terms(kind):
        return (
            (Decimal(".06"), Decimal("150"), Decimal("2500"))
            if kind == "usdt_trc20"
            else (Decimal(".03"), Decimal("30"), Decimal("1000"))
        )

    @staticmethod
    def calculate(debit, receive, kind, market_rate=None):
        debit = money(debit)
        receive = Decimal(str(receive))
        if debit <= 0 or not receive.is_finite() or receive <= 0:
            raise ValueError("Некорректные суммы вывода")
        if kind != "usdt_trc20":
            if receive > debit:
                raise ValueError("Неоднозначная валюта вывода")
            return {
                "fee": str(debit - money(receive)),
                "service_fee": str(debit - money(receive)),
                "fx_loss": "0.00",
                "receive_currency": "RUB",
            }
        market = Decimal(str(market_rate))
        if not market.is_finite() or market <= 0:
            raise ValueError("Нет актуального рыночного курса USDT/RUB")
        percentage, minimum, threshold = WithdrawalCosts.terms(kind)
        tariff_fee = money(max(debit * percentage, minimum))
        if debit <= tariff_fee:
            raise ValueError("Сумма меньше расчётной комиссии")
        net = debit - tariff_fee
        fp_rate = net / receive
        value = money(receive * market)
        total = debit - value
        return {
            "fee": str(total),
            "service_fee": str(tariff_fee),
            "fx_loss": str(total - tariff_fee),
            "receive_currency": "USDT",
            "market_rate": str(market),
            "receive_value_rub": str(value),
            "funpay_rate": str(fp_rate),
            "rate_markup_percent": str((fp_rate / market - 1) * 100),
            "fx_loss_percent": str((1 - market / fp_rate) * 100),
            "total_cost_percent": str(total / debit * 100),
            "tariff_basis": "reference_from_screenshots",
            "rate_basis": "inferred_from_preview",
        }


class FinancialLedger:
    """Раздельно учитывает движение средств и прибыль; суммы хранятся десятичными строками."""

    def __init__(self, state):
        self.state = state

    def observe(self, event):
        key = event["order_id"]
        rows = self.state.d["ledger"]
        row = rows.setdefault(
            key,
            {
                "source": "other",
                "cost": None,
                "credit": "0.00",
                "withdrawn": "0.00",
                "actual_fee": "0.00",
                "fee_correction": "0.00",
                "at": time.time(),
            },
        )
        row.update(
            status=event["status"],
            currency=event.get("currency", "RUB"),
            chat=event["chat"],
        )
        if "price" in event:
            row["credit"] = str(money(event["price"]))
        if event["status"] == "CLOSED":
            row.setdefault("confirmed_at", time.time())
        self.state.save()

    def delivery(self, source, order_id, cost_rub, spent_usdt, rate, save=True):
        row = self.state.d["ledger"].setdefault(
            order_id,
            {
                "source": source,
                "credit": "0.00",
                "status": "PAID",
                "currency": "RUB",
                "withdrawn": "0.00",
                "actual_fee": "0.00",
                "fee_correction": "0.00",
                "at": time.time(),
            },
        )
        if row.get("delivery_accounted"):
            return
        row.update(
            source=source,
            cost=str(money(cost_rub)),
            spent_usdt=str(spent_usdt),
            purchase_rate=str(rate),
            delivery_accounted=True,
            delivered_at=time.time(),
        )
        if save:
            self.state.save()

    def summary(self, source=None):
        rows = [
            r
            for r in self.state.d["ledger"].values()
            if source is None or r["source"] == source
        ]
        result = {
            k: Decimal("0")
            for k in (
                "credit",
                "pending",
                "cost",
                "estimated_fee",
                "actual_fee",
                "correction",
                "withdrawn",
                "refunds",
                "direct_credit",
                "direct_cost",
                "direct_profit",
                "direct_personal",
                "actual_service_fee",
                "actual_fx_loss",
            )
        }
        result.update(count=len(rows), unknown_cost=0, foreign=0)
        for r in rows:
            if r.get("kind") == "direct":
                result["direct_credit"] += money(r["credit"])
                result["direct_cost"] += money(r["cost"])
                result["direct_profit"] += money(r["credit"]) - money(r["cost"])
                if r.get("personal"):
                    result["direct_personal"] += money(r["cost"])
                continue
            if r.get("currency") != "RUB":
                result["foreign"] += 1
                continue
            credit = money(r["credit"])
            closed = r["status"] == "CLOSED"
            refunded = r["status"] == "REFUNDED"
            if closed:
                result["credit"] += credit
                result["estimated_fee"] += money(
                    credit * Decimal(r.get("fee_rate", "0.03"))
                )
            elif refunded:
                result["refunds"] += credit
            else:
                result["pending"] += credit
            if r.get("cost") is not None:
                result["cost"] += money(r["cost"])
            elif closed:
                result["unknown_cost"] += 1
            for k, field in [("actual_fee", "actual_fee"), ("withdrawn", "withdrawn")]:
                result[k] += money(r.get(field, 0))
            result["actual_service_fee"] += money(
                r.get("actual_service_fee", r.get("actual_fee", 0))
            )
            result["actual_fx_loss"] += money(r.get("actual_fx_loss", 0))
            result["correction"] += (
                money(r.get("fee_correction", 0))
                if closed
                else money(r.get("actual_fee", 0))
            )
        result["profit"] = (
            result["credit"]
            - result["estimated_fee"]
            - result["cost"]
            - result["correction"]
            + result["direct_profit"]
        )
        return result

    def allocate_withdrawal(self, operation):
        if operation.get("allocated"):
            return
        debit = money(operation["debit"])
        fee = money(operation["fee"])
        remaining = debit
        allocation = []
        # Known source attribution only. Never claim external balance belongs to a plugin.
        eligible = [
            (k, r)
            for k, r in self.state.d["ledger"].items()
            if r.get("status") == "CLOSED"
            and r.get("currency") == "RUB"
            and r.get("kind") != "direct"
        ]
        for key, row in sorted(eligible, key=lambda x: x[1]["at"]):
            take = min(
                remaining,
                max(
                    Decimal("0"), money(row["credit"]) - money(row.get("withdrawn", 0))
                ),
            )
            if take <= 0:
                continue
            allocation.append((key, take))
            remaining -= take
            if remaining <= 0:
                break
        allocated_fee = Decimal("0")
        allocated_service = Decimal("0")
        for index, (key, take) in enumerate(allocation):
            part = money(fee * take / debit) if debit else Decimal("0")
            if remaining == 0 and index == len(allocation) - 1:
                part = fee - allocated_fee
            allocated_fee += part
            row = self.state.d["ledger"][key]
            row["withdrawn"] = str(money(row.get("withdrawn", 0)) + take)
            previous_service = money(
                row.get("actual_service_fee", row.get("actual_fee", 0))
            )
            row["actual_fee"] = str(money(row.get("actual_fee", 0)) + part)
            service = (
                money(money(operation.get("service_fee", fee)) * take / debit)
                if debit
                else Decimal("0")
            )
            if remaining == 0 and index == len(allocation) - 1:
                service = money(operation.get("service_fee", fee)) - allocated_service
            allocated_service += service
            row["actual_service_fee"] = str(previous_service + service)
            row["actual_fx_loss"] = str(
                money(row.get("actual_fx_loss", 0)) + part - service
            )
            # Fee estimate is already present in profit; book only the delta.
            row["fee_correction"] = str(
                money(row.get("fee_correction", 0))
                + part
                - money(take * Decimal(row.get("fee_rate", "0.03")))
            )
        operation["unattributed_debit"] = str(remaining)
        operation["unattributed_fee"] = str(fee - allocated_fee)
        operation["allocation"] = [
            {"order_id": key, "debit": str(take)} for key, take in allocation
        ]
        operation["allocated"] = True
        self.state.save()


class RMTHubStatistics:
    """Получает публичную аналитику отзывов без использования её для личных балансов и прибыли."""

    @staticmethod
    def validate(payload, expected_id):
        if not isinstance(payload, dict):
            raise ValueError("RMTHub: неверный формат ответа")
        data = payload.get("data", {})
        user = data.get("user", {})
        stats = data.get("stats", {})
        if str(user.get("id")) != str(expected_id):
            raise ValueError("RMTHub вернул статистику другого профиля")
        if not isinstance(stats, dict):
            raise ValueError("RMTHub не вернул статистику")
        for key in ("totalAmount", "totalReviews", "averagePerReview", "gamesPlayed"):
            number = Decimal(str(stats.get(key, 0)))
            if not number.is_finite() or number < 0:
                raise ValueError("RMTHub вернул некорректные числовые данные")
        return {"data": data, "meta": payload.get("meta", {}), "cached_at": time.time()}

    @staticmethod
    async def fetch(username, expected_id):
        from urllib.parse import quote

        def request():
            import requests

            response = requests.get(
                "https://rmthub.com/api/v1/funpay/users/" + quote(username, safe=""),
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if response.status_code == 429:
                raise ValueError("RMTHub: достигнут лимит запросов. Повторите позже.")
            response.raise_for_status()
            return response.json()

        return RMTHubStatistics.validate(await asyncio.to_thread(request), expected_id)
