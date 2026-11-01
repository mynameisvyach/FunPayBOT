"""Funpay services."""

from __future__ import annotations

import asyncio
import html
import io
import json
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

from .finance import money
from .lot_tools import export_images, validate_images
from .metadata import LOG, ROOT


class SupportTickets:
    def __init__(self, adapter):
        self.adapter = adapter

    async def send(self, orders, message):
        def submit():
            from urllib.parse import parse_qs, urlparse

            import requests
            from bs4 import BeautifulSoup

            account = self.adapter.account
            session = requests.Session()
            if account.proxy:
                session.proxies.update(account.proxy)
            session.headers["User-Agent"] = account.user_agent or "Mozilla/5.0"
            session.cookies.set(
                "golden_key", account.golden_key, domain="funpay.com", secure=True
            )
            r = session.get(
                "https://funpay.com/support/sso?return_to=%2Ftickets%2Fnew",
                allow_redirects=False,
                timeout=20,
            )
            location = r.headers.get("Location", "")
            url = urlparse(location)
            jwt = parse_qs(url.query).get("jwt", [""])[0]
            if url.scheme != "https" or url.hostname != "support.funpay.com" or not jwt:
                raise ValueError("FunPay не подтвердил вход в поддержку")
            session.get(
                "https://support.funpay.com/access/jwt",
                params={"jwt": jwt, "return_to": "/tickets/new"},
                timeout=20,
            ).raise_for_status()
            form = session.get("https://support.funpay.com/tickets/new/1", timeout=20)
            form.raise_for_status()
            node = BeautifulSoup(form.text, "lxml").select_one(
                'input[name="ticket[_token]"]'
            )
            if not node or not node.get("value"):
                raise ValueError("Изменился формат формы поддержки; тикет не отправлен")
            payload = {
                "ticket[fields][1]": account.username,
                "ticket[fields][2]": orders[0],
                "ticket[fields][3]": "2",
                "ticket[fields][5]": "201",
                "ticket[comment][body_html]": "<p>" + html.escape(message) + "</p>",
                "ticket[comment][attachments]": "",
                "ticket[_token]": node["value"],
                "ticket[submit]": "Отправить",
            }
            result = session.post(
                "https://support.funpay.com/tickets/create/1", data=payload, timeout=25
            )
            result.raise_for_status()
            match = re.search(r"/tickets/(\d+)(?:[/?#]|$)", result.url)
            if not match:
                raise ValueError(
                    "Поддержка не подтвердила создание тикета; результат требует проверки"
                )
            return match.group(1)

        return await self.adapter.invoke(submit)


