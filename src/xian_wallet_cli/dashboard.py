"""Textual wallet overview and owner-operated approval inbox."""

import io

import qrcode
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option
from xian_py.exception import XianException

from .activity import Activity, Journal
from .chain import now
from .domain import WalletError
from .permissions import Request, decide
from .presentation import primary_detail, rows
from .vault import AccountBackup


class InboxReview(ModalScreen[bool]):
    CSS = """
    InboxReview { align: center bottom; background: #000000; }
    InboxReview VerticalScroll { border: round #4d7f00; padding: 1 2; height: 1fr; }
    InboxReview Horizontal { height: 3; }
    InboxReview Static { height: auto; margin-bottom: 1; }
    InboxReview Input { margin: 0 1; }
    #approve { background: #adff2f; color: #000000; }
    #review-error { color: #ff6868; }
    """

    def __init__(self, store, item: Request, account: AccountBackup):
        super().__init__()
        self.store, self.item, self.account = store, item, account

    def compose(self) -> ComposeResult:
        plan = self.item.proposal
        with VerticalScroll():
            yield Static(Text(primary_detail(plan), style="bold #adff2f", overflow="fold"))
            for label, value in rows(plan):
                yield Static(Text(f"{label}\n{value}", overflow="fold"))
            yield Static(Text(f"First block\n{plan.network.first_block_hash}", overflow="fold"))
            yield Static(Text(f"Proposal\n{plan.proposal_id}", overflow="fold"))
            yield Static("", id="review-error")
        yield Input(placeholder="Vault password", password=True, id="vault-password")
        with Horizontal():
            yield Button("Approve and sign", id="approve")
            yield Button("Back", id="back")

    @on(Button.Pressed, "#back")
    def close_review(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#approve")
    def start_signing(self) -> None:
        field = self.query_one(Input)
        password = field.value
        field.value = ""
        self.set_signing(True)
        self.app.run_worker(
            lambda: self.sign(password),
            name="sign-proposal",
            description="Sign the reviewed envelope",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def set_signing(self, signing: bool) -> None:
        for button in self.query(Button):
            button.disabled = signing

    def sign(self, password: str) -> None:
        try:
            wallet = self.account.unlock(password)
            result = decide(self.store, self.item.proposal.proposal_id, wallet)
            if result.approval:
                Journal(self.store).record(result.proposal, result.approval)
        except (WalletError, OSError) as error:
            self.app.call_from_thread(self.show_sign_error, str(error))
        else:
            self.app.call_from_thread(self.dismiss, result.status == "approved")

    def show_sign_error(self, error: str) -> None:
        self.set_signing(False)
        self.query_one("#review-error", Static).update(Text(error))


class WalletApp(App):
    TITLE = "Xian Wallet"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { background: #000000; color: #ffffff; }
    #brand { color: #adff2f; text-style: bold; padding: 1 2; }
    TabPane { padding: 0 2; }
    OptionList { height: auto; max-height: 10; border: round #4d7f00; }
    .details { height: auto; margin: 1 0; }
    VerticalScroll { height: 1fr; }
    """

    def __init__(self, store, node_factory):
        super().__init__()
        self.store, self.node_factory = store, node_factory
        self.inbox_digest: tuple = ()
        self.accounts = {p.account.name: p for p in store.entries("account", AccountBackup)}

    def compose(self) -> ComposeResult:
        yield Label("Xian Wallet", id="brand")
        with TabbedContent():
            with TabPane("Accounts", id="accounts-tab"):
                with VerticalScroll():
                    yield OptionList(
                        *(
                            Option(Text(p.account.name), id=p.account.name)
                            for p in self.accounts.values()
                        ),
                        id="accounts",
                    )
                    caption = "Select an account to view its address and balance."
                    if not self.accounts:
                        caption = "Create or restore an account with xian-wallet account."
                    yield Static(caption, id="account-detail", classes="details")
            with TabPane("Activity", id="activity-tab"):
                with VerticalScroll():
                    yield Static(self.activity_text(), id="activity", classes="details")
            with TabPane("Approvals", id="inbox-tab"):
                yield OptionList(*self.inbox_options(), id="inbox")
                yield Static("Select a pending proposal to review it.", classes="details")
        yield Footer()

    def activity_text(self) -> Text:
        rows = sorted(
            self.store.entries("activity", Activity),
            key=lambda row: row.timestamp,
            reverse=True,
        )
        return Text(
            "\n".join(
                f"{row.status} · {row.contract}.{row.function}\n{row.tx_hash}" for row in rows
            )
            or "No locally tracked transactions.",
            overflow="fold",
        )

    def inbox_options(self) -> list[Option]:
        return [
            Option(
                Text(f"{item.status} · {primary_detail(item.proposal)}"),
                id=item.proposal.proposal_id,
            )
            for item in self.store.entries("request", Request)
        ]

    @on(OptionList.OptionSelected, "#accounts")
    @work(exclusive=True)
    async def show_account(self, event: OptionList.OptionSelected) -> None:
        account = self.accounts[event.option_id].account
        qr = qrcode.QRCode(border=2)
        qr.add_data(account.address)
        image = io.StringIO()
        qr.print_ascii(out=image, tty=False)
        detail = self.query_one("#account-detail", Static)
        detail.update(
            Text(f"{account.name}\n{account.address}\n{image.getvalue()}", overflow="fold")
        )
        try:
            amount = await self.node_factory().balance(account.address)
        except WalletError, XianException, OSError:
            balance = "RPC balance unavailable"
        else:
            balance = f"{amount} XIAN"
        detail.update(
            Text(
                f"{account.name} · {balance}\n{account.address}\n{image.getvalue()}",
                overflow="fold",
            )
        )

    @on(OptionList.OptionSelected, "#inbox")
    def open_inbox(self, event: OptionList.OptionSelected) -> None:
        item = self.store.read("request", event.option_id, Request)
        if item.status != "awaiting_approval" or item.proposal.expires_at <= now():
            self.notify("This proposal is no longer awaiting approval.")
            return
        for account in self.accounts.values():
            if account.account.address == item.proposal.sender and account.account.kind != "watch":
                self.push_screen(InboxReview(self.store, item, account), self.refresh_inbox)
                return
        self.notify(
            "Import the sender's signing account to approve this proposal.", severity="warning"
        )

    def on_mount(self) -> None:
        self.set_interval(2, self.sync_views)

    def sync_views(self) -> None:
        if isinstance(self.screen, InboxReview):
            return
        items = self.store.entries("request", Request)
        digest = tuple((item.proposal.proposal_id, item.status) for item in items)
        if digest != self.inbox_digest:
            self.inbox_digest = digest
            self.refresh_inbox(False)
        self.query_one("#activity", Static).update(self.activity_text())

    def refresh_inbox(self, signed: bool) -> None:
        options = self.query_one("#inbox", OptionList)
        options.clear_options().add_options(self.inbox_options())
        if signed:
            self.notify("Signed approval is available to the agent.")
