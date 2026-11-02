"""Общее чтение деклараций и установка зависимостей ядра и плагинов.

Плагины объявляют точные версии пакетов и проверки импорта.
Модуль не содержит названий и правил конкретных плагинов.
"""

from __future__ import annotations

import os
import re
import sys

from .metadata import ROOT


class StartupError(RuntimeError):
    """Описывает ошибку запуска понятным сообщением без раскрытия секретов."""


def runtime_dependency_spec(plugin_names=None):
    """Читает статические объявления зависимостей без выполнения плагина и чтения его доступов."""
    import ast

    path = ROOT / "requirements.txt"
    if not path.is_file():
        raise StartupError(
            "Не найден requirements.txt. Восстановите файл из релиза; конфиги менять не нужно."
        )
    requirements = {}
    modules = {
        "aiogram": "aiogram",
        "requests": "requests",
        "requests_toolbelt": "requests-toolbelt",
        "bs4": "beautifulsoup4",
        "lxml.etree": "lxml",
        "PIL.Image": "pillow",
        "aiohttp_socks": "aiohttp_socks",
        "matplotlib.pyplot": "matplotlib",
    }

    def register(name, version, source):
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
        ):
            raise StartupError(source + ": неверное имя зависимости")
        if not isinstance(version, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version
        ):
            raise StartupError(source + ": нужна точная версия зависимости")
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in requirements and requirements[key][1] != version:
            raise StartupError(
                source + ": версия " + name + " конфликтует с другой зависимостью"
            )
        requirements[key] = (name, version)

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise StartupError(
                "Неподдерживаемая запись requirements.txt: нужен формат name==version"
            )
        register(*match.groups(), "requirements.txt")
    for plugin in sorted((ROOT / "plugins").glob("*.py")):
        if plugin_names is not None and plugin.stem not in plugin_names:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", plugin.stem):
            continue
        try:
            tree = ast.parse(plugin.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError):
            # The plugin loader reports malformed code once; the core can still start.
            continue
        declared = {}
        for node in tree.body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id in (
                    "REQUIREMENTS",
                    "REQUIRED_IMPORTS",
                ):
                    try:
                        declared[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        raise StartupError(
                            plugin.name
                            + ": зависимости должны быть литеральными словарями"
                        ) from None
        pins = declared.get("REQUIREMENTS", {})
        imports = declared.get("REQUIRED_IMPORTS", {})
        if not isinstance(pins, dict) or not isinstance(imports, dict):
            raise StartupError(
                plugin.name + ": REQUIREMENTS и REQUIRED_IMPORTS должны быть словарями"
            )
        for name, version in pins.items():
            register(name, version, plugin.name)
        for module, package in imports.items():
            if not isinstance(module, str) or not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module
            ):
                raise StartupError(plugin.name + ": неверное имя импортируемого модуля")
            if (
                not isinstance(package, str)
                or re.sub(r"[-_.]+", "-", package).lower() not in requirements
            ):
                raise StartupError(
                    plugin.name + ": для импортируемого модуля не объявлена зависимость"
                )
            modules[module] = package
    return list(requirements.values()), modules


def dependency_issues(plugin_names=None):
    """Проверяет пакеты ядра и плагинов до настройки и выполнения расширений."""
    from importlib import import_module, metadata

    requirements, modules = runtime_dependency_spec(plugin_names)
    issues = []
    for name, expected in requirements:
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            issues.append(
                {
                    "kind": "missing" if actual is None else "version",
                    "package": name,
                    "expected": expected,
                }
            )
    if issues:
        return issues
    for module, package in modules.items():
        try:
            import_module(module)
        except Exception:
            issues.append({"kind": "import", "package": package, "module": module})
    return issues


def ensure_runtime_dependencies():
    """Устанавливает пакеты отдельно от настроек и повторяет исходный способ запуска."""
    import subprocess

    if sys.version_info < (3, 11):
        raise StartupError(
            "Нужен Python 3.11 или новее, рекомендуется 3.12. Конфиги не изменялись."
        )
    issues = dependency_issues()
    flag = "FUNPAY_BOT_DEPENDENCY_REPAIR"
    if not issues:
        os.environ.pop(flag, None)
        print("Проверка пакетов ядра и плагинов: OK.")
        return True
    names = ", ".join(sorted({row["package"] for row in issues}))
    print("Не готовы зависимости Python: " + names)
    print("Это проверка пакетов, а не ошибка настроенных конфигов.")
    if os.environ.get(flag) == "1":
        raise StartupError(
            "Пакеты после установки не прошли проверку: "
            + names
            + ". Конфиги не изменялись."
        )
    if sys.prefix == sys.base_prefix:
        env = ROOT / ".venv"
        python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            print("Создаю локальное окружение .venv…")
            subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
        os.execv(str(python), [str(python), str(ROOT / "main.py"), *sys.argv[1:]])
        return False
    try:
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if pip.returncode:
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade"],
                check=True,
                timeout=120,
            )
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(ROOT / "requirements.txt"),
        ]
        requirements, _ = runtime_dependency_spec()
        command.extend(name + "==" + version for name, version in requirements)
        if any(row["kind"] == "import" for row in issues):
            command.append("--force-reinstall")
        print("Устанавливаю закреплённые зависимости ядра и обнаруженных плагинов…")
        build_environment = dict(os.environ)
        build_environment.setdefault("CMAKE_BUILD_PARALLEL_LEVEL", "2")
        build_environment.setdefault("MAX_JOBS", "2")
        subprocess.run(command, check=True, timeout=1800, env=build_environment)
        subprocess.run([sys.executable, "-m", "pip", "check"], check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise StartupError(
            "Установка зависимостей не завершена. Проверьте сеть, Python и вывод pip. Для сборки нативных пакетов нужен C/C++ компилятор. Настроенные конфиги не изменялись."
        ) from error
    os.environ[flag] = "1"
    os.execv(sys.executable, [sys.executable, str(ROOT / "main.py"), *sys.argv[1:]])
    return False
