"""Durable proposal inbox and scoped, revocable transfer grants."""

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from typing import Literal

from xian_py.wallet import Wallet

from .activity import sign_once
from .chain import now
from .domain import Address, Approval, Contract, Digest, Document, Network, Proposal, WalletError
from .state import Store
from .vault import Name


class Request(Document):
    proposal: Proposal
    status: Literal["awaiting_approval", "approved", "declined"] = "awaiting_approval"
    approval: Approval | None = None


class Grant(Document):
    name: Name
    account: Name
    sender: Address
    network: Network
    recipient: Address
    contract: Contract
    tx_limit: str
    total_limit: str
    max_chi: int
    total_chi: int
    expires_at: int
    token_hash: Digest
    spent: str = "0"
    spent_chi: int = 0


def enqueue(store: Store, proposal: Proposal) -> Request:
    proposal.require_unexpired(now())
    request = Request(proposal=proposal)
    with store.transaction(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO records VALUES ('request', ?, ?)",
            (proposal.proposal_id, request.model_dump_json()),
        )
        row = db.execute(
            "SELECT data FROM records WHERE namespace='request' AND name=?", (proposal.proposal_id,)
        ).fetchone()
    return Request.model_validate_json(row[0])


def decide(store: Store, proposal_id: str, wallet: Wallet | None) -> Request:
    with store.transaction(write=True) as db:
        row = db.execute(
            "SELECT data FROM records WHERE namespace='request' AND name=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise WalletError("record_not_found", "No matching approval request.")
        request = Request.model_validate_json(row[0])
        if request.status != "awaiting_approval":
            return request
        if wallet is None:
            result = Request(proposal=request.proposal, status="declined")
        else:
            proof = sign_once(db, request.proposal, wallet)
            result = Request(proposal=request.proposal, status="approved", approval=proof)
        db.execute(
            "UPDATE records SET data=? WHERE namespace='request' AND name=?",
            (result.model_dump_json(), proposal_id),
        )
        return result


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest().upper()


def mint_capability() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def units(amount: str) -> int:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,63})(?:\.[0-9]{1,30})?", amount):
        raise WalletError("invalid_amount", "Use a plain decimal with up to 30 fractional digits.")
    whole, _, fraction = amount.partition(".")
    return int(whole) * 10**30 + int(fraction.ljust(30, "0"))


def decimal_text(value: int) -> str:
    whole, fraction = divmod(value, 10**30)
    return f"{whole}.{fraction:030d}".rstrip("0").rstrip(".") if fraction else str(whole)


def transfer_amount(grant: Grant, proposal: Proposal) -> int:
    payload = proposal.payload
    kwargs = payload["kwargs"]
    if (
        proposal.network != grant.network
        or proposal.sender != grant.sender
        or payload["contract"] != grant.contract
        or payload["function"] != "transfer"
        or set(kwargs) != {"amount", "to"}
        or kwargs["to"] != grant.recipient
    ):
        raise WalletError("grant_scope", "The proposal is outside the permitted transfer scope.")
    amount = kwargs["amount"]
    text = amount.get("__fixed__") if isinstance(amount, dict) else str(amount)
    if not isinstance(text, str) or (value := units(text)) <= 0:
        raise WalletError("grant_scope", "The transfer amount is outside the permitted scope.")
    return value


def enforce_budget(grant: Grant, proposal: Proposal, amount: int) -> Grant:
    spent, spent_chi = units(grant.spent) + amount, grant.spent_chi + proposal.chi_limit
    if amount > units(grant.tx_limit) or spent > units(grant.total_limit):
        raise WalletError("grant_budget", "The transfer exceeds the grant's spending budget.")
    if proposal.chi_limit > grant.max_chi or spent_chi > grant.total_chi:
        raise WalletError("grant_budget", "The transfer exceeds the grant's chi budget.")
    return grant.model_copy(update={"spent": decimal_text(spent), "spent_chi": spent_chi})


def authorize(
    store: Store,
    name: str,
    token: str,
    proposal: Proposal,
    unlock: Callable[[str], Wallet],
) -> Approval:
    # Unlock before taking the write lock; read the grant again inside the transaction.
    grant = store.read("grant", name, Grant)
    validate_grant(grant, token)
    wallet = unlock(grant.account)
    with store.transaction(write=True) as db:
        return authorize_in_transaction(db, grant, token, proposal, wallet)


def validate_grant(grant: Grant, token: str) -> None:
    if not hmac.compare_digest(grant.token_hash, token_digest(token)):
        raise WalletError("unauthorized", "The grant credential is invalid.")
    if grant.expires_at <= now():
        raise WalletError("grant_expired", "The grant has expired.")


def authorize_in_transaction(db, original: Grant, token: str, proposal: Proposal, wallet: Wallet):
    name = original.name
    row = db.execute(
        "SELECT data FROM records WHERE namespace='grant' AND name=?", (name,)
    ).fetchone()
    if row is None:
        raise WalletError("grant_revoked", "The grant has been revoked.")
    grant = Grant.model_validate_json(row[0])
    validate_grant(grant, token)
    proposal.require_unexpired(now())
    amount = transfer_amount(grant, proposal)
    key = f"{grant.token_hash}/{proposal.proposal_id}"
    used = db.execute(
        "SELECT data FROM records WHERE namespace='grant-use' AND name=?", (key,)
    ).fetchone()
    if used:
        return Approval.model_validate_json(used[0])
    updated = enforce_budget(grant, proposal, amount)
    proof = sign_once(db, proposal, wallet)
    db.execute(
        "UPDATE records SET data=? WHERE namespace='grant' AND name=?",
        (updated.model_dump_json(), name),
    )
    db.execute("INSERT INTO records VALUES ('grant-use', ?, ?)", (key, proof.model_dump_json()))
    return proof
