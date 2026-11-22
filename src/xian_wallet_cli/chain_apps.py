"""Reviewed Xian swaps, allowances, NFT transfers and message signatures."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter
from xian_py.wallet import verify_msg

from .accounts_cli import PasswordFile, password_for
from .chain import prepare_call
from .domain import Address, Contract, WalletError, exact_amount
from .files import write_new
from .permissions import decimal_text, units
from .profiles import resolve_address
from .vault import AccountBackup

dex = typer.Typer(no_args_is_help=True, help="Quote and prepare bounded single-pair DEX swaps.")
nft = typer.Typer(no_args_is_help=True, help="Inspect and transfer XSC-0005 NFTs.")
message = typer.Typer(no_args_is_help=True, help="Sign or verify a chain-bound Xian message.")

ROUTER = "con_dex"
PAIRS = "con_pairs"


def save_proposal(ctx, plan, out):
    write_new(out, plan.model_dump_json() + "\n")
    ctx.obj.plan(plan)


async def swap_quote(node, token_in, token_out, amount, sender):
    for contract in (token_in, token_out):
        TypeAdapter(Contract).validate_python(contract)
    if token_in == token_out:
        raise WalletError("invalid_pair", "Use two different token contracts.")
    amount = exact_amount(amount)
    pair = await node.call(PAIRS, "pairFor", {"tokenA": token_in, "tokenB": token_out})
    if not isinstance(pair, int) or isinstance(pair, bool) or pair <= 0:
        raise WalletError("pair_not_found", "No DEX pair exists for these tokens.")
    for contract in (token_in, token_out):
        if await node.state(ROUTER, "fee_on_transfer_tokens", contract):
            raise WalletError(
                "unsupported_pair", "This swap flow requires standard transfer tokens."
            )
    quote = await node.call(
        ROUTER,
        "getAmountsOut",
        {"amountIn": {"__fixed__": amount}, "src": token_in, "path": [pair]},
        sender=sender,
    )
    actual_in, amount_out = map(str, quote)
    if units(actual_in) != units(amount):
        raise WalletError(
            "token_precision", f"The input token normalizes this amount to {actual_in}."
        )
    return {
        "pair": pair,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount,
        "quoted_output": exact_amount(amount_out),
    }


def minimum_output(quoted: str, slippage_bps: int) -> str:
    floor = units(quoted) * (10_000 - slippage_bps) // 10_000
    if floor <= 0:
        raise WalletError("invalid_quote", "A swap must require a positive minimum output.")
    return decimal_text(floor)


@dex.command()
def quote(
    ctx: typer.Context,
    token_in: str,
    token_out: str,
    amount: str,
    sender: Annotated[str, typer.Option("--from")],
) -> None:
    """Quote an exact-input direct swap for a specific account."""
    address = resolve_address(ctx, sender)
    result = asyncio.run(swap_quote(ctx.obj.node(), token_in, token_out, amount, address))
    ctx.obj.emit(result)


@dex.command("prepare")
def prepare_swap(
    ctx: typer.Context,
    token_in: str,
    token_out: str,
    amount: str,
    sender: Annotated[str, typer.Option("--from")],
    recipient: Annotated[str, typer.Option("--to")],
    slippage_bps: Annotated[int, typer.Option(min=0, max=2500)],
    out: Annotated[Path, typer.Option()],
    expires_in: Annotated[int, typer.Option(min=30, max=900)] = 300,
) -> None:
    """Prepare a direct swap after its exact allowance is on-chain."""
    node = ctx.obj.node()
    address = resolve_address(ctx, sender)
    recipient_address = resolve_address(ctx, recipient, "contact")

    async def draft():
        quoted = await swap_quote(node, token_in, token_out, amount, address)
        allowance = await node.state(token_in, "approvals", address, ROUTER)
        if units(str(allowance or 0)) < units(quoted["amount_in"]):
            raise WalletError(
                "allowance_required",
                f"Approve {quoted['amount_in']} {token_in} for {ROUTER} first.",
            )
        bound = minimum_output(quoted["quoted_output"], slippage_bps)
        deadline = datetime.fromtimestamp(datetime.now(UTC).timestamp() + expires_in, UTC)
        args = {
            "amountIn": {"__fixed__": quoted["amount_in"]},
            "amountOutMin": {"__fixed__": bound},
            "pair": quoted["pair"],
            "src": token_in,
            "to": recipient_address,
            "deadline": {
                "__time__": [
                    deadline.year,
                    deadline.month,
                    deadline.day,
                    deadline.hour,
                    deadline.minute,
                    deadline.second,
                    0,
                ]
            },
        }
        return await prepare_call(
            node, address, ROUTER, "swapExactTokenForToken", args, None, expires_in
        )

    save_proposal(ctx, asyncio.run(draft()), out)


@dex.command("allowance")
def allowance(
    ctx: typer.Context,
    token: str,
    amount: str,
    sender: Annotated[str, typer.Option("--from")],
    out: Annotated[Path, typer.Option()],
    spender: str = ROUTER,
) -> None:
    """Prepare an exact token allowance. Use 0 to revoke it."""
    to = TypeAdapter(Address if len(spender) == 64 else Contract).validate_python(spender)
    exact = "0" if units(amount) == 0 else exact_amount(amount)
    call = prepare_call(
        ctx.obj.node(),
        resolve_address(ctx, sender),
        token,
        "approve",
        {"amount": {"__fixed__": exact}, "to": to},
        None,
        300,
    )
    save_proposal(ctx, asyncio.run(call), out)


@nft.command("inspect")
def inspect_nft(ctx: typer.Context, collection: str, token_id: str) -> None:
    """Read ownership and metadata from an XSC-0005 collection."""
    collection = TypeAdapter(Contract).validate_python(collection)
    node = ctx.obj.node()

    async def inspect():
        args = {"token_id": token_id}
        owner = await node.call(collection, "owner_of", args)
        metadata = await node.call(collection, "token_metadata", args)
        return {
            "collection": collection,
            "token_id": token_id,
            "owner": owner,
            "metadata": metadata,
        }

    ctx.obj.emit(asyncio.run(inspect()))


@nft.command("transfer")
def transfer_nft(
    ctx: typer.Context,
    collection: str,
    token_id: str,
    sender: Annotated[str, typer.Option("--from")],
    recipient: Annotated[str, typer.Option("--to")],
    out: Annotated[Path, typer.Option()],
) -> None:
    """Prepare a reviewed XSC-0005 NFT transfer."""
    call = prepare_call(
        ctx.obj.node(),
        resolve_address(ctx, sender),
        collection,
        "transfer",
        {"token_id": token_id, "to": resolve_address(ctx, recipient, "contact")},
        None,
        300,
    )
    save_proposal(ctx, asyncio.run(call), out)


def message_payload(chain_id: str, address: str, content: str) -> str:
    return "\n".join(
        [
            "\x19Xian Signed Message:",
            "1",
            *(
                f"{key}:{len(value.encode())}:{value}"
                for key, value in (
                    ("chain-id", chain_id),
                    ("account", address),
                    ("message", content),
                )
            ),
        ]
    )


def load_message(path: Path) -> str:
    with path.open("rb") as stream:
        value = stream.read(16_385)
    if len(value) > 16_384:
        raise WalletError("message_too_large", "Message exceeds 16 KiB.")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WalletError("invalid_message", "Use a UTF-8 text file.") from exc


def owner_review(ctx, review: dict, yes: bool, prompt: str) -> None:
    from .cli import require_terminal

    if yes:
        return
    require_terminal(ctx.obj)
    ctx.obj.emit(review)
    if not typer.confirm(prompt):
        raise WalletError("approval_cancelled", "Signing cancelled.")


@message.command()
def sign(
    ctx: typer.Context,
    path: Path,
    account: Annotated[str, typer.Option()],
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
    yes: bool = False,
) -> None:
    """Review and sign with Xian's chain/account-bound message envelope."""
    network = asyncio.run(ctx.obj.node().identity())
    entry = ctx.obj.store.read("account", account, AccountBackup)
    value = load_message(path)
    details = {
        "chain_id": network.chain_id,
        "account": entry.account.address,
        "message_sha256": hashlib.sha256(value.encode()).hexdigest(),
    }
    owner_review(
        ctx, {**details, "message": json.dumps(value, ensure_ascii=True)}, yes, "Sign this message?"
    )
    wallet = entry.unlock(password_for(ctx, password_file))
    payload = message_payload(network.chain_id, entry.account.address, value)
    signature = wallet.sign_msg(payload)
    signed = {**details, "signature": signature}
    write_new(out, json.dumps(signed, indent=2) + "\n")
    ctx.obj.emit(signed)


@message.command()
def verify(ctx: typer.Context, path: Path, proof: Path) -> None:
    value = load_message(path)
    with proof.open("rb") as stream:
        try:
            data = json.loads(stream.read(16_385))
            payload = message_payload(data["chain_id"], data["account"], value)
            address = TypeAdapter(Address).validate_python(data["account"])
            valid = verify_msg(address, payload, data["signature"])
        except (TypeError, ValueError, KeyError) as exc:
            raise WalletError(
                "invalid_signature_file", "Use a wallet message signature file."
            ) from exc
    ctx.obj.emit({"valid": valid, "chain_id": data["chain_id"], "account": address})
    if not valid:
        raise typer.Exit(1)
