"""Xian RPC and the prepare, submit, and receipt workflow."""

from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import TypeAdapter
from xian_py import chi_to_supply
from xian_py.exception import TransportError, TxNotFoundError, TxTimeoutError
from xian_py.models import TransactionReceipt
from xian_py.transaction import (
    PreparedTransaction,
    broadcast_tx_wait_async,
    get_block_async,
    get_nonce_async,
    simulate_tx_async,
)
from xian_py.xian_async import XianAsync

from .domain import (
    Address,
    Approval,
    CallPlan,
    Contract,
    Digest,
    Document,
    Function,
    Network,
    Plan,
    Proposal,
    UnsignedPayload,
    WalletError,
    exact_amount,
    transfer_call,
    verify_approval,
)

_ADDRESS = TypeAdapter(Address)


class Finality(Document):
    tx_hash: Digest
    status: Literal["completed", "failed", "pending", "rejected"]
    height: int | None = None
    chi_used: int | None = None
    message: str | None = None


class TransferNetwork(Protocol):
    async def identity(self) -> Network: ...
    async def nonce(self, sender: str) -> int: ...
    async def estimate(self, payload: dict) -> int: ...
    async def lookup(self, tx_hash: str) -> Finality | None: ...
    async def broadcast(self, transaction: PreparedTransaction) -> str | None: ...
    async def wait(self, tx_hash: str, seconds: float) -> Finality | None: ...


def now() -> int:
    return int(datetime.now(UTC).timestamp())


def rpc_url(url: str, *, allow_https: bool = False) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme in ("http", "https")
            and (
                parts.hostname in ("127.0.0.1", "::1", "localhost")
                or (allow_https and parts.scheme == "https" and parts.hostname)
            )
            and parts.port != 0
            and not any((parts.username, parts.password, parts.query, parts.fragment))
            and parts.path in ("", "/")
        ):
            return url.rstrip("/")
    except ValueError as exc:
        raise WalletError("invalid_rpc", "Use a loopback node RPC URL.") from exc
    raise WalletError("invalid_rpc", "Use a loopback node RPC URL.")


def local_rpc_url(url: str) -> str:
    return rpc_url(url)


def confirmed_receipt(tx_hash: str, receipt: TransactionReceipt) -> Finality:
    height = int(receipt.raw.get("result", {}).get("height", 0))
    if receipt.tx_hash != tx_hash or height < 1:
        raise WalletError("invalid_receipt", "The node returned an inconsistent receipt.")
    return Finality(
        tx_hash=tx_hash,
        status="completed" if receipt.success else "failed",
        height=height,
        chi_used=receipt.chi_used,
        message=str(receipt.message) if receipt.message else None,
    )


class Rpc:
    def __init__(self, url: str, *, expected: Network | None = None):
        self.url = rpc_url(url, allow_https=expected is not None)
        self.expected = expected
        self.client = XianAsync(self.url)

    async def identity(self) -> Network:
        async with self.client as client:
            status = await client.get_node_status()
            if status.catching_up is not False or not status.network:
                raise WalletError("node_not_ready", "The node has not reported a synced chain.")
            block = await get_block_async(self.url, 1, session=client.session)
        result = block.get("result", {})
        if result.get("block", {}).get("header", {}).get("chain_id") != status.network:
            raise WalletError("network_unverified", "The node could not supply its first block.")
        observed = Network(chain_id=status.network, first_block_hash=result["block_id"]["hash"])
        if self.expected is not None and observed != self.expected:
            raise WalletError("network_mismatch", "RPC does not match the saved network identity.")
        return observed

    async def balance(self, address: str, contract: str = "currency") -> str:
        if self.expected is not None:
            await self.identity()
        async with self.client as client:
            return str(
                await client.get_balance(_ADDRESS.validate_python(address), contract=contract)
            )

    async def call(self, contract: str, function: str, kwargs: dict, *, sender: str | None = None):
        if self.expected is not None:
            await self.identity()
        async with self.client as client:
            if sender is None:
                return await client.call(contract, function, kwargs)
            payload = {
                "contract": contract,
                "function": function,
                "kwargs": kwargs,
                "sender": sender,
            }
            result = await simulate_tx_async(self.url, payload, session=client.session)
        if result.get("status") != 0:
            raise WalletError(
                "simulation_failed", str(result.get("result") or "Simulation failed.")
            )
        return result.get("result")

    async def state(self, contract: str, variable: str, *keys: object):
        if self.expected is not None:
            await self.identity()
        async with self.client as client:
            return await client.get_state(contract, variable, *keys)

    async def incoming(self, address: str, contract: str, limit: int, offset: int) -> dict:
        if self.expected is not None:
            await self.identity()
        async with self.client as client:
            indexer = await client.get_bds_status()
            if indexer.indexed_height is None:
                raise WalletError("indexer_unavailable", "Incoming history requires a BDS index.")
            events = await client.list_events(contract, "Transfer", limit=limit, offset=offset)
        return {
            "indexed_height": indexer.indexed_height,
            "next_offset": offset + len(events) if len(events) == limit else None,
            "incoming": [
                {
                    "tx_hash": item.tx_hash,
                    "height": item.block_height,
                    "event_id": item.id,
                    "from": item.data_indexed.get("from"),
                    "to": address,
                    "amount": str((item.data or {}).get("amount")),
                    "asset": contract,
                }
                for item in events
                if (item.data_indexed or {}).get("to") == address
            ],
        }

    async def nonce(self, sender: str) -> int:
        async with self.client as client:
            return await get_nonce_async(self.url, sender, session=client.session)

    async def estimate(self, payload: dict) -> int:
        async with self.client as client:
            result = await simulate_tx_async(self.url, payload, session=client.session)
        if result.get("status") != 0:
            raise WalletError(
                "simulation_failed", str(result.get("result") or "Simulation failed.")
            )
        return int(result["chi_used"])

    async def lookup(self, tx_hash: str) -> Finality | None:
        async with self.client as client:
            try:
                receipt = await client.get_tx(tx_hash)
            except TxNotFoundError:
                return None
        return confirmed_receipt(tx_hash, receipt)

    async def broadcast(self, transaction: PreparedTransaction) -> str | None:
        async with self.client as client:
            response = await broadcast_tx_wait_async(self.url, transaction, session=client.session)
        result = response.get("result", {})
        if result.get("code", 0) != 0:
            return str(result.get("log") or "The node rejected the transaction at admission.")
        return None

    async def wait(self, tx_hash: str, seconds: float) -> Finality | None:
        if self.expected is not None:
            await self.identity()
        async with self.client as client:
            try:
                receipt = await client.wait_for_tx(tx_hash, timeout_seconds=seconds)
            except TransportError, TxTimeoutError, TxNotFoundError:
                return None
        return confirmed_receipt(tx_hash, receipt)


