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
    write_new_bytes(path, data.encode())


def write_new_bytes(path: Path, data: bytes) -> None:
    """Publish a complete 0600 file without replacing an existing destination."""
    with NamedTemporaryFile(dir=path.parent, mode="wb") as temporary:
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


def read_private_text(path: Path, *, label: str, max_bytes: int) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise WalletError("unsafe_key_file", f"Use an owner-only {label} file (0600).")
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError
        return raw.decode()
    except (OSError, ValueError) as exc:
        raise WalletError("invalid_key_file", f"Could not read the {label} file safely.") from exc


def read_key(path: Path) -> Wallet:
    key = read_private_text(path, label="private-key", max_bytes=128).strip()
    if len(key) != 64 or not Wallet.is_valid_key(key):
        raise WalletError("invalid_key_file", "Could not read a 32-byte private key safely.")
    return Wallet(key)
