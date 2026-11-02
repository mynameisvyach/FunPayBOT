"""Название проекта, закреплённая ревизия API и рабочие пути."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from main import VERSION as VERSION

BOT_NAME = "FunPay BOT"


API_TAG = "v0.1.17.15"


API_REVISION = "c09dd13be25072bb3d5cdbba33d1e0d70e477710"


ROOT = Path(__file__).resolve().parent.parent


TITLES = {
    "Fulfillment": "✅ Сообщение о выполнении",
    "AUTOGREETINGS": "👋 Приветствия",
    "AUTORESPONSE": "💬 Автоответы",
    "CONFIRMATIONREPLY": "✅ Ответ после подтверждения",
    "CONFIRMREMINDER": "⏳ Напоминание о подтверждении",
    "REVIEWREMINDER": "⭐ Напоминание об отзыве",
    "REVIEWREPLY": "📝 Ответ на отзыв",
    "REVIEWCHATREPLY": "❤️ Ответ в чат после отзыва",
    "AUTOTICKET": "🎫 Тикеты",
    "Messages": "🤖 Вотермарка",
    "Rates": "🤑 Автообновление курсов",
    "WeeklyReport": "📊 Еженедельный отчёт",
}


LOG = logging.getLogger("FunPayBOT")


def human_time(cfg, timestamp=None):
    zone = ZoneInfo(cfg.get("General", "timezone", "Europe/Moscow"))
    dt = datetime.fromtimestamp(
        timestamp if timestamp is not None else time.time(), zone
    )
    return dt.strftime("%d.%m.%Y в %H:%M")
