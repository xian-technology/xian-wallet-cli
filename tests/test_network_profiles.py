import json

import pytest
from typer.testing import CliRunner

from xian_wallet_cli.cli import app


@pytest.fixture
def network_cli(tmp_path):
    runner = CliRunner()
    home = tmp_path / "wallet"

    def run(*args, succeeds=True):
        result = runner.invoke(app, ["--home", str(home), "--json", "network", *args])
        assert result.exit_code == (0 if succeeds else 2), result.output
        return json.loads(result.stdout)["data" if succeeds else "error"]

    def add(name):
        run(
            "add",
            name,
            "http://127.0.0.1:26657",
            "--chain-id",
            name,
            "--first-block-hash",
            "A" * 64,
        )

    return run, add


@pytest.mark.parametrize("select_profile", [False, True])
def test_cannot_remove_only_saved_profile(network_cli, select_profile):
    run, add = network_cli
    add("local")
    if select_profile:
        run("use", "local")

    error = run("remove", "local", succeeds=False)

    assert error["code"] == "last_network_profile"
    assert [profile["name"] for profile in run("list")["networks"]] == ["local"]


def test_select_another_profile_before_removing_the_active_profile(network_cli):
    run, add = network_cli
    add("local")
    add("other")
    run("use", "local")

    error = run("remove", "local", succeeds=False)

    assert error["code"] == "active_network_profile"
    assert "network use" in error["message"]
    assert [profile["name"] for profile in run("list")["networks"]] == ["local", "other"]
    run("use", "other")
    assert run("remove", "local")["removed"] == "local"
    assert [profile["name"] for profile in run("list")["networks"]] == ["other"]
    assert run("use", "local", succeeds=False)["code"] == "record_not_found"


def test_missing_profile_reports_record_not_found(network_cli):
    run, add = network_cli
    add("local")

    assert run("remove", "unknown", succeeds=False)["code"] == "record_not_found"