class FunPayOperations:
    async def old_paid_orders(self):
        def read():
            cursor = None
            seen = set()
            orders = []
            for _ in range(100):
                page = self.account.get_sales(start_from=cursor, state="paid")
                for o in page[1]:
                    if o.date.timestamp() <= time.time() - 86400:
                        orders.append(o.id)
                cursor = page[0]
                if cursor is None or cursor in seen or len(orders) >= 100:
                    break
                seen.add(cursor)
            return orders[:100]

        return await self.invoke(read)

    async def ticket_context(self, key):
        def read():
            order = self.account.get_order(key)
            if order.seller_id != self.account.id:
                raise ValueError("Не наша продажа")
            chat = self.resolve_chat(order.chat_id, order.buyer_username)
            messages = []
            seen = set()
            cursor = None
            complete = False
            for _ in range(100):
                page = self.account.get_chat_history(
                    self.chat_id(chat),
                    last_message_id=cursor,
                    interlocutor_username=order.buyer_username,
                )
                if not page:
                    complete = True
                    break
                fresh = [m for m in page if m.id not in seen]
                if not fresh:
                    break
                for m in fresh:
                    seen.add(m.id)
                    messages.append(
                        {
                            "id": m.id,
                            "text": m.text or "",
                            "seller": m.author_id == self.account.id,
                            "system": m.author_id == 0,
                            "employee": m.is_employee,
                        }
                    )
                oldest = min(m.id for m in fresh)
                if cursor == oldest:
                    break
                cursor = oldest
            messages.sort(key=lambda m: m["id"])
            return {
                "order_id": key,
                "status": order.status.name,
                "chat": str(chat),
                "complete": complete,
                "messages": messages,
            }

        return await self.invoke(read)

    async def send_ticket(self, orders, message):
        return await SupportTickets(self).send(orders, message)

    async def balance(self, cfg):
        def read():
            lot_id = int(cfg.get("Finance", "balance_lot_id", "0")) or getattr(
                self, "balance_probe", 0
            )
            if not lot_id:
                common = next(
                    (
                        v
                        for k, v in self.account.get_sorted_subcategories().items()
                        if k.name == "COMMON"
                    ),
                    {},
                )
                for category in list(common)[:12]:
                    subcategory = common[category]
                    offers = self.account.get_subcategory_public_lots(
                        subcategory.type, int(category)
                    )
                    if offers:
                        lot_id = int(
                            next(iter(offers.values())).id
                            if isinstance(offers, dict)
                            else offers[0].id
                        )
                        break
            if not lot_id:
                raise ValueError(
                    "Не удалось найти доступную публичную страницу для чтения баланса. Повторите проверку позже."
                )
            try:
                b = self.account.get_balance(lot_id)
            except Exception:
                self.balance_probe = 0
                raise
            self.balance_probe = lot_id
            return {
                "total": str(money(b.total_rub)),
                "available": str(money(b.available_rub)),
                "usd": str(money(b.total_usd)),
                "available_usd": str(money(b.available_usd)),
                "eur": str(money(b.total_eur)),
                "available_eur": str(money(b.available_eur)),
            }

        return await self.invoke(read)

    async def withdraw_data(self):
        def read():
            from bs4 import BeautifulSoup

            r = self.account.method(
                "get", "account/balance", {}, {}, raise_not_200=True
            )
            box = BeautifulSoup(r.text, "lxml").select_one("div.withdraw-box")
            if not box:
                raise ValueError("FunPay не вернул форму вывода")
            data = json.loads(box["data-data"])
            if not isinstance(data.get("currencies"), dict) or not isinstance(
                data.get("extCurrencies"), dict
            ):
                raise ValueError("Изменился формат способов вывода FunPay")
            return data

        return await self.invoke(read)

    @staticmethod
    def withdrawal_specs(data):
        result = {}
        for channel in data["currencies"].get("rub", {}).get("channels", []):
            key = channel["extCurrency"]
            ext = data["extCurrencies"].get(key, {})
            label = (
                str(key)
                + " "
                + str(channel.get("name", ""))
                + " "
                + str(ext.get("name", ""))
            ).lower()
            if "usdt" in label and "trc20" in re.sub(r"[^a-z0-9]", "", label):
                kind = "usdt_trc20"
                currency = "USDT"
            elif key == "card_rub":
                kind = "card"
                currency = "RUB"
            elif "сбп" in label or "sbp" in label:
                kind = "sbp"
                currency = "RUB"
            else:
                continue
            result[key] = {
                "kind": kind,
                "currency": currency,
                "name": channel.get("name", ext.get("name", key)),
                "wallets": ext.get("wallets", []),
            }
        return result

    async def withdraw_methods(self):
        data = await self.withdraw_data()
        self.withdrawal_options = self.withdrawal_specs(data)
        return [
            (key, spec["name"], wallet)
            for key, spec in self.withdrawal_options.items()
            for wallet in spec["wallets"]
            if isinstance(wallet, str)
        ]

    async def withdraw_request(self, cfg, payload):
        def send():
            data = {
                "csrf_token": self.account.csrf_token,
                "currency_id": "rub",
                "ext_currency_id": cfg.get("AutoWithdraw", "method"),
                "wallet": cfg.get("AutoWithdraw", "wallet"),
                **payload,
            }
            r = self.account.method(
                "post",
                "withdraw/withdraw",
                {"x-requested-with": "XMLHttpRequest"},
                data,
                raise_not_200=True,
            )
            value = r.json()
            if value.get("error") or value.get("errors"):
                raise ValueError("FunPay отклонил операцию вывода")
            return value

        return await self.invoke(send)

    async def withdraw_quote(self, cfg, debit):
        specs = self.withdrawal_specs(await self.withdraw_data())
        spec = specs.get(cfg.get("AutoWithdraw", "method"))
        if not spec or cfg.get("AutoWithdraw", "wallet") not in spec["wallets"]:
            raise ValueError("Выберите доступный способ и сохранённые реквизиты FunPay")
        response = await self.withdraw_request(
            cfg, {"preview": "1", "amount_int": str(debit)}
        )
        return {**response, "_spec": spec}

    async def withdraw_submit(self, cfg, receive):
        return await self.withdraw_request(
            cfg, {"preview": "", "amount_ext": str(receive)}
        )

    async def sales_periods(self):
        cached = getattr(self, "sales_period_cache", None)
        if cached and time.time() - cached["at"] < 600:
            return cached

        def read():
            cursor = None
            seen = set()
            rows = []
            complete = False
            cutoff = time.time() - 30 * 86400
            for _ in range(100):
                page = self.account.get_sales(start_from=cursor)
                stop = False
                for order in page[1]:
                    if order.id in seen:
                        continue
                    seen.add(order.id)
                    stamp = order.date.timestamp()
                    if stamp < cutoff:
                        stop = True
                        continue
                    rows.append(
                        {
                            "id": order.id,
                            "at": stamp,
                            "status": order.status.name,
                            "currency": order.currency.name,
                            "price": str(order.price),
                        }
                    )
                cursor = page[0]
                if stop or not cursor:
                    complete = True
                    break
            result = {"at": time.time(), "rows": rows, "complete": complete}
            self.sales_period_cache = result
            return result

        return await self.invoke(read)

    async def profile(self):
        p = await self.invoke(self.account.get_user, self.account.id)
        text = f"👤 {p.username}\nID: {p.id}\nЛоты: {len(p.get_lots())}\nБлокировка: {'да' if p.banned else 'нет'}\nАктивные продажи: {self.account.active_sales}\nhttps://funpay.com/users/{p.id}/"
        try:
            balance = await self.balance(self.cfg)
            text += f"\n\nБаланс / доступно: RUB {balance['total']} / {balance['available']}; USD {balance.get('usd', '—')} / {balance.get('available_usd', '—')}; EUR {balance.get('eur', '—')} / {balance.get('available_eur', '—')}"
        except Exception:
            LOG.exception("Баланс расширенного профиля не обновлён")
            text += "\nБаланс временно не обновлён."
        try:
            sales = await self.sales_periods()
            now = time.time()
            text += "\n\nЗаказы по дате создания (не расчёт прибыли):"
            for days in (1, 7, 30):
                selection = [
                    row for row in sales["rows"] if now - row["at"] <= days * 86400
                ]
                groups = {}
                for row in selection:
                    group = groups.setdefault(
                        (row["status"], row["currency"]), [0, Decimal("0")]
                    )
                    group[0] += 1
                    group[1] += money(row["price"])
                text += f"\n{days} дн.: " + (
                    "; ".join(
                        f"{status}/{currency}: {values[0]} на {values[1]:.2f}"
                        for (status, currency), values in sorted(groups.items())
                    )
                    or "нет заказов"
                )
            if not sales["complete"]:
                text += "\n⚠ Скан ограничен 100 страницами, суммы могут быть неполными."
        except Exception:
            LOG.exception("Продажи расширенного профиля не обновлены")
            text += "\nСтатистика продаж временно недоступна."
        return text

    async def rating(self):
        def read():
            from bs4 import BeautifulSoup

            r = self.account.method("get", "sras/info", {}, {}, raise_not_200=True)
            soup = BeautifulSoup(r.text, "lxml")
            body = soup.find("tbody")
            if not body:
                note = soup.find("p", class_="text-bold")
                if note:
                    return "⚖ Ограничения рейтинга\n" + note.get_text(" ", strip=True)
                raise ValueError("Не удалось распознать ограничения FunPay")
            return "⚖ Ограничения рейтинга\n" + "\n".join(
                tr.get_text(" ", strip=True) for tr in body.find_all("tr")
            )

        return await self.invoke(read)

    async def verify_order(self, order):
        o = await self.invoke(self.account.get_order, order)
        if o.seller_id != self.account.id:
            raise ValueError("Заказ принадлежит другому продавцу")
        # Use order shortcut for seller credit rather than buyer gross price.
        return {
            "status": o.status.name,
            "currency": o.currency.name,
            "chat": self.resolve_chat(o.chat_id, o.buyer_username),
        }

    async def refund_order(self, order):
        await self.invoke(self.account.refund, order)

    async def lot_export(self, include_secrets=False):
        def read():
            result = []
            self.lot_export_errors = []
            for lot in self.account.get_user(self.account.id).get_lots():
                if lot.subcategory.type.name != "COMMON":
                    continue
                try:
                    fields = self.account.get_lot_fields(int(lot.id))
                    clean = {
                        k: v
                        for k, v in fields.renew_fields().fields.items()
                        if k not in ("csrf_token", "offer", "secrets")
                    }
                    clean["active"] = ""
                    clean["auto_delivery"] = ""
                    clean["fields[images]"] = ""
                    row = {
                        "id": lot.id,
                        "category": lot.subcategory.id,
                        "fields": clean,
                        "images": export_images(self.account, lot.id),
                    }
                    if include_secrets:
                        row["warehouse_secrets"] = list(fields.secrets)
                    result.append(row)
                except Exception:
                    self.lot_export_errors.append(str(lot.id))
                    LOG.exception("Экспорт лота %s не завершён", lot.id)
            return result

        return await self.invoke(read)

    async def lot_import_one(self, row):
        def create():
            from FunPayAPI.types import LotFields

            category = int(row["category"])
            images = validate_images(row.get("images", []))
            secrets_ = row.get("warehouse_secrets", [])
            if not isinstance(secrets_, list) or any(
                not isinstance(v, str) for v in secrets_
            ):
                raise ValueError("Некорректный склад")
            template = self.account.get_lot_fields(0, category)
            clean = {
                k: v
                for k, v in row["fields"].items()
                if (k.startswith("fields[") and k != "fields[images]")
                or k in ("price", "amount", "deactivate_after_sale")
            }
            clean.update(
                offer="0",
                node=str(category),
                active="",
                auto_delivery="",
                secrets="",
                **{"fields[images]": ""},
            )
            for k, v in clean.items():
                template.fields[k] = v
            new = LotFields(
                0,
                dict(template.fields),
                template.subcategory,
                template.currency,
                template.calc_result,
            )
            new.active = False
            new.auto_delivery = False
            new.secrets = secrets_
            new.images = [
                self.account.upload_image(io.BytesIO(body), type_="offer")
                for body in images
            ]
            self.account.save_lot(new)

        await self.invoke(create)

    async def lot_update(
        self,
        lot_id,
        *,
        price=None,
        active=None,
        amount=None,
        deactivate_after_sale=None,
    ):
        def save():
            owned = self.account.get_user(self.account.id).get_lot(lot_id)
            if not owned:
                raise ValueError("Нет собственного лота с этим ID")
            f = self.account.get_lot_fields(lot_id)
            if price is not None:
                f.price = float(money(price))
            if active is not None:
                f.active = bool(active)
            if amount is not None:
                f.amount = int(amount)
            if deactivate_after_sale is not None:
                f.deactivate_after_sale = bool(deactivate_after_sale)
            self.account.save_lot(f)

        await self.invoke(save, _lot_request=True)

    async def lot_floor(self, lot_id):
        def read():
            fields = self.account.get_lot_fields(lot_id)
            # seller price / buyer price. Actual unit price already belongs to the seller.
            calc = fields.calc_result or self.account.calc(
                fields.subcategory.type, fields.subcategory.id, price=1000
            )
            coefficient = Decimal(str(calc.commission_coefficient))
            if coefficient <= 0:
                raise ValueError("Неизвестна комиссия категории")
            return {
                "price": str(fields.price),
                "active": fields.active,
                "title": fields.title_ru,
                "category": fields.subcategory.id,
                "category_rate": str(Decimal("1") / coefficient),
            }

        return await self.invoke(read, _lot_request=True)


