"""Проверка оформления Telegram без лишних изменений и остановки рабочего бота."""

from __future__ import annotations

import asyncio
import time

from .metadata import BOT_NAME, LOG


async def profile_call(method, **kwargs):
    """Повторяет запрос при кратком сетевом сбое, но не ожидает снятия лимита."""
    from aiogram.exceptions import TelegramNetworkError

    for attempt in range(3):
        try:
            return await method(**kwargs)
        except TelegramNetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)


async def reset_profile(bot, cfg, state, group_id):
    """Исправляет старое оформление и меню, откладывая недоступные изменения."""
    from aiogram.exceptions import (
        TelegramAPIError,
        TelegramBadRequest,
        TelegramRetryAfter,
    )
    from aiogram.types import (
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeChat,
        BotCommandScopeChatAdministrators,
        BotCommandScopeChatMember,
        BotCommandScopeDefault,
    )

    description = cfg.get("Telegram", "description", "").strip()
    short_description = cfg.get("Telegram", "short_description", "").strip()
    if len(description) > 512 or len(short_description) > 120:
        raise ValueError("Описание Telegram: максимум 512 символов, краткое — 120")
    deferred = state.d.setdefault("telegram_profile_retry", {})
    warned = set()
    pending = False
    unavailable = object()

    async def optional(method, *, missing_private=False, **kwargs):
        """Сохраняет срок ограничения метода и не прерывает запуск из-за оформления."""
        nonlocal pending
        name = method.__name__
        if deferred.get(name, 0) > time.time():
            pending = True
            if name not in warned:
                seconds = max(1, int(deferred[name] - time.time()))
                LOG.warning(
                    "Настройка Telegram (%s) отложена ещё на %s сек.; запуск продолжается",
                    name,
                    seconds,
                )
                warned.add(name)
            return unavailable
        try:
            result = await profile_call(method, **kwargs)
        except TelegramRetryAfter as error:
            deferred[name] = time.time() + error.retry_after + 1
            state.save()
            pending = True
            if name not in warned:
                LOG.warning(
                    "Telegram ограничил настройку профиля (%s) на %s сек.; запуск продолжается",
                    name,
                    error.retry_after,
                )
                warned.add(name)
            return unavailable
        except TelegramBadRequest as error:
            if missing_private and "chat not found" in error.message.lower():
                return unavailable
            return postpone(name)
        except TelegramAPIError:
            return postpone(name)
        else:
            if name in deferred:
                deferred.pop(name, None)
                state.save()
            return result

    def postpone(name):
        """Откладывает недоступную операцию без повторяющихся ошибок в журнале."""
        nonlocal pending
        pending = True
        deferred[name] = time.time() + 60
        state.save()
        if name not in warned:
            LOG.warning(
                "Не удалось проверить или изменить оформление Telegram (%s); "
                "повтор при следующем запуске, работа бота продолжается",
                name,
            )
            warned.add(name)
        return unavailable

    scopes = (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
        BotCommandScopeChat(chat_id=group_id),
        BotCommandScopeChatAdministrators(chat_id=group_id),
        BotCommandScopeChatMember(chat_id=group_id, user_id=cfg.owner_id),
        BotCommandScopeChat(chat_id=cfg.owner_id),
    )
    LOG.debug("Проверяю команды, имя и описание Telegram-бота")
    # Общий вариант тоже русский; других языков интерфейса нет.
    for scope in scopes:
        missing_private = scope.type == "chat" and scope.chat_id == cfg.owner_id
        for language in ("", "ru"):
            commands = await optional(
                bot.get_my_commands,
                scope=scope,
                language_code=language,
                missing_private=missing_private,
            )
            if commands is unavailable or not commands:
                continue
            if scope.type == "chat" and scope.chat_id == group_id and not language:
                # После удаления актуальное меню обязательно регистрируется заново.
                state.d.pop("command_sync", None)
                state.save()
            await optional(
                bot.delete_my_commands,
                scope=scope,
                language_code=language,
                missing_private=missing_private,
            )
    fields = (
        (bot.get_my_name, bot.set_my_name, "name", BOT_NAME),
        (bot.get_my_description, bot.set_my_description, "description", description),
        (
            bot.get_my_short_description,
            bot.set_my_short_description,
            "short_description",
            short_description,
        ),
    )
    for getter, setter, field, desired in fields:
        for language in ("", "ru"):
            current = await optional(getter, language_code=language)
            if current is unavailable or getattr(current, field) == desired:
                continue
            await optional(setter, **{field: desired, "language_code": language})
    # Удаляем старые переводы описания: это очистка, а не языки нашего интерфейса.
    # После удаления Telegram использует общее русское описание.
    for getter, setter, field, desired in fields[1:]:
        for extra_language in ("en", "uk"):
            current = await optional(getter, language_code=extra_language)
            if current is unavailable or getattr(current, field) in ("", desired):
                continue
            await optional(setter, **{field: "", "language_code": extra_language})
    if pending:
        LOG.debug("Проверка профиля Telegram завершена; часть изменений отложена")
    else:
        LOG.debug("Профиль Telegram проверен: %s", BOT_NAME)
