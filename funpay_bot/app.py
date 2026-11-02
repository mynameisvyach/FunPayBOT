"""Сборка приложения, жизненный цикл и главное меню."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from .automation import ReleaseFeatures
from .config import Config
from .dependencies import dependency_issues
from .framework import (
    PluginContext,
    configured_plugin_names,
    load_plugin_module,
)
from .handlers.automation_ui import AutomationUIMixin
from .handlers.chats import ChatsMixin
from .handlers.orders import OrdersMixin
from .handlers.reports import ReportsMixin
from .handlers.scheduler import SchedulerMixin
from .handlers.topics import TopicsMixin
from .handlers.updates import UpdatesMixin
from .lots import CoreLotDumper
from .management import ManagementFeatures
from .metadata import LOG, ROOT, TITLES, VERSION


class App(
    TopicsMixin,
    OrdersMixin,
    AutomationUIMixin,
    UpdatesMixin,
    ChatsMixin,
    SchedulerMixin,
    ReportsMixin,
    ManagementFeatures,
    ReleaseFeatures,
):
    def __init__(self, cfg, state, tg, fp):
        self.cfg, self.state, self.tg, self.fp = cfg, state, tg, fp
        self.tg.callback_encoder = self.encode_callback
        self.tg.callback_recorder = self.record_message_callbacks
        self.group = int(cfg.get("Telegram", "group_id", "0"))
        binding = {"group": self.group}
        previous = state.d.get("binding", binding)
        if previous.get("group") != self.group:
            raise ValueError(
                "state.json принадлежит другой группе. Сохраните его отдельно перед переключением."
            )
        state.d["binding"] = binding
        state.save()
        self.lock = asyncio.Lock()
        self.cash_lock = asyncio.Lock()
        self.card_locks = {}
        self.rates_lock = asyncio.Lock()
        self.rates_created = False
        self.topic_icons = None
        self.topic_icons_retry_at = 0
        self.topic_locks = {}
        self.plugins = {}
        self.dormant_plugins = {}
        self.tools_manager = None
        self.tools_screen_active = False
        self.pending = {}
        self.counts = {"events": 0, "duplicates": 0, "errors": 0}
        self.last_event = "—"
        self.baseline = set()
        self.lot_locks = {}
        self.lot_dumper = CoreLotDumper(self)
        self.service_pending = {}
        self.restart_requested = False
        self.started = time.time()

    async def boot(self):
        await self.topic("main", "⚙️ Главное")
        await self.topic("orders", "📦 Заказы")
        await self.reload_plugins()
        await self.refresh_all_chat_controls()
        await self.menu()
        for row in self.state.d.get("notification_outbox", {}).values():
            if row.get("status") == "sending":
                row["status"] = "unknown"
        unknown = sum(
            v["status"] in ("pending", "unknown")
            for v in self.state.d["actions"].values()
        )
        unknown += sum(
            v["status"] in ("pending", "unknown")
            for v in self.state.d["withdrawals"].values()
        )
        unknown += sum(
            v.get("status") == "unknown"
            for v in self.state.d.get("notification_outbox", {}).values()
        )
        if unknown:
            await self.say(
                "main",
                f"⚠ После предыдущего запуска требуют сверки {unknown} действий. Автоповтор отключён; проверьте журнал, историю вывода и FunPay.",
            )

    @staticmethod
    def plugin_icon(name):
        prefix = str(name).strip().split(" ", 1)[0]
        return prefix if prefix and not prefix[0].isalnum() else None

    async def reload_plugins(self, *, force=False):
        fresh = Config(self.cfg.path)
        self.cfg.c["Plugins"] = dict(fresh.c["Plugins"])
        names = configured_plugin_names(self.cfg)
        wanted = set()
        disabled = self.state.d["plugin_disabled"]
        failures = self.state.d.setdefault("plugin_load_failures", {})
        start_gate = asyncio.Event()
        prepared_plugins = []
        self._plugin_start_gate = start_gate
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
                raise ValueError("Некорректное имя плагина")
            path = ROOT / "plugins" / (name + ".py")
            if not path.exists() or name in disabled:
                continue
            wanted.add(name)
            if name in self.plugins:
                continue
            config_path = self.cfg.path.parent / "plugins" / name / "config.json"
            fingerprint = hashlib.sha256(
                path.read_bytes()
                + b"\0"
                + (config_path.read_bytes() if config_path.exists() else b"")
            ).hexdigest()
            failure = failures.get(name, {})
            dependency_installed = False
            missing = failure.get("missing_module")
            if missing:
                import importlib.util

                try:
                    dependency_installed = importlib.util.find_spec(missing) is not None
                except (ImportError, ValueError, AttributeError):
                    pass
            if (
                not force
                and failure.get("fingerprint") == fingerprint
                and time.time() < failure.get("retry_at", 0)
                and not dependency_installed
            ):
                continue
            plugin = None
            try:
                issues = await asyncio.to_thread(dependency_issues, (name,))
                if issues:
                    # Install in a fresh process; do not mutate packages while orders run.
                    notice = hashlib.sha256(repr(issues).encode("utf-8")).hexdigest()
                    if failure.get("dependency_restart_notice") != notice:
                        failures[name] = {
                            **failure,
                            "dependency_restart_notice": notice,
                        }
                        self.state.save()
                        await self.say(
                            "main",
                            f"📦 Для плагина {name} нужны зависимости. Бот перезапустится, установит объявленные плагином пакеты и продолжит работу.",
                        )
                    self.state.save()
                    self.restart_requested = True
                    for prepared in prepared_plugins:
                        await prepared.stop()
                    self._plugin_start_gate = None
                    return
                module = load_plugin_module(name)
                from .logging_setup import register_plugin_source

                register_plugin_source(name, module.NAME)
                if name not in self.state.d.get("plugin_metadata", {}):
                    LOG.debug(
                        "Обнаружен новый плагин: %s. Подготавливаю к работе.",
                        module.NAME,
                    )
                context = PluginContext(self, name)
                plugin = module.Plugin(context)
                if (
                    getattr(module, "TOPIC", True)
                    or "plugin:" + name in self.state.d["topics"]
                ):
                    await self.topic(
                        "plugin:" + name,
                        module.NAME,
                        icon_emoji=self.plugin_icon(module.NAME)
                        or getattr(module, "TOPIC_ICON", None),
                    )
                await plugin.start()
                runtime = getattr(plugin, "runtime", None)
                if runtime is not None:
                    await runtime.wait_prepared()
                prepared_plugins.append(plugin)
                self.plugins[name] = plugin
                self.dormant_plugins.pop(name, None)
                if name in failures:
                    failures.pop(name, None)
                    self.state.save()
                self.state.d.setdefault("plugin_metadata", {})[name] = {
                    "title": getattr(module, "NAME", name),
                    "version": getattr(module, "VERSION", ""),
                    "topic_icon": self.plugin_icon(module.NAME)
                    or getattr(module, "TOPIC_ICON", None),
                }
            except Exception as error:
                wanted.discard(name)
                if plugin is not None:
                    try:
                        await plugin.stop()
                    except Exception:
                        LOG.exception(
                            "Не удалось завершить не запустившийся плагин %s", name
                        )
                missing = (
                    getattr(error, "name", "")
                    if isinstance(error, ModuleNotFoundError)
                    else ""
                )
                if not isinstance(missing, str) or not re.fullmatch(
                    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", missing
                ):
                    missing = ""
                signature = (
                    type(error).__name__
                    + ":"
                    + (
                        missing
                        or hashlib.sha256(
                            str(error).encode("utf-8", errors="replace")
                        ).hexdigest()
                    )
                )
                attempts = (
                    failure.get("attempts", 0) + 1
                    if failure.get("fingerprint") == fingerprint
                    else 1
                )
                delay = (30, 60, 120, 300, 900, 1800)[min(attempts - 1, 5)]
                should_notify = failure.get("notified_signature") != signature
                failures[name] = {
                    "fingerprint": fingerprint,
                    "attempts": attempts,
                    "retry_at": time.time() + delay,
                    "missing_module": missing,
                    "notified_signature": signature,
                    "dependency_restart_notice": failure.get(
                        "dependency_restart_notice", ""
                    ),
                }
                self.state.save()
                if should_notify or attempts == 1:
                    LOG.exception(
                        "Не удалось загрузить плагин %s; следующая попытка через %s с",
                        name,
                        delay,
                    )
                else:
                    LOG.debug("Плагин %s пока не готов; повтор через %s с", name, delay)
                if should_notify:
                    detail = (
                        (
                            "Нет Python-модуля "
                            + missing
                            + ". Перезапустите бот для установки зависимостей."
                        )
                        if missing
                        else "Проверьте контракт API_VERSION = 1, настройки и зависимости."
                    )
                    await self.say(
                        "main",
                        f"⚠️ Плагин {name} не загружен: {type(error).__name__}. {detail} Повторные одинаковые уведомления отключены.",
                    )
        # Сначала подготовлены все плагины; только теперь разрешается рабочая фаза.
        start_gate.set()
        for plugin in prepared_plugins:
            runtime = getattr(plugin, "runtime", None)
            if runtime is not None:
                runtime.activate()
        self._plugin_start_gate = None
        for name in list(self.plugins):
            if name not in wanted:
                plugin = self.plugins.pop(name)
                if hasattr(plugin, "stop"):
                    await plugin.stop()
                self.dormant_plugins[name] = plugin
        for name, metadata in self.state.d.get("plugin_metadata", {}).items():
            if name not in names:
                continue
            if "plugin:" + name in self.state.d["topics"]:
                await self.topic(
                    "plugin:" + name,
                    metadata.get("title", name),
                    icon_emoji=metadata.get("topic_icon"),
                )
        self.state.save()
        await self.sync_commands()

    async def sync_commands(self):
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.types import BotCommand, BotCommandScopeChat

        commands = [
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(
                command="backup", description="Загрузить / выгрузить резервную копию"
            ),
        ]
        commands += [
            BotCommand(command=n, description=d)
            for n, d in [
                ("lots", "Менеджер лотов"),
                ("proxy", "Прокси Telegram и FunPay"),
                ("golden_key", "Сменить golden_key"),
                ("restart", "Перезапустить бота"),
            ]
        ]
        commands += [
            BotCommand(
                command=name,
                description=("Открыть " + getattr(plugin, "TITLE", name))[:256],
            )
            for name, plugin in sorted(self.plugins.items())
        ]
        signature = hashlib.sha256(
            json.dumps(
                {
                    "commands": [c.model_dump() for c in commands],
                    "group": self.group,
                    "identity": self.state.d.get("identity", {}),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        synced = self.state.d.setdefault("command_sync", {})
        scope_key = "chat:" + str(self.group)
        previous = synced.get(scope_key, {})
        if (
            previous.get("signature") == signature
            or previous.get("retry_at", 0) > time.time()
        ):
            return
        try:
            await self.tg.bot.set_my_commands(
                commands, scope=BotCommandScopeChat(chat_id=self.group)
            )
        except TelegramRetryAfter as error:
            synced[scope_key] = {"retry_at": time.time() + error.retry_after + 1}
            LOG.warning(
                "Telegram отложил регистрацию команд группы на %s сек.",
                error.retry_after,
            )
        except Exception:
            synced[scope_key] = {"retry_at": time.time() + 60}
            LOG.exception(
                "Не удалось зарегистрировать команды группы; повтор через 60 сек."
            )
        else:
            synced[scope_key] = {"signature": signature}
            LOG.debug("Команды Telegram обновлены только для группы %s", self.group)
        self.state.save()

    def status(self, section, key="enabled"):
        return "🟢" if self.cfg.flag(section, key) else "⚪"

    async def menu(self, screen="main", *, new_message=False):
        if screen == "finance":
            await self.finances()
            return
        back = [("‹ Главное", "menu:main")]
        if screen == "main":
            text = f"⚙️ FunPay BOT · {VERSION}\nЧаты FunPay: {'синхронизация включена' if self.cfg.flag('SellerStatus', 'online') else 'приостановлены — включите Онлайн'}\nПлагины: {len(self.plugins)} · Чаты: {sum(k.startswith('chat:') for k in self.state.d['topics'])}"
            rows = [
                [("🤖 Автоматизация", "menu:automation"), ("⚙️ Лоты", "lots_menu")],
                [("🧩 Плагины", "menu:plugins"), ("👤 Профиль", "profile")],
                [
                    (
                        f"{self.status('SellerStatus', 'online')} Онлайн / офлайн",
                        "toggle:SellerStatus:online",
                    )
                ],
                [
                    (
                        f"{self.status('AutoRaise')} Поднятие лотов",
                        "toggle:AutoRaise:enabled",
                    )
                ],
                [("💰 Финансы", "menu:finance")],
                [("📊 Отчёт", "report")],
                [("📋 Логи", "logs_menu"), ("🔔 Уведомления", "notifications_menu")],
            ]
        elif screen == "automation":
            text = "🤖 Автоматизация\nВыберите раздел: тумблеры, тексты и параметры."
            rows = (
                [[(title, "section:" + s)] for s, title in TITLES.items()]
                + [[("💬 Статус продавца", "menu:status")]]
                + [back]
            )
        elif screen == "plugins":
            text = "🧩 Плагины\nНазвание открывает меню плагина. Справа — запуск и остановка. Выдача включается в меню самого плагина."
            rows = self.system_rows() + [
                [("🔄 Обновить список", "reload_plugins")],
                back,
            ]
        elif screen == "status":
            command = self.cfg.get("SellerStatus", "command", "#status")
            text = f"💬 Последняя активность продавца\nУчитываются ваши сообщения и нажатия в Telegram, а также сообщения продавца, полученные с FunPay. Просмотры страниц и другие невидимые для API действия определить нельзя. Действия бота и плагинов время не обновляют."
            rows = [
                [
                    (
                        self.status("SellerStatus") + " Команда статуса",
                        "toggle:SellerStatus:enabled",
                    )
                ],
                [("✏ Изменить команду", "edit:SellerStatus:command")],
                [("Текст с активностью", "edit:SellerStatus:active_text")],
                [("Текст без данных", "edit:SellerStatus:unknown_text")],
                [("‹ Автоматизация", "menu:automation")],
            ]
        else:
            return
        await self.card("main", "menu", text, rows, new_message=new_message)

    async def file_to_main(self, name, path):
        if not path.exists():
            await self.say("main", "Файл пока пуст.")
            return
        await self.tg.call(
            "send_document",
            chat_id=self.group,
            message_thread_id=self.state.d["topics"]["main"],
            document=(name, path.read_bytes()),
        )
