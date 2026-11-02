"""Общие переменные шаблонов сообщений и заказов FunPay."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

MONTHS = (
    "Января",
    "Февраля",
    "Марта",
    "Апреля",
    "Мая",
    "Июня",
    "Июля",
    "Августа",
    "Сентября",
    "Октября",
    "Ноября",
    "Декабря",
)
PLACEHOLDER = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)|\{([A-Za-z_][A-Za-z_0-9]*)\}")


def order_template_values(order, fallback=None):
    """Извлекает текстовые данные полного заказа или его краткой карточки."""
    fallback = fallback or order
    subcategory = getattr(order, "subcategory", None) or getattr(
        fallback, "subcategory", None
    )
    game = category = fullname = ""
    if subcategory is not None:
        category = getattr(subcategory, "name", "") or ""
        game = getattr(getattr(subcategory, "category", None), "name", "") or ""
        fullname = getattr(subcategory, "fullname", "") or " ".join(
            filter(None, (category, game))
        )
    else:
        name = getattr(fallback, "subcategory_name", "") or ""
        if ", " in name:
            game, category = name.rsplit(", ", 1)
            fullname = " ".join(filter(None, (category, game)))
        else:
            category = fullname = name
    description = getattr(order, "short_description", None)
    if description is None:
        description = getattr(order, "description", None)
    if description is None:
        description = getattr(fallback, "description", "")
    params = getattr(order, "lot_params_text", "") or ""
    return {
        "order_id": str(getattr(order, "id", "") or getattr(fallback, "id", "")),
        "order_desc": str(description or ""),
        "order_params": str(params),
        "category_name": str(category),
        "category_fullname": str(fullname),
        "game": str(game),
    }


def render_template(template, event, timezone="Europe/Moscow", *, now=None):
    """Заменяет известные переменные одним проходом, не изменяя подставленные данные."""
    now = now or datetime.now(ZoneInfo(timezone))
    date_text = f"{now.day} {MONTHS[now.month - 1]}"
    description = event.get("order_desc", event.get("description", "")) or ""
    params = event.get("order_params", "") or ""
    order_id = str(event.get("order_id", "") or "").lstrip("#")
    values = {
        "full_date_text": f"{date_text} {now.year} года",
        "date_text": date_text,
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
        "full_time": now.strftime("%H:%M:%S"),
        "username": event.get("template_username", event.get("username", "покупатель")),
        "message_text": event.get("message_text", event.get("text", "")),
        "chat_id": event.get("chat", event.get("chat_id", "")),
        "chat_name": event.get("chat_name", event.get("username", "")),
        "order_desc_and_params": ", ".join(str(v) for v in (description, params) if v),
        "order_desc_or_params": description or params,
        "order_desc": description,
        "order_title": description,
        "order_params": params,
        "order_id": order_id,
        "order_link": f"https://funpay.com/orders/{order_id}/" if order_id else "",
        "category_fullname": event.get("category_fullname", ""),
        "category": event.get("category_name", ""),
        "game": event.get("game", ""),
        "command": event.get("text", ""),
        **{
            key: event.get(key, "")
            for key in (
                "recipient",
                "quantity",
                "product",
                "reference",
                "receipt_url",
                "last_activity",
                "activity_source",
            )
        },
    }

    def replace(match):
        """Оставляет неизвестные переменные и пользовательские фигурные скобки как есть."""
        key = match.group(1) or match.group(2)
        value = values.get(key)
        return (
            ("" if value is None else str(value)) if key in values else match.group(0)
        )

    return PLACEHOLDER.sub(replace, str(template))
