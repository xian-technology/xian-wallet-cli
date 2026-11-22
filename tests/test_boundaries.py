import asyncio
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner
from xian_py.exception import TransportError
from xian_py.models import TransactionReceipt

from xian_wallet_cli import chain, cli
from xian_wallet_cli.domain import Network, Plan, WalletError
from xian_wallet_cli.presentation import render_preview


@pytest.mark.parametrize(
    "url",
    ["https://example.org", "http://localhost/node", "http://u:p@127.0.0.1", "http://[::1]:99999"],
)
def test_rpc_refuses_unsupported_endpoints(url):
    with pytest.raises(WalletError, match="loopback"):
        chain.Rpc(url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:26657/", "http://[::1]:26657", "http://localhost"]
)
def test_rpc_accepts_loopback_endpoints(url):
    assert chain.Rpc(url).url == url.rstrip("/")


@pytest.mark.parametrize(
    ("catching_up", "header_chain", "error"),
    [(False, "testnet", None), (False, "other", "first block"), (True, "testnet", "synced chain")],
)
def test_node_identity_requires_synced_chain_and_matching_first_block(
    monkeypatch, catching_up, header_chain, error
):
    class Client:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_node_status(self):
            return SimpleNamespace(catching_up=catching_up, network="testnet")

    async def get_block(url, height, *, session):
        assert height == 1
        return {
            "result": {
                "block_id": {"hash": "A" * 64},
                "block": {"header": {"chain_id": header_chain}},
            }
        }

    monkeypatch.setattr(chain, "get_block_async", get_block)
    rpc = chain.Rpc("http://localhost")
    rpc.client = Client()
    if error:
        with pytest.raises(WalletError, match=error):
            asyncio.run(rpc.identity())
    else:
        assert asyncio.run(rpc.identity()) == Network(chain_id="testnet", first_block_hash="A" * 64)


def test_confirmed_execution_requires_matching_hash_and_height():
    tx_hash = "C" * 64
    receipt = TransactionReceipt.from_lookup(
        {
            "result": {"hash": tx_hash, "height": "12"},
            "success": False,
            "message": "Execution reverted",
        }
    )
    proof = chain.confirmed_receipt(tx_hash, receipt)
    assert (proof.status, proof.height, proof.message) == ("failed", 12, "Execution reverted")
    with pytest.raises(WalletError, match="inconsistent receipt"):
        chain.confirmed_receipt("D" * 64, receipt)


@pytest.mark.parametrize("width", [80, 100])
def test_rich_preview_shows_the_entire_transfer(width):
    timestamp = int(datetime.now(UTC).timestamp())
    plan = Plan(
        network=Network(chain_id="wallet-test", first_block_hash="A" * 64),
        sender="1" * 64,
        recipient="2" * 64,
        amount="1.000000000000000000000000000001",
        nonce=1,
        chi_estimate=500,
        chi_limit=502,
        created_at=timestamp,
        expires_at=timestamp + 300,
    )
    output = StringIO()
    render_preview(Console(file=output, width=width, color_system=None), plan)
    for expected in (plan.sender, plan.recipient, plan.amount, plan.proposal_id, "502 (est. 500)"):
        assert expected in output.getvalue()


def test_json_reports_file_validation_and_rpc_failures(tmp_path, monkeypatch):
    import json

    class Unreachable:
        async def balance(self, address):
            raise TransportError("RPC unreachable")

    monkeypatch.setattr(cli, "Rpc", lambda url: Unreachable())
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    commands = [
        (["preview", str(tmp_path / "missing.json")], "io_error"),
        (["preview", str(invalid)], "invalid_input"),
        (["balance", "1" * 64], "rpc_error"),
    ]
    runner = CliRunner()
    for args, code in commands:
        result = runner.invoke(cli.app, ["--json", *args])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == code
