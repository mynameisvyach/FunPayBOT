"""Редактор настроек автоматизации."""

from __future__ import annotations

import hashlib
import json
import secrets
import time

from ..metadata import TITLES


class AutomationUIMixin:
    AUTO_HELP = {
        "Fulfillment": "Общее сообщение о выполнении заказа: продавец и плагины используют один текст; подтверждение операции добавляется отдельно.",
        "AUTOGREETINGS": "Отправляет приветствие после сообщения покупателя или нового оплаченного заказа. Повтор ограничен паузой. Для лота с подходящим описанием используется его текст, иначе — общий. На сообщения продавца, системы и поддержки не отвечает.",
        "AUTORESPONSE": "Отвечает на точную команду покупателя, без учёта регистра. Общего выключателя нет. Заготовки продавца настраиваются отдельно и доступны кнопками в закрепах чатов.",
        "CONFIRMATIONREPLY": "Отправляет сообщение в чат, когда покупатель подтверждает выполнение заказа. Для одного заказа ответ отправляется один раз.",
        "CONFIRMREMINDER": "Запускается только после сигнала о выполнении от плагина или кнопки «Выполнено» в теме «Заказы». Категорию передаёт плагин/заказ, вручную выбирать её не нужно. Перед отправкой проверяет статус; при подтверждении или возврате отменяется.",
        "REVIEWREMINDER": "После явного выполнения и подтверждения заказа ждёт указанное время и просит оставить отзыв. Если выполнение не отмечено, отзыв уже есть или заказ возвращён, сообщение не отправляется.",
        "REVIEWREPLY": "Публикует ответ продавца под отзывом на FunPay. Для каждой оценки настраиваются отдельные текст и включение.",
        "REVIEWCHATREPLY": "Пишет покупателю в чат после отзыва. Это отдельное сообщение, а не публичный ответ под отзывом. Можно настроить реакции на изменение и удаление отзыва.",
        "AUTOTICKET": "Проверяет оплаченные заказы старше суток. Жалобы и неполная история требуют решения администратора. Остальные заказы включаются автоматически. Между попытками отправки тикетов минимум 24 часа, включая перезапуск и неизвестный результат.",
        "Messages": "Вотермарка добавляется к автоматическим ответам и заготовкам бота. Ваш обычный текст и изображения из Telegram отправляются без неё.",
        "Rates": "Обновляет закреплённые курсы в главной теме через заданный интервал. Кнопка «Курсы» позволяет получить их вручную.",
        "WeeklyReport": "Публикует единый отчёт в главной теме раз в неделю в выбранный день после выбранного часа. Включает текущую дату и шесть предыдущих дней. Кнопка «Отчёт» работает независимо от расписания.",
        "ChatTemplates": "Заготовки для ручной отправки продавцом. Все включённые заготовки появляются в закрепе каждого существующего чата. Изменение названия или текста обновляет кнопки, без повторной выгрузки истории.",
    }

    AUTO_FIELDS = {
        "Fulfillment": {
            "text": ("Сообщение покупателю", "text"),
            "receipt_text": ("Дополнение с чеком", "text"),
        },
        "AUTOGREETINGS": {
            "default_text": ("Общее приветствие", "text"),
            "cooldown_hours": ("Пауза между приветствиями, ч", "hours"),
            "only_new_chats": ("Только новые диалоги", "bool"),
        },
        "CONFIRMATIONREPLY": {"text": ("Текст ответа", "text")},
        "CONFIRMREMINDER": {
            "text": ("Текст напоминания", "text"),
            "delay_hours": ("Задержка после выполнения, ч", "hours"),
        },
        "REVIEWREMINDER": {
            "text": ("Текст напоминания", "text"),
            "delay_hours": ("Задержка после подтверждения, ч", "hours"),
        },
        "REVIEWCHATREPLY": {
            "on_feedback_changed": ("Отвечать при изменении отзыва", "bool")
        },
        "AUTOTICKET": {
            "message": ("Текст тикета", "text"),
            "hour": ("Час ежедневной проверки", "hour"),
        },
        "Messages": {
            "watermark": ("Добавлять вотермарку", "bool"),
            "watermark_text": ("Текст вотермарки", "text"),
        },
        "Rates": {"interval_seconds": ("Интервал обновления, мин", "minutes")},
        "WeeklyReport": {
            "weekday": ("День недели", "weekday"),
            "hour": ("Час публикации", "hour"),
        },
        "AUTORESPONSE": {"notification_text": ("Текст уведомления о вызове", "text")},
    }

    def auto_collection(self, section):
        if section == "ChatTemplates":
            return self.cfg.data(section, "items", {})
        if section == "AUTORESPONSE":
            return self.cfg.data(section, "commands", {})
        if section == "AUTOGREETINGS":
            return self.cfg.data(section, "lot_greetings", {})
        if section in ("REVIEWREPLY", "REVIEWCHATREPLY"):
            return {
                str(n): self.cfg.data(section, str(n), {"ENABLE": False, "TEXT": ""})
                for n in range(1, 7 if section == "REVIEWCHATREPLY" else 6)
            }
        return {}

    def save_auto_collection(self, section, items):
        if section in ("REVIEWREPLY", "REVIEWCHATREPLY"):
            for key, item in items.items():
                self.cfg.set(section, key, json.dumps(item, ensure_ascii=False))
        else:
            self.cfg.set(
                section,
                {
                    "ChatTemplates": "items",
                    "AUTORESPONSE": "commands",
                    "AUTOGREETINGS": "lot_greetings",
                }[section],
                json.dumps(items, ensure_ascii=False),
            )

    @staticmethod
    def auto_token(name):
        return hashlib.sha256(name.encode()).hexdigest()[:12]

    def auto_entry(self, section, token):
        return next(
            (
                (name, value)
                for name, value in self.auto_collection(section).items()
                if self.auto_token(name) == token
            ),
            (None, None),
        )

    async def section(self, section):
        if section not in self.AUTO_HELP:
            return
        title = (
            "📝 Заготовки продавца" if section == "ChatTemplates" else TITLES[section]
        )
        text = title + "\n\n" + self.AUTO_HELP[section]
        rows = []
        if section not in ("AUTORESPONSE", "ChatTemplates", "Messages", "Fulfillment"):
            rows.append(
                [
                    (
                        self.status(section)
                        + " "
                        + ("Включено" if self.cfg.flag(section) else "Выключено"),
                        "toggle:" + section + ":enabled",
                    )
                ]
            )
        for key, (label, kind) in self.AUTO_FIELDS.get(section, {}).items():
            value = self.cfg.get(section, key)
            if kind == "bool":
                value = "🟢" if self.cfg.flag(section, key) else "⚪"
            elif kind == "text":
                value = "изменить"
            elif kind == "categories":
                value = ", ".join(map(str, self.cfg.data(section, key, []))) or "все"
            elif kind == "weekday":
                value = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][int(value or 0)]
            elif kind == "minutes":
                value = f"{self.cfg.number(section, key, 3600) / 60:g}"
            rows.append([(f"{label}: {value}", f"a:{section}:field:{key}")])
        if section == "AUTORESPONSE":
            rows = [
                [("💬 Команды покупателей", "a:AUTORESPONSE:list")],
                [("📝 Заготовки продавца", "section:ChatTemplates")],
            ] + rows
        elif section == "AUTOGREETINGS":
            rows.append([("Приветствия для лотов", "a:AUTOGREETINGS:list")])
        elif section in ("REVIEWREPLY", "REVIEWCHATREPLY", "ChatTemplates"):
            for name, item in self.auto_collection(section).items():
                enabled = (
                    item.get("enabled", True)
                    if section == "ChatTemplates"
                    else item.get("ENABLE", False)
                )
                label = (
                    item.get("title", "Заготовка")
                    if section == "ChatTemplates"
                    else "Отзыв удалён"
                    if name == "6"
                    else name + " ⭐"
                )
                rows.append(
                    [
                        (
                            ("🟢 " if enabled else "⚪ ") + label,
                            f"a:{section}:entry:{self.auto_token(name)}",
                        )
                    ]
                )
            if section == "ChatTemplates":
                rows.append([("＋ Добавить заготовку", "a:ChatTemplates:add")])
        rows.append(
            [
                ("‹ Автоответы", "section:AUTORESPONSE")
                if section == "ChatTemplates"
                else ("‹ Автоматизация", "menu:automation")
            ]
        )
        await self.card("main", "menu", text, rows)

    async def auto_list(self, section):
        rows = []
        for name, item in self.auto_collection(section).items():
            enabled = item.get("ENABLED", True) if isinstance(item, dict) else True
            rows.append(
                [
                    (
                        ("🟢 " if enabled else "⚪ ") + name[:65],
                        f"a:{section}:entry:{self.auto_token(name)}",
                    )
                ]
            )
        rows += [
            [("＋ Добавить", "a:" + section + ":add")],
            [("‹ Назад", "section:" + section)],
        ]
        await self.card(
            "main",
            "menu",
            (
                "Команды покупателей"
                if section == "AUTORESPONSE"
                else "Приветствия для лотов"
            )
            + "\nВыберите запись.",
            rows,
        )

    async def auto_entry_menu(self, section, token):
        name, item = self.auto_entry(section, token)
        if name is None:
            return
        review = section in ("REVIEWREPLY", "REVIEWCHATREPLY")
        label = (
            item.get("title", "Заготовка")
            if section == "ChatTemplates"
            else name + " ⭐"
            if review and name != "6"
            else "Отзыв удалён"
            if review
            else name
        )
        value = (
            item
            if isinstance(item, str)
            else item.get("text", item.get("TEXT", item.get("RESPONSE", "")))
        )
        base = f"a:{section}:"
        rows = [[("✏ Текст", base + "text:" + token)]]
        if section != "AUTOGREETINGS":
            enabled = item.get("enabled", item.get("ENABLE", item.get("ENABLED", True)))
            rows.insert(
                0,
                [
                    (
                        ("🟢 Включено" if enabled else "⚪ Выключено"),
                        base + "switch:" + token,
                    )
                ],
            )
        if not review:
            rows += [
                [
                    (
                        "✏ Название"
                        if section == "ChatTemplates"
                        else "✏ Команда"
                        if section == "AUTORESPONSE"
                        else "✏ Описание лота",
                        base + "name:" + token,
                    )
                ],
                [("Удалить", base + "delete:" + token)],
            ]
        if section == "AUTORESPONSE":
            notify = item.get("NOTIFY", True)
            rows.append(
                [
                    (
                        (
                            "🔔 Вызов в главную: да"
                            if notify
                            else "🔕 Вызов в главную: нет"
                        ),
                        base + "notify:" + token,
                    )
                ]
            )
        back = (
            "section:" + section
            if review or section == "ChatTemplates"
            else base + "list"
        )
        await self.card(
            "main", "menu", label + "\n\n" + value, rows + [[("‹ Назад", back)]]
        )

    async def auto_prompt(self, user, payload, prompt, current=""):
        self.pending[user] = ("_automation", payload, time.time() + 900)
        await self.card(
            "main",
            "menu",
            prompt
            + ("\n\nСейчас:\n" + str(current) if current else "")
            + "\n\nПришлите значение в эту тему.",
            [[("‹ Отмена", "section:" + payload["section"])]],
        )

    async def automation_callback(self, data, user):
        if not data.startswith("a:"):
            return False
        parts = data.split(":")
        _, section, action = parts[:3]
        if section not in self.AUTO_HELP:
            return True
        arg = parts[3] if len(parts) > 3 else ""
        if action == "list":
            await self.auto_list(section)
        elif action == "entry":
            await self.auto_entry_menu(section, arg)
        elif action == "field":
            spec = self.AUTO_FIELDS.get(section, {}).get(arg)
            if not spec:
                return True
            label, kind = spec
            if kind == "bool":
                self.cfg.set(section, arg, not self.cfg.flag(section, arg))
                await self.section(section)
            elif kind in ("hours", "hour", "minutes", "weekday"):
                choices = (
                    range(7)
                    if kind == "weekday"
                    else (0, 6, 9, 10, 12, 15, 18, 21, 23)
                    if kind == "hour"
                    else (1, 5, 15, 30, 60, 120)
                    if kind == "minutes"
                    else (1, 6, 12, 24, 48, 72)
                )
                rows = [
                    [
                        (
                            str(n)
                            if kind != "weekday"
                            else ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][n],
                            f"a:{section}:set:{arg}:{n}",
                        )
                    ]
                    for n in choices
                ]
                if kind != "weekday":
                    rows.append([("Другое значение", f"a:{section}:custom:{arg}")])
                await self.card(
                    "main",
                    "menu",
                    label + "\nВыберите значение.",
                    rows + [[("‹ Назад", "section:" + section)]],
                )
            else:
                instruction = (
                    "Введите ID категорий через запятую. Напишите «все», чтобы убрать ограничение."
                    if kind == "categories"
                    else "Введите "
                    + label.lower()
                    + ". Можно использовать $username, $order_id, $date, $time."
                )
                await self.auto_prompt(
                    user,
                    {"section": section, "kind": "field", "key": arg},
                    instruction,
                    self.cfg.get(section, arg),
                )
        elif action in ("set", "custom"):
            if arg not in self.AUTO_FIELDS.get(section, {}):
                return True
            if action == "set":
                await self.automation_input(
                    {"section": section, "kind": "field", "key": arg}, parts[4], user
                )
            else:
                await self.auto_prompt(
                    user,
                    {"section": section, "kind": "field", "key": arg},
                    "Введите число: " + self.AUTO_FIELDS[section][arg][0],
                )
        elif action == "add":
            if section not in ("ChatTemplates", "AUTORESPONSE", "AUTOGREETINGS"):
                return True
            await self.auto_prompt(
                user,
                {"section": section, "kind": "new_name"},
                "Введите название заготовки."
                if section == "ChatTemplates"
                else "Введите команду, начиная с !"
                if section == "AUTORESPONSE"
                else "Введите описание лота. {} обозначает переменную часть.",
            )
        else:
            name, item = self.auto_entry(section, arg)
            if name is None:
                return True
            items = self.auto_collection(section)
            if action in ("text", "name"):
                value = (
                    item
                    if isinstance(item, str)
                    else item.get("text", item.get("TEXT", item.get("RESPONSE", "")))
                )
                await self.auto_prompt(
                    user,
                    {"section": section, "kind": action, "token": arg},
                    "Введите новый текст."
                    if action == "text"
                    else "Введите новое название или команду.",
                    value
                    if action == "text"
                    else item.get("title", name)
                    if isinstance(item, dict)
                    else name,
                )
            elif action == "switch":
                field = (
                    "enabled"
                    if section == "ChatTemplates"
                    else "ENABLE"
                    if section.startswith("REVIEW")
                    else "ENABLED"
                )
                item[field] = not item.get(field, True)
                items[name] = item
                self.save_auto_collection(section, items)
                await self.refresh_all_chat_controls()
                await self.auto_entry_menu(section, arg)
            elif action == "notify":
                item["NOTIFY"] = not item.get("NOTIFY", True)
                items[name] = item
                self.save_auto_collection(section, items)
                await self.auto_entry_menu(section, arg)
            elif action == "delete":
                await self.card(
                    "main",
                    "menu",
                    "Удалить «"
                    + str(item.get("title", name) if isinstance(item, dict) else name)
                    + "»?",
                    [
                        [("Удалить", f"a:{section}:remove:{arg}")],
                        [("‹ Отмена", f"a:{section}:entry:{arg}")],
                    ],
                )
            elif action == "remove":
                del items[name]
                if section == "AUTORESPONSE":
                    self.cfg.set(
                        section,
                        "notify_commands",
                        json.dumps(
                            [
                                n
                                for n in self.cfg.data(section, "notify_commands", [])
                                if n != name
                            ],
                            ensure_ascii=False,
                        ),
                    )
                self.save_auto_collection(section, items)
                await self.refresh_all_chat_controls()
                await (
                    self.section(section)
                    if section == "ChatTemplates"
                    else self.auto_list(section)
                )
        return True

    async def automation_input(self, payload, text, user):
        section = payload["section"]
        kind = payload["kind"]
        text = text.strip()
        try:
            if not text:
                raise ValueError("Значение не может быть пустым.")
            if kind == "field":
                key = payload["key"]
                label, fieldtype = self.AUTO_FIELDS[section][key]
                if fieldtype in ("hours", "hour", "minutes", "weekday"):
                    value = float(text.replace(",", "."))
                    if (
                        not 0 <= value <= 100000
                        or fieldtype in ("hour", "weekday")
                        and value != int(value)
                    ):
                        raise ValueError("Нужно неотрицательное число.")
                    if (
                        fieldtype == "hour"
                        and value > 23
                        or fieldtype == "weekday"
                        and value > 6
                        or fieldtype == "minutes"
                        and value < 1
                    ):
                        raise ValueError("Число вне допустимого диапазона.")
                    value = value * 60 if fieldtype == "minutes" else value
                    self.cfg.set(section, key, f"{value:g}")
                elif fieldtype == "categories":
                    values = (
                        []
                        if text.casefold() == "все"
                        else [v.strip() for v in text.split(",")]
                    )
                    if any(not v.isdigit() or int(v) <= 0 for v in values):
                        raise ValueError(
                            "Нужны положительные ID через запятую или слово «все»."
                        )
                    self.cfg.set(section, key, json.dumps(list(dict.fromkeys(values))))
                else:
                    self.cfg.set(section, key, text)
                await self.section(section)
                return
            items = self.auto_collection(section)
            if kind == "new_name":
                if section == "AUTORESPONSE" and (
                    not text.startswith("!")
                    or any(c.isspace() for c in text)
                    or len(text) > 40
                ):
                    raise ValueError(
                        "Команда начинается с !, без пробелов, до 40 символов."
                    )
                if len(text) > 300:
                    raise ValueError("Название слишком длинное.")
                if section != "ChatTemplates" and text.casefold() in {
                    name.casefold() for name in items
                }:
                    raise ValueError("Такая запись уже есть.")
                await self.auto_prompt(
                    user,
                    {"section": section, "kind": "new_text", "name": text},
                    "Теперь пришлите текст ответа.",
                )
                return
            if kind == "new_text":
                name = (
                    secrets.token_hex(8)
                    if section == "ChatTemplates"
                    else payload["name"].casefold()
                    if section == "AUTORESPONSE"
                    else payload["name"]
                )
                if section != "ChatTemplates" and name in items:
                    raise ValueError(
                        "Такая запись уже добавлена. Откройте её для изменения."
                    )
                items[name] = (
                    {"title": payload["name"], "text": text, "enabled": True}
                    if section == "ChatTemplates"
                    else {"ENABLED": True, "RESPONSE": text}
                    if section == "AUTORESPONSE"
                    else text
                )
            else:
                name, item = self.auto_entry(section, payload["token"])
                if name is None:
                    raise ValueError("Запись уже удалена.")
                if kind == "text":
                    if isinstance(item, str):
                        items[name] = text
                    else:
                        item[
                            "text"
                            if section == "ChatTemplates"
                            else "TEXT"
                            if section.startswith("REVIEW")
                            else "RESPONSE"
                        ] = text
                        items[name] = item
                elif section == "ChatTemplates":
                    item["title"] = text
                    items[name] = item
                else:
                    if section == "AUTORESPONSE" and (
                        not text.startswith("!") or any(c.isspace() for c in text)
                    ):
                        raise ValueError(
                            "Команда начинается с ! и не содержит пробелов."
                        )
                    new = text.casefold() if section == "AUTORESPONSE" else text
                    if new != name and new in items:
                        raise ValueError("Такое название уже используется.")
                    del items[name]
                    items[new] = item
                    name = new
                    if section == "AUTORESPONSE":
                        notify = self.cfg.data(section, "notify_commands", [])
                        self.cfg.set(
                            section,
                            "notify_commands",
                            json.dumps(
                                [
                                    new
                                    if n
                                    == payload.get(
                                        "old",
                                        self.auto_entry(section, payload["token"])[0],
                                    )
                                    else n
                                    for n in notify
                                ],
                                ensure_ascii=False,
                            ),
                        )
            self.save_auto_collection(section, items)
            await self.refresh_all_chat_controls()
            await self.auto_entry_menu(section, self.auto_token(name))
        except (ValueError, TypeError) as error:
            self.pending[user] = ("_automation", payload, time.time() + 900)
            await self.card(
                "main",
                "menu",
                "❌ " + str(error) + "\nПришлите значение ещё раз.",
                [[("‹ Отмена", "section:" + section)]],
            )
