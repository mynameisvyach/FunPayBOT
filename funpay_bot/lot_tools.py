"""Операции переноса, импорта и очистки лотов."""

from __future__ import annotations

import base64
import copy
import io
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from .lot_files import (
    build_series,
    normalize_lots,
    parse_quantities,
    validate_form_values,
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024


MAX_TOTAL_BYTES = 30 * 1024 * 1024


def validate_images(images):
    if not isinstance(images, list) or len(images) > 20:
        raise ValueError("До 20 изображений на лот")
    total = 0
    decoded = []
    for row in images:
        if not isinstance(row, dict) or not isinstance(row.get("base64"), str):
            raise ValueError("Неверный формат вложений")
        body = base64.b64decode(row["base64"], validate=True)
        total += len(body)
        if not body or len(body) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Превышен размер изображений")
        from PIL import Image

        with Image.open(io.BytesIO(body)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                raise ValueError("Неподдерживаемое изображение")
            image.verify()
        decoded.append(body)
    return decoded


def export_images(account, lot_id):
    from bs4 import BeautifulSoup

    # The pinned API's LotPage image parser has a selector defect; adapt outside vendor.
    page = account.method(
        "get", f"lots/offer?id={int(lot_id)}", {}, {}, raise_not_200=True
    )
    soup = BeautifulSoup(page.text, "lxml")
    urls = list(
        dict.fromkeys(
            node["href"]
            for node in soup.select(".param-item a.attachments-thumb[href]")
        )
    )
    images = []
    total = 0
    for url in urls:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not (
                parsed.hostname == "funpay.com"
                or parsed.hostname.endswith(".funpay.com")
            )
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Изображение не на HTTPS-домене FunPay")
        with account.session.get(
            url, timeout=20, stream=True, allow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise ValueError("Не удалось получить вложение FunPay")
            chunks = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                total += len(chunk)
                if size > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError("Превышен размер изображений")
                chunks.append(chunk)
        images.append(
            {"base64": base64.b64encode(b"".join(chunks)).decode(), "source_url": url}
        )
    validate_images(images)
    return images


class MainLotToolsContext:
    """Открывает встроенные инструменты лотов в главном меню ядра."""

    def __init__(self, app, backend):
        self.app, self.backend = app, backend

    async def card(self, text, buttons=(), *, new_message=False):
        rows = []
        for row in buttons:
            converted = []
            for cell in row:
                if isinstance(cell, dict):
                    converted.append(cell)
                    continue
                label, action = cell
                converted.append(
                    (
                        label,
                        "lots_menu"
                        if action == "__back_lots"
                        else "lot_tools_action:" + action,
                    )
                )
            rows.append(converted)
        await self.app.card("main", "menu", text, rows, new_message=new_message)
        self.app.tools_screen_active = True

    async def say(self, text):
        await self.app.say("main", text)

    async def file(self, name, body):
        await self.app.tg.call(
            "send_document",
            chat_id=self.app.group,
            message_thread_id=self.app.state.d["topics"]["main"],
            document=(name, body),
        )


class LotToolsManager:
    def __init__(self, app, backend):
        self.ctx = MainLotToolsContext(app, backend)
        self.inputs = {}
        self.plans = {}
        self.imports = {}
        self.series = {}

    async def stop(self):
        self.inputs.clear()
        self.plans.clear()
        self.imports.clear()
        self.series.clear()

    async def menu(self, *, new_message=False):
        await self.ctx.card(
            "🧰 Инструменты лотов"
            + "\nРабота в главном меню.\nОбычные лоты; валютные предложения исключены.",
            [
                [("🧹 Быстрая очистка", "clean")],
                [("🗑 Удаление по категории", "category")],
                [("🌐 Экспорт публичных лотов", "public")],
                [("📦 Выгрузить / загрузить свои лоты", "transfer")],
                [("🔢 Создать серию по количествам", "series")],
                [("‹ Лоты", "__back_lots")],
            ],
            new_message=new_message,
        )

    async def callback(self, action, user):
        if action == "series":
            self.inputs[user] = ("series_file", time.time() + 900)
            await self.ctx.card(
                "Отправьте JSON-шаблон лота или старую выгрузку. После выбора шаблона укажите количества. Поле {quantity} в тексте заменится количеством.",
                [[("Отмена", "menu")]],
            )
        elif action.startswith("series_pick:"):
            plan = self.series.get(user)
            if not plan or plan["expires"] < time.time():
                return
            index = int(action.split(":")[1])
            if not 0 <= index < len(plan["templates"]):
                return
            plan["template"] = plan["templates"][index]
            fields = [
                key
                for key in plan["template"]["fields"]
                if key.startswith("fields[")
                and ("quantity" in key or "amount" in key)
                and len(re.findall(r"[0-9]+", str(plan["template"]["fields"][key])))
                == 1
            ]
            if not fields:
                fields = [
                    key
                    for key in plan["template"]["fields"]
                    if key.startswith("fields[")
                    and re.fullmatch(r"[0-9]+", str(plan["template"]["fields"][key]))
                ]
            plan["fields"] = fields
            await self.ctx.card(
                "Выберите поле с количеством единиц товара. Не путайте со складским остатком.",
                [
                    [
                        (
                            key + " = " + str(plan["template"]["fields"][key])[:25],
                            "series_field:" + str(i),
                        )
                    ]
                    for i, key in enumerate(fields)
                ]
                + [[("Отмена", "menu")]],
            )
        elif action.startswith("series_field:"):
            plan = self.series.get(user)
            if not plan or plan["expires"] < time.time():
                return
            index = int(action.split(":")[1])
            if not 0 <= index < len(plan.get("fields", [])):
                return
            plan["field"] = plan["fields"][index]
            self.inputs[user] = ("series_quantities", time.time() + 900)
            await self.ctx.card(
                "Введите количества через пробел, например: 50 75 100 150 200 250. До 100 разных количеств за запуск инструмента.",
                [[("Отмена", "menu")]],
            )
        elif action.startswith("series_price:"):
            plan = self.series.get(user)
            if not plan or plan["expires"] < time.time():
                return
            lots = build_series(
                plan["template"],
                plan["field"],
                plan["quantities"],
                scale_price=action.endswith(":scale"),
                rules=await self.ctx.backend.lot_form_rules(
                    plan["template"]["category"]
                ),
            )
            await self.prepare_import(user, lots)
        elif action.startswith("import_mode:"):
            _, token, mode = action.split(":")
            plan = self.imports.get(user)
            if not plan or plan["token"] != token or plan["expires"] < time.time():
                return
            plan["active"] = mode == "active"
            for row in plan["lots"]:
                row["create_active"] = plan["active"]
            await self.ctx.file(
                "lots-create-preview.json",
                json.dumps(
                    {"format": "unified-lots-v1", "lots": plan["lots"]},
                    ensure_ascii=False,
                    indent=2,
                ).encode(),
            )
            await self.ctx.card(
                f"Создать лотов: {len(plan['lots'])}. Состояние новых: {'активны' if plan['active'] else 'выключены'}. Точные количества, цены и поля — в файле выше. Существующие лоты не меняются. При первом отказе или неизвестном результате операция остановится.",
                [
                    [("Создать перечисленные", "import_confirm_" + token)],
                    [("Отмена", "menu")],
                ],
            )
        elif action == "menu":
            self.inputs.pop(user, None)
            self.plans.pop(user, None)
            self.imports.pop(user, None)
            self.series.pop(user, None)
            await self.menu()
        elif action in ("public", "category"):
            self.inputs[user] = (action, time.time() + 300)
            await self.ctx.card(
                "Отправьте числовой ID продавца FunPay."
                if action == "public"
                else "Отправьте числовой ID подкатегории FunPay.",
                [[("Отмена", "menu")]],
            )
        elif action == "clean":
            await self.prepare_delete(user, await self.ctx.backend.lots())
        elif action == "transfer":
            await self.ctx.card(
                "📦 Перенос своих лотов\nВыгрузите редактируемые поля, затем отправьте JSON в главную тему на нужном аккаунте. Перед созданием выбирается активность новых лотов. Изображения переносятся файлами; складские товары включаются только отдельной кнопкой экспорта. Автовыдача в копиях выключена.",
                [
                    [("Экспорт с изображениями", "export_own")],
                    [("Загрузить JSON", "upload")],
                    [("Экспорт со складскими товарами", "export_stock")],
                    [("Экспорт категории, включая выключенные", "export_category")],
                    [("‹ Назад", "menu")],
                ],
            )
        elif action == "export_category":
            self.inputs[user] = ("export_category", time.time() + 300)
            await self.ctx.card(
                "Введите ID категории. Будут выгружены все свои обычные лоты в ней, включая выключенные. Этот способ работает и когда в категории нет активных лотов.",
                [[("Отмена", "menu")]],
            )
        elif action == "upload":
            self.inputs[user] = ("import_file", time.time() + 900)
            await self.ctx.card(
                "Отправьте JSON своих лотов: экспорт FunPay BOT или старый список полей формы.",
                [[("Отмена", "menu")]],
            )
        elif action in ("export_own", "export_stock"):
            rows = await self.ctx.backend.lot_export(
                include_secrets=action == "export_stock"
            )
            errors = getattr(self.ctx.backend, "lot_export_errors", [])
            if errors:
                await self.ctx.say("Не выгружены лоты: " + ", ".join(errors))
            await self.ctx.file(
                "own-lots-transfer.json",
                json.dumps(
                    {"format": "unified-lots-v1", "lots": rows},
                    ensure_ascii=False,
                    indent=2,
                ).encode(),
            )
        elif action.startswith("import_confirm_"):
            plan = self.imports.pop(user, None)
            if (
                not plan
                or plan["token"] != action[15:]
                or plan["expires"] < time.time()
            ):
                return
            if not self.ctx.app.cfg.flag("SellerStatus", "online"):
                await self.ctx.say("Включите онлайн перед импортом.")
                return
            if "active" not in plan:
                await self.ctx.say("Сначала выберите активность новых лотов.")
                return
            await self.check_limits(plan["lots"])
            count = 0
            for i, row in enumerate(plan["lots"]):
                ok = await self.ctx.app.once(
                    "lot_import:" + plan["token"] + ":" + str(i),
                    lambda row=row: self.ctx.backend.lot_import_one(row),
                )
                if not ok:
                    break
                count += 1
            await self.ctx.say(
                f"Создано лотов: {count} из {len(plan['lots'])}. Состояние новых: {'активны' if plan['active'] else 'выключены'}. Если операция остановилась, проверьте результат на FunPay перед повторной загрузкой."
            )
            await self.menu()
        elif action.startswith("confirm_"):
            plan = self.plans.pop(user, None)
            if not plan or plan["token"] != action[8:] or plan["expires"] < time.time():
                await self.ctx.say("Подтверждение истекло. Сформируйте список снова.")
                return
            if not self.ctx.app.cfg.flag("SellerStatus", "online"):
                await self.ctx.say("Офлайн: удаление не выполняется.")
                return
            current = {str(v["id"]) for v in await self.ctx.backend.lots()}
            if not set(plan["ids"]).issubset(current):
                await self.ctx.say("Список лотов изменился. Сформируйте план заново.")
                return
            done = 0
            for lot in plan["ids"]:
                ok = await self.ctx.app.once(
                    "delete:" + plan["token"] + ":" + lot,
                    lambda lot=lot: self.ctx.backend.delete_lot(int(lot)),
                )
                if not ok:
                    break
                done += 1
            await self.ctx.say(f"Удаление: подтверждено {done} из {len(plan['ids'])}.")
            await self.menu()

    async def message(self, text, user):
        pending = self.inputs.pop(user, None)
        if not pending:
            return
        action, expires = pending
        if action == "series_quantities" and expires >= time.time():
            plan = self.series.get(user)
            if not plan or plan["expires"] < time.time():
                return
            try:
                plan["quantities"] = parse_quantities(text)
            except ValueError as error:
                self.inputs[user] = pending
                await self.ctx.say(str(error))
                return
            await self.ctx.card(
                "Как считать цену? Пропорционально: цена шаблона × новое количество / количество шаблона. Либо одна цена для каждого лота. Итоговые цены будут показаны до создания.",
                [
                    [("Пропорционально количеству", "series_price:scale")],
                    [("Сохранить цену шаблона", "series_price:fixed")],
                    [("Отмена", "menu")],
                ],
            )
            return
        if expires < time.time() or not text.strip().isdigit():
            await self.ctx.say("Нужен числовой ID; откройте действие снова.")
            return
        category = int(text.strip())
        if action == "export_category":
            lots = await self.ctx.backend.lot_export(category=category)
            await self.export(lots, "own-lots-category.json")
            return
        if action == "public":
            await self.export(
                await self.ctx.backend.public_seller_export(category),
                "public-lots.json",
            )
        else:
            lots = [
                v
                for v in await self.ctx.backend.lots()
                if str(v["category"]) == str(category)
            ]
            await self.prepare_delete(user, lots)

    async def document(self, name, body, user):
        try:
            if len(body) > 45 * 1024 * 1024:
                raise ValueError()
            lots = normalize_lots(json.loads(body))
            if not isinstance(lots, list) or not 1 <= len(lots) <= 1000:
                raise ValueError()
            total_images = 0
            for row in lots:
                if not isinstance(row, dict):
                    raise ValueError()
                total_images += sum(
                    len(image) for image in validate_images(row.get("images", []))
                )
                if total_images > 30 * 1024 * 1024:
                    raise ValueError()
                warehouse = row.get("warehouse_secrets", [])
                if not isinstance(warehouse, list) or any(
                    not isinstance(item, str) for item in warehouse
                ):
                    raise ValueError()
                if (
                    not isinstance(row, dict)
                    or int(row["category"]) <= 0
                    or not isinstance(row["fields"], dict)
                ):
                    raise ValueError()
                if any(
                    not isinstance(k, str) or not isinstance(v, (str, int, float))
                    for k, v in row["fields"].items()
                ):
                    raise ValueError()
        except (ValueError, KeyError, TypeError, OSError, ArithmeticError):
            await self.ctx.say(
                "Не удалось прочитать файл переноса unified-lots-v1. Лимит — 1000 лотов и 30 МБ изображений."
            )
            return
        pending = self.inputs.get(user)
        if pending and pending[0] == "series_file" and pending[1] >= time.time():
            self.inputs.pop(user, None)
            self.series[user] = {"templates": lots, "expires": time.time() + 900}
            await self.ctx.card(
                "Выберите образец для серии. Будут созданы только указанные вами количества.",
                [
                    [
                        (
                            "Образец "
                            + str(i + 1)
                            + ": "
                            + str(
                                row["fields"].get(
                                    "fields[summary][ru]",
                                    row["fields"].get("fields[quantity2]", "лот"),
                                )
                            )[:35],
                            "series_pick:" + str(i),
                        )
                    ]
                    for i, row in enumerate(lots[:100])
                ]
                + [[("Отмена", "menu")]],
            )
            return
        self.inputs.pop(user, None)
        await self.prepare_import(user, lots)

    async def check_limits(self, lots):
        """Проверяет свежие ограничения формы каждой задействованной категории."""
        rules = {}
        for row in lots:
            category = int(row["category"])
            if category not in rules:
                rules[category] = await self.ctx.backend.lot_form_rules(category)
            validate_form_values(row, rules[category])

    async def prepare_import(self, user, lots):
        """Создаёт план и требует явно выбрать активность новых лотов."""
        await self.check_limits(lots)
        token = secrets.token_hex(6)
        self.imports[user] = {
            "token": token,
            "expires": time.time() + 900,
            "lots": copy.deepcopy(lots),
        }
        await self.ctx.card(
            f"Подготовлено новых лотов: {len(lots)}. Оставить их активными или создать сразу выключенными? Это относится только к создаваемым лотам. Изображения по старым ID не переносятся; нужны вложения из экспорта бота.",
            [
                [
                    ("Оставить активными", "import_mode:" + token + ":active"),
                    ("Сразу выключить", "import_mode:" + token + ":inactive"),
                ],
                [("Отмена", "menu")],
            ],
        )

    async def export(self, lots, name):
        await self.ctx.file(
            name,
            json.dumps(
                {
                    "format": "unified-lots-v1"
                    if name in ("public-lots.json", "own-lots-category.json")
                    else "lot-list-v1",
                    "full_backup": False,
                    "lots": lots,
                },
                ensure_ascii=False,
                indent=2,
            ).encode(),
        )

    async def prepare_delete(self, user, lots):
        lots = [lot for lot in lots if lot.get("kind", "common") == "common"]
        if not lots:
            await self.ctx.say("Подходящих лотов нет.")
            return
        token = secrets.token_hex(6)
        self.plans[user] = {
            "token": token,
            "expires": time.time() + 300,
            "ids": [str(v["id"]) for v in lots],
        }
        await self.export(lots, "delete-plan.json")
        await self.ctx.card(
            f"🗑 Удалить {len(lots)} лотов?\nТочный список отправлен выше. Действие необратимо.\nПодтверждение действует 5 минут и только для автора запроса.",
            [
                [("Удалить перечисленные лоты", "confirm_" + token)],
                [("Отмена", "menu")],
            ],
        )
