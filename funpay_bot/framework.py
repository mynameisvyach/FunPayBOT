"""Контракты плагинов, контекст, маршрутизация и жизненный цикл."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .config import is_placeholder, load_parser, settings_payload
from .metadata import ROOT
from .primitives import ActivityGeneration, SerialTaskQueue, TimerRegistry
from .storage import atomic_write


class PluginConfigStore:
    """Хранит JSON-настройки и предоставляет доступ к разделам через ConfigParser."""

    def __init__(self, path, defaults=None):
        self.path = Path(path).with_suffix(".json")
        self.data = load_parser(self.path, defaults or {"Plugin": {}, "Provider": {}})
        self.save()

    def save(self):
        atomic_write(self.path, settings_payload(self.data))


PLUGIN_SECRET_FIELDS = {}


class CallbackRouter:
    """Хранит маршруты плагина; авторизацию и разделение тем обеспечивает ядро."""

    def __init__(self):
        self.exact = {}
        self.prefixes = {}

    def on(self, action, handler):
        if action in self.exact:
            raise ValueError("Callback уже зарегистрирован: " + action)
        self.exact[action] = handler

    def on_prefix(self, prefix, handler):
        if prefix in self.prefixes:
            raise ValueError("Префикс уже зарегистрирован: " + prefix)
        self.prefixes[prefix] = handler

    async def dispatch(self, action, user):
        handler = self.exact.get(action)
        if handler is None:
            handler = next(
                (
                    self.prefixes[p]
                    for p in sorted(self.prefixes, key=len, reverse=True)
                    if action.startswith(p)
                ),
                None,
            )
        if handler is None:
            return False
        result = handler(action, user)
        if inspect.isawaitable(result):
            await result
        return True


class PluginRuntime:
    """Управляет жизненным циклом, компонентами, обработчиками и асинхронными задачами плагина."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.components = {}
        self.capabilities = {}
        self.hooks = {}
        self.router = CallbackRouter()
        self.tasks = {}
        self.health = {}
        self.started = False
        self.ready = False
        self.initializing = False
        self.prepared = False
        self.initialization_error = None
        self.initialization_complete = asyncio.Event()
        self.start_gate = getattr(ctx.app, "_plugin_start_gate", None)
        self.generation = ActivityGeneration()
        self.timers = TimerRegistry()
        self.queue = SerialTaskQueue()
        self.state = (
            ctx.app.state.d["plugin_state"]
            .setdefault(ctx.id, {})
            .setdefault("_runtime", {})
        )
        self.log = getattr(
            ctx, "logger", logging.getLogger("FunPayBOT.plugins." + ctx.id)
        )

    def register(self, name, component):
        if name in self.components:
            raise ValueError("Компонент уже зарегистрирован: " + name)
        self.components[name] = component
        return component

    def provide(self, name, service):
        if name in self.capabilities:
            raise ValueError("Возможность уже зарегистрирована: " + name)
        self.capabilities[name] = service

    def on(self, event, handler):
        self.hooks.setdefault(event, []).append(handler)

    async def emit(self, event, *args):
        for handler in tuple(self.hooks.get(event, ())):
            value = handler(*args)
            if inspect.isawaitable(value):
                await value

    def start_worker(self, name, worker, *, initialization=False):
        """Запускает подготовку сразу, а рабочие задачи — после общего разрешения ядра."""
        existing = self.tasks.get(name)
        if existing and not existing.done():
            return existing
        self.health[name] = {"running": True, "error": None, "at": time.time()}

        async def run():
            try:
                if not initialization:
                    await self.wait_running()
                await worker()
                if initialization and not self.prepared:
                    self.initialization_error = RuntimeError(
                        "Подготовка плагина завершилась без подтверждения готовности"
                    )
                    self.initialization_complete.set()
                self.health[name]["at"] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.health[name]["error"] = type(error).__name__
                if initialization and not self.prepared:
                    self.initialization_error = error
                    self.initialization_complete.set()
                self.log.exception("Ошибка задачи %s", name)
            finally:
                self.health[name]["running"] = False

        self.tasks[name] = asyncio.create_task(run(), name=self.ctx.id + ":" + name)
        return self.tasks[name]

    def begin_initialization(self, message="Инициализация..."):
        """Показывает один старт и сбрасывает готовность компонента."""
        if self.initializing and not self.ready:
            return
        self.ready = False
        self.prepared = False
        self.initialization_error = None
        self.initialization_complete.clear()
        self.initializing = True
        self.log.info("Инициализация...")

    def mark_ready(self):
        """Подтверждает подготовку; объявляет готовность только после общего этапа инициализации."""
        self.prepared = True
        self.initialization_complete.set()
        if self.start_gate is None or self.start_gate.is_set():
            self.activate()

    def activate(self):
        """Завершает инициализацию одним сообщением после разрешения общего запуска."""
        if self.started and self.prepared and not self.ready:
            self.ready = True
            self.initializing = False
            self.log.info("Готов к работе")

    async def wait_running(self):
        """Удерживает рабочие задачи до завершения подготовки всех плагинов."""
        if self.start_gate is not None:
            await self.start_gate.wait()

    async def wait_prepared(self, timeout=90):
        """Дожидается готовности либо возвращает исходную ошибку подготовки."""
        try:
            await asyncio.wait_for(self.initialization_complete.wait(), timeout)
        except TimeoutError:
            raise RuntimeError(
                "Истекло время подготовки плагина " + self.ctx.id
            ) from None
        if self.initialization_error is not None:
            raise self.initialization_error

    async def start(self):
        if self.started:
            return
        self.started = True
        self.generation.advance()
        self.queue.closed = False
        try:
            await self.emit("start")
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        try:
            if self.started:
                await self.emit("stop")
        finally:
            self.started = False
            self.ready = False
            self.initializing = False
            self.prepared = False
            self.initialization_complete.clear()
            self.generation.advance()
            self.queue.close()
            await self.timers.close()
            for task in self.tasks.values():
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self.tasks.clear()


