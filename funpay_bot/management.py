"""Управление доступами, прокси и резервными копиями."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import secrets
import stat
import time
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .automation import TicketClassifier
from .commerce import CommercePlugin
from .finance import money
from .framework import configured_plugin_names, load_plugin_module, plugin_capability
from .lot_tools import LotToolsManager
from .lots import LotProfile
from .storage import atomic_write


class BackupArchive:
    limit = 40 * 1024 * 1024

    @staticmethod
    def allowed(name):
        return name in ("config.json", "data/state.json") or bool(
            re.fullmatch(r"plugins/[a-z][a-z0-9_]{0,23}/config\.json", name)
        )

    @classmethod
    def build(cls, root, state, config_path=None):
        files = {
            "config.json": Path(config_path or root / "config.json").read_bytes(),
            "data/state.json": json.dumps(state, ensure_ascii=False, indent=2).encode(),
        }
        for path in sorted((root / "plugins").glob("*/*.json")):
            relative = path.relative_to(root).as_posix()
            if cls.allowed(relative):
                files[relative] = path.read_bytes()
        manifest = {
            "format": "unified-backup-v2",
            "created_at": time.time(),
            "sha256": {
                name: hashlib.sha256(body).hexdigest() for name, body in files.items()
            },
        }
        body = io.BytesIO()
        with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, content in files.items():
                archive.writestr(name, content)
        if body.tell() > cls.limit:
            raise ValueError("ZIP превышает лимит 40 МБ")
        return body.getvalue()

    @classmethod
    def read(cls, body):
        if len(body) > cls.limit:
            raise ValueError("Максимум 40 МБ")
        files = {}
        total = 0
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if len(archive.infolist()) > 500:
                raise ValueError("Слишком много файлов")
            for item in archive.infolist():
                name = item.filename
                path = PurePosixPath(name)
                if item.is_dir():
                    continue
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or str(path) != name
                    or name in files
                ):
                    raise ValueError("Недопустимое имя или повтор файла")
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Символические ссылки запрещены")
                if name != "manifest.json" and not cls.allowed(name):
                    raise ValueError("Недопустимый файл: " + name)
                total += item.file_size
                if total > cls.limit:
                    raise ValueError("Распакованные данные превышают 40 МБ")
                content = archive.read(item)
                if not isinstance(json.loads(content), dict):
                    raise ValueError("Ожидаются JSON-объекты")
                files[name] = content
        manifest = json.loads(files.pop("manifest.json", b"{}"))
        if manifest.get("format") != "unified-backup-v2" or not {
            "config.json",
            "data/state.json",
        } <= set(files):
            raise ValueError("Нужен полный архив резервной копии FunPayBOT")
        hashes = manifest.get("sha256", {})
        if set(hashes) != set(files) or any(
            hashlib.sha256(body).hexdigest() != hashes[name]
            for name, body in files.items()
        ):
            raise ValueError("Контрольные суммы не совпадают")
        return files

    @staticmethod
    def validate_state(current, restored):
        if restored.get("identity") != current.get("identity") or restored.get(
            "binding", {}
        ).get("group") != current.get("binding", {}).get("group"):
            raise ValueError("Архив принадлежит другому боту, аккаунту или группе")
        for section in ("actions", "withdrawals"):
            for key, value in current.get(section, {}).items():
                if value.get("status") in ("done", "pending", "unknown"):
                    other = restored.get(section, {}).get(key, {})
                    if other.get("status") != value.get("status"):
                        raise ValueError(
                            "Архив устарел: потеряется защита операций. Сначала выгрузите свежий архив."
                        )
        for plugin, state in current.get("plugin_state", {}).items():
            for key, order in state.get("orders", {}).items():
                if order.get("payment_intent"):
                    old = (
                        restored.get("plugin_state", {})
                        .get(plugin, {})
                        .get("orders", {})
                        .get(key, {})
                    )
                    if not old.get("payment_intent") or old.get(
                        "reference"
                    ) != order.get("reference"):
                        raise ValueError(
                            "В архиве отсутствует текущая оплачиваемая операция"
                        )
        last = current.get("ticket_last_attempt", 0)
        restored["ticket_last_attempt"] = max(
            last, restored.get("ticket_last_attempt", 0)
        )


class ManagementFeatures:
    def service_state(self):
        if not hasattr(self, "service_pending"):
            self.service_pending = {}
        return self.service_pending

    async def notify(self, kind, text, rows=()):
        if not self.cfg.flag("Notifications", kind):
            return
        if rows:
            await self.tg.call(
                "send_message",
                chat_id=self.group,
                message_thread_id=self.state.d["topics"]["main"],
                text=text[:3800],
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": label, "callback_data": action}
                            for label, action in row
                        ]
                        for row in rows
                    ]
                },
            )
        else:
            await self.say("main", text)

    async def service_command(self, name, user):
        self.service_state().pop(user, None)
        self.pending.pop(user, None)
        for plugin in self.plugins.values():
            if hasattr(plugin, "inputs"):
                plugin.inputs.pop(user, None)
        if name == "backup":
            await self.backup_menu()
        elif name == "proxy":
            await self.proxy_menu()
        elif name == "golden_key":
            self.service_pending[user] = {
                "kind": "golden_key",
                "expires": time.time() + 300,
            }
            await self.card(
                "main",
                "menu",
                "🔑 Пришлите новый golden_key в эту тему. Сообщение с ключом будет удалено. Для проверки авторизации потребуется перезапуск.",
                [[("Отмена", "menu:main")]],
            )
        elif name == "restart":
            await self.card(
                "main",
                "menu",
                "Перезапустить бота? Незавершённые оплаты будут восстановлены в режиме проверки.",
                [[("Перезапустить", "service:restart")], [("Отмена", "menu:main")]],
            )

    async def backup_menu(self):
        await self.card(
            "main",
            "menu",
            "📦 Резервная копия\nZIP включает настройки, доступы, статистику и защиту платежей. Создаётся только по кнопке выгрузки. Для загрузки отправьте ZIP после нажатия кнопки.",
            [
                [
                    ("⬆️ Выгрузить ZIP", "service:backup_export"),
                    ("⬇️ Загрузить ZIP", "service:backup_import"),
                ],
                [("‹ Главное", "menu:main")],
            ],
        )

    async def proxy_menu(self):
        text = "🌐 Прокси\n" + "\n".join(
            name + ": " + ("задан" if self.cfg.get(section, "proxy") else "выключен")
            for section, name in [("Telegram", "Telegram"), ("FunPay", "FunPay")]
        )
        await self.card(
            "main",
            "menu",
            text,
            [
                [
                    ("Telegram", "service:proxy:Telegram"),
                    ("FunPay", "service:proxy:FunPay"),
                ],
                [("‹ Главное", "menu:main")],
            ],
        )

    async def service_callback(self, data, user, topic, message):
        if data.startswith("ticketreview:"):
            if topic != "main":
                return True
            _, key, choice = data.split(":")
            row = self.state.d.get("ticket_reviews", {}).get(key)
            if not row or row.get("resolved"):
                return True
            if message.get("message_id") != row.get("message_id"):
                return True
            if choice not in ("clear", "ambiguous", "skip"):
                return True
            row.update(resolved=choice, reviewed_by=user, reviewed_at=time.time())
            self.state.save()
            await self.say(
                "main",
                f"🎫 Заказ #{key}: решение сохранено — "
                + {
                    "clear": "список 1",
                    "ambiguous": "список 2",
                    "skip": "не включать в тикет",
                }[choice],
            )
            return True
        if data == "finance:open":
            if topic == "main":
                await self.withdraw_menu()
            return True
        if not data.startswith("service:"):
            return False
        if topic != "main" or message.get("message_id") != self.state.d["cards"].get(
            "main/menu"
        ):
            return True
        action = data[8:]
        if action == "backup_export":
            await self.backup()
        elif action == "backup_import":
            self.service_state()[user] = {
                "kind": "backup",
                "expires": time.time() + 300,
            }
            await self.card(
                "main",
                "menu",
                "Пришлите ZIP резервной копии FunPayBOT, до 40 МБ. Сначала проверю архив и покажу состав.",
                [[("Отмена", "service:backup_menu")]],
            )
        elif action == "backup_menu":
            self.service_state().pop(user, None)
            await self.backup_menu()
        elif action.startswith("restore:"):
            await self.restore_backup(user, action[8:])
        elif action.startswith("proxy:"):
            section = action[6:]
            if section not in ("Telegram", "FunPay"):
                return True
            self.service_state()[user] = {
                "kind": "proxy",
                "section": section,
                "expires": time.time() + 300,
            }
            await self.card(
                "main",
                "menu",
                f"Прокси {section}\nВведите http://, https:// или socks5:// URL. Для отключения — off. Сообщение с доступами будет удалено. Применение после перезапуска.",
                [[("Отмена", "service:proxy_menu")]],
            )
        elif action == "proxy_menu":
            self.service_state().pop(user, None)
            await self.proxy_menu()
        elif action == "restart":
            await self.say("main", "🔄 Перезапускаюсь.")
            self.restart_requested = True
        return True

    async def service_input(self, message, user):
        command = message.get("text", "").split(" ", 1)[0].split("@", 1)[0]
        if command.startswith("/"):
            self.service_state().pop(user, None)
            if self.tools_manager:
                self.tools_manager.inputs.pop(user, None)
                self.tools_manager.plans.pop(user, None)
                self.tools_manager.imports.pop(user, None)
            return False
        pending = self.service_state().get(user)
        if not pending:
            manager = (
                self.lot_tools_manager()
                if getattr(self, "tools_screen_active", False)
                else None
            )
            if manager and (user in manager.inputs or message.get("document")):
                if message.get("document"):
                    doc = message["document"]
                    if doc.get("file_size", 0) > 40_000_000:
                        await self.say("main", "Максимум 40 МБ.")
                        return True
                    if not hasattr(self.tg, "bot"):
                        return True
                    body = await self.tg.bot.download(doc["file_id"])
                    await manager.document(doc.get("file_name", ""), body.read(), user)
                else:
                    await manager.message(message.get("text", ""), user)
                return True
            return False
        if pending["expires"] < time.time():
            self.service_pending.pop(user, None)
            await self.say("main", "Время ввода истекло.")
            return True
        kind = pending["kind"]
        if kind == "withdraw_amount":
            text = message.get("text", "").strip().casefold()
            try:
                if text in ("all", "всё", "все"):
                    value = "all"
                else:
                    value = Decimal(text.replace(",", "."))
                    if (
                        not value.is_finite()
                        or value <= 0
                        or value != value.quantize(Decimal("0.01"))
                    ):
                        raise ValueError()
                    value = str(value)
            except (ValueError, ArithmeticError):
                await self.say(
                    "main", "Введите сумму в рублях с точностью до копейки или «всё»."
                )
                return True
            self.cfg.set("AutoWithdraw", "manual_amount", value)
            self.service_pending.pop(user, None)
            await self.withdraw_menu()
            return True
        if kind == "lot_setting":
            plugin = self.plugins.get(pending["plugin"]) or self.dormant_plugins.get(
                pending["plugin"]
            )
            if not plugin:
                self.service_pending.pop(user, None)
                return True
            try:
                text = message.get("text", "")
                if message.get("document") and hasattr(self.tg, "bot"):
                    if message["document"].get("file_size", 0) > 1000000:
                        raise ValueError("Максимум 1 МБ")
                    body = await self.tg.bot.download(message["document"]["file_id"])
                    text = body.read().decode("utf-8")
                if pending["field"] == "profit":
                    value = Decimal(text.replace(",", "."))
                    if not value.is_finite() or value < 0:
                        raise ValueError("Нужна неотрицательная сумма")
                    plugin.config["Plugin"]["target_profit_per_unit"] = str(value)
                    plugin.save_config()
                    plugin.state["floor_dirty"] = True
                    plugin.last_lots = 0
                    plugin.save()
                    await plugin.manage_lots("floor")
                elif pending["field"] == "managed":
                    rules = json.loads(text)
                    profile = LotProfile(
                        plugin.id,
                        int(plugin.get("category_id")),
                        plugin.get("lot_kind", "common"),
                        rules,
                        plugin.parse_quantity,
                    )
                    entries = profile.entries()
                    if any(
                        not plugin.valid_quantity(entry.quantity) for entry in entries
                    ):
                        raise ValueError("Неверное количество товара")
                    plugin.config["Plugin"]["managed_lots"] = json.dumps(
                        rules, ensure_ascii=False
                    )
                    plugin.save_config()
                    plugin.last_lots = 0
                elif pending["field"] == "category":
                    value = int(text)
                    if value <= 0:
                        raise ValueError()
                    plugin.config["Plugin"]["category_id"] = str(value)
                    plugin.save_config()
                    plugin.last_lots = 0
                elif pending["field"] == "payout":
                    value = Decimal(text.replace(",", "."))
                    if not value.is_finite() or not 0 < value <= 1:
                        raise ValueError()
                    plugin.config["Plugin"]["payout_rate"] = str(value)
                    plugin.save_config()
                    plugin.state["floor_dirty"] = True
                    plugin.last_lots = 0
                    plugin.save()
                    await plugin.manage_lots("floor")
                self.service_pending.pop(user, None)
                await self.central_lot_menu(pending["plugin"])
            except (ValueError, KeyError, TypeError, ArithmeticError):
                await self.say(
                    "main", "Неверное значение. Проверьте формат и повторите ввод."
                )
            return True
        if kind in ("proxy", "golden_key"):
            value = message.get("text", "").strip()
            # Delete credentials before parsing or performing any network operation.
            await self.tg.call(
                "delete_message", chat_id=self.group, message_id=message["message_id"]
            )
            if kind == "proxy":
                if value.casefold() in ("off", "нет", "выкл", "-"):
                    value = ""
                if value:
                    try:
                        url = urlsplit(value)
                        if (
                            url.scheme not in ("http", "https", "socks5")
                            or not url.hostname
                            or not url.port
                            or url.path not in ("", "/")
                            or url.query
                            or url.fragment
                        ):
                            raise ValueError()
                    except ValueError:
                        await self.say("main", "Нужен URL прокси с адресом и портом.")
                        return True
                self.cfg.set(pending["section"], "proxy", value)
            else:
                if not re.fullmatch(r"[A-Za-z0-9]{16,256}", value):
                    await self.say("main", "Неверный формат golden_key.")
                    return True
                self.cfg.set("FunPay", "golden_key", value)
            self.service_pending.pop(user, None)
            await self.card(
                "main",
                "menu",
                "Настройка сохранена. Перезапустите бота для применения.",
                [[("Перезапустить", "service:restart")], [("‹ Главное", "menu:main")]],
            )
            return True
        if kind == "backup":
            document = message.get("document", {})
            if not document:
                return True
            if document.get("file_size", 0) > BackupArchive.limit:
                await self.say("main", "Максимум 40 МБ.")
                return True
            if not hasattr(self.tg, "bot"):
                return True
            downloaded = await self.tg.bot.download(document["file_id"])
            try:
                files = BackupArchive.read(downloaded.read())
                restored = json.loads(files["data/state.json"])
                BackupArchive.validate_state(self.state.d, restored)
                cfg = json.loads(files["config.json"])
                tg = cfg.get("telegram", {})
                if (
                    int(tg.get("group_id", 0)) != self.group
                    or int(tg.get("owner_id", 0)) != self.cfg.owner_id
                ):
                    raise ValueError("Другая группа или владелец")
            except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
                await self.say(
                    "main",
                    "Архив не принят: формат, привязки или защита операций не прошли проверку.",
                )
                return True
            token = secrets.token_hex(8)
            pending.update(
                kind="restore", files=files, token=token, expires=time.time() + 300
            )
            await self.card(
                "main",
                "menu",
                f"Восстановить {len(files)} файлов?\nБудут заменены настройки и состояние; затем бот перезапустится.\n"
                + "\n".join(files)[:2500],
                [
                    [("Восстановить и перезапустить", "service:restore:" + token)],
                    [("Отмена", "service:backup_menu")],
                ],
            )
            return True
        return kind == "restore"

    async def backup(self):
        async with self.lock:
            body = BackupArchive.build(
                self.cfg.path.parent, copy.deepcopy(self.state.d), self.cfg.path
            )
        await self.tg.call(
            "send_document",
            chat_id=self.group,
            message_thread_id=self.state.d["topics"]["main"],
            document=("FunPayBOT-backup.zip", body),
        )

    async def restore_backup(self, user, token):
        plan = self.service_state().get(user)
        if (
            not plan
            or plan.get("kind") != "restore"
            or plan.get("token") != token
            or plan["expires"] < time.time()
        ):
            return
        async with self.lock:
            BackupArchive.validate_state(
                self.state.d, json.loads(plan["files"]["data/state.json"])
            )
            self.restart_requested = True
            for plugin in list(self.plugins.values()):
                await plugin.stop()
            self.plugins.clear()
            restored = json.loads(plan["files"]["data/state.json"])
            BackupArchive.validate_state(self.state.d, restored)
            restored["tg_offset"] = (
                max(restored.get("tg_offset", 0), self.state.d.get("tg_offset", 0)) + 1
            )
            plan["files"]["data/state.json"] = json.dumps(
                restored, ensure_ascii=False
            ).encode()
            # Explicit restore is staged, never applied underneath running workers.
            # Startup replays this journal before network activity; a crash halfway through is recoverable.
            files = plan["files"]
            body = io.BytesIO()
            manifest = {
                "format": "unified-backup-v2",
                "sha256": {
                    name: hashlib.sha256(content).hexdigest()
                    for name, content in files.items()
                },
            }
            with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                for name, content in files.items():
                    archive.writestr(name, content)
            pending = self.cfg.path.parent / "data" / "restore-pending.zip"
            pending.parent.mkdir(parents=True, exist_ok=True)
            import os
            import tempfile

            fd, tmp = tempfile.mkstemp(dir=pending.parent, prefix=".restore-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body.getvalue())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, pending)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self.service_pending.pop(user, None)
        await self.say("main", "✅ Архив проверен. Применю его при перезапуске.")

    def available_system_names(self):
        return configured_plugin_names(self.cfg)

    def system_rows(self):
        rows = []
        for name in sorted(
            self.available_system_names(),
            key=lambda item: (item not in self.plugins, item),
        ):
            component = self.plugins.get(name)
            running = component is not None
            metadata = self.state.d.get("plugin_metadata", {}).get(name, {})
            title = getattr(
                component or self.dormant_plugins.get(name),
                "TITLE",
                metadata.get("title", name),
            )
            label = title
            topic = self.state.d["topics"].get("plugin:" + name)
            opener = (
                {
                    "text": label,
                    "url": f"https://t.me/c/{str(abs(self.group))[3:]}/{topic}",
                }
                if running and topic
                else (label, "plugin_open:" + name)
            )
            row = [
                opener,
                (
                    "⏹ Остановить" if running else "▶ Запустить",
                    f"system:{name}:{'stop' if running else 'start'}",
                ),
            ]
            rows.append(row)
        return rows

    async def system_control(self, name, enabled):
        from .bootstrap import provider_configuration_issues, read_plugin_setup

        if name not in self.available_system_names():
            return
        async with self.lock:
            component = self.plugins.get(name) or self.dormant_plugins.get(name)
            if enabled:
                if (
                    component
                    and plugin_capability(component, "commerce")
                    and component.flag("enabled")
                ):
                    component.validate_credentials()
                    if money(component.get("purchase_rate_rub", "0")) <= 0:
                        raise ValueError("Сначала учтите стоимость закупки USDT")
                elif component is None and issubclass(
                    load_plugin_module(name).Plugin, CommercePlugin
                ):
                    module, parser, path = read_plugin_setup(self.cfg, name)
                    if parser.getboolean("Plugin", "enabled", fallback=False):
                        if provider_configuration_issues(module, parser):
                            raise ValueError("Сначала заполните доступы " + name)
                        if (
                            money(
                                parser.get("Plugin", "purchase_rate_rub", fallback="0")
                            )
                            <= 0
                        ):
                            raise ValueError("Сначала учтите стоимость закупки USDT")
            disabled = self.state.d["plugin_disabled"]
            if enabled:
                if name in disabled:
                    disabled.remove(name)
            elif name not in disabled:
                disabled.append(name)
            self.state.save()
            await self.reload_plugins()
        await self.menu("plugins")

    def lot_tools_manager(self):
        """Возвращает встроенные инструменты, использующие адаптер FunPay напрямую."""
        if self.tools_manager is None:
            self.tools_manager = LotToolsManager(self, self.fp)
        return self.tools_manager

    async def management_callback(self, data, user):
        self.service_state().pop(user, None)
        if data.startswith("system:"):
            _, name, operation = data.split(":", 2)
            if operation in ("start", "stop"):
                await self.system_control(name, operation == "start")
        elif data.startswith("plugin_open:"):
            name = data.split(":", 1)[1]
            if name not in self.available_system_names():
                return True
            component = self.plugins.get(name)
            if component is None:
                await self.card(
                    "main",
                    "menu",
                    "Плагин остановлен. Запустите его, чтобы открыть настройки.",
                    [
                        [("▶ Запустить", f"system:{name}:start")],
                        [("‹ Плагины", "menu:plugins")],
                    ],
                )
            elif hasattr(component, "menu"):
                self.reopen_plugin_menu(name)
                await component.menu(new_message=True)
        elif data.startswith("lot_tools_action:"):
            manager = self.lot_tools_manager()
            if manager:
                await manager.callback(data.split(":", 1)[1], user)
        elif data == "withdraw_amount":
            self.service_state()[user] = {
                "kind": "withdraw_amount",
                "expires": time.time() + 300,
            }
            await self.card(
                "main",
                "menu",
                "Введите сумму списания в рублях или «всё». Комиссия будет показана в расчёте перед подтверждением.",
                [[("Отмена", "withdraw_menu")]],
            )
        elif data == "lots_menu":
            rows = []
            for name, plugin in {**self.dormant_plugins, **self.plugins}.items():
                if hasattr(plugin, "lot_profile"):
                    rows.append(
                        [(getattr(plugin, "TITLE", name), "lot_source:" + name)]
                    )
            rows.append([("🧰 Инструменты переноса и очистки", "lot_source:lot_tools")])
            await self.card(
                "main",
                "menu",
                "⚙️ Единый менеджер лотов\nМенеджеры лотов и инструменты переноса находятся в главной теме.",
                rows + [[("‹ Главное", "menu:main")]],
            )
        elif data.startswith("lot_source:"):
            name = data[11:]
            await self.central_lot_menu(name)
        elif data.startswith("lot_action:"):
            _, name, action = data.split(":", 2)
            plugin = self.plugins.get(name) or self.dormant_plugins.get(name)
            if not plugin:
                return True
            if action in (
                "price_lots",
                "floor",
                "activate",
                "deactivate",
                "stock",
            ) and hasattr(plugin, "manage_lots"):
                await plugin.manage_lots(action)
            elif action in ("autodump", "auto_stock") and hasattr(plugin, "flag"):
                plugin.config["Plugin"][action] = str(not plugin.flag(action)).lower()
                plugin.save_config()
                plugin.last_lots = 0
            elif action == "discover" and hasattr(plugin, "parse_quantity"):
                lots = json.loads(plugin.get("managed_lots", "{}"))
                profile = plugin.lot_profile()
                for offer in await self.fp.lots():
                    if offer.get("kind", "common") != profile.kind or str(
                        offer["category"]
                    ) != str(profile.category_id):
                        continue
                    quantity = plugin.parse_quantity(offer.get("description", ""))
                    if quantity:
                        lots[str(offer["id"])] = quantity
                        plugin.state.setdefault("lot_settings", {}).setdefault(
                            str(offer["id"]), {}
                        ).update(
                            keywords=offer.get("description", ""),
                            match_quantity=quantity,
                        )
                plugin.config["Plugin"]["managed_lots"] = json.dumps(lots)
                plugin.save_config()
                plugin.save()
            elif action in ("profit", "managed", "category", "payout"):
                self.service_state()[user] = {
                    "kind": "lot_setting",
                    "plugin": name,
                    "field": action,
                    "expires": time.time() + 600,
                }
                hint = {
                    "profit": "Пришлите целевую прибыль за единицу в рублях.",
                    "managed": 'Пришлите JSON лотов: {"123":50} либо {"1-2-99-3-4":{"kind":"chips","category":99,"quantity":50,"keywords":"сервер|сторона"}}.',
                    "category": "Пришлите числовой ID категории.",
                    "payout": "Пришлите долю после комиссии вывода: например 0.97.",
                }[action]
                await self.card(
                    "main", "menu", hint, [[("Отмена", "lot_source:" + name)]]
                )
                return True
            await self.central_lot_menu(name)
        elif data == "notifications_menu":
            labels = {
                "autoresponse": "Автоответы",
                "new_order": "Новый заказ",
                "order_status": "Статусы заказа",
                "review": "Отзывы",
                "lot_manager": "Менеджер лотов",
                "delivery": "Выдача",
                "ticket": "Автотикеты",
                "withdrawal": "Вывод",
                "auto_raise": "Поднятие лотов",
            }
            await self.card(
                "main",
                "menu",
                "🔔 Уведомления",
                [
                    [
                        (
                            ("🟢 " if self.cfg.flag("Notifications", key) else "⚪ ")
                            + title,
                            "notification_toggle:" + key,
                        )
                    ]
                    for key, title in labels.items()
                ]
                + [[("‹ Главное", "menu:main")]],
            )
        elif data.startswith("notification_toggle:"):
            key = data[20:]
            if key in self.cfg.c["Notifications"]:
                self.cfg.set(
                    "Notifications", key, not self.cfg.flag("Notifications", key)
                )
            await self.management_callback("notifications_menu", user)
        elif data == "finance_settings":
            await self.card(
                "main",
                "menu",
                "⏳ Доступность вывода\nУчитывается 48 часов после подтверждения заказа и фактически доступный баланс FunPay.\nУведомление открывает меню и не выполняет вывод.",
                [
                    [
                        (
                            "🟢 Задержка 48 ч"
                            if self.cfg.flag("Finance", "hold_enabled")
                            else "⚪ Без нашего таймера",
                            "finance_toggle:hold_enabled",
                        )
                    ],
                    [
                        (
                            "🟢 Уведомления о выводе"
                            if self.cfg.flag("Finance", "withdraw_notifications")
                            else "⚪ Уведомления выключены",
                            "finance_toggle:withdraw_notifications",
                        )
                    ],
                    [("‹ Финансы", "menu:finance")],
                ],
            )
        elif data.startswith("finance_toggle:"):
            key = data[15:]
            if key in ("hold_enabled", "withdraw_notifications"):
                self.cfg.set("Finance", key, not self.cfg.flag("Finance", key))
            await self.management_callback("finance_settings", user)
        else:
            return False
        return True

    async def central_lot_menu(self, name):
        if name == "lot_tools":
            await self.lot_tools_manager().menu()
            return
        plugin = self.plugins.get(name) or self.dormant_plugins.get(name)
        if not plugin:
            return
        if hasattr(plugin, "lot_profile"):
            lots = json.loads(plugin.get("managed_lots", "{}"))
            text = f"⚙️ Лоты · {getattr(plugin, 'TITLE', name)}\nУправляемых лотов: {len(lots)}\nПрибыль за единицу: {plugin.get('target_profit_per_unit')} ₽\nДемпер: {plugin.flag('autodump')} · Остатки: {plugin.flag('auto_stock')}"
            entries = plugin.lot_profile().entries()
            text += f"\nТипы: обычных {sum(row.kind == 'common' for row in entries)}, chips {sum(row.kind == 'chips' for row in entries)}"
            text += f"\nКурс закупки: {plugin.get('purchase_rate_rub')} ₽/USDT · Стоимость единицы: {plugin.get('unit_cost_usdt')} USDT"
            text += f"\nДоля после вывода: {plugin.effective_payout_rate()} · Профит за 100: {Decimal(plugin.get('target_profit_per_unit', '0')) * 100:.2f} ₽"
            for entry in entries[:5]:
                minimum = (
                    plugin.state.get("lot_settings", {})
                    .get(entry.lot_id, {})
                    .get("min")
                )
                text += f"\n{entry.lot_id} · {entry.kind} · {entry.quantity} · MIN {minimum or 'после расчёта'}"
            actions = [
                ("Собрать свои лоты", "discover"),
                ("Обновить цены", "price_lots"),
                ("Защитный минимум", "floor"),
                ("Остатки", "stock"),
                ("Включить лоты", "activate"),
                ("Выключить лоты", "deactivate"),
                ("Лоты: common / chips", "managed"),
                ("Категория", "category"),
                ("Целевая прибыль", "profit"),
                ("Доля после вывода", "payout"),
                ("Демпер: " + ("🟢" if plugin.flag("autodump") else "⚪"), "autodump"),
                (
                    "Остатки: " + ("🟢" if plugin.flag("auto_stock") else "⚪"),
                    "auto_stock",
                ),
            ]
            buttons = [
                (title, f"lot_action:{name}:{action}") for title, action in actions
            ]
            rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        else:
            await self.lot_tools_manager().menu()
            return
        await self.card("main", "menu", text, rows + [[("‹ Лоты", "lots_menu")]])

    def eligible_balance(self, balance):
        available = Decimal(str(balance["available"]))
        if not self.cfg.flag("Finance", "hold_enabled"):
            return available
        now = time.time()
        held = Decimal("0")
        for row in self.state.d["ledger"].values():
            if (
                row.get("status") != "CLOSED"
                or row.get("currency") != "RUB"
                or row.get("kind") == "direct"
            ):
                continue
            if "confirmed_at" not in row:
                row["confirmed_at"] = now
            if now - row["confirmed_at"] < 48 * 3600:
                held += max(
                    Decimal("0"),
                    Decimal(row.get("credit", "0"))
                    - Decimal(row.get("withdrawn", "0")),
                )
        # Do not subtract money already unavailable on the platform a second time.
        platform_hold = max(
            Decimal("0"), Decimal(str(balance.get("total", available))) - available
        )
        return max(Decimal("0"), available - max(Decimal("0"), held - platform_hold))

    async def finance_notification_tick(self, now):
        if not self.cfg.flag("SellerStatus", "online") or now < self.state.d.get(
            "finance_notice_due", 0
        ):
            return
        self.state.d["finance_notice_due"] = now + 300
        balance = await self.refresh_balance()
        eligible = self.eligible_balance(balance) - Decimal(
            self.cfg.get("AutoWithdraw", "reserve", "0")
        )
        threshold = (
            Decimal("2500")
            if self.cfg.get("AutoWithdraw", "method_kind") == "usdt_trc20"
            else Decimal("1000")
        )
        ready = eligible >= threshold
        kind = self.cfg.get("AutoWithdraw", "method_kind", "card")
        previous = self.state.d.setdefault("finance_notice", {})
        withdrawal_count = sum(
            v.get("status") == "done" for v in self.state.d["withdrawals"].values()
        )
        episode = f"{kind}:{withdrawal_count}"
        if not ready:
            previous.pop("episode", None)
        elif (
            previous.get("episode") != episode
            and self.cfg.flag("Finance", "withdraw_notifications")
            and self.cfg.flag("Notifications", "withdrawal")
        ):
            await self.notify(
                "withdrawal",
                f"💸 Доступен вывод: {eligible:.2f} ₽. Достигнута сумма, при которой минимальная комиссия не повышает процент.",
                [[("Открыть вывод", "finance:open")]],
            )
            previous.update(episode=episode, at=now)
        self.state.save()

    async def ticket_tick(self, now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if not self.cfg.flag("AUTOTICKET") or not self.cfg.flag(
            "SellerStatus", "online"
        ):
            return
        if now < self.state.d.get("ticket_check_due", 0):
            return
        self.state.d["ticket_check_due"] = now + 300
        if now - self.state.d.get("ticket_last_attempt", 0) < 86400:
            return
        if datetime.now(ZoneInfo(self.cfg.get("General", "timezone"))).hour < int(
            self.cfg.number("AUTOTICKET", "hour", 12)
        ):
            return
        clear = []
        ambiguous = []
        reviews = self.state.d.setdefault("ticket_reviews", {})
        for key in await self.fp.old_paid_orders():
            try:
                context = await self.fp.ticket_context(key)
                if context.get("status") != "PAID":
                    continue
                fingerprint = hashlib.sha256(
                    json.dumps(context, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                review = reviews.get(key, {})
                if review.get("fingerprint") != fingerprint:
                    review = {}
                reasons = TicketClassifier.reasons(context.get("messages", []))
                if not context.get("complete", False):
                    reasons.append({"match": "Не удалось получить полную историю"})
                if reasons and not review.get("resolved"):
                    if not review:
                        notice = (
                            "⚠️ Проверьте проблемный заказ #"
                            + key
                            + "\n"
                            + "\n".join(str(row["match"]) for row in reasons[:8])
                        )
                        sent = await self.tg.call(
                            "send_message",
                            chat_id=self.group,
                            message_thread_id=self.state.d["topics"]["main"],
                            text=notice,
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "Нет претензий → список 1",
                                            "callback_data": f"ticketreview:{key}:clear",
                                        }
                                    ],
                                    [
                                        {
                                            "text": "Спорный → список 2",
                                            "callback_data": f"ticketreview:{key}:ambiguous",
                                        }
                                    ],
                                    [
                                        {
                                            "text": "Не включать",
                                            "callback_data": f"ticketreview:{key}:skip",
                                        }
                                    ],
                                    [
                                        {
                                            "text": "Открыть заказ",
                                            "url": "https://funpay.com/orders/"
                                            + key
                                            + "/",
                                        }
                                    ],
                                ]
                            },
                        )
                        reviews[key] = {
                            "fingerprint": fingerprint,
                            "reasons": reasons,
                            "message_id": sent["message_id"],
                            "at": now,
                        }
                        self.state.save()
                    continue
                decision = review.get("resolved", "clear")
                if decision == "skip":
                    continue
                (ambiguous if decision == "ambiguous" else clear).append(key)
            except Exception:
                # Failed history/status reads never imply absence of a complaint.
                import logging

                logging.getLogger("FunPayBOT").exception(
                    "Не удалось проверить заказ для тикета: %s", key
                )
        orders = clear + ambiguous
        if not orders:
            self.state.save()
            return
        first = ", ".join("#" + v for v in clear) or "Нет"
        second = ", ".join("#" + v for v in ambiguous) or "Нет"
        template = self.render(self.cfg.get("AUTOTICKET", "message"), {})
        if template.count("{}") == 2:
            text = template.replace("{}", first, 1).replace("{}", second, 1)
        else:
            text = (
                template.replace("{clear_orders}", first)
                .replace("{ambiguous_orders}", second)
                .replace("{order_ids}", first)
            )
        if "1)" not in text or "2)" not in text:
            text = f"1) {first}\n\n2) {second}\n\n" + text
        # One persisted global rolling cooldown, including ambiguous network outcomes.
        self.state.d["ticket_last_attempt"] = now
        self.state.d["ticket_attempt"] = {
            "at": now,
            "status": "pending",
            "clear": clear,
            "ambiguous": ambiguous,
        }
        self.state.save()
        try:
            ticket = await self.fp.send_ticket(orders, text)
            self.state.d["ticket_attempt"].update(status="done", id=ticket)
            await self.notify(
                "ticket",
                "🎫 Тикет отправлен. Следующая отправка — не раньше чем через 24 часа.",
            )
        except Exception:
            self.state.d["ticket_attempt"]["status"] = "unknown"
            await self.say(
                "main",
                "⚠️ Результат тикета не подтверждён. Проверьте поддержку FunPay. Автоповтор заблокирован на 24 часа.",
            )
        self.state.save()

    async def notification_outbox_tick(self, now):
        if not self.cfg.flag("SellerStatus", "online"):
            return
        for key, row in list(self.state.d.get("notification_outbox", {}).items()):
            if row["status"] != "retry" or row["due"] > now:
                continue
            if row.get("kind") == "main":
                row["status"] = "sending"
                row["attempts"] = row.get("attempts", 0) + 1
                self.state.save()
                try:
                    await self.notify(row["notification_kind"], row["text"])
                    row["status"] = "done"
                except Exception:
                    row["status"] = "unknown"
                self.state.save()
                continue
            if (
                self.state.d["ledger"]
                .get(row["event"].get("order_id"), {})
                .get("status")
                == "REFUNDED"
            ):
                row["status"] = "cancelled"
                self.state.save()
                continue
            action = self.state.d["actions"].get(row["action"], {})
            if action.get("status") in ("pending", "unknown"):
                row["status"] = "unknown"
                self.state.save()
                continue
            if action.get("status") == "done":
                row["status"] = "done"
                self.state.save()
                continue
            if action.get("status") == "failed":
                self.state.d["actions"].pop(row["action"], None)
            row["status"] = "sending"
            row["attempts"] = row.get("attempts", 0) + 1
            self.state.save()
            try:
                chat = "chat:" + str(row["event"]["chat"])
                if chat not in self.state.d["topics"]:
                    await self.topic(chat)
                await self.send_buyer(
                    row["event"], row["text"], row["action"], rendered=True
                )
                action = self.state.d["actions"].get(row["action"], {})
                if action.get("status") == "done":
                    row["status"] = "done"
                elif action.get("status") == "failed" and row["attempts"] < 5:
                    row.update(status="retry", due=now + min(300, 30 * row["attempts"]))
                else:
                    row["status"] = "unknown"
            except Exception:
                row["status"] = "unknown"
            self.state.save()


def apply_pending_restore(root, config_path=None):
    """Применяет восстановление под блокировкой процесса до загрузки настроек, состояния и сети."""
    root = Path(root)
    pending = root / "data" / "restore-pending.zip"
    if not pending.exists():
        return False
    files = BackupArchive.read(pending.read_bytes())
    # Validate the config shape before changing any working file.
    config = json.loads(files["config.json"])
    if not all(isinstance(value, dict) for value in config.values()):
        raise ValueError("Неверная структура настроек в восстановлении")
    for name, content in files.items():
        target = (
            Path(config_path).with_suffix(".json")
            if name == "config.json" and config_path
            else root / name
        )
        atomic_write(target, content.decode("utf-8"))
    pending.unlink()
    return True
