"""Аргументы командной строки, настройка зависимостей и запуск приложения."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .commerce import CommercePlugin
from .config import DEFAULTS, Config, is_placeholder, load_parser, settings_payload
from .dependencies import StartupError, ensure_runtime_dependencies
from .framework import configured_plugin_names, load_plugin_module
from .logging_setup import setup_logs
from .metadata import API_REVISION, LOG, ROOT
from .storage import SingleProcess, State, atomic_write
from .telegram import Telegram


async def live(cfg, state):
    from .app import App
    from .funpay import FunPayClient

    cfg.validate(telegram=True)
    tg = Telegram(cfg)
    fp = FunPayClient(cfg)
    app = App(cfg, state, tg, fp)
    try:
        chat = await tg.bot.get_chat(app.group)
        if not chat.is_forum:
            raise ValueError("Нужна супергруппа с включёнными темами")
        await tg.bot.delete_webhook(drop_pending_updates=False)
        me = await tg.bot.get_me()
        member = await tg.bot.get_chat_member(app.group, me.id)
        if member.status != "administrator" or not member.can_manage_topics:
            raise ValueError("Боту нужны права администратора с управлением темами")
        from .telegram_profile import reset_profile

        await reset_profile(tg.bot, cfg, state, app.group)
        await fp.start()
        identity = {
            "bot_id": me.id,
            "account_id": getattr(getattr(fp, "account", None), "id", None),
        }
        if state.d.get("identity", identity) != identity:
            raise ValueError("state.json принадлежит другому боту или аккаунту FunPay")
        state.d["identity"] = identity
        cleaned = state.cleanup_startup()
        if cleaned:
            LOG.info("Служебное состояние очищено: записей=%s", cleaned)
        state.save()
        for event in getattr(fp, "initial_events", []):
            if event["kind"] == "baseline":
                await app.handle_event(event)
            else:
                state.d["inbox"].setdefault(event["id"], event)
        state.save()
        await app.boot()
        if app.restart_requested:
            return True
        LOG.info("Готов к работе")
        from .logging_setup import start_banner_animation

        start_banner_animation()

        async def poll_fp():
            previous_online = None
            last_offline_log = 0
            while not app.restart_requested:
                try:
                    online = cfg.flag("SellerStatus", "online")
                    if online != previous_online:
                        LOG.debug(
                            "Опрос FunPay: %s",
                            "включён"
                            if online
                            else "ПРИОСТАНОВЛЕН — включите Онлайн в главном меню",
                        )
                        previous_online = online
                    if not online and time.monotonic() - last_offline_log >= 60:
                        last_offline_log = time.monotonic()
                        LOG.debug(
                            "Офлайн: новые события FunPay не запрашиваются; Telegram продолжает работать"
                        )
                    if online:
                        events = await fp.poll()
                        async with app.lock:
                            if app.restart_requested:
                                return
                            for event in events:
                                if event["kind"] == "baseline":
                                    await app.handle_event(event)
                                else:
                                    state.d["inbox"].setdefault(event["id"], event)
                            state.save()
                            for key, event in list(state.d["inbox"].items()):
                                try:
                                    await app.handle_event(event)
                                except Exception as e:
                                    LOG.exception(
                                        "Не обработано событие %s; оставлено в очереди для повторной обработки",
                                        key,
                                    )
                                    continue
                                del state.d["inbox"][key]
                                state.save()
                except Exception as e:
                    app.counts["errors"] += 1
                    LOG.exception("Ошибка опроса FunPay")
                await asyncio.sleep(cfg.number("FunPay", "poll_seconds", 6))

        async def scheduler():
            while not app.restart_requested:
                try:
                    await app.tick()
                except Exception as e:
                    app.counts["errors"] += 1
                    LOG.exception("Ошибка планировщика")
                await asyncio.sleep(2)

        tasks = [asyncio.create_task(poll_fp()), asyncio.create_task(scheduler())]
        try:
            # Explicit cursor persistence; process updates sequentially, never run user handlers concurrently.
            while not app.restart_requested:
                try:
                    updates = await tg.bot.get_updates(
                        offset=state.d["tg_offset"],
                        timeout=20,
                        allowed_updates=["message", "callback_query"],
                    )
                    for update in updates:
                        try:
                            await app.ingest_update(update)
                        except Exception as e:
                            app.counts["errors"] += 1
                            LOG.exception(
                                "Ошибка обработки Telegram: update_id=%s",
                                update.update_id,
                            )
                        state.d["tg_offset"] = update.update_id + 1
                        state.save()
                        if app.restart_requested:
                            break
                except Exception as e:
                    LOG.exception("Ошибка получения обновлений Telegram")
                    await asyncio.sleep(5)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for plugin in list(app.plugins.values()):
                if hasattr(plugin, "stop"):
                    await plugin.stop()
    finally:
        await tg.bot.session.close()
        LOG.info("Бот остановлен")
    return app.restart_requested


def repair_config_file(path):
    """Предлагает исправить повреждённый JSON, сохраняя исходный файл без перезаписи."""
    path = Path(path)
    while True:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(
                not isinstance(v, dict) for v in raw.values()
            ):
                raise ValueError()
            return
        except (ValueError, UnicodeError):
            print(
                "Установка: некорректный формат "
                + str(path)
                + ". Существующий файл не перезаписан."
            )
            input(
                "Исправьте JSON в редакторе и нажмите Enter для повторной проверки; Ctrl+C — выход: "
            )


def read_core_configuration(path):
    while True:
        try:
            return Config(path)
        except (json.JSONDecodeError, UnicodeError):
            repair_config_file(Path(path).with_suffix(".json"))
        except ValueError:
            if Path(path).with_suffix(".json").exists():
                repair_config_file(Path(path).with_suffix(".json"))
            else:
                raise


def configuration_issues(cfg):
    """Возвращает расположение полей с ошибками, не раскрывая секреты и состояние платежей."""
    issues = []

    def add(section, key, reason, secret=False):
        issues.append(
            {
                "section": section,
                "key": key,
                "label": section + "." + key,
                "reason": reason,
                "secret": secret,
            }
        )

    fields = [
        ("Telegram", "token", True),
        ("Telegram", "group_id", False),
        ("Telegram", "owner_id", False),
        ("FunPay", "golden_key", True),
        ("FunPay", "user_agent", False),
    ]
    for section, key, secret in fields:
        value = cfg.get(section, key).strip()
        bad = not value or is_placeholder(value)
        if not bad:
            if key == "token":
                bad = re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", value) is None
            elif key == "golden_key":
                bad = re.fullmatch(r"[A-Za-z0-9]{16,256}", value) is None
            elif key in ("group_id", "owner_id"):
                try:
                    bad = int(value) >= 0 if key == "group_id" else int(value) <= 0
                except ValueError:
                    bad = True
        if bad:
            add(
                section,
                key,
                "отсутствует, содержит шаблон или имеет неверный формат",
                secret,
            )
    for section in ("Telegram", "FunPay"):
        value = cfg.get(section, "proxy").strip()
        if value:
            try:
                url = urlsplit(value)
                if (
                    is_placeholder(value)
                    or url.scheme not in ("http", "https", "socks5")
                    or not url.hostname
                    or not url.port
                    or url.path not in ("", "/")
                    or url.query
                    or url.fragment
                ):
                    raise ValueError()
            except ValueError:
                add(
                    section,
                    "proxy",
                    "нужен URL прокси с адресом и портом либо пустая строка для отключения",
                    True,
                )
    try:
        ZoneInfo(cfg.get("General", "timezone", "Europe/Moscow"))
    except (ValueError, KeyError):
        add("General", "timezone", "неизвестный часовой пояс, например Europe/Moscow")
    numeric = {
        ("FunPay", "poll_seconds"): (3, None),
        ("AutoWithdraw", "minimum"): (0, None),
        ("AutoWithdraw", "reserve"): (0, None),
        ("AutoWithdraw", "interval_seconds"): (60, None),
        ("AUTOTICKET", "hour"): (0, 23),
        ("WeeklyReport", "weekday"): (0, 6),
        ("WeeklyReport", "hour"): (0, 23),
    }
    for (section, key), (minimum, maximum) in numeric.items():
        try:
            value = Decimal(cfg.get(section, key))
            if (
                not value.is_finite()
                or value < minimum
                or maximum is not None
                and value > maximum
            ):
                raise ValueError()
            if key in ("hour", "weekday") and value != int(value):
                raise ValueError()
        except (ValueError, ArithmeticError):
            add(section, key, "число вне допустимого диапазона")
    for section in cfg.c.sections():
        for key in cfg.c[section]:
            if section.casefold() != "plugins" and key in (
                "enabled",
                "online",
                "watermark",
                "only_new_chats",
                "on_feedback_changed",
                "hold_enabled",
                "withdraw_notifications",
            ):
                try:
                    cfg.c.getboolean(section, key)
                except ValueError:
                    add(section, key, "нужно true или false")
    for section, key in [
        ("AUTORESPONSE", "commands"),
        ("AUTOGREETINGS", "lot_greetings"),
        ("ChatTemplates", "items"),
    ]:
        try:
            if not isinstance(cfg.data(section, key), dict):
                raise ValueError()
        except (ValueError, TypeError):
            add(section, key, "ожидается JSON-объект, например {}")
    try:
        withdraw_enabled = cfg.flag("AutoWithdraw")
    except ValueError:
        withdraw_enabled = False
    if withdraw_enabled:
        for key in ("method", "wallet"):
            if not cfg.get("AutoWithdraw", key) or is_placeholder(
                cfg.get("AutoWithdraw", key)
            ):
                add(
                    "AutoWithdraw",
                    key,
                    "сначала заполните способ и сохранённые реквизиты для включённого автовывода",
                    key == "wallet",
                )
    amount = cfg.get("AutoWithdraw", "manual_amount", "all")
    if amount != "all":
        try:
            value = Decimal(amount)
            if (
                not value.is_finite()
                or value <= 0
                or value != value.quantize(Decimal("0.01"))
            ):
                raise ValueError()
        except (ValueError, ArithmeticError):
            add(
                "AutoWithdraw",
                "manual_amount",
                "нужна положительная сумма до копейки или all",
            )
    if cfg.get("AutoWithdraw", "scope", "available_balance") != "available_balance":
        add("AutoWithdraw", "scope", "поддерживается available_balance")
    if not issues:
        try:
            cfg.validate(telegram=True)
        except (ValueError, ArithmeticError) as error:
            raise StartupError(
                "Проверка настроек не завершена: проверьте формат чисел и JSON-параметров. Рабочие доступы не сбрасывались."
            ) from error
    for name in selected_plugin_names(cfg):
        try:
            module, parser, path = read_plugin_setup(cfg, name)
            if not parser.getboolean("Plugin", "enabled", fallback=False):
                continue
            provider_issues = provider_configuration_issues(module, parser)
            if provider_issues:
                issues.append(
                    {
                        "plugin": name,
                        "reason": "не заполнены включённые доступы поставщика",
                    }
                )
        except (ValueError, UnicodeError):
            issues.append(
                {"plugin": name, "reason": "неверный формат конфигурации плагина"}
            )
    return issues


def selected_plugin_names(cfg):
    result = []
    for name in configured_plugin_names(cfg):
        try:
            if issubclass(load_plugin_module(name).Plugin, CommercePlugin):
                result.append(name)
        except Exception:
            LOG.exception("Не удалось прочитать контракт плагина %s", name)
    return result


def read_plugin_setup(cfg, name, repair=False):
    module = load_plugin_module(name)
    if not issubclass(module.Plugin, CommercePlugin):
        raise ValueError("Плагин не объявил контракт поставщика")
    path = cfg.path.parent / "plugins" / name / "config.json"
    while True:
        try:
            parser = load_parser(
                path,
                {"Plugin": {"enabled": "false"}, "Provider": module.Plugin.DEFAULTS},
            )
            return module, parser, path
        except (ValueError, UnicodeError):
            if not repair:
                raise
            repair_config_file(path)


def provider_configuration_issues(module, parser):
    return module.Plugin.PROVIDER.configuration_issues(parser["Provider"])


def installer(cfg, issues=None):
    """Запрашивает только неверные поля, сохраняя корректно настроенные доступы."""
    print("Настройка конфигурации. Заполненные корректные поля сохраняются.")
    while True:
        current = configuration_issues(cfg) if issues is None else issues
        issues = None
        if not current:
            print("Обязательные настройки заполнены. Повторный ввод не требуется.")
            return
        changed = False
        for issue in current:
            if issue.get("plugin"):
                configure_plugin(cfg, issue["plugin"])
                changed = True
                continue
            section, key = issue["section"], issue["key"]
            if issue.get("format_error"):
                repair_config_file(cfg.path)
                cfg.c = load_parser(cfg.path, DEFAULTS)
                changed = True
                break
            label = issue.get("label", section + "." + key)
            print("Требует настройки: " + label + " — " + issue["reason"])
            read = getpass.getpass if issue.get("secret") else input
            value = read(label + ": ").strip()
            if not value and key != "proxy":
                print("Значение не изменено; это поле ещё нужно исправить.")
                continue
            cfg.set(section, key, value)
            changed = True
        if not changed:
            print(
                "Настройка не завершена. Введите нужные значения или завершите Ctrl+C."
            )


def verify_api():
    manifest_path = ROOT / "vendor" / "api-lock.json"
    marker = ROOT / "vendor" / "revision.txt"
    if not manifest_path.exists() or not marker.exists():
        raise ValueError(
            "Не найден закреплённый FunPayAPI. Распакуйте полный архив релиза."
        )
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest.get("revision") != API_REVISION
        or marker.read_text().strip() != API_REVISION
    ):
        raise ValueError("Ревизия FunPayAPI не совпадает с закреплённой")
    files = {
        str(p.relative_to(ROOT / "vendor")).replace(os.sep, "/"): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in (ROOT / "vendor" / "FunPayAPI").rglob("*.py")
    }
    if files != manifest["sha256"]:
        raise ValueError("Проверка целостности FunPayAPI не пройдена")


def install_api():
    """Использует закреплённую копию API из релиза без загрузки изменяемой версии из сети."""
    verify_api()


def configure_plugin(cfg, name):
    """Настраивает реквизиты поставщика без создания платёжных операций."""
    module, parser, path = read_plugin_setup(cfg, name, repair=True)
    while True:
        try:
            parser.getboolean("Plugin", "enabled", fallback=False)
        except ValueError:
            value = (
                input(name + ": plugin.enabled должен быть true/false: ")
                .strip()
                .lower()
            )
            if value not in ("true", "false"):
                continue
            parser["Plugin"]["enabled"] = value
        issues = provider_configuration_issues(module, parser)
        if not issues:
            atomic_write(path, settings_payload(parser))
            print(
                name + ": доступы заполнены; автоматическая выдача сама не включается."
            )
            return
        for issue in issues:
            field = issue["key"]
            descriptor = getattr(module.Plugin, "INSTALL_FIELDS", {}).get(
                field, (field, True)
            )
            label, secret = descriptor
            print(name + ": " + field + " — " + issue["reason"])
            value = (getpass.getpass if secret else input)(label + ": ").strip()
            if value:
                parser["Provider"][field] = value
                atomic_write(path, settings_payload(parser))
            else:
                print("Поле сохранено без изменений; требуется корректное значение.")


def main():
    from .management import apply_pending_restore

    parser = argparse.ArgumentParser(description="FunPay BOT")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--plugin-setup", metavar="PLUGIN_ID")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()
    if not ensure_runtime_dependencies():
        return
    guard = SingleProcess(args.config.resolve().parent / "data")
    apply_pending_restore(args.config.resolve().parent, args.config.resolve())
    cfg = read_core_configuration(args.config.resolve())
    if args.plugin_setup:
        configure_plugin(cfg, args.plugin_setup)
        return
    issues = configuration_issues(cfg)
    if args.install or issues:
        installer(cfg, issues)
    cfg.validate(telegram=True)
    install_api()
    directory = cfg.path.parent / "data"
    setup_logs(cfg, directory)
    state = State(directory / "state.json")
    restart = asyncio.run(live(cfg, state))
    guard.file.close()
    if restart:
        os.execv(
            sys.executable,
            [sys.executable, str(ROOT / "main.py"), "--config", str(cfg.path), "--run"],
        )
