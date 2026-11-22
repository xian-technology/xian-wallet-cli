# Wallet workflows

## Automation conventions

Use a wallet home for the trusted signer and a separate one for the agent.
Put global flags such as `--home`, `--network`, and `--json` before the
command. The sender argument accepts a named account or public address.
The recipient accepts a contact or a public address. Use the contract name
as the asset identifier in commands, even when a watchlist entry uses a
friendly display name.

Retain the proposal and approval files until finality. A proposal is a
snapshot of the current nonce, simulation and fee ceiling. Prepare a fresh
proposal for changed parameters or a stale nonce. After an ambiguous RPC
response, resolve the original hash before preparing a replacement.

## Agent approvals

On the trusted signer host, save a random 32-byte hex request token to an
owner-only file with `umask 077`. Provide a dedicated copy to the agent.
Start the signer with a Unix socket in an owner-controlled directory:

```sh
xian-wallet agent serve --socket "$SIGNER_SOCKET" --request-token-file request.token
```

The owner can review new requests from `xian-wallet ui`, or run
`inbox review PROPOSAL_ID --account OWNER`. With a dedicated Telegram bot,
let the owner open the bot's private chat before enabling the connection.
Add `--telegram-token-file BOT_TOKEN`, `--telegram-owner-id USER_ID`,
`--telegram-account OWNER`, and `--password-file PASSWORD_FILE` to
`agent serve` for automatic review. Each Telegram decision is bound to the
authenticated user's private chat and the message for the exact proposal.
For a one-off review, use `inbox telegram PROPOSAL_ID --account OWNER
--token-file BOT_TOKEN --owner-id USER_ID`. Large contract calls can use
the Textual inbox.

On the agent side, point `SIGNER_SOCKET` at an SSH-forwarded copy of the
signer's socket when running under a different identity or on another host:

```sh
xian-wallet --json agent request proposal.json --socket "$SIGNER_SOCKET" --token-file request.token
xian-wallet --json agent fetch "$PROPOSAL_ID" --socket "$SIGNER_SOCKET" --token-file request.token --out approval.json
xian-wallet --json submit proposal.json --approval approval.json
```

Poll `fetch` until the status is `approved` or `declined`, or the proposal
expires. The request token gives access to the inbox for that service,
including its signed handoffs. Use a dedicated signer instance for an agent
trust group, and share the grant credential only with the authorized agent.

For unattended recurring transfers, the owner runs:

```sh
xian-wallet --network local grant create payroll --account primary \
  --recipient "$RECIPIENT" --contract currency --tx-limit 1.25 --total-limit 10 \
  --max-chi 1000 --total-chi 10000 --seconds 3600 --out payroll.token
```

Run the signer with `--password-file` to enable grant signing. Give the agent
only the grant token, which works with `agent authorize payroll PROPOSAL
--socket SOCKET --token-file payroll.token --out approval.json`.
`grant list` shows used amount and chi. `grant revoke payroll` stops future
signing. A previously issued approval remains relayable by anyone holding
it, and the chain consumes its nonce once. Budgets charge at signing time,
so an unsubmitted or failed transaction still counts against the grant.

The owner can sign a competing proposal with `approve --replaces OLD_HASH`
after resolving the old receipt. Both signed transactions can be relayed,
and only one can consume the nonce. Keep that operation owner-controlled.

## DEX and NFTs

`dex quote TOKEN_IN TOKEN_OUT AMOUNT --from ACCOUNT` quotes an exact-input
single-pair swap. Use `dex allowance TOKEN_IN AMOUNT --from ACCOUNT --out
allowance.json` to review the amount the router can spend. An amount of zero
revokes that on-chain allowance. After the allowance finalizes, use
`dex prepare TOKEN_IN TOKEN_OUT AMOUNT --from ACCOUNT --to RECIPIENT
--slippage-bps BPS --out swap.json`. Its preview includes the exact amount,
minimum output, recipient, pair and deadline. The current flow uses
standard-transfer tokens on the direct router pair.

Use `nft inspect COLLECTION TOKEN_ID` to read ownership and metadata.
`nft transfer COLLECTION TOKEN_ID --from ACCOUNT --to RECIPIENT --out
nft.json` uses the same approval flow as a fungible transfer.

## Message signing and WalletConnect

`message sign MESSAGE_FILE --account ACCOUNT --out signature.json` displays
the UTF-8 message and signs the browser-compatible Xian chain/account
envelope. `message verify MESSAGE_FILE signature.json` verifies it locally.

Install the optional transport with `walletconnect setup` on Node.js 22 or
later, set `XIAN_WALLETCONNECT_PROJECT_ID` to your Reown project ID, and
run `walletconnect pair --account ACCOUNT --network PROFILE`. Pairing
requests identify scope, chain and account. Terminal confirmation is
required to connect and to sign each write. The app pairs for one selected
chain and account. The scope includes account reads, message signing, and
preparing, signing or sending transactions and contract calls. Ctrl+C
disconnects the active sessions.

## x402 payments

Use `x402 quote URL --from ACCOUNT --to PAYEE --asset TOKEN
--max-amount AMOUNT --settlement CONTRACT --out offer.json` to fetch an
exact HTTP 402 challenge. The command matches the payee, chain, asset,
amount ceiling, settlement contract and exact HTTPS or loopback resource.
Approve with `x402 approve offer.json --account ACCOUNT --out payment.json`.
The agent runs `x402 submit offer.json payment.json --out response.data`.
The same signed payment ID can be retried if the resource response is lost.
This flow uses GET resources and the SDK's Xian x402 v2 exact profile.

## Shielded notes

Install the optional proof SDK with `uv tool install 'xian-tech-wallet-cli[shielded]'`.
Select the profile for a chain with the zk feature, a deployed shielded-note
contract, its matching registered proof bundle, and BDS. Keep encrypted
snapshots and recovery seeds with the owner.

- `shielded create CONTRACT --out snapshot.json` creates a shielded account.
  Use `--seed-file` to recover one. `shielded export-recovery` writes an
  owner-only seed file. `shielded receive SNAPSHOT --out recipient.json`
  exports only public shielded receiving keys.
- `shielded sync SNAPSHOT --out synced.json` scans indexed output records,
  updates spent status and writes an encrypted replacement snapshot.
- `shielded prepare ACTION SNAPSHOT --from ACCOUNT --amount UNITS
  --bundle TRUSTED_BUNDLE --out proposal.json` reviews the private terms
  and proves locally. Actions are `deposit`, `transfer`, and `withdraw`.
  For `transfer`, `--to` is a public shielded recipient file. For
  `withdraw`, `--to` is a public Xian address or saved contact.
  The resulting proposal carries public proof data. Use the normal
  transaction approval and receipt workflow to submit it.

Direct shielded transactions can expose the transaction origin. The input
notes and shielded recipient are reviewed on the owner machine before
proving. Resync after commitment or when recovering from an older snapshot.
