import json

from typer.testing import CliRunner
from xian_py.wallet import Wallet

from xian_wallet_cli.cli import app

PHRASE = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


def test_restore_derive_backup_and_receive_without_exposing_secrets(tmp_path):
    runner = CliRunner()
    home = tmp_path / "wallet"
    password = tmp_path / "password"
    password.write_text("local test vault password\n")
    password.chmod(0o600)
    seed = tmp_path / "mnemonic"
    seed.write_text(PHRASE)
    seed.chmod(0o600)

    def run(*args, state=home, code=0):
        result = runner.invoke(app, ["--json", "--home", str(state), *map(str, args)])
        assert result.exit_code == code, result.output
        assert PHRASE not in result.output and password.read_text().strip() not in result.output
        return json.loads(result.stdout)

    first = run("account", "restore", "main", "--mnemonic-file", seed, "--password-file", password)
    expected = Wallet.from_mnemonic(PHRASE).public_key
    assert first["data"]["address"] == expected
    derived = run("account", "derive", "main", "savings", "--index", 1, "--password-file", password)
    assert derived["data"]["address"] == Wallet.from_mnemonic(PHRASE, account_index=1).public_key
    assert run("receive", "main")["data"]["address"] == expected
    backup = tmp_path / "backup.json"
    run("account", "backup", "main", "--out", backup)
    assert PHRASE not in backup.read_text()
    assert backup.stat().st_mode & 0o777 == 0o600
    recovery = run(
        "account",
        "restore-backup",
        backup,
        "--password-file",
        password,
        state=tmp_path / "restored",
    )
    assert recovery["data"]["address"] == expected
    assert PHRASE.encode() not in (home / "wallet.sqlite3").read_bytes()
