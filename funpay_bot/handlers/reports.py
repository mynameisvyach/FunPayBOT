"""Reports application handlers."""

from __future__ import annotations

import asyncio
import io
import secrets
import time
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .. import ui
from ..finance import FinancialLedger, WithdrawalCosts
from ..framework import (
    plugin_capability,
)
from ..metadata import LOG, human_time
from ..ui import ui_collect_statistics, ui_date


class ReportsMixin:
    async def publish_rates_card(self, text):
        async with self.rates_lock:
            await self.card("main", "rates", text, new_message=not self.rates_created)
            self.rates_created = True
            message = self.state.d["cards"]["main/rates"]
            if self.state.d.get("rates_pinned") != message:
                previous = self.state.d.get("rates_pinned")
                try:
                    await self.tg.call(
                        "pin_chat_message",
                        chat_id=self.group,
                        message_id=message,
                        disable_notification=True,
                    )
                    self.state.d["rates_pinned"] = message
                    self.state.save()
                    if previous:
                        try:
                            await self.tg.call(
                                "unpin_chat_message",
                                chat_id=self.group,
                                message_id=previous,
                            )
                        except Exception:
                            LOG.warning("Previous rates pin could not be removed")
                except Exception:
                    LOG.warning("Rates pin failed; check can_pin_messages permission")
                    if not getattr(self, "pin_warning", False):
                        self.pin_warning = True
                        await self.say(
                            "main",
                            "Не удалось закрепить курсы. Дайте боту право закреплять сообщения.",
                        )

    async def get_market_rates(self):
        cached = self.state.d.get("market_rates")
        if cached and 0 <= time.time() - cached["at"] < 60:
            return cached

        def fetch():
            import requests

            proxy = self.cfg.get("FunPay", "proxy")
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "tether,the-open-network",
                    "vs_currencies": "rub,usd",
                    "include_last_updated_at": "true",
                },
                timeout=15,
                proxies={"http": proxy, "https": proxy} if proxy else None,
            )
            response.raise_for_status()
            return response.json()

        data = await asyncio.to_thread(fetch)
        tether = data["tether"]
        price = Decimal(str(tether["rub"]))
        stamp = float(tether["last_updated_at"])
        if not price.is_finite() or price <= 0 or not 0 <= time.time() - stamp <= 900:
            raise ValueError("Источник не вернул свежий курс USDT/RUB")
        value = {
            "usdt_rub": str(price),
            "ton_usd": str(data.get("the-open-network", {}).get("usd", "—")),
            "at": stamp,
            "source": "CoinGecko",
        }
        self.state.d["market_rates"] = value
        self.state.save()
        return value

    def estimated_withdrawal_rate(self):
        kind = self.cfg.get("AutoWithdraw", "method_kind", "card")
        rate = WithdrawalCosts.terms(kind)[0]
        reference = self.state.d.get("withdrawal_reference", {})
        if (
            kind == "usdt_trc20"
            and reference.get("kind") == kind
            and 0 <= time.time() - reference.get("market_at", 0) <= 900
        ):
            rate = 1 - (1 - rate) * Decimal(reference["market_rate"]) / Decimal(
                reference["funpay_rate"]
            )
        return rate

    async def rates(self):
        stamp = time.time()
        if self.fp.mode == "demo":
            text = "USDT/RUB: —\nTON/USD: —"
        else:
            try:
                data = await self.get_market_rates()
                text = f"USDT/RUB: {data['usdt_rub']}\nTON/USD: {data['ton_usd']}"
            except Exception:
                LOG.exception("Не удалось обновить курсы")
                cached = self.state.d.get("market_rates")
                text = "Курсы временно недоступны."
                if cached:
                    stamp = cached["at"]
                    text += f"\nСохранённые данные устарели.\nUSDT/RUB: {cached['usdt_rub']}\nTON/USD: {cached.get('ton_usd', '—')}"
        await self.publish_rates_card(
            "🤑 Актуальные курсы\n"
            + text
            + "\nОбновлено: "
            + human_time(self.cfg, stamp)
        )

    def report_snapshot(self):
        ui.UI_TIMEZONE = self.cfg.get("General", "timezone", "Europe/Moscow")
        zone = ZoneInfo(ui.UI_TIMEZONE)
        today = datetime.now(zone).date()
        days = [today - timedelta(days=n) for n in range(6, -1, -1)]
        start = datetime.combine(days[0], datetime.min.time(), tzinfo=zone).timestamp()
        end = datetime.combine(
            today + timedelta(days=1), datetime.min.time(), tzinfo=zone
        ).timestamp()
        orders = [
            o
            for o in list(self.state.d["orders"].values())
            + list(self.state.d.get("completed_order_stats", {}).values())
            if start <= o.get("updated_at", 0) < end
        ]
        header = f"📊 Еженедельный отчёт за {days[0].strftime('%d.%m')}–{today.strftime('%d.%m.%Y')}"
        lines = [
            header,
            f"Заказы с обновлениями: {len(orders)}; подтверждены: {sum(o['status'] == 'CLOSED' for o in orders)}; возвраты: {sum(o['status'] == 'REFUNDED' for o in orders)}.",
        ]
        caption = list(lines)
        series = {}
        unit_series = {}
        for name, plugin in sorted(self.plugins.items()):
            exporter = plugin_capability(plugin, "statistics")
            if not exporter:
                continue
            report_data = exporter()
            if not report_data:
                continue
            title = report_data["title"]
            unit = report_data.get("unit", "единиц")
            raw = report_data["days"]
            col = ui_collect_statistics(raw)
            profits = []
            weekly = {}
            units = 0
            daily_units = []
            lines += [
                "",
                title,
                "Выдачи за неделю (результат после учтённых затрат и комиссии):",
            ]
            for day in days:
                item = raw.get(day.isoformat(), {})
                profit = float(
                    item.get("total_profit", item.get("TOTAL_PROFIT", 0)) or 0
                )
                quantities = (
                    item.get("quantities_sold", item.get("QUANTITIES_SOLD", {})) or {}
                )
                count = sum(int(q) * int(n) for q, n in quantities.items())
                units += count
                profits.append(profit)
                daily_units.append(count)
                for q, n in quantities.items():
                    weekly[int(q)] = weekly.get(int(q), 0) + int(n)
                lines.append(
                    f"{day.strftime('%d.%m')}: {profit:.2f} ₽ · {count} {unit}"
                )
            series[title] = profits
            unit_series[title + " · " + unit] = daily_units
            caption.append(f"{title}: {sum(profits):.2f} ₽ · {units} {unit}")
            lines.append(f"Итого за неделю: {sum(profits):.2f} ₽ · {units} {unit}")
            lines.append(
                f"За всё время: {col['total_profit']:.2f} ₽ · {col['total_units']} {unit}; дней со статистикой: {col['days']}"
            )
            lines.append(
                f"Лучший день за всё время: {ui_date(col['best_date'])} · {col['best_profit']:.2f} ₽"
            )
            top = sorted(weekly.items(), key=lambda v: (-v[1], v[0]))[:7]
            lines.append(
                "Популярные номиналы за неделю: "
                + (", ".join(f"{q} × {n}" for q, n in top) or "нет выдач")
            )
            lines.append(
                "Популярные номиналы за всё время: "
                + (
                    ", ".join(f"{q} × {n}" for q, n in col["popular_denominations"])
                    or "нет выдач"
                )
            )
        for name, note in sorted(self.state.d.get("report_notes", {}).items()):
            if name in self.plugins:
                lines += [
                    "",
                    self.source_name(name) + " · дополнительные сведения",
                    human_time(self.cfg, note["at"]),
                    note["text"],
                ]
        lines += ["", "💰 Накопительные итоги журнала (за всё время):"]
        for source in sorted({r["source"] for r in self.state.d["ledger"].values()}):
            stats = FinancialLedger(self.state).summary(source)
            lines.append(
                f"{self.source_name(source)}: подтверждено {stats['credit']:.2f} ₽; ожидается {stats['pending']:.2f} ₽; затраты {stats['cost']:.2f} ₽; оценка комиссии {stats['estimated_fee']:.2f} ₽; поправка вывода {stats['correction']:.2f} ₽; прямые продажи {stats['direct_profit']:.2f} ₽; результат {stats['profit']:.2f} ₽"
                + (" (себестоимость неполная)" if stats["unknown_cost"] else "")
                + (f"; не-RUB операций: {stats['foreign']}" if stats["foreign"] else "")
            )
        lines += [
            "",
            "График и недельные итоги плагинов — по дате выдачи, включая прямые закупки и импортированную статистику. Они могут включать ещё не подтверждённые продажи. Изменившийся возврат пересчитывает день выдачи.",
            "Журнал отдельно учитывает подтверждение, ожидание, возвраты и фактические поправки вывода. Эти итоги не складываются со статистикой плагинов. История FunPay до запуска не импортируется; это не баланс аккаунта.",
        ]
        caption += [
            "",
            "График: результат выдач по дням. Подробности и накопительные итоги — по кнопке ниже.",
        ]
        pages = []
        page = ""
        for line in lines:
            for pos in range(0, max(1, len(line)), 3500):
                part = line[pos : pos + 3500]
                if len(page) + len(part) + 1 > 3600:
                    pages.append(page)
                    page = ""
                page += ("\n" if page else "") + part
        if page:
            pages.append(page)
        return {
            "caption": "\n".join(caption)[:1000],
            "pages": pages,
            "labels": [d.strftime("%d.%m") for d in days],
            "series": series,
            "unit_series": unit_series,
        }

    @staticmethod
    def report_image(snapshot):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax, units_ax) = plt.subplots(2, 1, figsize=(10, 7.5), sharex=True)
        clean = lambda name: "".join(
            c for c in name if c.isalnum() or c in " ._·-"
        ).strip()
        for name, values in snapshot["series"].items():
            ax.plot(snapshot["labels"], values, marker="o", label=clean(name))
        if snapshot["series"]:
            total = [
                sum(values[i] for values in snapshot["series"].values())
                for i in range(len(snapshot["labels"]))
            ]
            ax.plot(
                snapshot["labels"],
                total,
                linestyle="--",
                color="#333",
                label="Общий результат",
            )
            ax.legend()
        else:
            ax.text(
                0.5,
                0.5,
                "Нет статистики выдач плагинов",
                ha="center",
                transform=ax.transAxes,
            )
        for name, values in snapshot.get("unit_series", {}).items():
            units_ax.plot(snapshot["labels"], values, marker="o", label=clean(name))
        if snapshot.get("unit_series"):
            units_ax.legend()
        ax.set_title("Общий отчёт · результат выдач за неделю")
        ax.set_ylabel("₽")
        units_ax.set_title("Выдано по плагинам")
        units_ax.set_ylabel("Единицы товара")
        for panel in (ax, units_ax):
            panel.axhline(0, color="#888", linewidth=0.7)
            panel.grid(axis="y", alpha=0.2)
        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", dpi=140)
        plt.close(fig)
        return buf.getvalue()

    async def report_details(self, token, page, *, new_message=False):
        report = self.state.d.get("reports", {}).get(token)
        if not report or not 0 <= page < len(report["pages"]):
            return
        navigation = []
        if page:
            navigation.append(("‹ Назад", f"reportpage:{token}:{page - 1}"))
        if page + 1 < len(report["pages"]):
            navigation.append(("Далее ›", f"reportpage:{token}:{page + 1}"))
        rows = ([navigation] if navigation else []) + [[("‹ Главное", "menu:main")]]
        await self.card(
            "main", "menu", report["pages"][page], rows, new_message=new_message
        )

    async def report(self):
        snapshot = self.report_snapshot()
        png = await asyncio.to_thread(self.report_image, snapshot)
        token = secrets.token_hex(6)
        rows = [
            [
                {"text": "Подробнее", "callback_data": f"reportpage:{token}:0"},
                {"text": "Закрыть", "callback_data": "ui:close"},
            ]
        ]
        msg = await self.tg.call(
            "send_photo",
            chat_id=self.group,
            message_thread_id=self.state.d["topics"]["main"],
            photo=("report.png", png),
            caption=snapshot["caption"],
            reply_markup={"inline_keyboard": rows},
        )
        reports = self.state.d.setdefault("reports", {})
        reports[token] = {"pages": snapshot["pages"], "message_id": msg["message_id"]}
        while len(reports) > 10:
            reports.pop(next(iter(reports)))
        self.register_ui_message(msg["message_id"], "main", "report:" + token)
        self.state.save()
