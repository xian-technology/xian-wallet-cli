"""Trusted signer commands and an authenticated Unix-socket handoff."""

import asyncio
import hmac
import os
import socket
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated

import typer
from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, web
from pydantic import ValidationError

from .accounts_cli import PasswordFile, password_for
from .activity import Journal
from .chain import now
from .domain import PROPOSAL, Approval, WalletError, exact_amount, read_proposal
from .files import write_new
from .permissions import Grant, Request, authorize, decide, enqueue, mint_capability
from .presentation import ReviewApp, preview_data
from .profiles import resolve_address
from .telegram import Telegram
from .vault import AccountBackup, secret_file

inbox = typer.Typer(no_args_is_help=True, help="Manage proposals awaiting the owner's decision.")
grants = typer.Typer(no_args_is_help=True, help="Manage time- and budget-limited agent grants.")
agent = typer.Typer(no_args_is_help=True, help="Connect an agent to a trusted wallet signer.")
Credential = Annotated[Path, typer.Option(help="Owner-only bearer credential file.")]
SocketPath = Annotated[Path, typer.Option("--socket", help="Wallet signer Unix socket.")]


def unlock_account(store, name: str, password: str):
    return store.read("account", name, AccountBackup).unlock(password)


def request_data(request: Request) -> dict:
    return {
        "proposal": preview_data(request.proposal),
        "status": request.status,
        "approval": request.approval.model_dump() if request.approval else None,
    }


@inbox.command("request")
def request_locally(ctx: typer.Context, proposal: Path) -> None:
    item = enqueue(ctx.obj.store, read_proposal(proposal))
    ctx.obj.emit(request_data(item))


@inbox.command("list")
def list_inbox(ctx: typer.Context) -> None:
    rows = ctx.obj.store.entries("request", Request)
    ctx.obj.emit({"requests": [request_data(item) for item in rows]})


@inbox.command("review")
def review_inbox(
    ctx: typer.Context,
    proposal_id: str,
    account: Annotated[str, typer.Option()],
    password_file: PasswordFile = None,
) -> None:
    from .cli import require_terminal

    output = ctx.obj
    require_terminal(output)
    item = output.store.read("request", proposal_id.upper(), Request)
    item.proposal.require_unexpired(now())
    if item.status != "awaiting_approval":
        raise WalletError("request_closed", "This request already has a decision.")
    if not ReviewApp(item.proposal, allow_sign=True).run():
        raise WalletError("approval_cancelled", "Review closed. The request remains pending.")
    result = decide(
        output.store,
        proposal_id.upper(),
        unlock_account(
            output.store,
            account,
            password_for(ctx, password_file),
        ),
    )
    if result.approval:
        Journal(output.store).record(result.proposal, result.approval)
    output.emit(request_data(result))


@inbox.command()
def decline(ctx: typer.Context, proposal_id: str) -> None:
    ctx.obj.emit(request_data(decide(ctx.obj.store, proposal_id.upper(), None)))


@grants.command()
def create(
    ctx: typer.Context,
    name: str,
    account: Annotated[str, typer.Option()],
    recipient: Annotated[str, typer.Option()],
    tx_limit: Annotated[str, typer.Option()],
    total_limit: Annotated[str, typer.Option()],
    max_chi: Annotated[int, typer.Option(min=1, max=2**53 - 1)],
    total_chi: Annotated[int, typer.Option(min=1, max=2**53 - 1)],
    seconds: Annotated[int, typer.Option(min=1, max=2_592_000)],
    out: Annotated[Path, typer.Option(help="New file for the agent's grant credential.")],
    contract: str = "currency",
    password_file: PasswordFile = None,
) -> None:
    from sqlite3 import IntegrityError

    output = ctx.obj
    backup = output.store.read("account", account, AccountBackup)
    sender = backup.account
    if sender.kind == "watch":
        raise WalletError("watch_only", "A watch-only account has no signing key.")
    backup.unlock(password_for(ctx, password_file))
    token, digest = mint_capability()
    grant = Grant(
        name=name,
        account=account,
        sender=sender.address,
        network=asyncio.run(output.node().identity()),
        recipient=resolve_address(ctx, recipient, "contact"),
        contract=contract,
        tx_limit=exact_amount(tx_limit),
        total_limit=exact_amount(total_limit),
        max_chi=max_chi,
        total_chi=total_chi,
        expires_at=now() + seconds,
        token_hash=digest,
    )
    try:
        with output.store.transaction(write=True) as db:
            db.execute(
                "INSERT INTO records VALUES ('grant', ?, ?)", (name, grant.model_dump_json())
            )
            write_new(out, token + "\n")
    except IntegrityError as exc:
        raise WalletError("duplicate_name", "A grant with this name already exists.") from exc
    output.emit(grant.model_dump(exclude={"token_hash"}))


