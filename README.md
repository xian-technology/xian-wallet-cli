# Xian Wallet CLI

`xian-wallet` prepares, reviews, signs, and submits native XIAN transfers on
a localnet. It uses Typer for commands, Rich for inline previews, Textual
for keyboard-driven review, and `xian-tech-py` for blockchain operations.

## Install

From the wallet checkout, use Python 3.14 and uv. The wallet runs on macOS and
Linux and uses owner-only POSIX key files.

```sh
uv sync
uv run xian-wallet --help
```

Use `uv run xian-wallet` in the checkout or install the checkout with
`uv tool install .` and use `xian-wallet` directly.

## Send on localnet

Set `XIAN_WALLET_RPC` or pass `--rpc` before the command to select a loopback
RPC. The default is `http://127.0.0.1:26657`. The node must be synced and able
to serve block 1.

```sh
xian-wallet generate --out owner.key
xian-wallet balance "$SENDER_ADDRESS"
xian-wallet prepare --from "$SENDER_ADDRESS" --to "$RECIPIENT_ADDRESS" \
  --amount 1.25 --out proposal.json
xian-wallet ui proposal.json
xian-wallet approve proposal.json --key-file owner.key --out approval.json --textual
xian-wallet submit proposal.json --approval approval.json
```

Use the public address printed by `generate` as `SENDER_ADDRESS` and fund it
on the localnet before preparing the transfer. Set `RECIPIENT_ADDRESS` to the
recipient's public address. Use `preview` for inline review or omit
`--textual` to approve from an inline Rich prompt. Keep the owner key file
private and backed up. File outputs are written once and use mode `0600`.

## Approve through a messaging client

1. The agent runs `xian-wallet --json prepare ... --out proposal.json`. Send
   the user a review with the full proposal ID, amount, asset, sender, recipient,
   network, chi limit, and expiry. The approval bridge retains that exact
   proposal and matches the authenticated user's response to its ID.
2. After approval, the trusted signer runs
   `xian-wallet --json approve proposal.json --key-file owner.key --out approval.json --yes`.
   This can run offline on the owner's machine or in the trusted integration.
   The integration must authenticate the user and enforce the approval before
   using `--yes`. It returns the signed approval to the agent.
3. The agent runs
   `xian-wallet --json submit proposal.json --approval approval.json`, then
   follows any pending outcome using
   `xian-wallet --json receipt "$TX_HASH"`. Both agent commands run without
   prompts or access to the spending key.

The signed approval authorizes one native transfer. Anyone who receives it can
relay that specific transaction. The CLI checks expiry before broadcasting.
The on-chain transaction has a nonce and chain ID, and its signature itself
does not expire. Keep the approval within the intended handoff. The wallet
works with any messaging bridge that implements these steps.

## JSON and retries

Put `--json` before the subcommand. Machine responses use schema
`xian-wallet-cli/v1` with a single `data` or `error` object on stdout.
Amounts are decimal strings. CLI usage errors such as missing arguments go
to stderr.

| Exit | Meaning |
| ---- | ------- |
| 0 | Command succeeded, or transfer committed successfully |
| 1 | Node rejected admission or confirmed a failed execution |
| 2 | Command, input, approval, file, or RPC error |
| 3 | Final outcome is pending or unknown |

Use the hash in the approval or result to recover after an interrupted
submission. A repeat submission with the same files uses the same transaction
bytes and first checks for an existing receipt. A changed recipient, network,
amount, chi limit, or nonce requires a new proposal and approval. Chi values
describe computation and transaction limits. A finalized receipt is the
source of truth for execution; simulation reflects the state at preparation.

## Validation

```sh
uv run ruff check
uv run ruff format --check
uv run pytest
```

## Related Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Backlog](docs/BACKLOG.md)
- [Xian Python SDK](../xian-py/README.md)
- [Agent wallet skill](../xian-ai-skills/xian-wallet-cli-skill/SKILL.md)
