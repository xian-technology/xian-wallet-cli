import asyncio

from textual.widgets import OptionList, Static

from xian_wallet_cli.dashboard import WalletApp
from xian_wallet_cli.state import Store
from xian_wallet_cli.vault import Account, AccountBackup


def test_dashboard_displays_watch_only_account_address_and_balance(tmp_path):
    store = Store(tmp_path / "wallet")
    account = Account(name="watch-test", address="a" * 64, kind="watch")
    store.add("account", account.name, AccountBackup(account=account))

    class Node:
        async def balance(self, address):
            assert address == account.address
            return "12.5"

    async def verify():
        app = WalletApp(store, Node)
        async with app.run_test(size=(88, 30)) as pilot:
            options = app.query_one("#accounts", OptionList)
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            detail = app.query_one("#account-detail", Static)
            assert "12.5 XIAN" in detail.render().plain
            assert account.address in detail.render().plain

    asyncio.run(verify())


def test_dashboard_inbox_signs_the_selected_proposal(tmp_path):
    from textual.widgets import Input
    from xian_py.wallet import Wallet

    from xian_wallet_cli.chain import now
    from xian_wallet_cli.dashboard import InboxReview
    from xian_wallet_cli.domain import Network, Plan, verify_approval
    from xian_wallet_cli.permissions import Request, enqueue
    from xian_wallet_cli.vault import seal_account

    store = Store(tmp_path / "wallet")
    owner = Wallet("12" * 32)
    password = "temporary testing password"
    backup = seal_account("owner", "private_key", owner.private_key, password)
    store.add("account", "owner", backup)
    created = now()
    draft = Plan(
        network=Network(chain_id="local-test", first_block_hash="A" * 64),
        sender=owner.public_key,
        recipient=Wallet("13" * 32).public_key,
        amount="0.125",
        nonce=0,
        chi_estimate=400,
        chi_limit=402,
        created_at=created,
        expires_at=created + 300,
    )
    enqueue(store, draft)

    async def verify():
        app = WalletApp(store, lambda: None)
        async with app.run_test(size=(88, 32)) as pilot:
            await app.push_screen(
                InboxReview(store, store.read("request", draft.proposal_id, Request), backup)
            )
            app.screen.query_one(Input).value = password
            await pilot.click("#approve")
            await app.workers.wait_for_complete()
            confirmed = store.read("request", draft.proposal_id, Request)
            assert confirmed.status == "approved"
            assert verify_approval(draft, confirmed.approval).tx_hash == confirmed.approval.tx_hash

    asyncio.run(verify())
