"""Настройка журналов, оформления консоли и скрытия секретов."""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_parser
from .metadata import LOG, VERSION

PLUGIN_SOURCES = {}


def register_plugin_source(identifier, title):
    """Связывает идентификатор плагина с читаемым именем автора событий."""
    title = re.sub(r"^[^\wА-Яа-яЁё]+", "", str(title)).strip()
    PLUGIN_SOURCES[identifier] = re.sub(r"[\r\n\t│]", " ", title)[:60] or identifier


def action_logger(action):
    """Возвращает журнал автора действия, сохраняя маршрутизацию в файл плагина."""
    if str(action).startswith("plugin:"):
        return logging.getLogger("FunPayBOT.plugins." + str(action).split(":", 2)[1])
    return LOG


def event_source(record):
    """Определяет автора события для общего журнала, консоли и файла плагина."""
    explicit = getattr(record, "event_source", None)
    if explicit:
        return str(explicit)
    identifier = getattr(record, "plugin_id", None)
    if not identifier and record.name.startswith("FunPayBOT.plugins."):
        identifier = record.name.split(".")[2]
    if identifier:
        return PLUGIN_SOURCES.get(identifier, identifier)
    if record.name.startswith(("FunPayAPI", "FunPayBOT.funpay")):
        return "FunPay"
    return "FunPay BOT"


LIMIT_MESSAGE = re.compile(
    r"(?:подождите|please wait|зачекайте)\s+\d|"
    r"(?:слишком часто|too many requests|rate limit|flood control)|"
    r"(?:попробуйте|повторите|доступно|доступен|можно будет).{0,35}через\s+\d|"
    r"(?:лимит|ограничение).{0,30}(?:запрос|частот)|"
    r"(?:не чаще|раз в).{0,30}\d.{0,10}(?:секунд|минут|час|сут|дн)",
    re.I,
)
FUNPAY_ERRORS = {
    "RequestFailedError",
    "RaiseError",
    "MessageNotDeliveredError",
    "FeedbackEditingError",
    "LotSavingError",
    "ImageUploadError",
    "WithdrawError",
    "RefundError",
    "LotParsingError",
}


def expected_limit(error):
    """Отличает явные ограничения частоты от сетевых сбоев, captcha и отказов операции."""
    if error is None:
        return None
    name = type(error).__name__
    if name == "TelegramRetryAfter":
        return (
            "Лимит Telegram: повторите действие через "
            + str(getattr(error, "retry_after", "?"))
            + " с."
        )
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(
        response, "status_code", None
    )
    if status == 429:
        return (
            "Лимит запросов FunPay: площадка просит подождать перед следующим запросом."
        )
    # HTTP 5xx и ошибки доступа остаются сбоями независимо от текста HTML.
    if status is not None and status != 200:
        return None
    message = str(getattr(error, "error_message", None) or "")
    if not LIMIT_MESSAGE.search(message) or re.search(
        r"captcha|провер.{0,15}(?:браузер|человек)|https?://", message, re.I
    ):
        return None
    try:
        return error.short_str()
    except (AttributeError, TypeError):
        return message[:300]


def record_error(record):
    """Находит исключение независимо от способа его передачи в журнал."""
    if record.exc_info:
        return record.exc_info[1]
    explicit = getattr(record, "error_exception", None)
    if isinstance(explicit, BaseException):
        return explicit
    if isinstance(record.msg, BaseException):
        return record.msg
    args = record.args.values() if isinstance(record.args, dict) else record.args or ()
    return next((value for value in args if isinstance(value, BaseException)), None)


