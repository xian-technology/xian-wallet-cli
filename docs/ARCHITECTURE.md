# Wallet CLI architecture

`domain.py` models the original transfer proposal and the typed contract
proposal. Both commit to a network identity, nonce, chi limit, exact payload,
and expiry. The signer produces a domain-bound approval and the exact SDK
transaction signature. Signing and submission verify that identity and the
transaction hash. The original v1 native transfer messages retain their
wire shape.

`state.py` owns the local SQLite transaction boundary. Public account
metadata, contacts, profiles and watched assets are separate from custody.
`vault.py` encrypts account secrets with Argon2id and XChaCha20-Poly1305.
Authenticated data binds each secret to its public account, derivation
index, and backup version. Plaintext recovery stays in owner-only files or
in memory for the active operation. A watch-only account has no secret.

`activity.py` records transaction hashes, receipts and signing reservations.
The signer serializes signing by network, address and nonce. Revocable
grant counters and the exact signing reservation commit in one database
transaction. The journal captures the transaction hash before broadcast so
it remains available on restart. SDK committed receipts override pending
attempts. An explicit owner replacement names the previous transaction
hash and competes for the same nonce.

`agent_cli.py` exposes request and approval exchange over a Unix socket.
The owner configures authentication and authorizes the action through a
terminal inbox, the Telegram bridge or a preauthorized transfer grant.
The agent receives the signed artifact and owns submission and recovery.
The signer database and password live under the signer’s OS authority.
Revoking a grant prevents future signing and the chain enforces replay
protection on released signatures.

`chain.py` adapts the Xian SDK for simulation, identity, balance, event
reads and finality. Public HTTPS RPCs use an explicit saved identity.
`chain_apps.py`, `payments.py`, and `shielded.py` own reviewed product
operations. x402 keeps its independent permit nonce ledger. Shielded
operations use the native proof toolkit and encrypted local snapshots.
The optional WalletConnect sidecar validates session scope and invokes
the same CLI review and signer for each write.

Rich previews, the Textual wallet and the remote review format use
the same proposal structure. The UI borrows the browser wallet's dark
surfaces and green accent. Accounts expose receiving addresses and a QR,
activity preserves status by transaction hash, and each approval presents
the exact call and limits before signing.
