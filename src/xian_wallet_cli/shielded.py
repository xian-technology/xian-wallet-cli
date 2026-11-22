"""Encrypted shielded snapshots and local SDK proof generation."""

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import typer
from nacl import pwhash, utils
from nacl.exceptions import CryptoError
from pydantic import StringConstraints

from .accounts_cli import PasswordFile, password_for
from .chain import prepare_call
from .chain_apps import owner_review, save_proposal
from .domain import Contract, Document, Network, WalletError
from .files import load_document, write_new
from .profiles import resolve_address
from .vault import account_box, secret_file

shielded = typer.Typer(
    no_args_is_help=True, help="Manage encrypted shielded notes and prepare local proofs."
)


class ShieldedScope(Document):
    version: Literal["xian-wallet-shielded-v1"] = "xian-wallet-shielded-v1"
    contract: Contract
    network: Network


class Snapshot(Document):
    scope: ShieldedScope
    salt: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    ciphertext: str


class Recipient(Document):
    contract: Contract
    network: Network
    owner_public: str
    viewing_public_key: str


def toolkit():
    try:
        import xian_zk
    except ImportError as exc:
        raise WalletError("shielded_unavailable", "Install the wallet's shielded extra.") from exc
    return xian_zk


def read_snapshot(path: Path, password: str):
    with path.open("rb") as stream:
        data = stream.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise WalletError("snapshot_too_large", "The shielded snapshot exceeds 8 MiB.")
    snapshot = Snapshot.model_validate_json(data)
    try:
        box = account_box(password, bytes.fromhex(snapshot.salt))
        plaintext = box.decrypt(
            bytes.fromhex(snapshot.ciphertext), snapshot.scope.model_dump_json().encode()
        )
        wallet = toolkit().ShieldedWallet.from_json(plaintext.decode())
    except (CryptoError, ValueError, KeyError) as exc:
        raise WalletError("vault_locked", "The password or shielded snapshot is invalid.") from exc
    if wallet.asset_id != toolkit().asset_id_for_contract(snapshot.scope.contract):
        raise WalletError("asset_mismatch", "The shielded snapshot belongs to a different asset.")
    return snapshot.scope, wallet


def write_snapshot(path: Path, scope: ShieldedScope, wallet, password: str) -> None:
    if len(password) < 12:
        raise WalletError("weak_password", "Use a vault password of at least 12 characters.")
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    ciphertext = account_box(password, salt).encrypt(
        wallet.to_json().encode(), scope.model_dump_json().encode()
    )
    snapshot = Snapshot(scope=scope, salt=salt.hex(), ciphertext=bytes(ciphertext).hex())
    data = snapshot.model_dump_json()
    if len(data) > 8 * 1024 * 1024:
        raise WalletError("snapshot_too_large", "The shielded snapshot exceeds 8 MiB.")
    write_new(path, data + "\n")


def receive_data(scope: ShieldedScope, wallet) -> Recipient:
    return Recipient(
        contract=scope.contract,
        network=scope.network,
        owner_public=wallet.owner_public,
        viewing_public_key=wallet.viewing_public_key,
    )


