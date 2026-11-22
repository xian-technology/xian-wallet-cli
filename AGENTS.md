# Repository Guidelines

- Start with [README.md](README.md) for commands and
  [architecture](docs/ARCHITECTURE.md) for signing, submission, and rendering.
- Follow the [workspace conventions](../xian-meta/docs/REPO_CONVENTIONS.md)
  and [change workflow](../xian-meta/docs/CHANGE_WORKFLOW.md).
- Reuse the published `xian-tech-py` transaction and wallet primitives. Keep
  approval files bound to exact proposal contents and transaction bytes.
- Keep secret keys out of command-line arguments, JSON, logs, and agent-facing
  commands. Validate changes against a disposable localnet.
- Run `uv run ruff check`, `uv run ruff format --check`, and `uv run pytest`.
  Update the wallet CLI skill and public docs when user workflows change.
- For codebase questions and graph updates, follow the shared
  [code graph workflow](../xian-meta/docs/CODE_GRAPH_WORKFLOW.md).
