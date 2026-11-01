"""Debug application handlers."""

from __future__ import annotations

import logging
import time

from ..metadata import LOG, human_time


class DebugMixin:
    def capture_debug_event(self, event):
        if not self.debug:
            return
        allowed = (
            "kind",
            "id",
            "order_id",
            "status",
            "category",
            "currency",
            "amount",
            "seller",
            "system",
            "employee",
        )
        rows = self.state.d.setdefault("debug_events", [])
        rows.append({key: event[key] for key in allowed if key in event})
        rows[-1]["observed_at"] = time.time()
        del rows[:-200]

    async def debug_card(self):
        pending = sum(a["status"] != "done" for a in self.state.d["actions"].values())
        poll = getattr(self.fp, "last_poll", {})
        fp_status = (
            ("последний ответ " + human_time(self.cfg, poll["at"]))
            if poll
            else "ответов ещё нет"
        )
        await self.card(
            "main",
            "debug",
            f"🐞 Live debug · {'ВКЛ' if self.debug else 'ВЫКЛ'}\nСобытия: {self.counts['events']} · Дубли: {self.counts['duplicates']} · Ошибки: {self.counts['errors']}\nТребуют проверки: {pending}\nFunPay: {fp_status}\nНеобработанных событий: {len(self.state.d['inbox'])} · ожидают историю: {poll.get('history_pending', 0)}\nПоследнее: {self.last_event}\nПлагины: {len(self.plugins)} · Таймеры: {len(self.state.d['jobs'])}",
            [[("Выгрузить события", "core_debug:export")]],
        )

    async def toggle_debug(self):
        self.debug = not self.debug
        self.cfg.set("Debug", "enabled", self.debug)
        for handler in LOG.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        LOG.info("Debug %s", self.debug)
        await self.debug_card()
        if not self.debug:
            await self.file_to_main("debug.log", self.state.path.parent / "log.log")
        await self.menu()