def error_operation(record):
    """Возвращает короткое название действия без тела ответа сервиса и технических данных."""
    error = record_error(record)
    explicit = getattr(record, "error_operation", None) or getattr(
        error, "_funpaybot_operation", None
    )
    if explicit:
        return str(explicit)[:160]
    message = str(record.msg).split(":", 1)[0]
    labels = {
        "Ошибка планировщика": "задачи планировщика",
        "Ошибка опроса FunPay": "получения событий FunPay",
        "Не удалось запустить бота": "запуска бота",
        "Ошибка обработки Telegram": "обработки сообщения Telegram",
        "Ошибка меню плагина": "действия меню плагина",
        "Ошибка обработки события": "обработки события",
        "Не удалось загрузить плагин": "загрузки плагина",
        "Task exception was never retrieved": "фоновой задачи",
        "Exception in callback": "фонового обработчика",
        "Не удалось получить истории чатов": "получения истории чатов FunPay",
        "Не удалось обновить список": "обновления списка заказов FunPay",
    }
    for prefix, label in labels.items():
        if message.startswith(prefix):
            return label
    if record.name.startswith("FunPayAPI"):
        return "получения данных FunPay"
    if "Traceback" in message:
        return "действия бота"
    message = re.sub(r"%[-+0-9.]*[sdrf]", "", message)
    message = re.sub(r"[\r\n\t│]", " ", message).strip()
    return "действия «" + message[:100] + "»" if message else "действия бота"


class ErrorPolicyFilter(logging.Filter):
    """Назначает ожидаемым лимитам WARNING, сохраняя настоящие ошибки и их трассировку."""

    def filter(self, record):
        """Применяет уровень и добавляет стек пойманного исключения только для файлового вывода."""
        if record.levelno < logging.WARNING:
            return True
        error = record_error(record) or sys.exc_info()[1]
        if error is not None:
            record.error_exception = error
        limit = expected_limit(error)
        if (
            error is None
            and record.name.startswith("FunPayAPI")
            and "429" in str(record.msg)
            and "Too Many Requests" in str(record.msg)
        ):
            limit = "Лимит запросов FunPay: площадка просит подождать перед следующим запросом."
        if limit:
            record.levelno, record.levelname = logging.WARNING, "WARNING"
            record.msg, record.args = limit, ()
            record.expected_limit = True
        elif (
            error is not None
            or re.match(
                r"(?:Ошибка(?:\s|:)|Не удалось\s|Что-то пошло не так|Произошла ошибка)",
                str(record.msg),
            )
            or "Traceback (most recent call last)" in str(record.msg)
        ):
            record.levelno, record.levelname = logging.ERROR, "ERROR"
        if (
            error is not None
            and record.levelno >= logging.ERROR
            and not record.exc_info
        ):
            record.exc_info = (type(error), error, error.__traceback__)
        return True


class JournalLevelFilter(logging.Filter):
    """Применяет настроенный уровень журнала после классификации ошибки."""

    def __init__(self, cfg):
        """Читает минимальный уровень записей из текущих настроек."""
        super().__init__()
        self.minimum = getattr(
            logging, cfg.get("Logging", "level", "INFO").upper(), logging.INFO
        )

    def filter(self, record):
        """Сохраняет записи с уже исправленным уровнем важности."""
        return record.levelno >= self.minimum


class ConsoleErrorFilter(logging.Filter):
    """Оставляет в терминале одно сообщение об исключении, прошедшем несколько обработчиков."""

    def filter(self, record):
        """Пропускает повторную запись того же исключения только после короткого интервала."""
        error = record_error(record)
        if error is None or record.levelno < logging.WARNING:
            return True
        previous = getattr(error, "_funpaybot_console_at", 0)
        if 0 <= record.created - previous < 1:
            return False
        try:
            error._funpaybot_console_at = record.created
        except (AttributeError, TypeError):
            pass
        return True


