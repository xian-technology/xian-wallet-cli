import pytest


@pytest.fixture(autouse=True)
def isolated_wallet_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XIAN_WALLET_HOME", str(tmp_path / "isolated-state"))