class DemoFunPay:
    mode = "demo"

    def __init__(self):
        self.sent = []

    async def start(self):
        pass

    async def poll(self):
        return []

    async def send(self, chat, text):
        self.sent.append(("message", chat, text))

    async def image(self, chat, content):
        self.sent.append(("image", chat, len(content)))

    async def review(self, order, text):
        self.sent.append(("review", order, text))

    async def categories(self):
        return [100, 200]

    async def raise_lots(self, category):
        self.sent.append(("raise", category))
        return 3600

    async def order_status(self, order):
        return None

    async def lots(self, public_category=None):
        return [
            {
                "id": 501,
                "category": 100,
                "description": "Демо-лот",
                "price": "100",
                "currency": "RUB",
            }
        ]

    async def delete_lot(self, lot_id):
        self.sent.append(("delete_lot", lot_id))

    async def old_paid_orders(self):
        return []

    async def send_ticket(self, orders, message):
        return "demo-ticket"

    async def balance(self, cfg):
        return {"total": "1000.00", "available": "1000.00"}

    async def withdraw_methods(self):
        return [("card_rub", "Демо-карта", "0000000000000000")]

    async def withdraw_quote(self, cfg, debit):
        return {"amount_ext": str(money(money(debit) * Decimal(".97")))}

    async def withdraw_submit(self, cfg, receive):
        return {
            "amount_int": str(money(money(receive) / Decimal(".97"))),
            "amount_ext": str(receive),
        }

    async def profile(self):
        return "👤 DemoSeller · демонстрационные данные"

    async def rating(self):
        return "⚖ Демонстрация: ограничения не запрашивались"

    async def verify_order(self, order):
        return {"status": "PAID", "currency": "RUB", "chat": "101"}

    async def refund_order(self, order):
        self.sent.append(("refund", order))

    async def lot_export(self):
        return [
            {
                "id": 501,
                "category": 100,
                "fields": {"price": "100", "fields[summary][ru]": "Демо-лот"},
            }
        ]

    async def lot_import_one(self, row):
        self.sent.append(("import", row["category"]))

    async def lot_update(self, lot_id, **kw):
        self.sent.append(("lot_update", lot_id, kw))

    async def lot_floor(self, lot_id):
        return {
            "price": "100",
            "active": True,
            "title": "50 единиц",
            "category": 2418,
            "category_rate": "0.9",
        }


