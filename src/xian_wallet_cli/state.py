"""Private, transactional wallet state. Each record has one owner and schema."""

import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TypeVar

from .domain import Document, WalletError

T = TypeVar("T", bound=Document)


def default_home() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "xian-wallet"


class Store:
    def __init__(self, home: Path):
        self.home = home.expanduser()
        self.path = self.home / "wallet.sqlite3"

    @contextmanager
    def transaction(self, *, write: bool = False):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.home.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise WalletError("unsafe_wallet_home", "Wallet home must be an owner-only directory.")
        fd = os.open(self.path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "rb") as stream:
            file_info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(file_info.st_mode)
                or file_info.st_mode & 0o077
                or file_info.st_uid != os.geteuid()
            ):
                raise WalletError(
                    "unsafe_wallet_home", "Wallet database must be an owner-only file."
                )
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "namespace TEXT, name TEXT, data TEXT, PRIMARY KEY(namespace,name))"
            )
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def read(self, namespace: str, name: str, model: type[T]) -> T:
        with self.transaction() as db:
            row = db.execute(
                "SELECT data FROM records WHERE namespace=? AND name=?", (namespace, name)
            ).fetchone()
        if row is None:
            raise WalletError("record_not_found", f"No {namespace} named {name}.")
        return model.model_validate_json(row[0])

    def entries(self, namespace: str, model: type[T]) -> list[T]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT data FROM records WHERE namespace=? ORDER BY name", (namespace,)
            ).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def add(self, namespace: str, name: str, value: Document) -> None:
        try:
            with self.transaction(write=True) as db:
                db.execute(
                    "INSERT INTO records VALUES (?, ?, ?)",
                    (namespace, name, value.model_dump_json()),
                )
        except sqlite3.IntegrityError as exc:
            raise WalletError(
                "duplicate_name", f"A {namespace} named {name} already exists."
            ) from exc

    def remove(self, namespace: str, name: str) -> None:
        with self.transaction(write=True) as db:
            if not db.execute(
                "DELETE FROM records WHERE namespace=? AND name=?", (namespace, name)
            ).rowcount:
                raise WalletError("record_not_found", f"No {namespace} named {name}.")
