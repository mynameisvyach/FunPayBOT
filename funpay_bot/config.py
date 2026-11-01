"""Config services."""

from __future__ import annotations

import configparser
import copy
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from .finance import money
from .metadata import TITLES
from .primitives import compatible, lowercase_schema
from .storage import atomic_write


def text(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def load_parser(path, defaults):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict(defaults)
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ValueError("Настройки поддерживаются только в JSON")
    if not path.exists():
        return parser
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Настройки должны быть JSON-объектом")
    names = {s.casefold(): s for s in parser.sections()}
    for section, values in raw.items():
        if not isinstance(values, dict):
            raise ValueError("Раздел настроек должен быть объектом: " + section)
        name = names.get(section.casefold(), section)
        if name not in parser:
            parser[name] = {}
        for key, value in values.items():
            parser[name][key.lower()] = text(value)
    return parser


def settings_payload(parser):
    result = {}
    for section in parser.sections():
        values = {}
        for key, value in parser[section].items():
            try:
                # Preserve numeric identifiers/credentials as strings; only containers and booleans are typed.
                typed = (
                    json.loads(value)
                    if value.startswith(("{", "[")) or value in ("true", "false")
                    else value
                )
            except (ValueError, TypeError):
                typed = value
            values[key.lower()] = lowercase_schema(typed)
        result[section.lower()] = values
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


DEFAULTS = {
    "General": {"timezone": "Europe/Moscow", "schema_version": "3"},
    "Telegram": {"token": "", "group_id": "0", "owner_id": "0", "proxy": ""},
    "FunPay": {"golden_key": "", "user_agent": "", "proxy": "", "poll_seconds": "6"},
    "Plugins": {"enabled": "auto"},
    "SellerStatus": {"enabled": "true", "online": "true", "command": "#status"},
    "AutoRaise": {"enabled": "false", "interval_seconds": "3600"},
    "AutoWithdraw": {
        "enabled": "false",
        "method": "card_rub",
        "method_kind": "card",
        "wallet": "",
        "minimum": "1000",
        "reserve": "0",
        "interval_seconds": "300",
        "scope": "available_balance",
        "manual_amount": "all",
    },
    "Finance": {
        "balance_lot_id": "0",
        "hold_enabled": "true",
        "hold_hours": "48",
        "withdraw_notifications": "true",
    },
    "Notifications": {
        k: "true"
        for k in (
            "autoresponse",
            "new_order",
            "order_status",
            "review",
            "lot_manager",
            "delivery",
            "ticket",
            "withdrawal",
            "auto_raise",
        )
    },
    "RatingRestrictions": {"enabled": "true"},
    "Statistics": {"rmthub_enabled": "false", "rmthub_cache_minutes": "60"},
    "AUTOGREETINGS": {
        "enabled": "false",
        "default_text": "Здравствуйте! Напишите, если нужна помощь.",
        "cooldown_hours": "48",
        "only_new_chats": "false",
        "lot_greetings": "{}",
    },
    "AUTORESPONSE": {
        "commands": "{}",
        "notification_text": "Покупатель {username} вызвал автоответ.",
    },
    "CONFIRMATIONREPLY": {
        "enabled": "false",
        "text": "Спасибо за подтверждение заказа!",
    },
    "CONFIRMREMINDER": {
        "enabled": "false",
        "text": "Если заказ получен, подтвердите его выполнение на FunPay.",
        "delay_hours": "24",
    },
    "REVIEWREMINDER": {
        "enabled": "false",
        "text": "Будем благодарны за отзыв о заказе.",
        "delay_hours": "24",
    },
    "REVIEWREPLY": {"enabled": "false"},
    "REVIEWCHATREPLY": {"enabled": "false", "on_feedback_changed": "false"},
    "AUTOTICKET": {
        "enabled": "false",
        "hour": "12",
        "message": "1) {clear_orders}\n\n2) {ambiguous_orders}",
    },
    "ChatTemplates": {"items": "{}"},
    "Messages": {"watermark": "false", "watermark_text": "[Бот]"},
    "Rates": {"enabled": "false", "interval_seconds": "3600"},
    "WeeklyReport": {"enabled": "false", "weekday": "0", "hour": "10"},
    "Debug": {"enabled": "false", "refresh_seconds": "10"},
    "Logging": {"level": "INFO", "max_bytes": "5000000", "backup_count": "3"},
}


for _section, _values in {
    "Telegram": {
        "token": "YOUR-BOT-TOKEN",
        "group_id": "YOUR-GROUP-ID",
        "owner_id": "YOUR-OWNER-ID",
        "proxy": "",
    },
    "FunPay": {
        "golden_key": "YOUR-GOLDEN-KEY",
        "user_agent": "YOUR-USER-AGENT",
        "proxy": "",
        "poll_seconds": "6",
    },
}.items():
    DEFAULTS[_section].update(_values)


def is_placeholder(value):
    raw = str(value or "").strip()
    return bool(
        re.search(r"YOUR[-_]", raw, re.I)
        or raw
        and all(re.fullmatch(r"word\d+", part, re.I) for part in raw.split())
    )


class Config:
    def __init__(self, path: Path):
        self.path = Path(path)
        needs_write = not self.path.exists()
        self.c = load_parser(self.path, DEFAULTS)
        if needs_write:
            atomic_write(self.path, settings_payload(self.c))

    def get(self, section, key, default=""):
        return self.c.get(section, key, fallback=default)

    def flag(self, section, key="enabled"):
        return self.c.getboolean(section, key, fallback=False)

    def number(self, section, key, default=0):
        return float(self.get(section, key, str(default)))

    def data(self, section, key, default=None):
        raw = self.get(section, key)
        return (
            compatible(json.loads(raw))
            if raw
            else compatible({} if default is None else default)
        )

    def set(self, section, key, value):
        old = copy.deepcopy(self.c)
        if section not in self.c:
            self.c[section] = {}
        self.c[section][key] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
        try:
            atomic_write(self.path, settings_payload(self.c))
        except Exception:
            self.c = old
            raise

    @property
    def owner_id(self):
        return int(self.get("Telegram", "owner_id", "0"))

    def validate(self, telegram=False):
        ZoneInfo(self.get("General", "timezone", "Europe/Moscow"))
        if self.number("FunPay", "poll_seconds", 6) < 3:
            raise ValueError("FunPay.poll_seconds должен быть >= 3")
        if telegram:
            required = [
                ("Telegram", "token"),
                ("Telegram", "group_id"),
                ("Telegram", "owner_id"),
                ("FunPay", "golden_key"),
                ("FunPay", "user_agent"),
            ]
            if any(
                not self.get(section, key) or is_placeholder(self.get(section, key))
                for section, key in required
            ):
                raise ValueError(
                    "Замените YOUR-* в config.json своими данными или запустите --install"
                )
            if not re.fullmatch(r"\d+:[\w-]+", self.get("Telegram", "token")):
                raise ValueError("Заполните Telegram.token через --install")
            if int(self.get("Telegram", "group_id", "0")) >= 0 or self.owner_id <= 0:
                raise ValueError("Заполните отрицательный group_id и owner_id")
        if (
            self.get("AutoWithdraw", "scope", "available_balance")
            != "available_balance"
        ):
            raise ValueError(
                "Поддержан scope = available_balance: доступный баланс минус остаток"
            )
        for key in ("minimum", "reserve"):
            if money(self.get("AutoWithdraw", key, "0")) < 0:
                raise ValueError(
                    "Порог и остаток автовывода не могут быть отрицательными"
                )
        for section in TITLES:
            for key in ("commands", "lot_greetings"):
                if self.get(section, key):
                    self.data(section, key)
