import asyncio
import json

import pytest
from xian_py.wallet import Wallet
from xian_py.x402 import verify_xian_x402_payment

from xian_wallet_cli.chain import now
from xian_wallet_cli.chain_apps import message_payload, minimum_output, swap_quote
from xian_wallet_cli.domain import Network, WalletError
from xian_wallet_cli.payments import Offer, payment_url, sign_offer
from xian_wallet_cli.state import Store

NET = Network(chain_id="local-test", first_block_hash="A" * 64)
SENDER = Wallet("19" * 32)
RECIPIENT = Wallet("20" * 32).public_key


def test_x402_recovery_reuses_permit_and_exact_resource(tmp_path):
    store = Store(tmp_path / "wallet")
    offer = Offer(
        network=NET,
        url="http://127.0.0.1:8012/report",
        asset="currency",
        amount="0.25",
        pay_to=RECIPIENT,
        settlement="x402_settlement",
        sender=SENDER.public_key,
        expires_at=now() + 180,
    )
    proof = sign_offer(store, offer, SENDER, 4)
    assert sign_offer(store, offer, SENDER, 4) == proof
    assert verify_xian_x402_payment(proof, offer.requirements).valid
    different_resource = offer.model_copy(update={"url": "http://127.0.0.1:8012/other"})
    assert not verify_xian_x402_payment(proof, different_resource.requirements).valid
    with pytest.raises(WalletError, match="permit nonce"):
        sign_offer(store, different_resource, SENDER, 4)


def test_http_payment_rejects_cleartext_remote_and_url_controls():
    for url in (
        "http://example.test/paid",
        "https://user@example.test/",
        "https://example.test/\npaid",
    ):
        with pytest.raises(WalletError):
            payment_url(url)


def test_message_envelope_matches_existing_wallets_for_unicode():
    signature_input = message_payload("xian-local", SENDER.public_key, "approve snowman ☃")
    assert signature_input == (
        f"\x19Xian Signed Message:\n1\nchain-id:10:xian-local\naccount:64:{SENDER.public_key}"
        "\nmessage:19:approve snowman ☃"
    )


def test_swap_quote_minimum_and_fee_on_transfer_guard():
    class Node:
        def __init__(self, flagged=False):
            self.flagged = flagged

        async def call(self, contract, function, args, **kwargs):
            if function == "pairFor":
                return 7
            assert contract == "con_dex" and args["path"] == [7]
            assert kwargs["sender"] == SENDER.public_key
            return ["1.5", "3.141592653589793238462643383279"]

        async def state(self, contract, field, token):
            return self.flagged

    quote = asyncio.run(swap_quote(Node(), "currency", "con_token", "1.5", SENDER.public_key))
    assert minimum_output(quote["quoted_output"], 100) == "3.110176727053895306078016949446"
    with pytest.raises(WalletError, match="standard transfer"):
        asyncio.run(swap_quote(Node(True), "currency", "con_token", "1.5", SENDER.public_key))


def test_shielded_recovery_snapshot_uses_real_sdk_without_exposing_seed(tmp_path):
    zk = pytest.importorskip("xian_zk")
    from xian_wallet_cli.shielded import ShieldedScope, read_snapshot, write_snapshot

    scope = ShieldedScope(contract="con_demo", network=NET)
    asset = zk.asset_id_for_contract(scope.contract)
    original = zk.ShieldedWallet.generate(asset)
    encrypted = tmp_path / "encrypted.json"
    write_snapshot(encrypted, scope, original, "private test password")
    assert original.owner_secret not in encrypted.read_text()
    assert original.viewing_private_key not in encrypted.read_text()
    checked, restored = read_snapshot(encrypted, "private test password")
    assert checked == scope
    assert restored.owner_public == original.owner_public
    with pytest.raises(WalletError, match="password"):
        read_snapshot(encrypted, "wrong password")
    tampered = json.loads(encrypted.read_text())
    tampered["scope"]["network"]["chain_id"] = "wrong-chain"
    encrypted.write_text(json.dumps(tampered))
    with pytest.raises(WalletError, match="password"):
        read_snapshot(encrypted, "private test password")
