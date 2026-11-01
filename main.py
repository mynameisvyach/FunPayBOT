#!/usr/bin/env python3
"""FunPay BOT command-line entry point."""

import sys

from funpay_bot.bootstrap import StartupError, main
from funpay_bot.metadata import LOG

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        LOG.exception("Не удалось запустить бота")
        if isinstance(error, (ValueError, ModuleNotFoundError, StartupError)):
            print("Не удалось запустить:", str(error), file=sys.stderr)
        else:
            print(
                "Не удалось запустить:",
                type(error).__name__,
                "— проверьте data/log.log",
                file=sys.stderr,
            )
        sys.exit(1)
