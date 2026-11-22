"""Versioned approval documents and their exact Xian transaction."""

import hashlib
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from xian_py.transaction import (
    PreparedTransaction,
    canonical_json,
    create_tx,
    normalize_transaction_payload,
    prepare_transaction,
)
from xian_py.wallet import Wallet, verify_msg

Address = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[A-F0-9]{64}$")]
Signature = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{128}$")]
UInt = Annotated[int, Field(ge=0, le=2**53 - 1)]
Timestamp = Annotated[int, Field(ge=0, le=253_402_300_799)]
MAX_TTL = 3600


class WalletError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Network(Document):
    chain_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$")]
    first_block_hash: Digest


def exact_amount(value: str) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,63})(?:\.[0-9]{1,30})?", value):
        raise WalletError("invalid_amount", "Use a plain decimal with up to 30 fractional digits.")
    amount = value.rstrip("0").rstrip(".") if "." in value else value
    if amount == "0":
        raise WalletError("invalid_amount", "Transfer amount must be greater than zero.")
    return amount


def transfer_call(sender: str, recipient: str, amount: str, nonce: int) -> dict:
    return {
        "sender": sender,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": {"__fixed__": amount}, "to": recipient},
        "nonce": nonce,
    }


class Plan(Document):
    version: Literal["xian-wallet-transfer-v1"] = "xian-wallet-transfer-v1"
    network: Network
    sender: Address
    recipient: Address
    amount: str
    nonce: UInt
    chi_estimate: UInt
    chi_limit: UInt
    created_at: Timestamp
    expires_at: Timestamp

    @field_validator("amount")
    @classmethod
    def canonical_amount(cls, value: str) -> str:
        if exact_amount(value) != value:
            raise ValueError("amount must be in canonical decimal form")
        return value

    @model_validator(mode="after")
    def check_limits(self):
        if self.chi_limit < max(1, self.chi_estimate):
            raise ValueError("chi limit must cover the simulated cost")
        if not 0 < self.expires_at - self.created_at <= MAX_TTL:
            raise ValueError("invalid proposal lifetime")
        return self

    @property
    def proposal_id(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump()).encode()).hexdigest().upper()

    @property
    def payload(self) -> dict:
        return normalize_transaction_payload(
            {
                **transfer_call(self.sender, self.recipient, self.amount, self.nonce),
                "chain_id": self.network.chain_id,
                "chi_supplied": self.chi_limit,
            }
        )

    def require_unexpired(self, now: int) -> None:
        if now >= self.expires_at:
            raise WalletError("proposal_expired", "This proposal has expired.")

    @property
    def approval_message(self) -> str:
        return f"Xian wallet transfer approval v1\n{self.proposal_id}"


class Approval(Document):
    version: Literal["xian-wallet-approval-v1"] = "xian-wallet-approval-v1"
    proposal_id: Digest
    approval_signature: Signature
    transaction_signature: Signature
    tx_hash: Digest


def sign_plan(plan: Plan, wallet: Wallet, now: int) -> Approval:
    plan.require_unexpired(now)
    if wallet.public_key != plan.sender:
        raise WalletError("wrong_signer", "The signing key does not match the sender.")
    transaction = create_tx(plan.payload, wallet)
    return Approval(
        proposal_id=plan.proposal_id,
        approval_signature=wallet.sign_msg(plan.approval_message),
        transaction_signature=transaction["metadata"]["signature"],
        tx_hash=prepare_transaction(transaction).tx_hash,
    )


def verify_approval(plan: Plan, approval: Approval) -> PreparedTransaction:
    if approval.proposal_id != plan.proposal_id:
        raise WalletError("approval_mismatch", "The approval covers a different proposal.")
    payload = plan.payload
    if not verify_msg(plan.sender, plan.approval_message, approval.approval_signature):
        raise WalletError("invalid_approval", "The proposal approval signature is invalid.")
    if not verify_msg(plan.sender, canonical_json(payload), approval.transaction_signature):
        raise WalletError("invalid_approval", "The transaction signature is invalid.")
    prepared = prepare_transaction(
        {
            "payload": payload,
            "metadata": {"signature": approval.transaction_signature},
        }
    )
    if prepared.tx_hash != approval.tx_hash:
        raise WalletError("invalid_approval", "The transaction hash does not match the approval.")
    return prepared
