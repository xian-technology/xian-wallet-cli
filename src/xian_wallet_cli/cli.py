"""Typer entry point for people and automation."""

import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError
from rich.console import Console
from rich.text import Text
from xian_py.exception import XianException
from xian_py.wallet import Wallet

from .accounts_cli import PasswordFile, accounts, password_for, receive
from .activity import Activity, Journal, refresh_activity, reserve_signing
from .agent_cli import agent, grants, inbox
from .chain import (
    Finality,
    Rpc,
    now,
    prepare_call,
    prepare_payload,
    prepare_transfer,
    submit_transfer,
)
from .chain_apps import dex, message, nft
from .domain import (
    MAX_TTL,
    Approval,
    Contract,
    Digest,
    Proposal,
    UnsignedPayload,
    WalletError,
    exact_amount,
    read_arguments,
    read_proposal,
    verify_approval,
)
from .files import load_document, read_key, write_new
from .payments import payments
from .presentation import ReviewApp, preview_data, render_preview
from .profiles import Token, contacts, networks, resolve_address, resolve_rpc, tokens
from .shielded import shielded
from .state import Store, default_home
from .vault import AccountBackup
from .walletconnect_cli import walletconnect

SCHEMA = "xian-wallet-cli/v1"
Wait = Annotated[float, typer.Option(min=0.1, max=120)]


@dataclass(frozen=True)
class Output:
    as_json: bool
    rpc_url: str | None
    store: Store
    network: str | None

    def node(self):
        return resolve_rpc(self.store, self.rpc_url, self.network, Rpc)

    def emit(self, data: dict) -> None:
        if self.as_json:
            typer.echo(json.dumps({"schema": SCHEMA, "data": data}, ensure_ascii=True))
            return
        console = Console()
        for key, value in data.items():
            if value is not None:
                console.print(
                    Text(f"{key.replace('_', ' ').capitalize()}  {value}", overflow="fold")
                )

    def plan(self, plan: Proposal) -> None:
        if self.as_json:
            self.emit(preview_data(plan))
        else:
            render_preview(Console(), plan)

    def finality(self, result: Finality) -> None:
        self.emit(result.model_dump(mode="json", exclude_none=True))
        if result.status != "completed":
            raise typer.Exit(3 if result.status == "pending" else 1)

    def fail(self, code: str, message: str) -> None:
        if self.as_json:
            typer.echo(json.dumps({"schema": SCHEMA, "error": {"code": code, "message": message}}))
        else:
            Console(stderr=True).print(Text(message, style="bold red"))
        raise typer.Exit(2)


class WalletGroup(typer.core.TyperGroup):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except WalletError as exc:
            ctx.obj.fail(exc.code, str(exc))
        except ValidationError as exc:
            error = exc.errors(include_input=False)[0]
            field = ".".join(str(part) for part in error["loc"])
            ctx.obj.fail("invalid_input", f"{field or 'Input'}: {error['msg']}")
        except XianException as exc:
            ctx.obj.fail("rpc_error", str(exc))
        except sqlite3.Error:
            ctx.obj.fail("state_error", "Could not read or update wallet state.")
        except OSError as exc:
            ctx.obj.fail("io_error", str(exc))


app = typer.Typer(
    cls=WalletGroup,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help="Manage Xian accounts, transactions and wallet approvals.",
)


app.add_typer(accounts, name="account")
app.add_typer(inbox, name="inbox")
app.add_typer(grants, name="grant")
app.add_typer(agent, name="agent")
app.add_typer(dex, name="dex")
app.add_typer(nft, name="nft")
app.add_typer(message, name="message")
app.add_typer(payments, name="x402")
app.add_typer(shielded, name="shielded")
app.add_typer(walletconnect, name="walletconnect")
app.add_typer(contacts, name="contact")
app.add_typer(networks, name="network")
app.add_typer(tokens, name="token")
app.command()(receive)


@app.callback()
def main(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON on stdout.")] = False,
    rpc: Annotated[str | None, typer.Option(envvar="XIAN_WALLET_RPC")] = None,
    home: Annotated[Path | None, typer.Option(envvar="XIAN_WALLET_HOME")] = None,
    network: Annotated[str | None, typer.Option(help="Named network profile.")] = None,
) -> None:
    ctx.obj = Output(as_json, rpc, Store(home or default_home()), network)


@app.command()
def generate(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option(help="New private-key file, created with mode 0600.")],
) -> None:
    """Create a dedicated Ed25519 account."""
    wallet = Wallet()
    write_new(out, wallet.private_key + "\n")
    ctx.obj.emit({"address": wallet.public_key, "key_file": str(out)})