class FrameworkPlugin:
    """Базовый класс расширения; торговые функции и API поставщика необязательны."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.runtime = PluginRuntime(ctx)
        PLUGIN_SECRET_FIELDS[ctx.id] = {
            field
            for field, descriptor in getattr(self, "INSTALL_FIELDS", {}).items()
            if descriptor[1]
        }

    async def start(self):
        if self.runtime.started:
            return
        self.runtime.begin_initialization("Инициализация...")
        await self.runtime.start()
        if not getattr(self, "DEFER_READY", False):
            self.runtime.mark_ready()

    async def stop(self):
        await self.runtime.stop()

    async def tick(self):
        await self.runtime.emit("tick")

    async def event(self, event):
        await self.runtime.emit("event", event)

    async def callback(self, action, user):
        return await self.runtime.router.dispatch(action, user)

    async def message(self, text, user):
        await self.runtime.emit("message", text, user)

    def statistics_snapshot(self):
        exporter = self.runtime.capabilities.get("statistics")
        return exporter() if exporter else None


class BoundComponent:
    """Связывает компонент с экземпляром плагина без глобального состояния."""

    def __init__(self, plugin):
        self.plugin = plugin

    def __getattr__(self, key):
        return getattr(self.plugin, key)


class ProviderAdapter(BoundComponent):
    """Определяет контракт API поставщика; его запросы и проверки реализует плагин."""

    REQUIRED_FIELDS = ()
    FIELD_VALIDATORS = {}

    @classmethod
    def configuration_issues(cls, values):
        issues = []
        for key in cls.REQUIRED_FIELDS:
            value = values.get(key, "").strip()
            if not value or is_placeholder(value):
                issues.append(
                    {
                        "key": key,
                        "reason": "Замените YOUR-* и заполните поле своим значением",
                    }
                )
        missing = {row["key"] for row in issues}
        for key, validator in cls.FIELD_VALIDATORS.items():
            if key in missing:
                continue
            try:
                validator(values.get(key, ""))
            except (ValueError, TypeError) as error:
                issues.append({"key": key, "reason": str(error)})
        return issues

    def validate_credentials(self):
        issues = self.configuration_issues(self.plugin.config["Provider"])
        if issues:
            raise ValueError(issues[0]["key"] + ": " + issues[0]["reason"])

    def validate_setting(self, key, values):
        descriptor = self.plugin.SETTINGS_SCHEMA[key]
        if descriptor.get("section", "Plugin") != "Provider":
            return
        proposed = dict(self.plugin.config["Provider"])
        proposed.update(values)
        issues = [
            row for row in self.configuration_issues(proposed) if row["key"] in values
        ]
        if issues:
            raise ValueError(issues[0]["key"] + ": " + issues[0]["reason"])

    def receipt_url(self, order):
        return None

    def operation_reference(self, order):
        return order.get("reference", order["order_id"])

    def extra_operation_cost(self):
        return Decimal("0")


class ProductAdapter(BoundComponent):
    """Разбирает данные и команды покупателя по правилам, объявленным товарным плагином."""

    QUANTITY_PATTERNS = ()
    RECIPIENT_PATTERN = None
    CATALOG_COMMANDS = ()
    HELP_COMMANDS = {}

    def paid_units(self, quantity):
        return int(quantity)

    def parse_quantity(self, text):
        match = next(
            (
                match
                for pattern in self.QUANTITY_PATTERNS
                if (match := re.search(pattern, text, re.I))
            ),
            None,
        )
        if not match:
            return None
        quantity = int(re.sub(r"\D", "", match.group(1)))
        return quantity if self.valid_quantity(quantity) else None

    def recipient_candidates(self, event):
        candidates = [
            str(event[key]) for key in ("recipient", "player") if event.get(key)
        ]
        for key in ("params", "buyer_params"):
            values = event.get(key, {})
            if isinstance(values, Mapping):
                candidates.extend(str(value) for value in values.values())
        if self.RECIPIENT_PATTERN:
            candidates.extend(
                match.group(0)
                for match in re.finditer(
                    self.RECIPIENT_PATTERN, event.get("description", ""), re.I
                )
            )
        return candidates

    async def buyer_command(self, event):
        raw = event.get("text", "").casefold()
        for command, text in self.HELP_COMMANDS.items():
            if command.casefold() in raw:
                await self.ctx.send_buyer(
                    event, text, command.lstrip("!") + "_help:" + event["id"]
                )
                return True
        if any(command.casefold() in raw for command in self.CATALOG_COMMANDS):
            return await self.plugin.send_product_catalog(event)
        return False


class SettingsSchema:
    """Описывает типы и ограничения полей, явно заданные плагином."""

    @staticmethod
    def validate(descriptor, text):
        fields = descriptor["fields"]
        kind = descriptor.get("kind", "text")
        values = json.loads(text) if len(fields) > 1 else {fields[0]: text}
        if not isinstance(values, dict) or set(values) != set(fields):
            raise ValueError("Нужны поля: " + ", ".join(fields))
        if len(fields) == 1:
            if kind in ("number", "integer"):
                value = Decimal(str(text).replace(",", "."))
                if not value.is_finite():
                    raise ValueError("Нужно конечное число")
                if kind == "integer" and value != int(value):
                    raise ValueError("Нужно целое число")
                if "minimum" in descriptor and value < Decimal(
                    str(descriptor["minimum"])
                ):
                    raise ValueError("Значение меньше допустимого")
                if "maximum" in descriptor and value > Decimal(
                    str(descriptor["maximum"])
                ):
                    raise ValueError("Значение больше допустимого")
                values[fields[0]] = str(int(value)) if kind == "integer" else str(value)
            elif kind == "bool":
                if str(text).lower() not in ("true", "false"):
                    raise ValueError("Нужно true или false")
                values[fields[0]] = str(text).lower()
            if (
                descriptor.get("choices")
                and values[fields[0]] not in descriptor["choices"]
            ):
                raise ValueError("Выберите допустимое значение")
        validator = descriptor.get("validator")
        if validator:
            validator(values)
        return {key: str(value) for key, value in values.items()}


def plugin_capability(plugin, name):
    runtime = getattr(plugin, "runtime", None)
    if runtime and name in runtime.capabilities:
        return runtime.capabilities[name]
    return None


def configured_plugin_names(cfg):
    selected = cfg.get("Plugins", "enabled", "auto").strip()
    names = (
        [
            path.stem
            for path in (ROOT / "plugins").glob("*.py")
            if not path.name.startswith("_")
        ]
        if selected == "auto"
        else [name.strip() for name in selected.split(",") if name.strip()]
    )
    result = []
    for name in sorted(set(names)):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
            raise ValueError("Некорректное имя плагина")
        if (ROOT / "plugins" / (name + ".py")).is_file():
            result.append(name)
    return result


def load_plugin_module(name):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
        raise ValueError("Некорректное имя плагина")
    spec = importlib.util.spec_from_file_location(
        "funpaybot_plugin_" + name, ROOT / "plugins" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if getattr(module, "API_VERSION", None) != 1:
        raise ValueError(f"{name}: нужен контракт плагина API_VERSION = 1")
    return module


class PluginContext:
    """Предоставляет контекст доверенному плагину; маршрутизацию интерфейса контролирует ядро."""

    def __init__(self, app, plugin_id):
        from .logging_setup import plugin_logger

        self.app, self.id = app, plugin_id
        self.logger = plugin_logger(app.cfg, app.state.path.parent, plugin_id)

    def config_store(self, defaults=None):
        return PluginConfigStore(
            self.app.cfg.path.parent / "plugins" / self.id / "config.json", defaults
        )

    @property
    def state(self):
        return self.app.state.d["plugin_state"].setdefault(self.id, {})

    def save_state(self):
        self.app.state.save()

    async def say(self, text):
        await self.app.say("plugin:" + self.id, text)

    async def card(
        self, text, buttons=(), *, parse_mode=None, new_message=False, main_menu=False
    ):
        rows = []
        for row in buttons:
            converted = []
            for cell in row:
                if isinstance(cell, dict):
                    cell = dict(cell)
                    if "callback_data" in cell:
                        cell["callback_data"] = f"{self.id}:" + cell["callback_data"]
                    converted.append(cell)
                else:
                    label, action = cell
                    converted.append((label, f"{self.id}:{action}"))
            rows.append(converted)
        if not main_menu:
            rows = [
                [
                    cell
                    for cell in row
                    if (
                        cell.get("callback_data", "")
                        if isinstance(cell, dict)
                        else cell[1]
                    ).split(":")[-1]
                    != "close"
                ]
                for row in rows
            ]
            rows = [row for row in rows if row]
            has_back = False
            for row in rows:
                for index, cell in enumerate(row):
                    label = cell.get("text", "") if isinstance(cell, dict) else cell[0]
                    action = (
                        cell.get("callback_data", "")
                        if isinstance(cell, dict)
                        else cell[1]
                    )
                    if label.startswith(("‹", "<")) or label in ("Отмена", "Назад"):
                        target = action.split(":", 1)[-1].split(":", 1)[0]
                        title = {
                            "menu": "Меню",
                            "knowledge": "База знаний",
                            "questions": "Вопросы",
                            "settings": "Настройки",
                            "lot": "Товар",
                            "question": "Вопрос",
                        }.get(
                            target,
                            label.lstrip("‹< ")
                            if label not in ("Отмена", "Назад")
                            else "Меню",
                        )
                        replacement = "< " + title
                        if isinstance(cell, dict):
                            cell["text"] = replacement
                        else:
                            row[index] = (replacement, action)
                        has_back = True
            if not has_back:
                rows.append([("< Меню", self.id + ":menu")])
        await self.app.card(
            "plugin:" + self.id,
            "menu",
            text,
            rows,
            parse_mode=parse_mode,
            new_message=new_message,
            plugin_main=main_menu,
        )

    async def notify_main(self, text):
        await self.app.say("main", text)

    async def publish_rates(self, text):
        """Обновляет общую карточку только рыночными данными CoinGecko."""
        await self.app.rates(force=True)

    async def publish_report(self, text):
        # Plugins contribute to the common report, never publish separate reports.
        self.app.state.d.setdefault("report_notes", {})[self.id] = {
            "text": str(text),
            "at": time.time(),
        }
        self.app.state.save()

    def render(self, text, event):
        """Подставляет общие переменные сообщения или заказа в шаблон плагина."""
        return self.app.render(text, event)

    async def send_buyer(self, event, text, action_id, *, rendered=False):
        """Отправляет шаблон или готовый текст через общий журнал защиты от повторов."""
        await self.app.send_buyer(
            event, text, f"plugin:{self.id}:{action_id}", rendered=rendered
        )

    async def claim_order(self, event):
        result = self.app.claim_order(self.id, event)
        self.app.state.save()
        return result

    async def order_progress(self, event, phase, description=None):
        if self.app.claim_order(self.id, event):
            self.app.record_order_progress(event["order_id"], phase, description)
            self.app.state.save()

    async def fulfilled(self, event, *, details=None):
        await self.app.fulfilled(event, plugin_id=self.id, details=details)

    async def file(self, name, body):
        await self.app.tg.call(
            "send_document",
            chat_id=self.app.group,
            message_thread_id=self.app.state.d["topics"]["plugin:" + self.id],
            document=(name, body),
        )
