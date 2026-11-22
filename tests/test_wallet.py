import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner
from xian_py.wallet import Wallet

from xian_wallet_cli import cli
from xian_wallet_cli.chain import Finality, prepare_transfer, submit_transfer
from xian_wallet_cli.domain import Network, WalletError, sign_plan

SENDER = Wallet("11" * 32)
RECIPIENT = Wallet("22" * 32).public_key
NETWORK = Network(chain_id="wallet-test", first_block_hash="A" * 64)
NOW = int(datetime.now(UTC).timestamp())


class Localnet:
    def __init__(self):
        self.network = NETWORK
        self.broadcasts = []
        self.final = None
        self.lose_response = False

    async def identity(self):
        return self.network

    async def nonce(self, sender):
        assert sender == SENDER.public_key
        return 3

    async def estimate(self, payload):
        assert payload["kwargs"]["to"] == RECIPIENT
        return 4000

    async def lookup(self, tx_hash):
        return self.final

    async def broadcast(self, transaction):
        self.broadcasts.append(transaction.tx_hash)
        self.final = Finality(
            tx_hash=transaction.tx_hash, status="completed", height=5, chi_used=4000
        )
        if self.lose_response:
            raise TimeoutError
        return None

    async def wait(self, tx_hash, seconds):
        return self.final


def prepare(node):
    return asyncio.run(prepare_transfer(node, SENDER.public_key, RECIPIENT, "1.25", 6000, 300))


def test_remote_approval_and_retry_after_lost_response():
    node = Localnet()
    plan = prepare(node)
    approval = sign_plan(plan, SENDER, NOW)
    node.lose_response = True
    receipt = asyncio.run(submit_transfer(node, plan, approval, 1))
    retry = asyncio.run(submit_transfer(node, plan, approval, 1))
    assert receipt == retry
    assert receipt.status == "completed"
    assert node.broadcasts == [approval.tx_hash]


def test_approval_binds_fields_and_network():
    node = Localnet()
    plan = prepare(node)
    proof = sign_plan(plan, SENDER, NOW)
    changed = plan.model_copy(update={"amount": "1.26"})
    with pytest.raises(WalletError, match="different proposal"):
        asyncio.run(submit_transfer(node, changed, proof, 1))
    node.network = Network(chain_id=NETWORK.chain_id, first_block_hash="B" * 64)
    with pytest.raises(WalletError, match="different network"):
        asyncio.run(submit_transfer(node, plan, proof, 1))
    assert node.broadcasts == []


def test_cli_agents_need_no_terminal_or_signer_on_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "Rpc", lambda url: Localnet())
    runner = CliRunner()
    key = tmp_path / "owner.key"
    key.write_text(SENDER.private_key)
    key.chmod(0o600)
    plan, approval = tmp_path / "proposal.json", tmp_path / "approval.json"

    def run(*args):
        result = runner.invoke(cli.app, ["--json", *map(str, args)])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    prepared = run(
        "prepare",
        "--from",
        SENDER.public_key,
        "--to",
        RECIPIENT,
        "--amount",
        "1.250",
        "--chi-limit",
        6000,
        "--out",
        plan,
    )
    reviewed = run("preview", plan)
    assert prepared["data"]["proposal_id"] == reviewed["data"]["proposal_id"]
    assert reviewed["data"]["amount"] == "1.25"
    run("approve", plan, "--key-file", key, "--out", approval, "--yes")
    key.unlink()
    completed = run("submit", plan, "--approval", approval)
    assert completed["data"]["status"] == "completed"
    assert completed["data"]["tx_hash"] == json.loads(Path(approval).read_text())["tx_hash"]


@pytest.mark.parametrize(
    "field",
    ["approval_signature", "transaction_signature", "tx_hash"],
)
def test_each_approval_proof_is_verified(field):
    node = Localnet()
    plan = prepare(node)
    approval = sign_plan(plan, SENDER, NOW)
    original = getattr(approval, field)
    altered = approval.model_copy(
        update={field: ("1" if original[0] == "0" else "0") + original[1:]}
    )
    with pytest.raises(WalletError, match="signature|hash"):
        asyncio.run(submit_transfer(node, plan, altered, 1))
    assert not node.broadcasts


def test_expired_approval_can_find_receipt_but_cannot_broadcast(monkeypatch):
    node = Localnet()
    plan = prepare(node)
    approval = sign_plan(plan, SENDER, NOW)
    monkeypatch.setattr("xian_wallet_cli.chain.now", lambda: plan.expires_at)
    with pytest.raises(WalletError, match="expired"):
        asyncio.run(submit_transfer(node, plan, approval, 1))
    assert not node.broadcasts

    node.final = Finality(tx_hash=approval.tx_hash, status="completed", height=5)
    assert asyncio.run(submit_transfer(node, plan, approval, 1)) == node.final


def test_pending_result_keeps_hash_after_failed_transport():
    class UnreachableAfterLookup(Localnet):
        async def broadcast(self, transaction):
            raise TimeoutError

    node = UnreachableAfterLookup()
    plan = prepare(node)
    approval = sign_plan(plan, SENDER, NOW)
    receipt = asyncio.run(submit_transfer(node, plan, approval, 1))
    assert (receipt.status, receipt.tx_hash) == ("pending", approval.tx_hash)


def test_wallet_never_overwrites_key_or_accepts_unprotected_key(tmp_path):
    node = Localnet()
    plan = prepare(node)
    runner = CliRunner()
    owner = tmp_path / "owner.key"
    owner.write_text(SENDER.private_key)
    owner.chmod(0o644)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(plan.model_dump_json())

    generated = runner.invoke(cli.app, ["--json", "generate", "--out", str(owner)])
    assert json.loads(generated.stdout)["error"]["code"] == "file_exists"

    approval = tmp_path / "approval.json"
    command = [
        "--json",
        "approve",
        str(proposal),
        "--key-file",
        str(owner),
        "--out",
        str(approval),
        "--yes",
    ]
    denied = runner.invoke(cli.app, command)
    assert json.loads(denied.stdout)["error"]["code"] == "unsafe_key_file"
    assert not approval.exists()
    assert Wallet(owner.read_text()).public_key == SENDER.public_key


@pytest.mark.parametrize("size", [(96, 32), (44, 20)])
def test_textual_review_supports_keyboard_and_narrow_terminals(size):
    from textual.containers import VerticalScroll
    from textual.widgets import Collapsible

    from xian_wallet_cli.presentation import ReviewApp

    plan = prepare(Localnet())

    async def review():
        app = ReviewApp(plan, allow_sign=True)
        async with app.run_test(size=size) as pilot:
            details = app.query_one(Collapsible)
            assert details.collapsed
            await pilot.press("tab", "enter")
            assert not details.collapsed
            assert app.query_one(VerticalScroll).virtual_size.width <= size[0]
            await pilot.press("escape")
        assert app.return_value is False

    asyncio.run(review())


def test_textual_signing_is_an_explicit_action():
    from xian_wallet_cli.presentation import ReviewApp

    async def review():
        app = ReviewApp(prepare_plan, allow_sign=True)
        async with app.run_test(size=(44, 20)) as pilot:
            await pilot.press("enter")
            assert app.return_value is None
            await pilot.press("tab", "tab", "enter")
        assert app.return_value is True

    prepare_plan = prepare(Localnet())
    asyncio.run(review())
