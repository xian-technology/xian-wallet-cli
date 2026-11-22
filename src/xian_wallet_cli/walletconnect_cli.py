"""Install and run the optional WalletConnect terminal bridge."""

import asyncio
import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer

from .domain import WalletError
from .profiles import NetworkProfile
from .vault import AccountBackup, secret_file

walletconnect = typer.Typer(
    no_args_is_help=True, help="Pair a dapp with a scoped interactive wallet session."
)


def bridge_home(store) -> Path:
    with store.transaction():
        directory = store.home / "walletconnect"
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory.resolve()


@walletconnect.command()
def setup(ctx: typer.Context) -> None:
    """Install the pinned optional Node transport into the wallet home."""
    if not shutil.which("npm"):
        raise WalletError("npm_required", "Install Node.js 22 or later to use WalletConnect.")
    directory = bridge_home(ctx.obj.store)
    resources = files("xian_wallet_cli").joinpath("walletconnect")
    for name in ("package.json", "package-lock.json", "policy.mjs", "bridge.mjs"):
        (directory / name).write_bytes(resources.joinpath(name).read_bytes())
    result = subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=directory,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=None,
    )
    if result.returncode:
        raise WalletError(
            "walletconnect_install_failed", "Could not install the WalletConnect transport."
        )
    ctx.obj.emit({"transport": str(directory)})


@walletconnect.command()
def pair(
    ctx: typer.Context,
    account: Annotated[str, typer.Option()],
    network: Annotated[str, typer.Option(help="Pinned wallet network name.")],
    uri_file: Annotated[Path | None, typer.Option(help="Owner-only pairing URI file.")] = None,
) -> None:
    """Pair on a pinned network and review each write request in the terminal."""
    from .chain import Rpc
    from .cli import require_terminal

    output = ctx.obj
    require_terminal(output)
    entry = output.store.read("account", account, AccountBackup)
    if entry.account.kind == "watch":
        raise WalletError("watch_only", "Choose a signing account for WalletConnect.")
    profile = output.store.read("network", network, NetworkProfile)
    verified = asyncio.run(Rpc(profile.url, expected=profile.network).identity())
    directory = bridge_home(output.store)
    executable = shutil.which("xian-wallet")
    if not shutil.which("node") or not executable or not (directory / "node_modules").is_dir():
        raise WalletError("walletconnect_unavailable", "Run xian-wallet walletconnect setup first.")
    if not os.environ.get("XIAN_WALLETCONNECT_PROJECT_ID"):
        raise WalletError("walletconnect_project_required", "Set XIAN_WALLETCONNECT_PROJECT_ID.")
    uri = (
        secret_file(uri_file, "pairing URI")
        if uri_file
        else typer.prompt("Pairing URI", hide_input=True)
    )
    if not uri.startswith("wc:") or len(uri) > 2048:
        raise WalletError("invalid_pairing_uri", "Use a WalletConnect pairing URI.")
    with NamedTemporaryFile(dir=directory, mode="w", encoding="utf-8") as temporary:
        temporary.write(uri)
        temporary.flush()
        result = subprocess.run(
            [
                "node",
                str(directory / "bridge.mjs"),
                executable,
                str(output.store.home.resolve()),
                network,
                verified.chain_id,
                account,
                entry.account.address,
                temporary.name,
            ],
            cwd=directory,
            check=False,
            env={key: value for key, value in os.environ.items() if key != "XIAN_WALLET_RPC"},
        )
    if result.returncode:
        raise WalletError(
            "walletconnect_closed", "The WalletConnect transport ended with an error."
        )
