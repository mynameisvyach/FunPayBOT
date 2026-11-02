"""Хранение состояния и атомарная запись файлов."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .primitives import state_transaction


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    """Сохраняет темы, защиту от повторов, финансовый журнал и данные восстановления платежей."""

    def __init__(self, path):
        self.path = Path(path)
        self.d = {
            "topics": {},
            "cards": {},
            "events": {},
            "actions": {},
            "greetings": {},
            "orders": {},
            "plugin_disabled": [],
            "last_human": 0,
            "raise_due": {},
            "jobs": {},
            "tg_offset": 0,
            "inbox": {},
            "ledger": {},
            "withdrawals": {},
            "plugin_state": {},
        }
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Повреждён state.json")
            self.d.update(data)
        self.d.setdefault("ticket_last_attempt", 0)
        self.save()

    def save(self):
        atomic_write(self.path, json.dumps(self.d, ensure_ascii=False, indent=2))

    @staticmethod
    def message_identity(identity):
        """Выделяет числовые идентификаторы чата и сообщения FunPay."""
        parts = str(identity).split(":")
        if (
            len(parts) == 3
            and parts[0] == "m"
            and parts[1].isdigit()
            and parts[2].isdigit()
        ):
            return parts[1], int(parts[2])
        return None

    def event_seen(self, event):
        """Проверяет точные квитанции событий, включая сообщения вне порядка поступления."""
        identity = event["id"]
        if identity in self.d["events"]:
            return True
        message = self.message_identity(identity)
        if message:
            chat, mid = message
            return any(
                start <= mid <= end
                for start, end in self.d.get("message_receipts", {}).get(chat, [])
            )
        return False

    def finish_event(self, event):
        """Сохраняет минимальную защиту от повторов без текста обработанного события."""
        import time

        message = self.message_identity(event["id"])
        if not message:
            self.d["events"][event["id"]] = time.time()
            return
        chat, mid = message
        receipts = self.d.setdefault("message_receipts", {})
        intervals = sorted(receipts.get(chat, []) + [[mid, mid]])
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        receipts[chat] = merged
        self.d["events"].pop(event["id"], None)

    def cleanup_completed(self, now=None):
        """Удаляет завершённый кэш, сохраняя финансы, незавершённые действия и точную защиту от повторов."""
        import time

        now = time.time() if now is None else now
        removed = 0
        ledger = self.d.get("ledger", {})
        actions = self.d.get("actions", {})
        inbox = self.d.get("inbox", {})
        outbox = self.d.get("notification_outbox", {})
        busy_orders = set()
        for key, row in actions.items():
            if row.get("status") in ("pending", "unknown"):
                busy_orders.update(order for order in ledger if order in key.split(":"))
        for row in outbox.values():
            if row.get("status") not in ("done", "cancelled"):
                busy_orders.add(str(row.get("event", {}).get("order_id", "")))
        for order, row in list(self.d.get("order_cards", {}).items()):
            if row.get("status") not in ("CLOSED", "REFUNDED") or order in busy_orders:
                continue
            if row.get("rendered_revision") != row.get("revision"):
                continue
            if not self.d.get("cards", {}).get("orders/order:" + order):
                continue
            ledger.get(order, {}).setdefault("handled_status", row["status"])
            for section in (
                "order_cards",
                "orders",
                "ticket_reviews",
                "completed_order_stats",
            ):
                if self.d.get(section, {}).pop(order, None) is not None:
                    removed += 1
        for order, row in list(self.d.get("orders", {}).items()):
            if (
                row.get("status") in ("CLOSED", "REFUNDED")
                and order not in busy_orders
                and order not in self.d.get("order_cards", {})
            ):
                self.d["orders"].pop(order)
                removed += 1
        for key, row in list(outbox.items()):
            if row.get("status") in ("done", "cancelled"):
                outbox.pop(key)
                removed += 1
        for identity in list(self.d["events"]):
            if identity in inbox:
                continue
            if self.message_identity(identity):
                self.finish_event({"id": identity})
                removed += 1
            elif str(identity).startswith(("o:", "statuscheck:")):
                parts = str(identity).split(":")
                if len(parts) > 1 and parts[1] in ledger:
                    if len(parts) > 2 and parts[2] == ledger[parts[1]].get("status"):
                        ledger[parts[1]].setdefault("handled_status", parts[2])
                    self.d["events"].pop(identity)
                    removed += 1
        active_plugin_jobs = {}
        terminal = {
            "answered",
            "answered_by_owner",
            "superseded",
            "unmatched",
            "disabled",
        }
        for plugin_id, plugin in self.d.get("plugin_state", {}).items():
            jobs = plugin.get("jobs", {})
            if not isinstance(jobs, dict):
                continue
            active_plugin_jobs[plugin_id] = set()
            for token, row in list(jobs.items()):
                if not isinstance(row, dict):
                    continue
                if row.get("status") in terminal:
                    jobs.pop(token)
                    removed += 1
                else:
                    active_plugin_jobs[plugin_id].add(token)
            for chat, token in list(plugin.get("latest", {}).items()):
                if token not in jobs:
                    plugin["latest"].pop(chat)
                    removed += 1
        for key, row in list(actions.items()):
            if row.get("status") != "done":
                continue
            if key in self.d.get("jobs", {}) or key in outbox:
                continue
            parts = key.split(":", 2)
            plugin_id = parts[1] if len(parts) == 3 and parts[0] == "plugin" else None
            if plugin_id and key.rsplit(":", 1)[-1] in active_plugin_jobs.get(
                plugin_id, set()
            ):
                continue
            order = next((value for value in ledger if value in key.split(":")), None)
            message_action = key.startswith(
                (
                    "greeting:",
                    "response:",
                    "status:",
                    "REVIEWREPLY:",
                    "REVIEWCHATREPLY:",
                    "template:",
                    "tg:",
                    "owner:",
                )
            )
            if plugin_id and parts[2].startswith(
                ("reply:", "holding:", "simple:", "unmatched:", "handoff:", "chat:")
            ):
                message_action = True
            finished_order = (
                order
                and ledger[order].get("status") in ("CLOSED", "REFUNDED")
                and order not in busy_orders
            )
            if not message_action and not finished_order:
                continue
            # Текущий входящий пакет должен полностью завершиться перед удалением квитанции.
            if any(identity in key for identity in inbox):
                continue
            actions.pop(key)
            removed += 1
        live_outgoing = self.d.get("outgoing_messages", {})
        for identity, row in list(live_outgoing.items()):
            if identity not in inbox and self.event_seen({"id": identity}):
                live_outgoing.pop(identity)
                removed += 1
        for plugin in self.d.get("plugin_state", {}).values():
            for order, row in list(plugin.get("orders", {}).items()):
                if order in busy_orders or ledger.get(order, {}).get("status") not in (
                    "CLOSED",
                    "REFUNDED",
                ):
                    continue
                if row.get("phase", row.get("status")) in (
                    "done",
                    "refunded",
                    "cancelled",
                ):
                    if "quantity" in row:
                        ledger[order].setdefault(
                            "delivery_units",
                            int(row["quantity"]) * int(row.get("packs", 1)),
                        )
                    plugin["orders"].pop(order)
                    removed += 1
        for plugin in self.d.get("plugin_state", {}).values():
            for key, row in list(plugin.get("helper_messages", {}).items()):
                if row.get("status") == "shown":
                    plugin["helper_messages"].pop(key)
                    removed += 1
        # Квитанции отправки уже отражены в Telegram; повторные update отсекаются tg_offset.
        removed += len(self.d.get("send_receipts", {}))
        self.d.get("send_receipts", {}).clear()
        return removed

    def cleanup_startup(self, now=None):
        """При каждом запуске удаляет лишние интерфейсные ссылки и завершённые рабочие данные."""
        import time

        now = time.time() if now is None else now
        removed = self.cleanup_completed(now)
        topics, cards = self.d.get("topics", {}), self.d.get("cards", {})
        for key in list(cards):
            if key.split("/", 1)[0] not in topics:
                cards.pop(key)
                removed += 1
        ui = self.d.get("ui_messages", {})
        for mid, row in list(ui.items()):
            if not isinstance(row, dict) or row.get("topic") not in topics:
                ui.pop(mid)
                removed += 1
        reports = self.d.get("reports", {})
        for key in list(reports)[:-10]:
            reports.pop(key)
            removed += 1
        for mid, row in list(ui.items()):
            if (
                row.get("key", "").startswith("report:")
                and row["key"][7:] not in reports
            ):
                ui.pop(mid)
                removed += 1
        messages = self.d.get("message_callbacks", {})
        for mid in list(messages):
            if mid not in ui:
                messages.pop(mid)
                removed += 1
        references = self.d.get("card_callbacks", {})
        for key in list(references):
            if key not in cards:
                references.pop(key)
                removed += 1
        live = {
            value
            for values in list(references.values()) + list(messages.values())
            if isinstance(values, list)
            for value in values
        }
        for key in list(self.d.get("callback_registry", {})):
            if key not in live:
                self.d["callback_registry"].pop(key)
                removed += 1
        for section in (
            "topic_titles",
            "topic_icon_ids",
            "plugin_menu_pins",
            "plugin_menu_closed",
        ):
            for key in list(self.d.get(section, {})):
                if key not in topics:
                    self.d[section].pop(key)
                    removed += 1
        for section, field in (("rmthub", "cached_at"), ("market_rates", "at")):
            row = self.d.get(section)
            if isinstance(row, dict) and now - row.get(field, now) > 7 * 86400:
                self.d.pop(section)
                removed += 1
        if removed:
            self.save()
        return removed

    def transaction(self):
        return state_transaction(self)


class SingleProcess:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "process.lock").open("a+")
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Уже запущен другой экземпляр с этим state.json")
        else:
            import msvcrt

            self.file.seek(0)
            self.file.write("0")
            self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Уже запущен другой экземпляр")