@app.command()
def balance(ctx: typer.Context, address: str, token: str = "currency") -> None:
    """Read an account or address balance."""
    output: Output = ctx.obj
    address = resolve_address(ctx, address)
    contract = TypeAdapter(Contract).validate_python(token)
    node = output.node()
    amount = asyncio.run(
        node.balance(address) if contract == "currency" else node.balance(address, contract)
    )
    output.emit(
        {"address": address, "amount": amount, "asset": "XIAN" if token == "currency" else contract}
    )


@app.command()
def prepare(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--from", help="Sender's public address.")],
    recipient: Annotated[str, typer.Option("--to", help="Recipient's public address.")],
    amount: Annotated[str, typer.Option(help="Positive, exact decimal amount in XIAN.")],
    out: Annotated[Path, typer.Option(help="New proposal JSON file.")],
    chi_limit: Annotated[int | None, typer.Option(min=1, max=2**53 - 1)] = None,
    expires_in: Annotated[int, typer.Option(min=1, max=MAX_TTL)] = 300,
    token: Annotated[str, typer.Option(help="Fungible token contract.")] = "currency",
) -> None:
    """Simulate a native or token transfer and write an unsigned proposal."""
    output: Output = ctx.obj
    node = output.node()
    sender = resolve_address(ctx, sender)
    recipient = resolve_address(ctx, recipient, "contact")
    if token == "currency":
        proposal = prepare_transfer(node, sender, recipient, amount, chi_limit, expires_in)
    else:
        args = {"to": recipient, "amount": {"__fixed__": exact_amount(amount)}}
        proposal = prepare_call(node, sender, token, "transfer", args, chi_limit, expires_in)
    plan = asyncio.run(proposal)
    write_new(out, plan.model_dump_json() + "\n")
    output.plan(plan)


@app.command()
def preview(ctx: typer.Context, proposal: Path) -> None:
    """Review a proposal without connecting to a node."""
    ctx.obj.plan(read_proposal(proposal))


def require_terminal(output: Output) -> None:
    if output.as_json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise WalletError("terminal_required", "This action requires an interactive terminal.")


@app.command()
def ui(ctx: typer.Context, proposal: Path | None = None) -> None:
    """Open the wallet or review a specific proposal."""
    from .dashboard import WalletApp

    require_terminal(ctx.obj)
    if proposal is None:
        WalletApp(ctx.obj.store, ctx.obj.node).run()
    else:
        ReviewApp(read_proposal(proposal)).run()


@app.command()
def approve(
    ctx: typer.Context,
    proposal: Path,
    out: Annotated[Path, typer.Option(help="New signed approval JSON file.")],
    key_file: Annotated[
        Path | None, typer.Option(help="Owner-only sender private-key file.")
    ] = None,
    account: Annotated[str | None, typer.Option(help="Named encrypted signing account.")] = None,
    password_file: PasswordFile = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Use only after authenticated approval.")
    ] = False,
    textual: Annotated[bool, typer.Option("--textual", help="Use the full-screen review.")] = False,
    replaces: Annotated[
        str | None, typer.Option(help="Previously signed hash using this nonce.")
    ] = None,
) -> None:
    """Review and sign one proposal on the wallet owner's machine."""
    output: Output = ctx.obj
    plan = read_proposal(proposal)
    plan.require_unexpired(now())
    if yes and textual:
        raise WalletError("invalid_options", "Choose either --yes or --textual.")
    if replaces is not None:
        replaces = TypeAdapter(Digest).validate_python(replaces.upper())
    if not yes:
        require_terminal(output)
        if replaces and not typer.confirm(
            f"The original transaction {replaces} is still relayable. "
            "Review a competing transaction?"
        ):
            raise WalletError("approval_cancelled", "Replacement cancelled.")
        if textual:
            accepted = ReviewApp(plan, allow_sign=True).run()
        else:
            output.plan(plan)
            accepted = typer.confirm("Approve and sign this transfer?")
        if not accepted:
            raise WalletError("approval_cancelled", "Transfer approval cancelled.")
    wallet = choose_signer(ctx, account, key_file, password_file)
    proof = reserve_signing(output.store, plan, wallet, replaces=replaces)
    Journal(output.store).record(plan, proof)
    write_new(out, proof.model_dump_json(indent=2) + "\n")
    output.emit(
        {"proposal_id": proof.proposal_id, "tx_hash": proof.tx_hash, "approval_file": str(out)}
    )


def choose_signer(ctx, account: str | None, key_file: Path | None, password_file: Path | None):
    if (account is None) == (key_file is None):
        raise WalletError("signer_required", "Choose one signing account or key file.")
    if key_file is not None:
        return read_key(key_file)
    backup = ctx.obj.store.read("account", account, AccountBackup)
    return backup.unlock(password_for(ctx, password_file))


