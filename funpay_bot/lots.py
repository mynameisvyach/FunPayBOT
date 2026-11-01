"""Lots services."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Mapping

from .metadata import LOG
from .primitives import UniversalDumper


@dataclass(frozen=True)
class ManagedLot:
    lot_id: str
    category_id: int
    kind: str
    quantity: int
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LotProfile:
    """Plugin-to-worker contract for compact quantities and explicit lot descriptors."""

    owner: str
    category_id: int
    kind: str
    lots: Mapping[str, Any]
    parse_quantity: Any = None

    def entries(self):
        if self.kind not in ("common", "chips") or not isinstance(self.lots, Mapping):
            raise ValueError("Некорректный профиль лотов")
        result = []
        for lot_id, value in self.lots.items():
            options = dict(value) if isinstance(value, Mapping) else {}
            raw_quantity = options.get("quantity", value)
            if isinstance(raw_quantity, bool):
                raise ValueError("Количество должно быть числом")
            quantity = int(raw_quantity)
            category = int(options.get("category", self.category_id))
            kind = options.get("kind", self.kind)
            if category <= 0 or quantity <= 0 or kind not in ("common", "chips"):
                raise ValueError("Неверная категория, тип или количество")
            lot_id = str(lot_id)
            if kind == "common" and not lot_id.isdigit():
                raise ValueError("Обычный лот: числовой ID")
            if kind == "chips":
                parts = lot_id.split("-")
                if (
                    len(parts) < 4
                    or not all(p.isdigit() for p in parts)
                    or int(parts[2]) != category
                ):
                    raise ValueError("Нужен полный ID chips указанной категории")
            result.append(ManagedLot(lot_id, category, kind, quantity, options))
        return result


class CoreLotDumper:
    """Internal worker called by plugin commerce runtimes. No UI or independent schedule."""

    def __init__(self, app):
        self.app = app

    async def reprice(self, profile, floor_for, *, floor_only=False):
        if not isinstance(profile, LotProfile):
            raise TypeError("Плагин должен передать LotProfile")
        result = {"checked": 0, "changed": 0, "errors": 0}
        cache = {}
        app = self.app
        if not app.cfg.flag("SellerStatus", "online"):
            return result
        records = app.state.d.setdefault("lot_worker", {}).setdefault(
            profile.owner, {"last_results": {}}
        )
        settings = (
            app.state.d["plugin_state"]
            .setdefault(profile.owner, {})
            .setdefault("lot_settings", {})
        )
        for spec in profile.entries():
            if spec.options.get("enabled", True) is False:
                continue
            key = spec.lot_id
            try:
                lock_key = f"chips:{spec.category_id}" if spec.kind == "chips" else key
                async with app.lot_locks.setdefault(lock_key, asyncio.Lock()):
                    fields = (
                        await app.fp.chip_floor(spec.category_id, key)
                        if spec.kind == "chips"
                        else await app.fp.lot_floor(int(key))
                    )
                    result["checked"] += 1
                    if str(fields["category"]) != str(spec.category_id):
                        raise ValueError("Категория предложения не совпадает")
                    coefficient = Decimal(fields["category_rate"])
                    if not 0 < coefficient <= 1:
                        raise ValueError("Неизвестна комиссия категории")
                    floor = Decimal(str(floor_for(spec)))
                    if not floor.is_finite() or floor <= 0:
                        raise ValueError("Защитная цена должна быть положительной")
                    floor = max(floor, Decimal(str(spec.options.get("minimum", "0"))))
                    seller_tick = Decimal(
                        str(
                            spec.options.get(
                                "seller_tick",
                                "0.0001" if spec.kind == "chips" else "0.01",
                            )
                        )
                    )
                    if not seller_tick.is_finite() or seller_tick <= 0:
                        raise ValueError("Некорректная точность цены")
                    floor = (floor / seller_tick).to_integral_value(
                        rounding=ROUND_CEILING
                    ) * seller_tick
                    state = settings.setdefault(key, {})
                    rules = {
                        "minimum_rating": 3,
                        "ignore_offline": True,
                        "match_quantity": True,
                        "keywords": state.get("keywords", ""),
                    }
                    rules.update(spec.options)
                    state.update(
                        min=str(floor),
                        match_quantity=spec.quantity,
                        kind=spec.kind,
                        category=spec.category_id,
                        category_rate=str(coefficient),
                    )
                    if floor_only:
                        price = max(floor, Decimal(str(fields["price"])))
                    else:
                        cache_key = (spec.kind, spec.category_id)
                        if cache_key not in cache:
                            cache[cache_key] = await app.fp.lots(
                                spec.category_id, kind=spec.kind
                            )
                        offers = cache[cache_key]
                        if spec.kind == "chips":
                            suffix = key.split("-")[3:]
                            offers = [
                                offer
                                for offer in offers
                                if len(str(offer.get("id", "")).split("-")) >= 4
                                and str(offer["id"]).split("-")[3:] == suffix
                            ]
                        if rules.get("maximum"):
                            rules["maximum"] = str(
                                Decimal(str(rules["maximum"])) / coefficient
                            )
                        target, stats = UniversalDumper.choose(
                            offers,
                            rules,
                            floor / coefficient,
                            own_name=app.seller_name(),
                            quantity=spec.quantity if spec.kind == "common" else None,
                            parse_quantity=profile.parse_quantity,
                        )
                        price = max(
                            floor,
                            (target * coefficient / seller_tick).to_integral_value(
                                rounding=ROUND_CEILING
                            )
                            * seller_tick,
                        )
                        if spec.options.get("maximum"):
                            maximum = (
                                Decimal(str(spec.options["maximum"])) / seller_tick
                            ).to_integral_value(rounding=ROUND_FLOOR) * seller_tick
                            if maximum < floor:
                                raise ValueError("Максимум ниже защитной цены")
                            price = min(price, maximum)
                        records["last_results"][key] = {
                            "at": time.time(),
                            "price": str(price),
                            "kind": spec.kind,
                            **stats,
                        }
                    if price != Decimal(str(fields["price"])):
                        if spec.kind == "chips":
                            await app.fp.chip_update(spec.category_id, key, price=price)
                        else:
                            await app.fp.lot_update(int(key), price=price)
                        result["changed"] += 1
            except Exception:
                result["errors"] += 1
                LOG.exception("Ошибка воркера лотов %s: %s", profile.owner, key)
        records["health"] = {"at": time.time(), **result}
        app.state.save()
        return result