@grants.command("list")
def list_grants(ctx: typer.Context) -> None:
    rows = ctx.obj.store.entries("grant", Grant)
    ctx.obj.emit({"grants": [item.model_dump(exclude={"token_hash"}) for item in rows]})


@grants.command()
def revoke(ctx: typer.Context, name: str) -> None:
    ctx.obj.store.remove("grant", name)
    ctx.obj.emit({"revoked": name})


def agent_app(store, request_token: str, password: str | None) -> web.Application:
    @web.middleware
    async def wallet_errors(request, handler):
        try:
            return await handler(request)
        except WalletError as exc:
            status = 401 if exc.code == "unauthorized" else 422
            return web.json_response(
                {"error": {"code": exc.code, "message": str(exc)}}, status=status
            )
        except ValidationError, ValueError:
            return web.json_response(
                {"error": {"code": "invalid_proposal", "message": "Proposal could not be read."}},
                status=422,
            )

    def bearer(request):
        value = request.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise WalletError("unauthorized", "A signer credential is required.")
        return value[7:]

    def require_request_token(request):
        if not hmac.compare_digest(bearer(request).encode(), request_token.encode()):
            raise WalletError("unauthorized", "The signer credential is invalid.")

    async def submit_request(request):
        require_request_token(request)
        draft = PROPOSAL.validate_json(await request.read())
        return web.json_response(request_data(await asyncio.to_thread(enqueue, store, draft)))

    async def get_request(request):
        require_request_token(request)
        item = await asyncio.to_thread(store.read, "request", request.match_info["id"], Request)
        return web.json_response(request_data(item))

    async def sign_grant(request):
        token = bearer(request)
        if password is None:
            raise WalletError("vault_locked", "Grant signing is disabled on this service.")
        proposal = PROPOSAL.validate_json(await request.read())
        proof = await asyncio.to_thread(
            authorize,
            store,
            request.match_info["name"],
            token,
            proposal,
            lambda name: unlock_account(store, name, password),
        )
        await asyncio.to_thread(Journal(store).record, proposal, proof)
        return web.json_response({"approval": proof.model_dump()})

    application = web.Application(middlewares=[wallet_errors], client_max_size=16_384)
    application.add_routes(
        [
            web.post("/requests", submit_request),
            web.get("/requests/{id}", get_request),
            web.post("/grants/{name}", sign_grant),
        ]
    )
    return application