class Redactor(logging.Formatter):
    def __init__(self, cfg):
        super().__init__(
            "%(asctime)s [%(levelname)s] [%(source)s] %(message)s",
            datefmt="%d.%m.%Y %H:%M:%S",
        )
        self.cfg = cfg

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(
            record.created,
            ZoneInfo(self.cfg.get("General", "timezone", "Europe/Moscow")),
        ).strftime(datefmt or "%d.%m.%Y %H:%M:%S")

    def formatException(self, exc_info):
        import traceback

        lines = ["Traceback (most recent call last):"]
        for frame in traceback.extract_tb(exc_info[2]):
            lines.append(f"  {frame.filename}:{frame.lineno} in {frame.name}")
        error = exc_info[1]
        if hasattr(error, "short_str"):
            try:
                detail = error.short_str()
            except Exception:
                detail = ""
        elif isinstance(error, ModuleNotFoundError):
            name = getattr(error, "name", "") or ""
            detail = (
                "Нет Python-модуля " + name
                if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name)
                else "Не найдена зависимость Python"
            )
        elif getattr(error, "response", None) is not None:
            detail = "HTTP " + str(getattr(error.response, "status_code", "?"))
        elif type(error).__name__ in (
            "ValueError",
            "TypeError",
            "KeyError",
            "AttributeError",
            "TelegramBadRequest",
            "TelegramForbiddenError",
            "TelegramRetryAfter",
        ):
            detail = str(error)[:500]
        else:
            detail = ""
        lines.append(type(error).__name__ + (": " + str(detail) if detail else ""))
        return "\n".join(lines)

    def format(self, record):
        if getattr(record, "session_separator", False):
            return "\n" + "=" * 72
        # API exceptions may include raw headers/body/HTML; log only their short form.
        # Use its short description while retaining the stack and HTTP status.
        from .framework import PLUGIN_SECRET_FIELDS

        record = copy.copy(record)
        record.source = event_source(record)
        if isinstance(record.msg, BaseException) and hasattr(record.msg, "short_str"):
            record.msg = record.msg.short_str()
            record.args = ()
        if record.args:

            def safe(value):
                return (
                    value.short_str()
                    if isinstance(value, BaseException) and hasattr(value, "short_str")
                    else value
                )

            record.args = (
                {k: safe(v) for k, v in record.args.items()}
                if isinstance(record.args, dict)
                else tuple(safe(v) for v in record.args)
            )
        value = super().format(record)
        secrets_to_hide = [
            self.cfg.get(section, key)
            for section, key in [
                ("Telegram", "token"),
                ("Telegram", "proxy"),
                ("FunPay", "golden_key"),
                ("FunPay", "proxy"),
            ]
        ]
        for path in (self.cfg.path.parent / "plugins").glob("*/config.json"):
            try:
                config = load_parser(path, {"Plugin": {}, "Provider": {}})
                fields = PLUGIN_SECRET_FIELDS.get(path.parent.name, set())
                fields = fields | {
                    key
                    for section in config.sections()
                    for key in config[section]
                    if re.search(
                        r"(?:password|secret|token|cookie|mnemonic|(?:^|_)key$|(?:^|_)hash$)",
                        key,
                        re.I,
                    )
                }
                secrets_to_hide.extend(
                    config.get(section, key, fallback="")
                    for section in config.sections()
                    for key in fields
                )
            except Exception:
                pass
        for secret in sorted(filter(None, secrets_to_hide), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", "[TOKEN]", value)
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[PROXY]@", value)
        value = re.sub(r"(?i)([?&](?:hash|token|key)=)[^&\s]+", r"\1[REDACTED]", value)
        return value


class SafeAPILogFilter(logging.Filter):
    def filter(self, record):
        # This pinned client's account DEBUG branch prints whole HTTP responses.
        # Keep its exception stacks and INFO/WARNING/ERROR; suppress raw responses.
        return not (
            record.name == "FunPayAPI.account"
            and record.levelno < logging.INFO
            and not record.exc_info
        )


def print_banner(version, stream=None, *, animate=True):
    """Показывает логотип и резервирует ему область для плавного затухания."""
    stream = stream or sys.stdout
    interactive = bool(getattr(stream, "isatty", lambda: False)())
    color = (
        interactive
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    )
    if color and os.name == "nt":
        try:
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(stream.fileno())
            console_mode = ctypes.c_ulong()
            kernel = ctypes.windll.kernel32
            color = bool(
                kernel.GetConsoleMode(
                    ctypes.c_void_p(handle), ctypes.byref(console_mode)
                )
            ) and bool(
                kernel.SetConsoleMode(
                    ctypes.c_void_p(handle), console_mode.value | 0x0004
                )
            )
        except (OSError, ValueError, AttributeError):
            color = False
    art = """
  █████▒█    ██  ███▄    █  ██▓███   ▄▄▄     ▓██   ██▓    ▄▄▄▄    ▒█████  ▄▄▄█████▓
▓██   ▒ ██  ▓██▒ ██ ▀█   █ ▓██░  ██▒▒████▄    ▒██  ██▒   ▓█████▄ ▒██▒  ██▒▓  ██▒ ▓▒
▒████ ░▓██  ▒██░▓██  ▀█ ██▒▓██░ ██▓▒▒██  ▀█▄   ▒██ ██░   ▒██▒ ▄██▒██░  ██▒▒ ▓██░ ▒░
░▓█▒  ░▓▓█  ░██░▓██▒  ▐▌██▒▒██▄█▓▒ ▒░██▄▄▄▄██  ░ ▐██▓░   ▒██░█▀  ▒██   ██░░ ▓██▓ ░
░▒█░   ▒▒█████▓ ▒██░   ▓██░▒██▒ ░  ░ ▓█   ▓██▒ ░ ██▒▓░   ░▓█  ▀█▓░ ████▓▒░  ▒██▒ ░
 ▒ ░   ░▒▓▒ ▒ ▒ ░ ▒░   ▒ ▒ ▒▓▒░ ░  ░ ▒▒   ▓▒█░  ██▒▒▒    ░▒▓███▀▒░ ▒░▒░▒░   ▒ ░░
 ░     ░░▒░ ░ ░ ░ ░░   ░ ▒░░▒ ░       ▒   ▒▒ ░▓██ ░▒░    ▒░▒   ░   ░ ▒ ▒░     ░
 ░ ░    ░░░ ░ ░    ░   ░ ░ ░░         ░   ▒   ▒ ▒ ░░      ░    ░ ░ ░ ░ ▒    ░
          ░              ░                ░  ░░ ░         ░          ░ ░
                                              ░ ░              ░
""".strip("\n")
    lines = [line.rstrip() for line in art.splitlines()]
    footer = "  FunPayBOT " + version + "  •  Telegram / FunPay"
    height = len(lines) + 2
    width = max(len(line) for line in lines + [footer])
    size = shutil.get_terminal_size(fallback=(120, 30))
    blinking = color and animate and size.columns >= width and size.lines >= height + 2

    if blinking and size.lines >= height + 5:
        from .console_animation import BannerPulse

        global _banner_animation
        stop_banner_animation()
        _banner_animation = BannerPulse(stream, lines, footer, size.lines)
        _banner_animation.setup()
        return
    body = "\n".join(lines)
    if color:
        body = "\033[96m" + body + "\033[0m"
    stream.write(body + "\n" + footer + "\n\n")
    stream.flush()


_banner_animation = None


def start_banner_animation():
    """Включает медленное затухание после инициализации; в файлы ANSI не попадает."""
    if _banner_animation:
        _banner_animation.start()


def stop_banner_animation():
    """Возвращает терминал в исходный режим после остановки бота."""
    global _banner_animation
    if _banner_animation:
        _banner_animation.stop()
        _banner_animation = None


def log_account_summary(account, balance=None):
    """Показывает сведения авторизованного аккаунта в отдельном консольном блоке."""

    def clean(value):
        """Убирает из отображаемого значения управляющие символы терминала."""
        return "".join(char for char in str(value) if char.isprintable())

    balance_text = (
        " | ".join(
            clean(balance.get(key, "—")) + " " + currency
            for key, currency in (("total", "RUB"), ("usd", "USD"), ("eur", "EUR"))
        )
        if balance is not None
        else "не удалось получить"
    )
    lines = [
        "Аккаунт FunPay подключён: " + clean(getattr(account, "username", None) or "—"),
        "Ваш ID: " + clean(getattr(account, "id", None) or "—"),
        "Ваш текущий баланс: " + balance_text,
        "Текущие незавершённые сделки: "
        + clean(
            getattr(account, "active_sales", None)
            if getattr(account, "active_sales", None) is not None
            else "—"
        ),
        "Удачной торговли!",
    ]
    width = max(60, max(map(len, lines)) + 4)
    border = "-" * width
    body = "\n".join("* " + line.ljust(width - 4) + " *" for line in lines)
    LOG.info("\n%s\n%s\n%s", border, body, border, extra={"account_summary": True})


def log_event_reception(cfg):
    """Показывает текущее положение переключателя получения событий FunPay."""
    online = cfg.flag("SellerStatus", "online")
    LOG.info(
        "Статус FunPay BOT: %s",
        "Онлайн (слушает FunPay)" if online else "Офлайн (включи в главном меню)",
        extra={"funpay_online": online},
    )


class ConsoleFormatter(Redactor):
    """Оформляет консольные события и ошибки, скрывая секретные значения."""

    def __init__(self, cfg, stream=None):
        super().__init__(cfg)
        self._style._fmt = "%(asctime)s │ %(levelname)s │ %(source)s │ %(message)s"
        self.datefmt = "%H:%M:%S"
        self.color = (
            bool(getattr(stream or sys.stderr, "isatty", lambda: False)())
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )

    def format(self, record):
        record = copy.copy(record)
        if record.levelno >= logging.ERROR:
            record.msg = (
                "Произошла ошибка при выполнении "
                + error_operation(record)
                + " (подробнее в журнале)"
            )
            record.args = ()
        # Файловый форматтер не должен передать в терминал закэшированный traceback.
        record.exc_info = record.exc_text = record.stack_info = None
        text = super().format(record)
        if getattr(record, "account_summary", False):
            text = text.split("\n", 1)[1]
        if not self.color:
            return text
        online = getattr(record, "funpay_online", None)
        if online is not None:
            status = "Онлайн" if online else "Офлайн"
            color = "\033[92m" if online else "\033[97m"
            return text.replace(status, color + status + "\033[0m", 1)
        color = (
            "\033[91m"
            if record.levelno >= logging.ERROR
            else "\033[93m"
            if record.levelno >= logging.WARNING
            else "\033[96m"
        )
        return color + text + "\033[0m"


def setup_logs(cfg, directory):
    directory.mkdir(parents=True, exist_ok=True)
    api = logging.getLogger("FunPayAPI")
    for handler in set(LOG.handlers + api.handlers + logging.getLogger().handlers):
        handler.close()
    file = RotatingFileHandler(
        directory / "log.log",
        maxBytes=max(65536, int(cfg.get("Logging", "max_bytes", "5000000"))),
        backupCount=max(0, int(cfg.get("Logging", "backup_count", "3"))),
        encoding="utf-8",
    )
    file.setLevel(logging.DEBUG)
    file.setFormatter(Redactor(cfg))
    file.addFilter(SafeAPILogFilter())
    file.addFilter(ErrorPolicyFilter())
    file.addFilter(JournalLevelFilter(cfg))
    from .console_animation import ConsoleHandler

    console = ConsoleHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(ConsoleFormatter(cfg, console.stream))
    console.addFilter(SafeAPILogFilter())
    console.addFilter(ErrorPolicyFilter())
    console.addFilter(ConsoleErrorFilter())
    LOG.handlers[:] = [file, console]
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False
    api.handlers[:] = [file, console]
    api.setLevel(logging.DEBUG)
    api.propagate = False
    root = logging.getLogger()
    root.handlers[:] = [file, console]
    root.setLevel(logging.WARNING)
    LOG.configured = True
    separator = LOG.makeRecord(LOG.name, logging.INFO, __file__, 0, "", (), None)
    separator.session_separator = True
    file.handle(separator)
    LOG.info("Инициализация FunPayBOT %s. Журнал: /data/log.log", VERSION)
    log_event_reception(cfg)


def plugin_logger(cfg, directory, name):
    logger = logging.getLogger("FunPayBOT.plugins." + name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    if getattr(LOG, "configured", False):
        path = directory / "plugins" / name / "log.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not any(
            isinstance(h, RotatingFileHandler)
            and Path(h.baseFilename) == path.resolve()
            for h in logger.handlers
        ):
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()
            handler = RotatingFileHandler(
                path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(Redactor(cfg))
            handler.addFilter(ErrorPolicyFilter())
            handler.addFilter(JournalLevelFilter(cfg))
            logger.addHandler(handler)
    return logger
