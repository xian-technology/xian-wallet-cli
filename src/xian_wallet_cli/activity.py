"""Persistent signing reservations and outgoing transaction receipts."""

import sqlite3
from typing import Literal

from .chain import Finality, now
from .domain import Address, Approval, Digest, Document, Network, Proposal, WalletError, sign_plan
from .state import Store


class Activity(Document):
    network: Network
    tx_hash: Digest
    proposal_id: Digest
    sender: Address
    contract: str
    function: str
    timestamp: int
    status: Literal["approved", "completed", "failed", "pending", "rejected"] = "approved"
    height: int | None = None
    chi_used: int | None = None


def activity_key(network: Network, tx_hash: str) -> str:
    return f"{network.chain_id}/{network.first_block_hash}/{tx_hash}"


def nonce_key(plan: Proposal) -> str:
    return activity_key(plan.network, f"{plan.sender}/{plan.nonce}")


def sign_once(
    db: sqlite3.Connection, plan: Proposal, wallet, *, replaces: str | None = None
) -> Approval:
    if wallet.public_key != plan.sender:
        raise WalletError("wrong_signer", "The signing key does not match the sender.")
    row = db.execute(
        "SELECT data FROM records WHERE namespace='signed-nonce' AND name=?", (nonce_key(plan),)
    ).fetchone()
    prior = Approval.model_validate_json(row[0]) if row else None
    if prior and prior.proposal_id == plan.proposal_id:
        plan.require_unexpired(now())
        return prior
    if prior and replaces is None:
        raise WalletError(
            "nonce_already_signed", "This account already signed another proposal for that nonce."
        )
    if replaces is not None and (prior is None or prior.tx_hash != replaces):
        raise WalletError(
            "replacement_mismatch", "The nonce is not reserved by the specified hash."
        )
    signed = sign_plan(plan, wallet, now())
    db.execute(
        "INSERT OR REPLACE INTO records VALUES ('signed-nonce', ?, ?)",
        (nonce_key(plan), signed.model_dump_json()),
    )
    return signed


def reserve_signing(
    store: Store, plan: Proposal, wallet, *, replaces: str | None = None
) -> Approval:
    with store.transaction(write=True) as db:
        return sign_once(db, plan, wallet, replaces=replaces)


class Journal:
    def __init__(self, store: Store):
        self.store = store

    def record(self, plan: Proposal, approval: Approval) -> None:
        entry = Activity(
            network=plan.network,
            tx_hash=approval.tx_hash,
            proposal_id=plan.proposal_id,
            sender=plan.sender,
            contract=plan.payload["contract"],
            function=plan.payload["function"],
            timestamp=now(),
        )
        with self.store.transaction(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO records VALUES ('activity', ?, ?)",
                (activity_key(plan.network, approval.tx_hash), entry.model_dump_json()),
            )

    def update(self, network: Network, receipt: Finality) -> None:
        key = activity_key(network, receipt.tx_hash)
        with self.store.transaction(write=True) as db:
            row = db.execute(
                "SELECT data FROM records WHERE namespace='activity' AND name=?", (key,)
            ).fetchone()
            if row is None:
                return
            previous = Activity.model_validate_json(row[0])
            if previous.status in ("completed", "failed"):
                return
            updated = previous.model_copy(update=receipt.model_dump(exclude={"message"}))
            db.execute(
                "UPDATE records SET data=? WHERE namespace='activity' AND name=?",
                (updated.model_dump_json(), key),
            )


async def refresh_activity(node, store: Store, address: str | None = None) -> None:
    network = await node.identity()
    journal = Journal(store)
    entries = sorted(
        store.entries("activity", Activity), key=lambda item: item.timestamp, reverse=True
    )
    open_entries = [
        row
        for row in entries
        if row.network == network
        and address in (None, row.sender)
        and row.status in ("approved", "pending", "rejected")
    ]
    for row in open_entries[:50]:
        if result := await node.lookup(row.tx_hash):
            journal.update(network, result)
