"""Review, sign and relay native Xian x402 exact GET payments."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import typer
from aiohttp import ClientError, ClientSession, ClientTimeout
from pydantic import field_validator
from xian_py.transaction import canonical_json
from xian_py.x402 import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    XianX402PaymentExpectation,
    XianX402PaymentPayload,
    XianX402PaymentRequirement,
    decode_json_header,
    sign_xian_x402_payment,
    verify_xian_x402_payment,
)
from yarl import URL

from .accounts_cli import PasswordFile, password_for
from .chain import now
from .chain_apps import owner_review
from .domain import Address, Contract, Document, Network, WalletError, exact_amount
from .files import load_document, write_new, write_new_bytes
from .profiles import resolve_address
from .vault import AccountBackup

payments = typer.Typer(no_args_is_help=True, help="Approve and relay exact x402 GET payments.")


class Offer(Document):
    version: Literal["xian-wallet-x402-v1"] = "xian-wallet-x402-v1"
    network: Network
    url: str
    asset: Contract
    amount: str
    pay_to: Address
    settlement: Contract
    sender: Address
    expires_at: int

    @field_validator("amount")
    @classmethod
    def canonical_amount(cls, value: str) -> str:
        return exact_amount(value)

    @property
    def offer_id(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump()).encode()).hexdigest().upper()

    @property
    def requirements(self) -> XianX402PaymentRequirement:
        return XianX402PaymentRequirement(
            network=f"xian:{self.network.chain_id}",
            asset=self.asset,
            amount=self.amount,
            pay_to=self.pay_to,
            resource=self.url,
            settlement_contract=self.settlement,
        )


def payment_url(raw: str) -> URL:
    if len(raw) > 2048 or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        raise WalletError(
            "invalid_url", "Use a resource URL without whitespace or control characters."
        )
    try:
        parsed = urlsplit(raw)
        if (
            parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and (
                parsed.scheme == "https"
                or (
                    parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                )
            )
        ):
            return URL(raw, encoded=True)
    except ValueError as exc:
        raise WalletError(
            "invalid_url", "Use an HTTPS resource URL or a loopback HTTP URL."
        ) from exc
    raise WalletError("invalid_url", "Use an HTTPS resource URL or a loopback HTTP URL.")


async def read_response(url: URL, *, headers=None, max_bytes: int) -> tuple[int, dict, bytes]:
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                data = bytearray()
                async for chunk in response.content.iter_chunked(65_536):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise WalletError(
                            "response_too_large", "The resource response exceeds the size limit."
                        )
                return response.status, response.headers, bytes(data)
    except (ClientError, TimeoutError) as exc:
        raise WalletError("resource_unavailable", "Could not read the x402 resource.") from exc


@payments.command("quote")
def quote(
    ctx: typer.Context,
    url: str,
    sender: Annotated[str, typer.Option("--from")],
    to: Annotated[str, typer.Option("--to")],
    asset: Annotated[str, typer.Option()],
    max_amount: Annotated[str, typer.Option()],
    settlement: Annotated[str, typer.Option()],
    out: Annotated[Path, typer.Option()],
) -> None:
    """Fetch an HTTP 402 challenge within explicit payment terms."""
    address = resolve_address(ctx, sender)
    pay_to = resolve_address(ctx, to, "contact")
    resource = payment_url(url)
    node = ctx.obj.node()
    network = asyncio.run(node.identity())
    status, headers, body = asyncio.run(read_response(resource, max_bytes=65_536))
    if status != 402:
        raise WalletError("not_payment_required", f"The resource returned HTTP {status}.")
    try:
        header = headers.get(PAYMENT_REQUIRED_HEADER)
        challenge = decode_json_header(header) if header else json.loads(body)
        if not isinstance(challenge, dict):
            raise ValueError("malformed payment challenge")
        expected = XianX402PaymentExpectation(
            network.chain_id, asset, settlement, max_amount, pay_to
        )
        requirement = expected.select(challenge)
    except (ValueError, TypeError, KeyError) as exc:
        raise WalletError(
            "invalid_payment_offer", "The server offered no payment matching the stated terms."
        ) from exc
    if requirement.resource != str(resource) or requirement.x402_version != 2:
        raise WalletError(
            "resource_mismatch", "The payment is for a different resource or protocol version."
        )
    offer = Offer(
        network=network,
        url=str(resource),
        asset=requirement.asset,
        amount=str(requirement.amount),
        pay_to=pay_to,
        settlement=settlement,
        sender=address,
        expires_at=now() + 300,
    )
    write_new(out, offer.model_dump_json(indent=2) + "\n")
    ctx.obj.emit({**offer.model_dump(), "offer_id": offer.offer_id})


def sign_offer(store, offer: Offer, wallet, permit_nonce: int) -> XianX402PaymentPayload:
    if offer.expires_at <= now() or offer.expires_at > now() + 300:
        raise WalletError("offer_expired", "The payment offer is outside its approval window.")
    if wallet.public_key != offer.sender:
        raise WalletError("wrong_signer", "The signing key does not match the payer.")
    key = f"{offer.network.chain_id}/{offer.network.first_block_hash}/{offer.sender}/{permit_nonce}"
    payment_id = "pay_" + offer.offer_id.lower()[:32]
    with store.transaction(write=True) as db:
        saved = db.execute(
            "SELECT data FROM records WHERE namespace='x402-permit' AND name=?", (key,)
        ).fetchone()
        if saved:
            old = XianX402PaymentPayload.from_dict(json.loads(saved[0]))
            if old.payment_id != payment_id:
                raise WalletError(
                    "permit_already_signed", "Another payment was approved with this permit nonce."
                )
            return old
        result = sign_xian_x402_payment(
            offer.requirements,
            wallet,
            payment_id=payment_id,
            permit_nonce=permit_nonce,
            deadline=datetime.fromtimestamp(offer.expires_at, UTC),
        )
        db.execute(
            "INSERT INTO records VALUES ('x402-permit', ?, ?)", (key, json.dumps(result.to_dict()))
        )
        return result


@payments.command()
def approve(
    ctx: typer.Context,
    offer_file: Path,
    account: Annotated[str, typer.Option()],
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
    yes: bool = False,
) -> None:
    """Sign exactly one reviewed resource, amount and permit."""
    offer = load_document(offer_file, Offer)
    payment_url(offer.url)
    owner_review(
        ctx, {**offer.model_dump(), "offer_id": offer.offer_id}, yes, "Approve this x402 payment?"
    )
    node = ctx.obj.node()
    if asyncio.run(node.identity()) != offer.network:
        raise WalletError("network_mismatch", "The payment is for a different network.")
    nonce = asyncio.run(node.state("permit_authorizer", "nonces", offer.sender)) or 0
    entry = ctx.obj.store.read("account", account, AccountBackup)
    wallet = entry.unlock(password_for(ctx, password_file))
    result = sign_offer(ctx.obj.store, offer, wallet, nonce)
    write_new(out, json.dumps(result.to_dict(), indent=2) + "\n")
    ctx.obj.emit(
        {"offer_id": offer.offer_id, "payment_id": result.payment_id, "approval_file": str(out)}
    )


@payments.command()
def submit(
    ctx: typer.Context,
    offer_file: Path,
    approval_file: Path,
    out: Annotated[Path, typer.Option()],
) -> None:
    """Relay the signed payment and save the resource without the payer's key."""
    offer = load_document(offer_file, Offer)
    url = payment_url(offer.url)
    if out.exists() or out.is_symlink():
        raise WalletError("file_exists", "The resource output already exists.")
    try:
        with approval_file.open("rb") as stream:
            approved = XianX402PaymentPayload.from_dict(json.loads(stream.read(16_385)))
        checked = verify_xian_x402_payment(approved, offer.requirements)
    except (ValueError, TypeError, KeyError) as exc:
        raise WalletError("invalid_payment", "Could not verify the x402 approval file.") from exc
    if checked.valid is not True or approved.payer != offer.sender:
        raise WalletError("invalid_payment", checked.error or "The payment payer does not match.")
    if approved.payment_id != "pay_" + offer.offer_id.lower()[:32]:
        raise WalletError("invalid_payment", "The signed payment ID does not match the offer.")
    status, headers, body = asyncio.run(
        read_response(
            url,
            headers={PAYMENT_SIGNATURE_HEADER: approved.to_header()},
            max_bytes=16 * 1024 * 1024,
        )
    )
    if not 200 <= status < 300:
        raise WalletError(
            "payment_unconfirmed", f"HTTP {status}. Retry the same signed payment ID."
        )
    write_new_bytes(out, body)
    ctx.obj.emit(
        {
            "http_status": status,
            "payment_id": approved.payment_id,
            "settlement": settlement_response(headers, approved.payment_id),
            "output": str(out),
        }
    )


def settlement_response(headers, payment_id: str) -> dict | None:
    header = headers.get(PAYMENT_RESPONSE_HEADER)
    if not header:
        return None
    try:
        data = decode_json_header(header)
    except ValueError, TypeError:
        return None
    if data.get("paymentId") != payment_id:
        return None
    return {"success": data.get("success") is True, "tx_hash": data.get("transaction")}
