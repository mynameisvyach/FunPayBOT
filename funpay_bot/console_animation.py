"""Плавный логотип в отдельной области интерактивного терминала."""

from __future__ import annotations

import atexit
import logging
import math
import shutil
import threading
import time

CONSOLE_LOCK = threading.RLock()


class ConsoleHandler(logging.StreamHandler):
    """Пишет цельные записи журнала без пересечения с перерисовкой логотипа."""

    def emit(self, record):
        """Синхронизирует журнал с анимацией на том же терминале."""
        with CONSOLE_LOCK:
            super().emit(record)


class BannerPulse:
    """Оставляет журналу отдельную область и медленно меняет яркость заголовка."""

    def __init__(self, stream, lines, footer, rows):
        """Запоминает размеры и безопасную границу яркости."""
        self.stream, self.lines, self.footer, self.rows = stream, lines, footer, rows
        self.height = len(lines) + 2
        self.width = max(map(len, lines + [footer]))
        self.stopped = threading.Event()
        self.thread = None
        self.closed = False

    @staticmethod
    def brightness(elapsed):
        """Даёт восьмисекундный цикл без полного исчезновения логотипа."""
        return 0.35 + 0.65 * (1 + math.cos(2 * math.pi * elapsed / 8)) / 2

    def setup(self):
        """Выделяет заголовок до первого вывода журналов, не очищая scrollback."""
        with CONSOLE_LOCK:
            self.stream.write(
                f"\033[r\033[2J\033[H\033[{self.height + 1};{self.rows}r\033[{self.height + 1};1H"
            )
            self.frame(1.0)
        atexit.register(self.stop)

    def frame(self, level):
        """Рисует только зарезервированные строки, сохраняя позицию журнала."""
        r, g, b = (int(value * level) for value in (70, 220, 255))
        with CONSOLE_LOCK:
            self.stream.write("\0337\033[1;1H" + f"\033[38;2;{r};{g};{b}m")
            for line in self.lines + [self.footer, ""]:
                self.stream.write("\033[2K" + line + "\n")
            self.stream.write("\033[0m\0338")
            self.stream.flush()

    def start(self):
        """Запускает фоновое затухание после готовности бота."""
        if self.thread or self.closed:
            return

        def run():
            """Останавливает перерисовку при слишком маленьком или закрытом терминале."""
            start = time.monotonic()
            try:
                while not self.stopped.wait(0.12):
                    size = shutil.get_terminal_size(fallback=(120, 30))
                    if size.columns < self.width or size.lines < self.height + 5:
                        break
                    if size.lines != self.rows:
                        with CONSOLE_LOCK:
                            self.rows = size.lines
                            self.stream.write(
                                f"\0337\033[{self.height + 1};{self.rows}r\0338"
                            )
                    self.frame(self.brightness(time.monotonic() - start))
            except (OSError, ValueError):
                pass
            finally:
                self.restore()

        self.thread = threading.Thread(target=run, name="funpaybot-banner", daemon=True)
        self.thread.start()

    def restore(self):
        """Возвращает обычную область прокрутки после остановки."""
        with CONSOLE_LOCK:
            if self.closed:
                return
            self.closed = True
            try:
                self.stream.write(f"\033[0m\033[r\033[{self.rows};1H\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def stop(self):
        """Завершает поток до возврата командной строки пользователю."""
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        self.restore()
