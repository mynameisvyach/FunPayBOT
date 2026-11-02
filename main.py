#!/usr/bin/env python3
"""Точка запуска FunPay BOT и единственное место задания версии проекта."""

VERSION = "v1.60"


def launch():
    import sys

    from funpay_bot.bootstrap import main
    from funpay_bot.logging_setup import print_banner
    from funpay_bot.metadata import LOG

    try:
        if not any(arg in ("-h", "--help") for arg in sys.argv[1:]):
            print_banner(VERSION)
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        if getattr(LOG, "configured", False):
            LOG.exception("Не удалось запустить бота")
        else:
            # Ошибки до настройки журналов тоже не должны печатать стек в терминал.
            import traceback
            from pathlib import Path

            try:
                directory = Path(__file__).resolve().parent / "data"
                directory.mkdir(parents=True, exist_ok=True)
                frames = traceback.extract_tb(error.__traceback__)
                with (directory / "log.log").open("a", encoding="utf-8") as stream:
                    stream.write(
                        "\nERROR: Ошибка запуска до инициализации журналов\nTraceback (most recent call last):\n"
                    )
                    for frame in frames:
                        stream.write(
                            f"  {frame.filename}:{frame.lineno} in {frame.name}\n"
                        )
                    stream.write(type(error).__name__ + "\n")
            except OSError:
                print(
                    "ERROR │ FunPay BOT │ Не удалось записать журнал запуска",
                    file=sys.stderr,
                )
            print(
                "ERROR │ FunPay BOT │ Произошла ошибка при выполнении запуска бота (подробнее в журнале)",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        from funpay_bot.logging_setup import stop_banner_animation

        stop_banner_animation()


if __name__ == "__main__":
    launch()
