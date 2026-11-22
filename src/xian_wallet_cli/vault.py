"""Versioned Argon2id and XChaCha20-Poly1305 account backups."""

from typing import Annotated, Literal

from nacl import pwhash, secret, utils
from nacl.exceptions import CryptoError
from pydantic import Field, StringConstraints, ValidationError, model_validator
from xian_py.transaction import canonical_json
from xian_py.wallet import HDWallet, Wallet

from .domain import Address, Document, WalletError
from .files import read_private_text

Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")]
AccountIndex = Annotated[int, Field(ge=0, le=2**32 - 1)]


class Account(Document):
    name: Name
    address: Address
    kind: Literal["mnemonic", "private_key", "watch"]
    index: AccountIndex | None = None


class AccountBackup(Document):
    version: Literal["xian-wallet-vault-v1"] = "xian-wallet-vault-v1"
    account: Account
    salt: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")] | None = None
    ciphertext: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{80,2048}$")] | None = None

    @model_validator(mode="after")
    def valid_custody(self):
        if self.account.kind == "watch" and self.salt is None and self.ciphertext is None:
            return self
        if self.account.kind != "watch" and self.salt and self.ciphertext:
            return self
        raise ValueError("invalid account custody record")

    @property
    def aad(self) -> bytes:
        return canonical_json(
            {"version": self.version, "account": self.account.model_dump()}
        ).encode()

    def secret_text(self, password: str) -> str:
        if self.account.kind == "watch":
            raise WalletError("watch_only", "A watch-only account has no signing key.")
        try:
            box = account_box(password, bytes.fromhex(self.salt))
            text = box.decrypt(bytes.fromhex(self.ciphertext), self.aad).decode()
        except (CryptoError, ValueError) as exc:
            raise WalletError(
                "vault_locked", "The password or encrypted backup is invalid."
            ) from exc
        if (
            account_wallet(self.account.kind, text, self.account.index).public_key
            != self.account.address
        ):
            raise WalletError("invalid_backup", "The backup does not restore its recorded address.")
        return text

    def unlock(self, password: str) -> Wallet:
        return account_wallet(self.account.kind, self.secret_text(password), self.account.index)


def account_box(password: str, salt: bytes) -> secret.Aead:
    return secret.Aead(
        pwhash.argon2id.kdf(
            secret.Aead.KEY_SIZE,
            password.encode(),
            salt,
            opslimit=pwhash.argon2id.OPSLIMIT_MODERATE,
            memlimit=pwhash.argon2id.MEMLIMIT_MODERATE,
        )
    )


def account_wallet(kind: str, text: str, index: int | None = None) -> Wallet:
    try:
        if kind == "mnemonic" and index is not None:
            return Wallet.from_mnemonic(text, account_index=index)
        if kind == "private_key":
            return Wallet(text)
    except (ValueError, ValidationError) as exc:
        raise WalletError("invalid_account_secret", "The account secret is invalid.") from exc
    raise WalletError("invalid_account_secret", "This account cannot be used for signing.")


def seal_account(
    name: str,
    kind: Literal["mnemonic", "private_key"],
    text: str,
    password: str,
    *,
    index: int | None = None,
) -> AccountBackup:
    if len(password) < 12:
        raise WalletError("weak_password", "Use a vault password of at least 12 characters.")
    account = Account(
        name=name, address=account_wallet(kind, text, index).public_key, kind=kind, index=index
    )
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    aad = canonical_json(
        {"version": "xian-wallet-vault-v1", "account": account.model_dump()}
    ).encode()
    ciphertext = bytes(account_box(password, salt).encrypt(text.encode(), aad)).hex()
    return AccountBackup(account=account, salt=salt.hex(), ciphertext=ciphertext)


def new_mnemonic() -> str:
    return HDWallet().mnemonic_str


def secret_file(path, label: str) -> str:
    return read_private_text(path, label=label, max_bytes=2048).rstrip("\r\n")