@agent.command()
def serve(
    ctx: typer.Context,
    socket_path: SocketPath,
    request_token_file: Annotated[Path, typer.Option()],
    password_file: PasswordFile = None,
    telegram_token_file: Annotated[Path | None, typer.Option()] = None,
    telegram_owner_id: Annotated[int | None, typer.Option(min=1)] = None,
    telegram_account: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Run the trusted signer handoff over an owner-only Unix socket."""
    password = password_for(ctx, password_file) if password_file else None
    request_token = secret_file(request_token_file, "request-token")
    if len(request_token) < 32 or not request_token.isascii() or not request_token.isalnum():
        raise WalletError(
            "weak_credential", "Use at least 32 ASCII letters or digits for the request token."
        )
    application = agent_app(ctx.obj.store, request_token, password)
    telegram_options = (telegram_token_file, telegram_owner_id, telegram_account)
    if any(option is not None for option in telegram_options):
        if None in telegram_options or password is None:
            raise WalletError(
                "invalid_telegram_config",
                "Configure token file, owner, signing account and password.",
            )
        bot = Telegram(secret_file(telegram_token_file, "Telegram bot-token"), telegram_owner_id)
        attach_telegram(application, ctx.obj.store, bot, telegram_account, password)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        try:
            os.chmod(socket_path, 0o600)
            listener.listen()
            web.run_app(application, sock=listener, print=None, access_log=None)
        finally:
            socket_path.unlink(missing_ok=True)


async def broker_call(
    socket_path: Path,
    credential: Path,
    method: str,
    endpoint: str,
    proposal=None,
) -> dict:
    value = secret_file(credential, "signer credential")
    options = {"json": proposal.model_dump()} if proposal else {}
    timeout = ClientTimeout(total=30)
    try:
        async with ClientSession(
            connector=UnixConnector(path=str(socket_path)), timeout=timeout
        ) as session:
            async with session.request(
                method,
                f"http://localhost{endpoint}",
                headers={"Authorization": f"Bearer {value}"},
                allow_redirects=False,
                **options,
            ) as response:
                result = await response.json()
    except (ClientError, TimeoutError, ValueError) as exc:
        raise WalletError("signer_unavailable", "Could not reach the signer service.") from exc
    if error := result.get("error"):
        raise WalletError(error["code"], error["message"])
    return result


@agent.command()
def request(
    ctx: typer.Context, proposal: Path, socket_path: SocketPath, token_file: Credential
) -> None:
    result = asyncio.run(
        broker_call(
            socket_path,
            token_file,
            "POST",
            "/requests",
            read_proposal(proposal),
        )
    )
    ctx.obj.emit(result)


def save_reply(ctx: typer.Context, result: dict, out: Path) -> None:
    if raw := result.get("approval"):
        proof = Approval.model_validate(raw)
        write_new(out, proof.model_dump_json(indent=2) + "\n")
        result["approval_file"] = str(out)
    ctx.obj.emit(result)


@agent.command()
def fetch(
    ctx: typer.Context,
    proposal_id: str,
    socket_path: SocketPath,
    token_file: Credential,
    out: Annotated[Path, typer.Option()],
) -> None:
    from pydantic import TypeAdapter

    from .domain import Digest

    proposal_id = TypeAdapter(Digest).validate_python(proposal_id.upper())
    result = asyncio.run(broker_call(socket_path, token_file, "GET", f"/requests/{proposal_id}"))
    save_reply(ctx, result, out)


@agent.command("authorize")
def authorize_agent(
    ctx: typer.Context,
    grant: str,
    proposal: Path,
    socket_path: SocketPath,
    token_file: Credential,
    out: Annotated[Path, typer.Option()],
) -> None:
    from pydantic import TypeAdapter

    from .vault import Name

    grant = TypeAdapter(Name).validate_python(grant)
    result = asyncio.run(
        broker_call(
            socket_path,
            token_file,
            "POST",
            f"/grants/{grant}",
            read_proposal(proposal),
        )
    )
    save_reply(ctx, result, out)


@inbox.command("telegram")
def telegram_review(
    ctx: typer.Context,
    proposal_id: str,
    account: Annotated[str, typer.Option()],
    token_file: Annotated[Path, typer.Option(help="Owner-only Telegram Bot API token file.")],
    owner_id: Annotated[int, typer.Option(help="Authorized Telegram user's numeric ID.", min=1)],
    password_file: PasswordFile = None,
) -> None:
    """Request a decision in the configured owner's Telegram private chat."""
    output = ctx.obj
    item = output.store.read("request", proposal_id.upper(), Request)
    if item.status != "awaiting_approval":
        raise WalletError("request_closed", "This request already has a decision.")
    backup = output.store.read("account", account, AccountBackup)
    if backup.account.address != item.proposal.sender:
        raise WalletError("wrong_signer", "The signing account does not match the sender.")
    bot = Telegram(secret_file(token_file, "Telegram bot-token"), owner_id)
    accepted = asyncio.run(bot.ask(item.proposal))
    wallet = backup.unlock(password_for(ctx, password_file)) if accepted else None
    result = decide(output.store, proposal_id.upper(), wallet)
    if result.approval:
        Journal(output.store).record(result.proposal, result.approval)
    output.emit(request_data(result))


def attach_telegram(application, store, bot: Telegram, account_name: str, password: str) -> None:
    account = store.read("account", account_name, AccountBackup).account
    if account.kind == "watch":
        raise WalletError("watch_only", "Choose a signing account for the Telegram bridge.")

    async def approve_requests():
        seen = set()
        while True:
            entries = await asyncio.to_thread(store.entries, "request", Request)
            for item in entries:
                if item.status != "awaiting_approval" or item.proposal.sender != account.address:
                    continue
                proposal_id = item.proposal.proposal_id
                if proposal_id in seen or item.proposal.expires_at <= now():
                    continue
                seen.add(proposal_id)
                try:
                    accepted = await bot.ask(item.proposal)
                    wallet = (
                        await asyncio.to_thread(unlock_account, store, account_name, password)
                        if accepted
                        else None
                    )
                    result = await asyncio.to_thread(decide, store, proposal_id, wallet)
                    if result.approval:
                        await asyncio.to_thread(
                            Journal(store).record, result.proposal, result.approval
                        )
                except WalletError as exc:
                    print(f"Telegram review {proposal_id}: {exc.code}", file=sys.stderr)
            await asyncio.sleep(1)

    async def lifecycle(_application):
        task = asyncio.create_task(approve_requests())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    application.cleanup_ctx.append(lifecycle)