async def prepare_transfer(
    rpc: TransferNetwork, sender: str, recipient: str, amount: str, chi_limit: int | None, ttl: int
) -> Plan:
    sender = _ADDRESS.validate_python(sender.strip().lower())
    recipient = _ADDRESS.validate_python(recipient.strip().lower())
    amount = exact_amount(amount.strip())
    network = await rpc.identity()
    nonce = await rpc.nonce(sender)
    call = transfer_call(sender, recipient, amount, nonce)
    limits = await estimate_limits(rpc, call, chi_limit, ttl)
    return Plan(
        network=network, sender=sender, recipient=recipient, amount=amount, nonce=nonce, **limits
    )


async def prepare_call(
    rpc: TransferNetwork,
    sender: str,
    contract: str,
    function: str,
    kwargs: dict,
    chi_limit: int | None,
    ttl: int,
) -> CallPlan:
    sender = _ADDRESS.validate_python(sender.strip().lower())
    contract = TypeAdapter(Contract).validate_python(contract)
    function = TypeAdapter(Function).validate_python(function)
    kwargs = CallPlan.validate_kwargs(kwargs)
    network = await rpc.identity()
    nonce = await rpc.nonce(sender)
    call = dict(sender=sender, nonce=nonce, contract=contract, function=function, kwargs=kwargs)
    return CallPlan(network=network, **call, **await estimate_limits(rpc, call, chi_limit, ttl))


async def prepare_payload(rpc: TransferNetwork, payload: UnsignedPayload) -> CallPlan:
    network = await rpc.identity()
    if payload.chain_id != network.chain_id:
        raise WalletError("network_mismatch", "The unsigned transaction is for a different chain.")
    nonce = await rpc.nonce(payload.sender)
    if nonce != payload.nonce:
        raise WalletError(
            "nonce_changed", "The unsigned transaction's nonce is no longer available."
        )
    call = payload.model_dump(exclude={"chain_id", "chi_supplied"})
    limits = await estimate_limits(rpc, call, payload.chi_supplied, 300)
    return CallPlan(network=network, **call, **limits)


async def estimate_limits(
    rpc: TransferNetwork, call: dict, chi_limit: int | None, ttl: int
) -> dict:
    estimate = await rpc.estimate(call)
    if chi_limit is not None and chi_limit < estimate:
        raise WalletError("chi_limit_too_low", f"Simulation requires {estimate} chi.")
    created_at = now()
    return {
        "chi_estimate": estimate,
        "chi_limit": chi_to_supply(estimate) if chi_limit is None else chi_limit,
        "created_at": created_at,
        "expires_at": created_at + ttl,
    }


async def submit_transfer(
    rpc: TransferNetwork, plan: Proposal, approval: Approval, wait_seconds: float
) -> Finality:
    transaction = verify_approval(plan, approval)
    if await rpc.identity() != plan.network:
        raise WalletError(
            "network_mismatch", "The RPC is on a different network than the proposal."
        )
    tx_hash = transaction.tx_hash
    current_receipt = await rpc.lookup(tx_hash)
    if current_receipt is not None:
        return current_receipt
    plan.require_unexpired(now())
    try:
        rejection = await rpc.broadcast(transaction)
    except TimeoutError, TransportError:
        rejection = None
    receipt = await rpc.wait(tx_hash, wait_seconds)
    if receipt is not None:
        return receipt
    if rejection:
        return Finality(tx_hash=tx_hash, status="rejected", message=rejection)
    return Finality(
        tx_hash=tx_hash,
        status="pending",
        message="No final receipt yet. Look up this hash before creating a new proposal.",
    )
