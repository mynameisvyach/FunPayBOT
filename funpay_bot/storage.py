"""Storage services."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .primitives import state_transaction


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    """Persistent topics, deduplication, financial ledger and payment recovery; atomic snapshots."""

    def __init__(self, path):
        self.path = Path(path)
        self.d = {
            "topics": {},
            "cards": {},
            "events": {},
            "actions": {},
            "greetings": {},
            "orders": {},
            "plugin_disabled": [],
            "last_human": 0,
            "raise_due": {},
            "jobs": {},
            "tg_offset": 0,
            "inbox": {},
            "ledger": {},
            "withdrawals": {},
            "plugin_state": {},
        }
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Повреждён state.json")
            self.d.update(data)
        self.d.setdefault("ticket_last_attempt", 0)
        self.save()

    def save(self):
        atomic_write(self.path, json.dumps(self.d, ensure_ascii=False, indent=2))

    def transaction(self):
        return state_transaction(self)


class SingleProcess:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "process.lock").open("a+")
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Уже запущен другой экземпляр с этим state.json")
        else:
            import msvcrt

            self.file.seek(0)
            self.file.write("0")
            self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Уже запущен другой экземпляр")