@shielded.command()
def create(
    ctx: typer.Context,
    contract: str,
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
    seed_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Create or recover a shielded account and write an encrypted snapshot."""
    zk = toolkit()
    scope = ShieldedScope(contract=contract, network=asyncio.run(ctx.obj.node().identity()))
    asset = zk.asset_id_for_contract(contract)
    if seed_file:
        wallet = zk.ShieldedWallet.from_seed_json(secret_file(seed_file, "shielded seed"))
    else:
        wallet = zk.ShieldedWallet.generate(asset)
    if wallet.asset_id != asset:
        raise WalletError("asset_mismatch", "The shielded seed belongs to a different asset.")
    write_snapshot(out, scope, wallet, password_for(ctx, password_file, confirm=True))
    ctx.obj.emit(receive_data(scope, wallet).model_dump())


@shielded.command()
def receive(
    ctx: typer.Context,
    snapshot: Path,
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
) -> None:
    """Export the public receiving keys for this shielded account."""
    scope, wallet = read_snapshot(snapshot, password_for(ctx, password_file))
    data = receive_data(scope, wallet)
    write_new(out, data.model_dump_json(indent=2) + "\n")
    ctx.obj.emit(data.model_dump())


@shielded.command("export-recovery")
def export_recovery(
    ctx: typer.Context,
    snapshot: Path,
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
) -> None:
    """Write a private shielded recovery seed to an owner-only file."""
    scope, wallet = read_snapshot(snapshot, password_for(ctx, password_file))
    write_new(out, wallet.export_seed_json() + "\n")
    ctx.obj.emit({"contract": scope.contract, "recovery_file": str(out)})


async def check_scope(node, scope: ShieldedScope) -> None:
    if await node.identity() != scope.network:
        raise WalletError("network_mismatch", "The shielded wallet is for a different network.")


async def index_records(node, scope: ShieldedScope, wallet) -> None:
    """Use contract-scoped BDS events and get payloads only from matching transaction outputs."""
    await check_scope(node, scope)
    async with node.client as client:
        status = await client.get_bds_status()
        if status.indexed_height is None:
            raise WalletError("indexer_unavailable", "Shielded sync needs a BDS index.")
        await sync_pages(client, scope.contract, wallet)
        spent = []
        for note in wallet.spendable_notes():
            if await client.call(
                scope.contract, "is_nullifier_spent", {"nullifier": note.nullifier}
            ):
                spent.append(note.nullifier)
        wallet.apply_spent_nullifiers(spent)


async def sync_pages(client, contract: str, wallet) -> None:
    page_size = 100
    while True:
        events = await client.list_events(
            contract,
            "ShieldedOutputsCommitted",
            limit=page_size,
            after_id=wallet.last_output_event_id or 0,
        )
        if not events:
            return
        for event in events:
            if event.id is None or event.id <= (wallet.last_output_event_id or 0):
                raise WalletError(
                    "invalid_shielded_index", "The shielded event cursor did not advance."
                )
            data = event.data or {}
            commitments = data["commitments_blob"].split("|")
            start = int(data["note_index_start"])
            receipt = await client.get_tx(event.tx_hash)
            call = (receipt.transaction or {}).get("payload", {}).get("kwargs", {})
            payloads = dict(
                zip(call.get("output_commitments", []), call.get("output_payloads") or [])
            )
            wallet.sync_records(
                [
                    {
                        "index": start + index,
                        "commitment": commitment,
                        "payload": payloads.get(commitment),
                    }
                    for index, commitment in enumerate(commitments)
                    if commitment
                ]
            )
            wallet.last_output_event_id = event.id
        if len(events) < page_size:
            return


@shielded.command()
def sync(
    ctx: typer.Context,
    snapshot: Path,
    out: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
) -> None:
    """Update note history and save a fresh encrypted snapshot."""
    password = password_for(ctx, password_file)
    scope, wallet = read_snapshot(snapshot, password)
    asyncio.run(index_records(ctx.obj.node(), scope, wallet))
    write_snapshot(out, scope, wallet, password)
    ctx.obj.emit(
        {
            "contract": scope.contract,
            "amount": str(sum(note.amount for note in wallet.spendable_notes())),
            "scanned_outputs": wallet.last_scanned_index,
            "snapshot": str(out),
        }
    )


def build_proof_call(zk, wallet, scope, action, amount, recipient, root_data, bundle):
    if (
        wallet.current_root() != root_data["root"]
        or len(wallet.commitments()) != root_data["note_count"]
    ):
        raise WalletError(
            "shielded_not_synced", "Sync the shielded wallet to the node's current note root."
        )
    prover = zk.ShieldedNoteProver(bundle.read_text(encoding="utf-8"))
    if prover.bundle["contract_name"] != scope.contract:
        raise WalletError("bundle_mismatch", "The proving bundle is for a different contract.")
    plan, proof = prove_operation(prover, wallet, scope, action, amount, recipient, zk)
    args = {
        "old_root": proof.old_root,
        "proof_hex": proof.proof_hex,
        "output_commitments": proof.output_commitments,
        "output_payloads": plan.output_payloads,
    }
    if action != "deposit":
        args["input_nullifiers"] = proof.input_nullifiers
    if action != "transfer":
        args["amount"] = amount
    if action == "withdraw":
        args["to"] = recipient
    return args


def prove_operation(prover, wallet, scope, action, amount, recipient, zk):
    if action == "deposit":
        plan = wallet.build_deposit(amount=amount)
        return plan, prover.prove_deposit(plan.request)
    if action == "withdraw":
        plan = wallet.build_withdraw(amount=amount, recipient=recipient)
        return plan, prover.prove_withdraw(plan.request)
    target = load_document(Path(recipient), Recipient)
    if target.network != scope.network or target.contract != scope.contract:
        raise WalletError(
            "recipient_mismatch", "The recipient belongs to a different shielded asset."
        )
    plan = wallet.build_transfer(
        amount=amount,
        recipient=zk.ShieldedRecipient(
            owner_public=target.owner_public,
            viewing_public_key=target.viewing_public_key,
        ),
    )
    return plan, prover.prove_transfer(plan.request)


@shielded.command()
def prepare(
    ctx: typer.Context,
    action: Literal["deposit", "transfer", "withdraw"],
    snapshot: Path,
    sender: Annotated[str, typer.Option("--from")],
    amount: Annotated[int, typer.Option(min=1, max=2**53 - 1)],
    bundle: Annotated[Path, typer.Option()],
    out: Annotated[Path, typer.Option()],
    recipient: Annotated[str | None, typer.Option("--to")] = None,
    password_file: PasswordFile = None,
    yes: bool = False,
) -> None:
    """Review the private details, prove locally and prepare a public transaction."""
    zk = toolkit()
    scope, wallet = read_snapshot(snapshot, password_for(ctx, password_file))
    address = resolve_address(ctx, sender)
    target = (
        resolve_address(ctx, recipient, "contact")
        if action == "withdraw" and recipient
        else recipient
    )
    if action != "deposit" and not target:
        raise WalletError(
            "recipient_required", "Provide the recipient address or shielded recipient file."
        )
    details = {
        "action": action,
        "amount": str(amount),
        "network": scope.network.chain_id,
        "contract": scope.contract,
        "recipient": target,
        "submitter": address,
    }
    if action == "transfer":
        note_recipient = load_document(Path(target), Recipient)
        details.update(
            owner_public=note_recipient.owner_public,
            viewing_public_key=note_recipient.viewing_public_key,
        )
    owner_review(ctx, details, yes, "Prepare this shielded movement?")

    async def draft():
        node = ctx.obj.node()
        await check_scope(node, scope)
        root = await node.call(scope.contract, "get_tree_state", {})
        args = await asyncio.to_thread(
            build_proof_call, zk, wallet, scope, action, amount, target, root, bundle
        )
        return await prepare_call(
            node, address, scope.contract, f"{action}_shielded", args, None, 300
        )

    save_proposal(ctx, asyncio.run(draft()), out)