class FunPayClient(FunPayOperations):
    mode = "live"

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.account = self.runner = None
        self.pending_chats = {}
        self.pending_events = {}
        self.poll_heartbeat = 0
        self.last_poll = {}
        self.session_refreshed = time.monotonic()

    async def invoke(self, func, *args, **kw):
        async with self.lock:
            lot_request = kw.pop("_lot_request", False)
            if lot_request:
                await asyncio.sleep(
                    max(0, getattr(self, "lot_request_due", 0) - time.monotonic())
                )
                self.lot_request_due = time.monotonic() + 2
            start = time.monotonic()
            try:
                result = await asyncio.to_thread(func, *args, **kw)
                LOG.debug(
                    "FunPay operation=%s duration_ms=%.0f result=ok",
                    func.__name__,
                    (time.monotonic() - start) * 1000,
                )
                return result
            except Exception as e:
                import traceback

                frames = [
                    f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                    for f in traceback.extract_tb(e.__traceback__)
                ]
                LOG.exception(
                    "Ошибка FunPay: операция=%s, время=%.0f мс",
                    func.__name__,
                    (time.monotonic() - start) * 1000,
                )
                raise

    async def start(self):
        from .bootstrap import verify_api

        sys.path.insert(0, str(ROOT / "vendor"))
        verify_api()
        from FunPayAPI import Account
        from requests.adapters import HTTPAdapter

        proxy = self.cfg.get("FunPay", "proxy")
        if not self.cfg.get("FunPay", "golden_key"):
            raise ValueError("Не задан golden_key")
        self.account = Account(
            self.cfg.get("FunPay", "golden_key"),
            user_agent=self.cfg.get("FunPay", "user_agent") or None,
            requests_timeout=20,
            proxy={"http": proxy, "https": proxy} if proxy else None,
        )
        # This fork retries POST by default. Disable retries to avoid duplicate writes.
        self.account.session.mount("https://", HTTPAdapter(max_retries=0))
        await self.invoke(self.account.get)
        self.runner = self.make_runner()
        LOG.info(
            "FunPay авторизован: account_id=%s. Получаю начальный снимок до создания Telegram-меню",
            self.account.id,
        )
        self.initial_events = await self.poll()
        LOG.info(
            "Начальная синхронизация FunPay завершена: чатов=%s. Старые сообщения в Telegram не отправляются",
            sum(e["kind"] == "baseline" for e in self.initial_events),
        )

    def make_runner(self):
        from FunPayAPI.updater.runner import Runner

        class DirectRunner(Runner):
            # This fork's get_result waits on its separate, endless runner_loop.
            # Our adapter already serializes requests: send directly instead.
            def get_result(self, payload):
                return self.account.runner_request(payload)

            def generate_new_message_events(self, chats_data, chats=None):
                # Unknown chats may contain old history outside the initial list.
                # Only mirror their latest message on first discovery.
                for cid in chats_data:
                    if cid not in self.last_messages_ids:
                        latest = self.runner_last_messages.get(cid, [0])[0]
                        if latest:
                            self.last_messages_ids[cid] = max(0, latest - 1)
                return super().generate_new_message_events(chats_data, chats)

        return DirectRunner(self.account)

    def normalize_event(self, event):
        result = []
        name = type(event).__name__
        if name == "InitialChatEvent":
            result.append({"kind": "baseline", "chat": str(event.chat.id)})
        elif name == "NewMessageEvent":
            m = event.message
            result.append(
                {
                    "kind": "message",
                    "id": f"m:{m.chat_id}:{m.id}",
                    "chat": str(m.chat_id),
                    "username": m.chat_name or str(m.chat_id),
                    "buyer_id": m.interlocutor_id,
                    "text": m.text or "",
                    "photo": m.image_link,
                    "seller": m.author_id == self.account.id,
                    "system": m.author_id == 0
                    or m.is_employee
                    or getattr(m.type, "name", "") != "NON_SYSTEM",
                    "by_bot": bool(m.by_bot),
                    "employee": bool(m.is_employee),
                    "author": getattr(m, "author", None),
                    "support_chat": bool(
                        str(m.chat_name).casefold()
                        in ("support", "поддержка", "funpay support")
                        or m.is_employee
                        and m.interlocutor_id == m.author_id
                        and m.author_id != 0
                    ),
                }
            )
            mt = getattr(m.type, "name", "")
            if m.author_id == 0 and mt in (
                "NEW_FEEDBACK",
                "FEEDBACK_CHANGED",
                "FEEDBACK_DELETED",
            ):
                match = re.search(r"#([A-Z0-9]{8})", m.text or "")
                if match:
                    order = self.account.get_order(match[1])
                    if order.seller_id == self.account.id:
                        result.append(
                            {
                                "kind": "review",
                                "id": f"r:{m.chat_id}:{m.id}",
                                "chat": str(m.chat_id),
                                "username": order.buyer_username,
                                "order_id": order.id,
                                "buyer_id": order.buyer_id,
                                "rating": order.review.stars if order.review else 6,
                                "changed": mt == "FEEDBACK_CHANGED",
                            }
                        )
        elif name in ("NewOrderEvent", "OrderStatusChangedEvent"):
            o = event.order
            # Runner also observes purchases. Never automate those as sales.
            if o.buyer_id == self.account.id:
                return result
            details = self.account.get_order(o.id) if o.status.name == "PAID" else None
            event_id = f"o:{o.id}:{o.status.name}"
            if name == "OrderStatusChangedEvent":
                event_id += ":" + str(time.time_ns())
            result.append(
                {
                    "kind": "order",
                    "id": event_id,
                    "chat": self.resolve_chat(o.chat_id, o.buyer_username),
                    "username": o.buyer_username,
                    "buyer_id": o.buyer_id,
                    "order_id": o.id,
                    "status": o.status.name,
                    "description": o.description,
                    "category": str(o.subcategory.id) if o.subcategory else "",
                    "price": str(o.price),
                    "currency": o.currency.name,
                    "amount": o.amount,
                    "player": getattr(details, "player", None),
                    "buyer_params": {
                        key: getattr(value, "value", "")
                        for key, value in getattr(details, "fields", {}).items()
                        if any(
                            token in key.casefold()
                            for token in (
                                "buyer",
                                "player",
                                "recipient",
                                "username",
                                "account",
                                "uid",
                            )
                        )
                    },
                }
            )
        return result

    def read_events(self):
        if time.monotonic() - self.session_refreshed >= 3600:
            LOG.info("Периодическое обновление сессии FunPay")
            self.account.get()
            self.session_refreshed = time.monotonic()
            LOG.info("Сессия FunPay обновлена")
        raw = self.runner.get_updates()
        if not isinstance(raw, dict) or not isinstance(raw.get("objects"), list):
            raise ValueError("FunPay Runner вернул ответ без списка objects")
        objects = raw["objects"]
        types = [obj.get("type", "?") for obj in objects]
        LOG.debug("Runner: получен ответ, объектов=%s, типы=%s", len(objects), types)
        result = []
        events = []
        # Preserve API batch initialization; normalize each returned event independently.
        try:
            events = self.runner.parse_updates(objects)
        except Exception:
            LOG.exception("Ошибка парсера Runner: типы объектов=%s", types)
            raise
        for event in events:
            name = type(event).__name__
            if name == "LastChatMessageChangedEvent":
                chat = event.chat
                if chat.node_msg_id > self.runner.last_messages_ids.get(chat.id, 0):
                    self.pending_chats[chat.id] = (chat.name, time.monotonic() + 15)
            order = getattr(event, "order", None)
            key = (
                ("order:" + str(order.id))
                if order is not None
                else name
                + ":"
                + str(getattr(getattr(event, "message", None), "id", id(event)))
            )
            try:
                result.extend(self.normalize_event(event))
                self.pending_events.pop(key, None)
            except Exception:
                LOG.exception(
                    "Не удалось преобразовать событие Runner: %s; запланирована повторная обработка",
                    key,
                )
                self.pending_events[key] = (event, time.monotonic() + 15)
        for key, (event, due) in list(self.pending_events.items()):
            if time.monotonic() < due:
                continue
            try:
                result.extend(self.normalize_event(event))
                del self.pending_events[key]
            except Exception:
                self.pending_events[key] = (event, time.monotonic() + 60)
                LOG.exception("Повторная обработка события Runner не удалась: %s", key)
        if self.pending_chats:
            # Runner has already advanced the bookmark, even when fetching history failed.
            # Retry just those changed chats, without replaying startup history.
            retry = {
                cid: name
                for cid, (name, due) in self.pending_chats.items()
                if time.monotonic() >= due
            }
            if retry:
                LOG.warning("Ожидается история новых сообщений: чаты=%s", list(retry))
            recovered = self.runner.generate_new_message_events(retry) if retry else {}
            for cid, new_events in recovered.items():
                for event in new_events:
                    try:
                        result.extend(self.normalize_event(event))
                    except Exception:
                        key = "NewMessageEvent:" + str(event.message.id)
                        self.pending_events[key] = (event, time.monotonic() + 15)
                        LOG.exception(
                            "Не удалось преобразовать восстановленное сообщение: %s",
                            key,
                        )
                latest = self.runner.runner_last_messages.get(cid, [0])[0]
                if self.runner.last_messages_ids.get(cid, 0) >= latest:
                    self.pending_chats.pop(cid, None)
                else:
                    self.pending_chats[cid] = (retry[cid], time.monotonic() + 15)
        self.last_poll = {
            "at": time.time(),
            "objects": len(objects),
            "raw_events": len(events),
            "events": len(result),
            "history_pending": len(self.pending_chats),
        }
        if result or time.monotonic() - self.poll_heartbeat >= 60:
            self.poll_heartbeat = time.monotonic()
            LOG.info(
                "Опрос FunPay работает: объектов=%s, событий=%s, сообщений=%s, начальных чатов=%s, ожидают историю=%s",
                len(objects),
                len(events),
                sum(e["kind"] == "message" for e in result),
                sum(e["kind"] == "baseline" for e in result),
                len(self.pending_chats),
            )
        return result

    async def poll(self):
        try:
            return await self.invoke(self.read_events)
        except Exception as error:
            if type(error).__name__ == "UnauthorizedError":
                self.session_refreshed = 0
            raise

    def resolve_chat(self, chat, username):
        if str(chat).isdigit():
            return str(chat)
        saved = self.account.get_chat_by_name(username, make_request=True)
        if saved:
            return str(saved.id)
        messages = self.account.get_chat_history(chat, interlocutor_username=username)
        if messages:
            return str(messages[-1].chat_id)
        raise ValueError("Не удалось определить числовой ID чата заказа")

    def chat_id(self, chat):
        return int(chat) if str(chat).isdigit() else chat

    async def send(self, chat, text):
        return await self.invoke(self.account.send_message, self.chat_id(chat), text)

    async def image(self, chat, content):
        return await self.invoke(
            self.account.send_image, self.chat_id(chat), io.BytesIO(content)
        )

    async def review(self, order, text):
        await self.invoke(self.account.send_review, order, text)

    async def categories(self):
        def read():
            profile = self.account.get_user(self.account.id)
            return sorted(
                {
                    s.category.id
                    for s in profile.get_sorted_lots(2)
                    if s.type.name == "COMMON"
                }
            )

        return await self.invoke(read)

    async def raise_lots(self, category):
        return await self.invoke(self.account.raise_lots, category)

    async def order_status(self, order):
        o = await self.invoke(self.account.get_order, order)
        return {"status": o.status.name, "has_review": bool(o.review)}

    async def lots(self, public_category=None, kind="common"):
        def read():
            from FunPayAPI.common.enums import SubCategoryTypes

            type_ = (
                SubCategoryTypes.CURRENCY
                if kind == "chips"
                else SubCategoryTypes.COMMON
            )
            lots = (
                self.account.get_subcategory_public_lots(type_, public_category)
                if public_category is not None
                else self.account.get_user(self.account.id).get_lots()
            )
            if isinstance(lots, dict):
                lots = lots.values()
            result = []
            for lot in lots:
                seller = getattr(lot, "seller", None)
                result.append(
                    {
                        "id": lot.id,
                        "category": lot.subcategory.id,
                        "kind": "chips"
                        if lot.subcategory.type == SubCategoryTypes.CURRENCY
                        else "common",
                        "description": (
                            " ".join(
                                str(value)
                                for value in (lot.server, lot.side, lot.description)
                                if value
                            )
                            if lot.subcategory.type == SubCategoryTypes.CURRENCY
                            else lot.description or ""
                        ),
                        "server": getattr(lot, "server", None),
                        "side": getattr(lot, "side", None),
                        "price": str(lot.price),
                        "currency": getattr(lot.currency, "name", str(lot.currency)),
                        "seller": getattr(seller, "username", None)
                        or (self.account.username if public_category is None else None),
                        "own": public_category is None
                        or getattr(seller, "id", None) == self.account.id
                        or getattr(seller, "username", None) == self.account.username,
                        "online": bool(getattr(seller, "online", False)),
                        "rating": str(getattr(seller, "stars", 0) or 0),
                    }
                )
            return result

        return await self.invoke(read)

    async def chip_floor(self, category, lot_id):
        def read():
            fields = self.account.get_chip_fields(int(category))
            offer = fields.chip_offers.get(str(lot_id))
            if not offer:
                raise ValueError(
                    "Валютное предложение не принадлежит аккаунту/категории"
                )
            from FunPayAPI.common.enums import SubCategoryTypes

            calc = self.account.calc(
                SubCategoryTypes.CURRENCY,
                subcategory_id=int(category),
                game_id=fields.game_id,
                price=1000,
            )
            coefficient = Decimal(str(calc.commission_coefficient))
            if coefficient <= 0:
                raise ValueError("Неизвестна комиссия категории")
            return {
                "price": str(offer.price or 0),
                "active": offer.active,
                "amount": offer.amount,
                "category": category,
                "category_rate": str(Decimal(1) / coefficient),
            }

        return await self.invoke(read, _lot_request=True)

    async def chip_update(
        self, category, lot_id, *, price=None, active=None, amount=None
    ):
        def save():
            fields = self.account.get_chip_fields(int(category))
            offer = fields.chip_offers.get(str(lot_id))
            if not offer:
                raise ValueError("Нет собственного валютного предложения")
            if price is not None:
                offer.price = float(Decimal(str(price)))
            if active is not None:
                offer.active = bool(active)
            if amount is not None:
                offer.amount = int(amount)
            self.account.save_chip(fields)

        await self.invoke(save, _lot_request=True)

    async def public_seller_export(self, seller_id):
        def read():
            result = []
            self.lot_export_errors = []
            for lot in self.account.get_user(int(seller_id)).get_lots():
                if lot.subcategory.type.name != "COMMON":
                    continue
                try:
                    page = self.account.get_lot_page(int(lot.id), locale="ru")
                    english = self.account.get_lot_page(int(lot.id), locale="en")
                    if not page or not english:
                        continue
                    images = export_images(self.account, lot.id)
                    result.append(
                        {
                            "id": lot.id,
                            "category": lot.subcategory.id,
                            "images": images,
                            "fields": {
                                "fields[summary][ru]": page.short_description or "",
                                "fields[desc][ru]": page.full_description or "",
                                "fields[summary][en]": english.short_description or "",
                                "fields[desc][en]": english.full_description or "",
                                "price": str(lot.price),
                            },
                            "image_urls": getattr(page, "image_urls", []),
                        }
                    )
                except Exception:
                    LOG.exception("Публичный экспорт лота %s не завершён", lot.id)
                    self.lot_export_errors.append(str(lot.id))
            return result

        return await self.invoke(read)

    async def delete_lot(self, lot_id):
        await self.invoke(self.account.delete_lot, lot_id)
