"""Account management and the receive workflow."""

import io
import sys
from pathlib import Path
from typing import Annotated

import qrcode
import typer
from rich.console import Console

from .domain import WalletError
from .files import load_document, write_new
from .vault import Account, AccountBackup, new_mnemonic, seal_account, secret_file

accounts = typer.Typer(no_args_is_help=True, help="Create, restore, back up or watch accounts.")
PasswordFile = Annotated[Path | None, typer.Option(help="Owner-only password file for the signer.")]
Index = Annotated[int, typer.Option(min=0, max=2**32 - 1)]


def password_for(ctx: typer.Context, path: Path | None, *, confirm: bool = False) -> str:
    if path is not None:
        return secret_file(path, "password")
    if ctx.obj.as_json or not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise WalletError("password_required", "Use an owner-only password file in headless mode.")
    return typer.prompt("Vault password", hide_input=True, confirmation_prompt=confirm, err=True)


def save_account(ctx: typer.Context, backup: AccountBackup) -> None:
    ctx.obj.store.add("account", backup.account.name, backup)
    ctx.obj.emit(backup.account.model_dump())


@accounts.command()
def create(ctx: typer.Context, name: str, password_file: PasswordFile = None) -> None:
    """Create an encrypted mnemonic account."""
    password = password_for(ctx, password_file, confirm=True)
    save_account(ctx, seal_account(name, "mnemonic", new_mnemonic(), password, index=0))


@accounts.command()
def restore(
    ctx: typer.Context,
    name: str,
    mnemonic_file: Annotated[Path, typer.Option(help="Owner-only BIP39 recovery phrase file.")],
    password_file: PasswordFile = None,
    index: Index = 0,
) -> None:
    """Restore the same indexed address as the browser and mobile wallets."""
    password = password_for(ctx, password_file, confirm=True)
    phrase = secret_file(mnemonic_file, "recovery phrase")
    save_account(
        ctx, seal_account(name, "mnemonic", " ".join(phrase.split()), password, index=index)
    )


@accounts.command("import-key")
def import_key(
    ctx: typer.Context,
    name: str,
    key_file: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
) -> None:
    """Move an existing Ed25519 key into the encrypted vault."""
    password = password_for(ctx, password_file, confirm=True)
    save_account(
        ctx,
        seal_account(name, "private_key", secret_file(key_file, "private-key").strip(), password),
    )


@accounts.command()
def derive(
    ctx: typer.Context,
    source: str,
    name: str,
    index: Annotated[int, typer.Option(min=0, max=2**32 - 1)],
    password_file: PasswordFile = None,
) -> None:
    """Add another account from the same recovery phrase."""
    original = ctx.obj.store.read("account", source, AccountBackup)
    if original.account.kind != "mnemonic":
        raise WalletError("mnemonic_required", "Choose an account backed by a recovery phrase.")
    password = password_for(ctx, password_file)
    save_account(
        ctx, seal_account(name, "mnemonic", original.secret_text(password), password, index=index)
    )


@accounts.command()
def watch(ctx: typer.Context, name: str, address: str) -> None:
    """Add a public address without a signing key."""
    save_account(
        ctx, AccountBackup(account=Account(name=name, address=address.lower(), kind="watch"))
    )


@accounts.command("list")
def list_accounts(ctx: typer.Context) -> None:
    """List local account names and public addresses."""
    rows = ctx.obj.store.entries("account", AccountBackup)
    ctx.obj.emit({"accounts": [entry.account.model_dump() for entry in rows]})


@accounts.command()
def backup(ctx: typer.Context, name: str, out: Annotated[Path, typer.Option()]) -> None:
    """Write an encrypted account backup."""
    entry = ctx.obj.store.read("account", name, AccountBackup)
    write_new(out, entry.model_dump_json(indent=2) + "\n")
    ctx.obj.emit({"account": name, "backup_file": str(out)})


@accounts.command()
def restore_backup(
    ctx: typer.Context, backup_file: Path, password_file: PasswordFile = None
) -> None:
    """Verify and restore a previously encrypted CLI backup."""
    entry = load_document(backup_file, AccountBackup)
    if entry.account.kind != "watch":
        entry.unlock(password_for(ctx, password_file))
    save_account(ctx, entry)


@accounts.command()
def export_recovery(
    ctx: typer.Context,
    name: str,
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
) -> None:
    """Write the mnemonic to an owner-only file for offline recovery."""
    entry = ctx.obj.store.read("account", name, AccountBackup)
    if entry.account.kind != "mnemonic":
        raise WalletError("mnemonic_required", "Choose an account backed by a recovery phrase.")
    write_new(out, entry.secret_text(password_for(ctx, password_file)) + "\n")
    ctx.obj.emit({"account": name, "recovery_file": str(out)})


def receive(ctx: typer.Context, name: str, qr: bool = False) -> None:
    """Show a public account address and optionally render its QR code."""
    entry = ctx.obj.store.read("account", name, AccountBackup)
    ctx.obj.emit(entry.account.model_dump())
    if qr and not ctx.obj.as_json:
        graphic = qrcode.QRCode(border=2)
        graphic.add_data(entry.account.address)
        output = io.StringIO()
        graphic.print_ascii(out=output, tty=False)
        Console().print(output.getvalue(), markup=False, highlight=False)
