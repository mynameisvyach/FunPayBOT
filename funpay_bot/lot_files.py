"""Форматы переноса лотов и подготовка количественных серий без запросов на запись."""

from __future__ import annotations

import copy
import re
from decimal import ROUND_HALF_UP, Decimal

MAX_SERIES = 100


def normalize_lots(data):
    """Читает экспорт ядра, список полей старой формы или один шаблон."""
    rows = (
        data
        if isinstance(data, list)
        else data.get("lots", [data])
        if isinstance(data, dict)
        else None
    )
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError("Нужно от 1 до 1000 лотов")
    result = []
    for source in rows:
        if not isinstance(source, dict):
            raise ValueError("Лот должен быть объектом JSON")
        fields = source.get("fields", source)
        if not isinstance(fields, dict):
            raise ValueError("Нет полей лота")
        category = source.get("category", fields.get("node_id", fields.get("node")))
        if not str(category).isdigit() or int(category) <= 0:
            raise ValueError("Не указан ID категории")
        clean = {}
        for key, value in fields.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, (str, int, float))
                or isinstance(value, bool)
            ):
                if key.startswith("fields[") or key in ("price", "amount"):
                    raise ValueError("Некорректное поле: " + key)
                continue
            if key.startswith("fields[") or key in (
                "price",
                "amount",
                "deactivate_after_sale",
            ):
                clean[key] = str(value)
        clean.pop("fields[images]", None)
        price = Decimal(clean.get("price", "0"))
        if not price.is_finite() or price <= 0:
            raise ValueError("Цена должна быть положительной")
        result.append(
            {
                "category": int(category),
                "fields": clean,
                "images": source.get("images", []),
                "warehouse_secrets": source.get("warehouse_secrets", []),
            }
        )
    return result


def parse_quantities(text):
    """Проверяет список положительных целых количеств и исключает повторы."""
    parts = re.split(r"[\s,;]+", str(text).strip())
    if not parts or any(
        not re.fullmatch(r"[0-9]{1,9}", part) or int(part) <= 0 for part in parts
    ):
        raise ValueError("Введите положительные целые количества через пробел")
    values = list(dict.fromkeys(int(part) for part in parts))
    if len(values) > MAX_SERIES:
        raise ValueError(
            f"За один запуск инструмента — до {MAX_SERIES} лотов; это ограничение бота, не FunPay"
        )
    return values


def build_series(template, field, quantities, *, scale_price=True, rules=None):
    """Меняет выбранное поле, подставляет количество и явно пересчитывает цену по образцу."""
    if field not in template["fields"] or not field.startswith("fields["):
        raise ValueError("Выберите существующее поле количества товара")
    original = str(template["fields"][field])
    numbers = list(re.finditer(r"[0-9]+(?:[.,][0-9]+)?", original))
    base = (
        Decimal(numbers[0].group().replace(",", "."))
        if len(numbers) == 1
        else Decimal(0)
    )
    if scale_price and (not base.is_finite() or base <= 0):
        raise ValueError(
            "Для пропорциональной цены в шаблоне нужно числовое исходное количество"
        )
    result = []
    for quantity in quantities:
        row = copy.deepcopy(template)
        replacement = str(quantity)
        if len(numbers) == 1:
            replacement = (
                original[: numbers[0].start()]
                + str(quantity)
                + original[numbers[0].end() :]
            )
        options = (rules or {}).get(field, {}).get("options", [])
        if options and replacement not in options:
            paired = field[:-1] + "2]"
            other = [
                value for value in options if value and not re.search(r"[0-9]", value)
            ]
            if paired not in row["fields"] or len(other) != 1:
                raise ValueError(
                    "Количество не разрешено формой FunPay: " + str(quantity)
                )
            row["fields"][field] = other[0]
            row["fields"][paired] = str(quantity)
        else:
            row["fields"][field] = replacement
        for key, value in list(row["fields"].items()):
            if key.startswith(
                ("fields[summary]", "fields[desc]", "fields[payment_msg]")
            ):
                text = str(value).replace("{quantity}", str(quantity))
                if key.startswith("fields[summary]") and base > 0:
                    text = re.sub(
                        r"(?<!\d)" + re.escape(str(int(base))) + r"(?!\d)",
                        str(quantity),
                        text,
                    )
                row["fields"][key] = text
        if scale_price:
            row["fields"]["price"] = format(
                (Decimal(template["fields"]["price"]) * quantity / base).quantize(
                    Decimal("0.001"), rounding=ROUND_HALF_UP
                ),
                "f",
            )
        # Складские коды нельзя размножать в нескольких объявлениях.
        if row.get("warehouse_secrets"):
            raise ValueError(
                "Серия со складскими товарами запрещена: загрузите шаблон без кодов"
            )
        result.append(row)
    return result


def validate_form_values(row, rules):
    """Проверяет ограничения свежей формы; неизвестные серверные квоты не выдумывает."""
    for key, value in row["fields"].items():
        rule = rules.get(key)
        if not rule:
            continue
        if rule.get("options") and str(value) not in rule["options"]:
            raise ValueError("Значение не разрешено формой FunPay: " + key)
        if not str(value).strip():
            if rule.get("required"):
                raise ValueError("Не заполнено обязательное поле: " + key)
            continue
        if rule.get("maxlength") and len(str(value)) > int(rule["maxlength"]):
            raise ValueError("Слишком длинное поле: " + key)
        if rule.get("type") == "number" or rule.get("min") or rule.get("max"):
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError("Некорректное число: " + key)
            if rule.get("min") and number < Decimal(rule["min"]):
                raise ValueError("Меньше минимума FunPay: " + key + " ≥ " + rule["min"])
            if rule.get("max") and number > Decimal(rule["max"]):
                raise ValueError(
                    "Больше максимума FunPay: " + key + " ≤ " + rule["max"]
                )
            step = rule.get("step")
            if (
                step
                and step != "any"
                and Decimal(step) > 0
                and (number - Decimal(rule.get("min") or "0")) % Decimal(step)
            ):
                raise ValueError("Не соответствует шагу FunPay: " + key)


def save_lot_once(account, fields):
    """Не даёт закреплённому API повторять POST сохранения при 429 или редиректе."""
    original = account.session.request
    sent = False

    def request(*args, **kwargs):
        """Допускает единственную попытку записи и возвращает отказ без внутреннего цикла API."""
        nonlocal sent
        method = str(kwargs.get("method", args[0] if args else "")).lower()
        if method == "post":
            if sent:
                raise RuntimeError(
                    "Повтор POST сохранения лота остановлен; проверьте результат на FunPay"
                )
            sent = True
        response = original(*args, **kwargs)
        if method == "post" and response.status_code == 429:
            response.raise_for_status()
        return response

    account.session.request = request
    try:
        account.save_lot(fields)
    finally:
        account.session.request = original
