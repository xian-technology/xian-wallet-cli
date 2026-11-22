"""Private, write-once artifacts and owner-only signing keys."""

import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeVar

from xian_py.wallet import Wallet

from .domain import Document, WalletError

T = TypeVar("T", bound=Document)


def write_new(path: Path, data: str) -> None:
    """Publish a complete 0600 file without replacing an existing destination."""
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8") as temporary:
        temporary.write(data)
        temporary.flush()
        os.fsync(temporary.fileno())
        try:
            os.link(temporary.name, path)
        except FileExistsError as exc:
            raise WalletError("file_exists", f"The output already exists: {path}") from exc


def load_document(path: Path, document: type[T]) -> T:
    with path.open("rb") as stream:
        raw = stream.read(16_385)
    if len(raw) > 16_384:
        raise WalletError("invalid_document", "The wallet document exceeds 16 KiB.")
    return document.model_validate_json(raw)


def read_key(path: Path) -> Wallet:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise WalletError("unsafe_key_file", "Use an owner-only private-key file (0600).")
            key = stream.read(129).decode("ascii").strip()
        if len(key) != 64 or not Wallet.is_valid_key(key):
            raise ValueError
        return Wallet(key)
    except (OSError, ValueError) as exc:
        raise WalletError(
            "invalid_key_file", "Could not read a 32-byte private key safely."
        ) from exc