@app.command()
def submit(
    ctx: typer.Context,
    proposal: Path,
    approval: Annotated[Path, typer.Option(help="Signed approval for this proposal.")],
    wait: Wait = 12,
) -> None:
    """Broadcast the approved transfer and report its outcome by hash."""
    output: Output = ctx.obj
    plan = read_proposal(proposal)
    proof = load_document(approval, Approval)
    node = output.node()
    verify_approval(plan, proof)
    journal = Journal(output.store)
    journal.record(plan, proof)
    result = asyncio.run(submit_transfer(node, plan, proof, wait))
    journal.update(plan.network, result)
    output.finality(result)


@app.command()
def receipt(ctx: typer.Context, tx_hash: str, wait: Wait = 12) -> None:
    """Look up a transfer without broadcasting or signing."""
    output: Output = ctx.obj
    tx_hash = TypeAdapter(Digest).validate_python(tx_hash.strip().upper())
    node = output.node()
    result = asyncio.run(node.wait(tx_hash, wait))
    if result is not None and any(
        item.tx_hash == tx_hash for item in output.store.entries("activity", Activity)
    ):
        Journal(output.store).update(asyncio.run(node.identity()), result)
    output.finality(result or Finality(tx_hash=tx_hash, status="pending"))


@app.command("prepare-call")
def create_call(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--from")],
    contract: Annotated[str, typer.Option()],
    function: Annotated[str, typer.Option()],
    args: Annotated[Path, typer.Option(help="JSON object containing exact typed arguments.")],
    out: Annotated[Path, typer.Option()],
    chi_limit: Annotated[int | None, typer.Option(min=1, max=2**53 - 1)] = None,
    expires_in: Annotated[int, typer.Option(min=1, max=MAX_TTL)] = 300,
) -> None:
    """Simulate and prepare a typed Xian contract call."""
    output: Output = ctx.obj
    plan = asyncio.run(
        prepare_call(
            output.node(),
            resolve_address(ctx, sender),
            contract,
            function,
            read_arguments(args),
            chi_limit,
            expires_in,
        )
    )
    write_new(out, plan.model_dump_json() + "\n")
    output.plan(plan)


@app.command()
def portfolio(ctx: typer.Context, address: str) -> None:
    """Read balances for the native and watched assets on the selected network."""
    output: Output = ctx.obj
    address = resolve_address(ctx, address)
    node = output.node()

    async def balances():
        network = await node.identity()
        watched = [t for t in output.store.entries("token", Token) if t.network == network]
        assets = {"currency": "XIAN", **{item.contract: item.name for item in watched}}
        return [
            {"contract": contract, "asset": name, "amount": await node.balance(address, contract)}
            for contract, name in assets.items()
        ]

    output.emit({"address": address, "balances": asyncio.run(balances())})


@app.command()
def activity(ctx: typer.Context, account: str | None = None, refresh: bool = False) -> None:
    """List locally tracked outgoing approvals, submissions and receipts."""
    output: Output = ctx.obj
    address = resolve_address(ctx, account) if account else None
    if refresh:
        asyncio.run(refresh_activity(output.node(), output.store, address))
    rows = output.store.entries("activity", Activity)
    rows = sorted(rows, key=lambda row: row.timestamp, reverse=True)
    output.emit({"activity": [row.model_dump() for row in rows if address in (None, row.sender)]})


@app.command()
def incoming(
    ctx: typer.Context,
    address: str,
    token: str = "currency",
    limit: Annotated[int, typer.Option(min=1, max=200)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Find incoming transfers in a page of the token's indexed events."""
    contract = TypeAdapter(Contract).validate_python(token)
    account = resolve_address(ctx, address)
    ctx.obj.emit(asyncio.run(ctx.obj.node().incoming(account, contract, limit, offset)))


@app.command("prepare-payload")
def prepare_unsigned_payload(
    ctx: typer.Context,
    payload_file: Path,
    out: Annotated[Path, typer.Option()],
) -> None:
    """Review a supplied unsigned transaction without rewriting its nonce or limit."""
    payload = UnsignedPayload.model_validate(read_arguments(payload_file))
    plan = asyncio.run(prepare_payload(ctx.obj.node(), payload))
    write_new(out, plan.model_dump_json() + "\n")
    ctx.obj.plan(plan)


@app.command()
def call(
    ctx: typer.Context,
    contract: str,
    function: str,
    args: Annotated[Path, typer.Option()],
    sender: Annotated[str | None, typer.Option("--from")] = None,
) -> None:
    """Read a contract result without signing or broadcasting."""
    from xian_runtime_types.encoding import encode_wire

    from .domain import Function

    node = ctx.obj.node()
    address = resolve_address(ctx, sender) if sender else None
    contract = TypeAdapter(Contract).validate_python(contract)
    function = TypeAdapter(Function).validate_python(function)
    result = asyncio.run(node.call(contract, function, read_arguments(args), sender=address))
    ctx.obj.emit({"result": json.loads(encode_wire(result))})
