import pytest
from xian_py.wallet import Wallet

from xian_wallet_cli.activity import reserve_signing
from xian_wallet_cli.chain import now
from xian_wallet_cli.domain import Network, Plan, WalletError
from xian_wallet_cli.permissions import Grant, authorize, mint_capability
from xian_wallet_cli.state import Store

SENDER = Wallet("11" * 32)
RECIPIENT = Wallet("12" * 32).public_key
NET = Network(chain_id="wallet-test", first_block_hash="A" * 64)


def proposal(nonce: int, amount="1.5", recipient=RECIPIENT):
    current = now()
    return Plan(
        network=NET,
        sender=SENDER.public_key,
        recipient=recipient,
        amount=amount,
        nonce=nonce,
        chi_limit=500,
        chi_estimate=498,
        created_at=current,
        expires_at=current + 300,
    )


def test_scope_budget_replay_and_nonce_are_enforced_together(tmp_path):
    store = Store(tmp_path / "state")
    token, digest = mint_capability()
    grant = Grant(
        name="payouts",
        account="sender",
        sender=SENDER.public_key,
        network=NET,
        recipient=RECIPIENT,
        contract="currency",
        tx_limit="2",
        total_limit="2",
        max_chi=600,
        total_chi=2000,
        expires_at=now() + 300,
        token_hash=digest,
    )
    store.add("grant", grant.name, grant)

    draft = proposal(1)
    signed = authorize(store, grant.name, token, draft, lambda _: SENDER)
    assert authorize(store, grant.name, token, draft, lambda _: SENDER) == signed

    with pytest.raises(WalletError, match="nonce"):
        reserve_signing(store, proposal(1, "1.6"), SENDER)
    with pytest.raises(WalletError, match="scope"):
        authorize(store, grant.name, token, proposal(2, recipient="3" * 64), lambda _: SENDER)
    with pytest.raises(WalletError, match="budget"):
        authorize(store, grant.name, token, proposal(2, "0.75"), lambda _: SENDER)
    store.remove("grant", grant.name)
    with pytest.raises(WalletError):
        authorize(store, grant.name, token, proposal(2, "0.25"), lambda _: SENDER)


def test_signer_allows_only_explicit_competing_transaction(tmp_path):
    store = Store(tmp_path / "state")
    previous = reserve_signing(store, proposal(1), SENDER)
    other = proposal(1, "0.1")
    with pytest.raises(WalletError, match="specified hash"):
        reserve_signing(store, other, SENDER, replaces="A" * 64)
    replacement = reserve_signing(store, other, SENDER, replaces=previous.tx_hash)
    assert replacement.tx_hash != previous.tx_hash
    assert reserve_signing(store, other, SENDER) == replacement


def test_exact_caps_and_chi_limits_are_enforced(tmp_path):
    store = Store(tmp_path / "state")
    token, digest = mint_capability()
    grant = Grant(
        name="exact",
        account="sender",
        sender=SENDER.public_key,
        network=NET,
        recipient=RECIPIENT,
        contract="currency",
        tx_limit="1.5",
        total_limit="1.5",
        max_chi=500,
        total_chi=500,
        expires_at=now() + 60,
        token_hash=digest,
    )
    store.add("grant", grant.name, grant)
    with pytest.raises(WalletError, match="credential"):
        authorize(store, grant.name, "wrong", proposal(1), lambda _: SENDER)
    expensive = proposal(1).model_copy(update={"chi_limit": 501})
    with pytest.raises(WalletError, match="chi budget"):
        authorize(store, grant.name, token, expensive, lambda _: SENDER)
    draft = proposal(1)
    proof = authorize(store, grant.name, token, draft, lambda _: SENDER)
    assert proof.proposal_id == draft.proposal_id
    updated = store.read("grant", grant.name, Grant)
    assert (updated.spent, updated.spent_chi) == ("1.5", 500)
    max_only = grant.model_copy(update={"name": "max-only", "total_chi": 800})
    store.add("grant", max_only.name, max_only)
    with pytest.raises(WalletError, match="chi budget"):
        authorize(store, max_only.name, token, expensive, lambda _: SENDER)
    total_only = grant.model_copy(update={"name": "total-only", "max_chi": 800})
    store.add("grant", total_only.name, total_only)
    with pytest.raises(WalletError, match="chi budget"):
        authorize(store, total_only.name, token, expensive, lambda _: SENDER)


def test_agent_fetches_only_the_authenticated_inbox_decision(tmp_path):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from xian_wallet_cli.agent_cli import agent_app
    from xian_wallet_cli.domain import Approval, verify_approval
    from xian_wallet_cli.permissions import decide

    async def verify():
        store = Store(tmp_path / "wallet")
        draft = proposal(1)
        token, _ = mint_capability()
        client = TestClient(TestServer(agent_app(store, token, None)))
        await client.start_server()
        try:
            response = await client.post("/requests", json=draft.model_dump())
            assert response.status == 401
            headers = {"Authorization": "Bearer " + token}
            response = await client.post("/requests", json=draft.model_dump(), headers=headers)
            assert (await response.json())["status"] == "awaiting_approval"
            decide(store, draft.proposal_id, SENDER)
            response = await client.get("/requests/" + draft.proposal_id, headers=headers)
            result = await response.json()
            assert result["status"] == "approved"
            approval = Approval.model_validate(result["approval"])
            assert verify_approval(draft, approval).tx_hash == approval.tx_hash
        finally:
            await client.close()

    asyncio.run(verify())
