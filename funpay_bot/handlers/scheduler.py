"""Планировщик заданий и отметка выполнения заказов."""

from __future__ import annotations

import copy
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..metadata import LOG


class SchedulerMixin:
    def schedule(self, section, e):
        if not self.cfg.flag(section):
            return
        row = self.state.d.get("order_cards", {}).get(e["order_id"], {})
        if section in ("CONFIRMREMINDER", "REVIEWREMINDER"):
            if (
                not row.get("fulfilled")
                or row.get("refund_pending")
                or row.get("status") == "REFUNDED"
            ):
                return
            if section == "CONFIRMREMINDER" and row.get("status") != "PAID":
                return
            if section == "REVIEWREMINDER" and row.get("status") != "CLOSED":
                return
        key = section + ":" + e["order_id"]
        if key not in self.state.d["jobs"] and key not in self.state.d["actions"]:
            self.state.d["jobs"][key] = {
                "section": section,
                "event": copy.deepcopy(e),
                "due": time.time() + self.cfg.number(section, "delay_hours", 24) * 3600,
            }

    def completion_message(self, event, details=None):
        values = {**event, **(details or {})}
        text = self.render(self.cfg.get("Fulfillment", "text"), values).strip()
        if not text:
            return ""
        if details and (details.get("receipt_url") or details.get("reference")):
            receipt = self.render(
                self.cfg.get("Fulfillment", "receipt_text"), values
            ).strip()
            if receipt:
                text = text + "\n\n" + receipt if text else receipt
        return text

    async def fulfilled(self, e, *, plugin_id=None, manual_user=None, details=None):
        row = self.state.d.get("order_cards", {}).get(e["order_id"])
        if row and (
            row.get("refund_pending")
            or row.get("status") == "REFUNDED"
            or row.get("fulfilled")
        ):
            return
        if not row and not plugin_id:
            return
        if plugin_id:
            if not self.claim_order(plugin_id, e):
                return
        elif manual_user is None or row.get("plugin"):
            return
        text = self.completion_message(e, details)
        if not text:
            raise ValueError(
                "Задайте текст выполнения заказа в настройках: Автоматизация → Сообщение о выполнении"
            )
        action = (
            f"plugin:{plugin_id}:{e['order_id']}:done"
            if plugin_id
            else "fulfillment:" + e["order_id"]
        )
        self.state.d.setdefault("notification_outbox", {}).setdefault(
            action,
            {
                "event": copy.deepcopy(e),
                "text": text,
                "action": action,
                "status": "retry",
                "due": time.time(),
                "attempts": 0,
            },
        )
        if row is None:
            self.state.save()
            await self.notification_outbox_tick(time.time())
            return
        row["fulfilled"] = {
            "at": time.time(),
            "source": plugin_id or "manual",
            "category": e.get("category") or row["event"].get("category"),
            "user": manual_user,
        }
        row.update(phase="done", progress_text=None)
        if row["fulfilled"]["category"]:
            row["event"]["category"] = row["fulfilled"]["category"]
        self.order_log(
            e["order_id"],
            "✅ Выполнение подтверждено плагином " + plugin_id
            if plugin_id
            else f"✅ Выполнено вручную · администратор {manual_user}",
            "fulfilled",
        )
        self.schedule(
            "REVIEWREMINDER" if row["status"] == "CLOSED" else "CONFIRMREMINDER",
            row["event"],
        )
        self.state.save()
        LOG.info(
            "Заказ #%s выполнен%s",
            e["order_id"],
            " плагином " + plugin_id if plugin_id else " вручную",
        )
        await self.notification_outbox_tick(time.time())

    async def tick(self):
        async with self.lock:
            now = time.time()
            await self.flush_order_cards()
            await self.release_tick()
            await self.flush_order_cards()
            self.state.cleanup_completed(now)
            self.state.save()
            if self.cfg.flag("SellerStatus", "online"):
                for key, job in list(self.state.d["jobs"].items()):
                    row = self.state.d.get("order_cards", {}).get(
                        job["event"]["order_id"], {}
                    )
                    if not row and job["section"] == "REVIEWREMINDER":
                        row = {
                            "fulfilled": True,
                            "event": job["event"],
                            "status": self.state.d.get("ledger", {})
                            .get(job["event"]["order_id"], {})
                            .get("status"),
                        }
                    if not row.get("fulfilled"):
                        del self.state.d["jobs"][key]
                        self.state.save()
                        continue
                    if row.get("refund_pending"):
                        continue
                    if job["due"] > now or not self.cfg.flag(job["section"]):
                        continue
                    current = await self.fp.order_status(job["event"]["order_id"])
                    if not current:
                        continue
                    if current:
                        if current["status"] != row.get("status"):
                            await self.handle_event(
                                {
                                    **row["event"],
                                    "id": "statuscheck:"
                                    + job["event"]["order_id"]
                                    + ":"
                                    + str(time.time_ns()),
                                    "status": current["status"],
                                    "kind": "order",
                                }
                            )
                        cancel = (
                            current["status"] == "REFUNDED"
                            or (
                                job["section"] == "CONFIRMREMINDER"
                                and current["status"] != "PAID"
                            )
                            or (
                                job["section"] == "REVIEWREMINDER"
                                and (
                                    current["status"] != "CLOSED"
                                    or current.get("has_review")
                                )
                            )
                        )
                        if cancel:
                            self.state.d["jobs"].pop(key, None)
                            self.state.save()
                            continue
                    await self.send_buyer(
                        job["event"], self.cfg.get(job["section"], "text"), key
                    )
                    if self.state.d["actions"].get(key, {}).get("status") == "done":
                        self.order_log(
                            job["event"]["order_id"],
                            "⏳ Отправлено напоминание о подтверждении"
                            if job["section"] == "CONFIRMREMINDER"
                            else "⭐ Отправлено напоминание об отзыве",
                            key,
                        )
                    self.state.d["jobs"].pop(key, None)
                    self.state.save()
                if self.cfg.flag("AutoRaise") and now >= self.state.d.get(
                    "raise_scan", 0
                ):
                    # Не опрашиваем профиль каждые две секунды при временном сбое.
                    self.state.d["raise_scan"] = now + 60
                    self.state.save()
                    for category in await self.fp.categories():
                        if now < self.state.d["raise_due"].get(str(category), 0):
                            continue
                        try:
                            wait = await self.fp.raise_lots(category)
                            await self.notify(
                                "auto_raise", f"⬆️ Лоты категории {category} подняты."
                            )
                        except Exception as e:
                            wait = getattr(e, "wait_time", None) or 300
                            # FunPayClient уже зарегистрировал ошибку с её исходным контекстом.
                        self.state.d["raise_due"][str(category)] = now + max(
                            60,
                            float(
                                wait
                                or self.cfg.number(
                                    "AutoRaise", "interval_seconds", 3600
                                )
                            ),
                        )
                        self.state.save()
                    self.state.d["raise_scan"] = now + 60
                    self.state.save()
            first_rates = not self.rates_created and not getattr(
                self, "rates_startup_attempted", False
            )
            if self.cfg.flag("Rates") and (
                first_rates or now >= self.state.d.get("rates_due", 0)
            ):
                self.rates_startup_attempted = True
                self.state.d["rates_due"] = now + max(
                    60, self.cfg.number("Rates", "interval_seconds", 3600)
                )
                self.state.save()
                await self.rates()
            dt = datetime.now(
                ZoneInfo(self.cfg.get("General", "timezone", "Europe/Moscow"))
            )
            week = dt.strftime("%G-%V")
            if (
                self.cfg.flag("WeeklyReport")
                and dt.weekday() == int(self.cfg.number("WeeklyReport", "weekday"))
                and dt.hour >= self.cfg.number("WeeklyReport", "hour", 10)
                and self.state.d.get("report_week") != week
            ):
                await self.report()
                self.state.d["report_week"] = week
                self.state.save()
