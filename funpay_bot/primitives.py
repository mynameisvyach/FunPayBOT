"""Общие механизмы очередей, таймеров и безопасного изменения состояния."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


class CaseDict(dict):
    """Предоставляет доступ к ключам текущей JSON-схемы без учёта регистра."""

    def _key(self, key):
        if key in self:
            return key
        return next((k for k in self if str(k).casefold() == str(key).casefold()), key)

    def __getitem__(self, key):
        return super().__getitem__(self._key(key))

    def get(self, key, default=None):
        return super().get(self._key(key), default)


def compatible(value):
    if isinstance(value, dict):
        return CaseDict({k: compatible(v) for k, v in value.items()})
    if isinstance(value, list):
        return [compatible(v) for v in value]
    return value


def lowercase_schema(value):
    # IDs, regex patterns and user-entered labels remain data, not schema keys.
    if isinstance(value, dict):
        return {
            (
                k.lower()
                if isinstance(k, str) and k.isupper() and re.fullmatch(r"[A-Z_]+", k)
                else k
            ): lowercase_schema(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [lowercase_schema(v) for v in value]
    return value


class ActivityGeneration:
    def __init__(self):
        self.value = 0

    def advance(self):
        self.value += 1
        return self.value

    def current(self, token):
        return token == self.value


class TimerRegistry:
    def __init__(self):
        self.tasks = {}

    def cancel(self, key):
        task = self.tasks.pop(key, None)
        if task:
            task.cancel()

    def schedule(self, key, delay, callback):
        self.cancel(key)

        async def run():
            try:
                await asyncio.sleep(max(0, delay))
                await callback()
            finally:
                if self.tasks.get(key) is asyncio.current_task():
                    self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(run())
        return self.tasks[key]

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


class SerialTaskQueue:
    """Последовательно выполняет задачи плагина и возвращает результат каждому вызывающему."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.closed = False

    async def submit(self, operation):
        async with self.lock:
            if self.closed:
                raise RuntimeError("Очередь остановлена")
            return await operation()

    def close(self):
        self.closed = True


class RetryPolicy:
    delays = (30, 45, 60, 120, 300)

    @classmethod
    def delay(cls, attempt):
        return cls.delays[attempt] if 0 <= attempt < len(cls.delays) else None

    @staticmethod
    def safe_error(error):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", 0)
        return (
            isinstance(error, (TimeoutError, ConnectionError, OSError))
            or type(error).__name__
            in ("Timeout", "ReadTimeout", "ConnectTimeout", "ConnectionError")
            or status in (429, 500, 502, 503, 504)
        )


@contextlib.contextmanager
def state_transaction(state):
    """Изменяет состояние синхронно; внутри транзакции запрещены сетевые ожидания await."""
    before = copy.deepcopy(state.d)
    try:
        yield state.d
        state.save()
    except BaseException:
        restore_mapping(state.d, before)
        raise


def restore_mapping(target, snapshot):
    for key in list(target):
        if key not in snapshot:
            del target[key]
    for key, value in snapshot.items():
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            restore_mapping(target[key], value)
        elif isinstance(target.get(key), list) and isinstance(value, list):
            target[key][:] = copy.deepcopy(value)
        else:
            target[key] = copy.deepcopy(value)


class UniversalDumper:
    """Выбирает конкурентную цену с явным защитным минимумом независимо от валюты."""

    @staticmethod
    def choose(
        offers, settings, floor, *, own_name="", quantity=None, parse_quantity=None
    ):
        floor = Decimal(str(floor))
        step = Decimal(str(settings.get("step", "0.5")))
        tick = Decimal(str(settings.get("tick", "0.01")))
        if (
            not floor.is_finite()
            or floor <= 0
            or not step.is_finite()
            or step <= 0
            or not tick.is_finite()
            or tick <= 0
        ):
            raise ValueError("Минимум, шаг и точность должны быть положительными")
        floor = (floor / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        maximum = Decimal(str(settings.get("maximum", "0")))
        if not maximum.is_finite() or maximum < 0 or maximum and maximum < floor:
            raise ValueError("Максимум ниже защитной цены")
        keywords = [
            v.strip().casefold().replace("ё", "е")
            for v in str(settings.get("keywords", "")).split("|")
            if v.strip()
        ]
        friends = {str(v).casefold() for v in settings.get("excluded_sellers", [])}
        targets = {str(v).casefold() for v in settings.get("only_sellers", [])}
        currency = settings.get("currency", "RUB")
        values = []
        rejected = {}

        def reject(reason):
            rejected[reason] = rejected.get(reason, 0) + 1

        for offer in offers:
            seller = str(offer.get("seller") or "").casefold()
            if offer.get("own") or seller == own_name.casefold():
                reject("own")
                continue
            if not seller:
                reject("seller")
                continue
            if offer.get("currency") not in (
                currency,
                "₽" if currency == "RUB" else currency,
            ):
                reject("currency")
                continue
            if settings.get("ignore_offline", True) and not offer.get("online"):
                reject("offline")
                continue
            if seller in friends or targets and seller not in targets:
                reject("seller_filter")
                continue
            if Decimal(str(offer.get("rating", 0))) < Decimal(
                str(settings.get("minimum_rating", 0))
            ):
                reject("rating")
                continue
            description = " ".join(
                str(offer.get("description", "")).casefold().replace("ё", "е").split()
            )
            if keywords and not all(k in description for k in keywords):
                reject("keywords")
                continue
            if (
                settings.get("match_quantity", True)
                and quantity is not None
                and parse_quantity
                and parse_quantity(description) != quantity
            ):
                reject("quantity")
                continue
            price = Decimal(str(offer["price"]))
            if not price.is_finite() or price <= 0:
                reject("price")
                continue
            if price < floor:
                reject("below_floor")
                continue
            values.append(price)
        if values:
            target = min(values) - step
        else:
            target = maximum if maximum else floor
        target = max(
            floor, (target / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        )
        if maximum:
            target = min(target, maximum)
        return target, {"matched": len(values), "rejected": rejected}
