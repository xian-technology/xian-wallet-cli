import json

from xian_py.wallet import Wallet

from xian_wallet_cli.domain import CallPlan, Network, sign_plan, verify_approval

SENDER = Wallet("ab" * 32)
RECIPIENT = Wallet("cd" * 32).public_key


def test_general_call_preserves_typed_values_in_signature():
    plan = CallPlan(
        network=Network(chain_id="test", first_block_hash="A" * 64),
        sender=SENDER.public_key,
        nonce=2,
        chi_estimate=500,
        chi_limit=501,
        created_at=1,
        expires_at=300,
        contract="con_example",
        function="transfer",
        kwargs={"amount": {"__fixed__": "1.000000000000000000000000000001"}, "to": RECIPIENT},
    )
    approval = sign_plan(plan, SENDER, 2)
    payload = json.loads(verify_approval(plan, approval).json_bytes)["payload"]
    assert payload["kwargs"] == plan.kwargs
    assert payload["contract"] == "con_example"
