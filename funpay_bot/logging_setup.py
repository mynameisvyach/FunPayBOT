"""Logging setup services."""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_parser
from .metadata import LOG, VERSION


class Redactor(logging.Formatter):
    def __init__(self, cfg):
        super().__init__(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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
        # API exceptions may include raw headers/body/HTML; log only their short form.
        # Use its short description while retaining the stack and HTTP status.
        from .framework import PLUGIN_SECRET_FIELDS

        record = copy.copy(record)
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


def setup_logs(cfg, directory):
    directory.mkdir(parents=True, exist_ok=True)
    api = logging.getLogger("FunPayAPI")
    for handler in set(LOG.handlers + api.handlers):
        handler.close()
    file = RotatingFileHandler(
        directory / "log.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file.setLevel(logging.DEBUG)
    file.setFormatter(Redactor(cfg))
    file.addFilter(SafeAPILogFilter())
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(Redactor(cfg))
    console.addFilter(SafeAPILogFilter())
    LOG.handlers[:] = [file, console]
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False
    api.handlers[:] = [file, console]
    api.setLevel(logging.DEBUG)
    api.propagate = False
    LOG.configured = True
    LOG.info("Запуск FunPay BOT %s. Журнал: %s", VERSION, directory / "log.log")


def plugin_logger(cfg, directory, name):
    logger = logging.getLogger("unified.plugins." + name)
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
            logger.addHandler(handler)
    return logger
