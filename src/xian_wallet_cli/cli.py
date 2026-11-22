"""Typer entry point for people and automation."""

import asyncio
import json
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

from .chain import Finality, Rpc, now, prepare_transfer, submit_transfer
from .domain import MAX_TTL, Approval, Digest, Plan, WalletError, sign_plan
from .files import load_document, read_key, write_new
from .presentation import ReviewApp, preview_data, render_preview

SCHEMA = "xian-wallet-cli/v1"
Wait = Annotated[float, typer.Option(min=0.1, max=120)]


@dataclass(frozen=True)
class Output:
    as_json: bool
    rpc_url: str

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

    def plan(self, plan: Plan) -> None:
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
        except OSError as exc:
            ctx.obj.fail("io_error", str(exc))


app = typer.Typer(
    cls=WalletGroup,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help="Prepare, review and send Xian localnet transfers.",
)


@app.callback()
def main(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON on stdout.")] = False,
    rpc: Annotated[str, typer.Option(envvar="XIAN_WALLET_RPC")] = "http://127.0.0.1:26657",
) -> None:
    ctx.obj = Output(as_json, rpc)


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
def balance(ctx: typer.Context, address: str) -> None:
    """Read an address's native XIAN balance."""
    output: Output = ctx.obj
    address = address.strip().lower()
    amount = asyncio.run(Rpc(output.rpc_url).balance(address))
    output.emit({"address": address, "amount": amount, "asset": "XIAN"})


@app.command()
def prepare(
    ctx: typer.Context,
    sender: Annotated[str, typer.Option("--from", help="Sender's public address.")],
    recipient: Annotated[str, typer.Option("--to", help="Recipient's public address.")],
    amount: Annotated[str, typer.Option(help="Positive, exact decimal amount in XIAN.")],
    out: Annotated[Path, typer.Option(help="New proposal JSON file.")],
    chi_limit: Annotated[int | None, typer.Option(min=1, max=2**53 - 1)] = None,
    expires_in: Annotated[int, typer.Option(min=1, max=MAX_TTL)] = 300,
) -> None:
    """Simulate a transfer and write an unsigned proposal."""
    output: Output = ctx.obj
    node = Rpc(output.rpc_url)
    plan = asyncio.run(prepare_transfer(node, sender, recipient, amount, chi_limit, expires_in))
    write_new(out, plan.model_dump_json(indent=2) + "\n")
    output.plan(plan)


@app.command()
def preview(ctx: typer.Context, proposal: Path) -> None:
    """Review a proposal without connecting to a node."""
    ctx.obj.plan(load_document(proposal, Plan))


def require_terminal(output: Output) -> None:
    if output.as_json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise WalletError("terminal_required", "This action requires an interactive terminal.")


@app.command()
def ui(ctx: typer.Context, proposal: Path) -> None:
    """Open the keyboard-driven transaction review."""
    require_terminal(ctx.obj)
    ReviewApp(load_document(proposal, Plan)).run()


@app.command()
def approve(
    ctx: typer.Context,
    proposal: Path,
    key_file: Annotated[Path, typer.Option(help="Owner-only sender private-key file.")],
    out: Annotated[Path, typer.Option(help="New signed approval JSON file.")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Use only after authenticated approval.")
    ] = False,
    textual: Annotated[bool, typer.Option("--textual", help="Use the full-screen review.")] = False,
) -> None:
    """Review and sign one proposal on the wallet owner's machine."""
    output: Output = ctx.obj
    plan = load_document(proposal, Plan)
    plan.require_unexpired(now())
    if yes and textual:
        raise WalletError("invalid_options", "Choose either --yes or --textual.")
    if not yes:
        require_terminal(output)
        if textual:
            accepted = ReviewApp(plan, allow_sign=True).run()
        else:
            output.plan(plan)
            accepted = typer.confirm("Approve and sign this transfer?")
        if not accepted:
            raise WalletError("approval_cancelled", "Transfer approval cancelled.")
    proof = sign_plan(plan, read_key(key_file), now())
    write_new(out, proof.model_dump_json(indent=2) + "\n")
    output.emit(
        {"proposal_id": proof.proposal_id, "tx_hash": proof.tx_hash, "approval_file": str(out)}
    )


@app.command()
def submit(
    ctx: typer.Context,
    proposal: Path,
    approval: Annotated[Path, typer.Option(help="Signed approval for this proposal.")],
    wait: Wait = 12,
) -> None:
    """Broadcast the approved transfer and report its outcome by hash."""
    output: Output = ctx.obj
    plan = load_document(proposal, Plan)
    proof = load_document(approval, Approval)
    node = Rpc(output.rpc_url)
    output.finality(asyncio.run(submit_transfer(node, plan, proof, wait)))


@app.command()
def receipt(ctx: typer.Context, tx_hash: str, wait: Wait = 12) -> None:
    """Look up a transfer without broadcasting or signing."""
    output: Output = ctx.obj
    tx_hash = TypeAdapter(Digest).validate_python(tx_hash.strip().upper())
    result = asyncio.run(Rpc(output.rpc_url).wait(tx_hash, wait))
    output.finality(result or Finality(tx_hash=tx_hash, status="pending"))
