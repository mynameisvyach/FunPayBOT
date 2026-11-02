"""Общие функции оформления интерфейса."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

UI_TEXT_LIMIT = 3800


UI_TIMEZONE = "Europe/Moscow"


UI_EMOJI = {
    "telegram": {
        "id": "5875465628285931233",
        "html": '<tg-emoji emoji-id="5875465628285931233">✈️</tg-emoji>',
    },
    "settings": {
        "id": "5875431869842985304",
        "html": '<tg-emoji emoji-id="5875431869842985304">🎛</tg-emoji>',
    },
    "download": {
        "id": "5899757765743615694",
        "html": '<tg-emoji emoji-id="5899757765743615694">⬇️</tg-emoji>',
    },
    "back": {
        "id": "5875082500023258804",
        "html": '<tg-emoji emoji-id="5875082500023258804">⬅️</tg-emoji>',
    },
    "file": {
        "id": "5875206779196935950",
        "html": '<tg-emoji emoji-id="5875206779196935950">📁</tg-emoji>',
    },
    "username": {
        "id": "5771887475421090729",
        "html": '<tg-emoji emoji-id="5771887475421090729">👤</tg-emoji>',
    },
    "contact": {
        "id": "5879770735999717115",
        "html": '<tg-emoji emoji-id="5879770735999717115">👤</tg-emoji>',
    },
    "delete": {
        "id": "5841541824803509441",
        "html": '<tg-emoji emoji-id="5841541824803509441">🗑</tg-emoji>',
    },
    "flower": {
        "id": "5967574255670399788",
        "html": '<tg-emoji emoji-id="5967574255670399788">🌸</tg-emoji>',
    },
    "location": {
        "id": "5778661935927004845",
        "html": '<tg-emoji emoji-id="5778661935927004845">📍</tg-emoji>',
    },
    "unit": {
        "id": "5258165702707125574",
        "html": '<tg-emoji emoji-id="5258165702707125574">⭐️</tg-emoji>',
    },
    "thinking": {
        "id": "5449875850046481967",
        "html": '<tg-emoji emoji-id="5449875850046481967">🤔</tg-emoji>',
    },
    "sleep": {
        "id": "5346167851730348282",
        "html": '<tg-emoji emoji-id="5346167851730348282">😴</tg-emoji>',
    },
    "warning": {
        "id": "5330188279876700094",
        "html": '<tg-emoji emoji-id="5330188279876700094">🫨</tg-emoji>',
    },
    "achievement": {
        "id": "5395597754166682968",
        "html": '<tg-emoji emoji-id="5395597754166682968">🤩</tg-emoji>',
    },
    "money": {
        "id": "5280818098960611598",
        "html": '<tg-emoji emoji-id="5280818098960611598">🤑</tg-emoji>',
    },
    "sad": {
        "id": "5458779239941681169",
        "html": '<tg-emoji emoji-id="5458779239941681169">😔</tg-emoji>',
    },
    "start": {
        "id": "5372917041193828849",
        "html": '<tg-emoji emoji-id="5372917041193828849">🚀</tg-emoji>',
    },
    "gift": {
        "id": "5359664288241829619",
        "html": '<tg-emoji emoji-id="5359664288241829619">🎁</tg-emoji>',
    },
    "stop": {
        "id": "5332296662142434561",
        "html": '<tg-emoji emoji-id="5332296662142434561">⛔️</tg-emoji>',
    },
    "heart": {
        "id": "5442678635909621223",
        "html": '<tg-emoji emoji-id="5442678635909621223">❤️</tg-emoji>',
    },
    "success": {
        "id": "5458604417592863845",
        "html": '<tg-emoji emoji-id="5458604417592863845">👍</tg-emoji>',
    },
}


def ui_collect_statistics(stats: Mapping[str, Mapping[str, Any]]) -> dict:
    """Собирает статистику продаж по переданным данным."""
    from datetime import datetime, timedelta

    today_dt = datetime.now(ZoneInfo(UI_TIMEZONE)).date()
    today = today_dt.isoformat()
    recent_start = today_dt - timedelta(days=6)
    rows = []
    denominations: Dict[int, int] = {}
    total_profit = 0.0
    total_units = 0
    for date, raw_day in stats.items():
        if str(date).startswith("_") or not isinstance(raw_day, Mapping):
            continue
        profit = float(raw_day.get("total_profit", raw_day.get("TOTAL_PROFIT", 0)) or 0)
        quantities = (
            raw_day.get("quantities_sold", raw_day.get("QUANTITIES_SOLD", {})) or {}
        )
        units = 0
        for quantity, count in quantities.items():
            quantity_i, count_i = (int(quantity), int(count))
            units += quantity_i * count_i
            denominations[quantity_i] = denominations.get(quantity_i, 0) + count_i
        rows.append((str(date), profit, units))
        total_profit += profit
        total_units += units
    today_row = next((row for row in rows if row[0] == today), (today, 0.0, 0))
    best = max(rows, key=lambda row: row[1]) if rows else ("—", 0.0, 0)
    recent_rows = []
    for row in rows:
        try:
            row_date = datetime.strptime(row[0], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if recent_start <= row_date <= today_dt:
            recent_rows.append(row)
    recent_rows.sort(key=lambda row: row[0], reverse=True)
    popular_denominations = sorted(
        denominations.items(), key=lambda item: (-item[1], item[0])
    )[:7]
    return {
        "today": today,
        "today_profit": today_row[1],
        "today_units": today_row[2],
        "total_profit": total_profit,
        "total_units": total_units,
        "best_date": best[0],
        "best_profit": best[1],
        "days": len(rows),
        "recent_dates": recent_rows,
        "popular_denominations": popular_denominations,
    }


def ui_format_statistics(stats: Mapping[str, Mapping[str, Any]], unit: str) -> str:
    data = ui_collect_statistics(stats)
    e = UI_EMOJI
    return "\n".join(
        [
            f"{e['settings']['html']} <b>Статистика за {ui_date(data['today'])}:</b>",
            f"├ {e['money']['html']} Прибыль: {data['today_profit']:.2f} ₽",
            f"└ {e['unit']['html']} {unit}: {data['today_units']}",
            "",
            f"{e['unit']['html']} <b>Общая статистика:</b>",
            f"├ {e['money']['html']} Общая прибыль: {data['total_profit']:.2f} ₽",
            f"└ {e['unit']['html']} Всего {unit}: {data['total_units']}",
        ]
    )


def ui__fit_telegram_ui_text(text: str, limit: int = UI_TEXT_LIMIT) -> str:
    """Ограничивает UI-текст лимитом Telegram, не разрезая HTML-теги и строки."""
    if len(text) <= int(limit):
        return text
    marker = "\n…"
    budget = max(0, int(limit) - len(marker))
    kept = []
    used = 0
    for line in str(text).splitlines():
        addition = len(line) + (1 if kept else 0)
        if used + addition > budget:
            break
        kept.append(line)
        used += addition
    return "\n".join(kept) + marker


def ui_format_health_summary(health: Mapping[str, Mapping[str, Any]]) -> str:
    """Кратко показывает состояние подключённых фоновых обработчиков независимо от их назначения."""
    if not health:
        return ""
    rows = []
    for worker, item in health.items():
        running = bool(item.get("running"))
        state = "работает" if running else "остановлен"
        detail = (
            f"; ошибка: {html.escape(str(item.get('last_error') or item.get('error')))}"
            if item.get("last_error") or item.get("error")
            else ""
        )
        rows.append(f"• <code>{html.escape(str(worker))}</code>: {state}{detail}")
    return "\n".join(rows)


def ui_format_main_screen(
    balance_title: str,
    balance: str,
    stats: Mapping[str, Mapping[str, Any]],
    unit: str,
    health: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> str:
    """Формирует текст главного экрана плагина."""
    e = UI_EMOJI
    health_text = ui_format_health_summary(health or {})
    text = (
        f"{e['money']['html']} <b>{balance_title}:</b> {balance}\n\n{ui_format_statistics(stats, unit)}\n"
        + (f"\n<b>Состояние:</b>\n{health_text}\n" if health_text else "")
    )
    return ui__fit_telegram_ui_text(text)


def ui_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def ui_button(text, action, icon):
    return {
        "text": text,
        "callback_data": action,
        "icon_custom_emoji_id": UI_EMOJI[icon]["id"],
    }
