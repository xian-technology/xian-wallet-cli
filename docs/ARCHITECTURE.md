# Wallet CLI architecture

`domain.py` models the unsigned proposal and signed approval.
`chain.py` adapts the SDK and runs the localnet workflow.
`files.py` handles private keys and write-once JSON artifacts.
`cli.py` and `presentation.py` provide the headless, inline, and Textual
views over the same model.

A proposal describes a single `currency.transfer`. Its digest covers the
wire version, chain ID, first block hash, sender, recipient, exact decimal
amount, nonce, estimated chi, chi limit, creation time, and expiry. The
sender signs the SDK transaction payload and a domain-specific message over
the proposal digest. The CLI verifies both signatures and recomputes the
transaction hash before submitting. The first block binds the approval to a
specific localnet for CLI use.

The signer is the wallet owner or a trusted bridge. The agent needs the
sender's public address, the proposal, and the returned approval. The
messaging system is responsible for user authentication, matching an approval
to the displayed digest, and secure key custody. The signed transaction is
relayable, so its recipient and amount are fixed by the chain signature.
Expiry and first-block verification govern CLI use of the approval, and the
chain enforces the transaction's signed chain ID and nonce.

Submission checks the exact hash before broadcasting. The approval file
contains enough information to recover the same transaction after a process
restart, including a broadcast that succeeded but lost its response. A
pending outcome carries the same hash. Take care when proposing concurrent
transfers from the same account, since two proposals may capture the same
next nonce and only one can commit.

## Terminal review

The browser wallet's dark surfaces and green accent carry over to the
terminal. Both preview surfaces lead with the exact amount, recipient,
sender, chain, chi limit, and expiry. Long addresses are rendered in full.
Textual puts the proposal digest, first block hash, and signed payload fields
under transaction details. The signing screen requires an explicit action.
The JSON interface supplies the same decision fields to messaging clients.
