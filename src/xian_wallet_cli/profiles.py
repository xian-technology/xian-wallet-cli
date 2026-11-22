"""Named network, recipient and watched-asset profiles."""

from typing import Annotated, Literal

import typer
from pydantic import TypeAdapter

from .chain import Rpc, rpc_url
from .domain import Address, Contract, Document, Network, WalletError
from .vault import AccountBackup, Name

LOCAL_RPC = "http://127.0.0.1:26657"
networks = typer.Typer(no_args_is_help=True, help="Manage explicitly pinned Xian networks.")
contacts = typer.Typer(no_args_is_help=True, help="Manage the wallet address book.")
tokens = typer.Typer(no_args_is_help=True, help="Manage tokens to show in the portfolio.")


class NetworkProfile(Document):
    name: Name
    url: str
    network: Network


class Contact(Document):
    name: Name
    address: Address


class Token(Document):
    name: Name
    contract: Contract
    network: Network


def resolve_address(
    ctx: typer.Context, value: str, source: Literal["account", "contact"] = "account"
) -> str:
    value = value.strip()
    if len(value) == 64:
        return TypeAdapter(Address).validate_python(value.lower())
    if source == "contact":
        return ctx.obj.store.read("contact", value, Contact).address
    return ctx.obj.store.read("account", value, AccountBackup).account.address


@networks.command()
def add(
    ctx: typer.Context,
    name: str,
    url: str,
    chain_id: Annotated[str, typer.Option()],
    first_block_hash: Annotated[str, typer.Option()],
) -> None:
    """Pin an RPC URL to the expected chain and first-block hash."""
    identity = Network(chain_id=chain_id, first_block_hash=first_block_hash.upper())
    profile = NetworkProfile(name=name, url=rpc_url(url, allow_https=True), network=identity)
    ctx.obj.store.add("network", name, profile)
    ctx.obj.emit(profile.model_dump())


@networks.command("list")
def list_networks(ctx: typer.Context) -> None:
    ctx.obj.emit(
        {"networks": [p.model_dump() for p in ctx.obj.store.entries("network", NetworkProfile)]}
    )


@networks.command()
def remove(ctx: typer.Context, name: str) -> None:
    ctx.obj.store.remove("network", name)
    ctx.obj.emit({"removed": name})


@networks.command()
def use(ctx: typer.Context, name: str) -> None:
    profile = ctx.obj.store.read("network", name, NetworkProfile)
    with ctx.obj.store.transaction(write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO records VALUES ('settings', 'active-network', ?)",
            (profile.model_dump_json(),),
        )
    ctx.obj.emit(profile.model_dump())


@networks.command()
def check(ctx: typer.Context) -> None:
    """Verify the selected RPC identity."""
    import asyncio

    ctx.obj.emit(asyncio.run(ctx.obj.node().identity()).model_dump())


def resolve_rpc(store, explicit_url: str | None, name: str | None, rpc_type=Rpc):
    if name and explicit_url:
        raise WalletError(
            "invalid_options", "Choose a network profile or an explicit loopback RPC."
        )
    if name:
        profile = store.read("network", name, NetworkProfile)
        return rpc_type(profile.url, expected=profile.network)
    if explicit_url:
        return rpc_type(explicit_url)
    try:
        current = store.read("settings", "active-network", NetworkProfile)
    except WalletError as exc:
        if exc.code != "record_not_found":
            raise
        return rpc_type(LOCAL_RPC)
    profile = store.read("network", current.name, NetworkProfile)
    return rpc_type(profile.url, expected=profile.network)


@contacts.command("add")
def add_contact(ctx: typer.Context, name: str, address: str) -> None:
    entry = Contact(name=name, address=address.lower())
    ctx.obj.store.add("contact", name, entry)
    ctx.obj.emit(entry.model_dump())


@contacts.command("list")
def list_contacts(ctx: typer.Context) -> None:
    ctx.obj.emit({"contacts": [p.model_dump() for p in ctx.obj.store.entries("contact", Contact)]})


@contacts.command("remove")
def remove_contact(ctx: typer.Context, name: str) -> None:
    ctx.obj.store.remove("contact", name)
    ctx.obj.emit({"removed": name})


@tokens.command("watch")
def watch_token(ctx: typer.Context, name: str, contract: str) -> None:
    import asyncio

    network = asyncio.run(ctx.obj.node().identity())
    token = Token(name=name, contract=contract, network=network)
    key = token_key(network, name)
    ctx.obj.store.add("token", key, token)
    ctx.obj.emit(token.model_dump())


@tokens.command("list")
def list_tokens(ctx: typer.Context) -> None:
    ctx.obj.emit({"tokens": [p.model_dump() for p in ctx.obj.store.entries("token", Token)]})


@tokens.command("forget")
def forget_token(ctx: typer.Context, name: str) -> None:
    import asyncio

    network = asyncio.run(ctx.obj.node().identity())
    ctx.obj.store.remove("token", token_key(network, name))
    ctx.obj.emit({"removed": name})


def token_key(network: Network, name: str) -> str:
    return f"{network.chain_id}/{network.first_block_hash}/{name}"
