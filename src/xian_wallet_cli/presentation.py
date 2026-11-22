"""The shared transaction review for terminal and full-screen use."""

import json
from datetime import UTC, datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Collapsible, Footer, Static

from .chain import now
from .domain import Plan, Proposal

ACCENT = "#adff2f"


def expiry_text(plan: Proposal) -> str:
    return datetime.fromtimestamp(plan.expires_at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def reviewed_action(plan: Proposal) -> tuple[str, str, list[tuple[str, str]]]:
    if isinstance(plan, Plan):
        return "Review transfer", f"{plan.amount} XIAN", [("To", plan.recipient)]
    args = plan.payload["kwargs"]
    if plan.contract == "con_dex" and plan.function == "swapExactTokenForToken":
        return (
            "Review swap",
            f"{wire_amount(args.get('amountIn'))} {args.get('src')}",
            [
                ("Minimum received", wire_amount(args.get("amountOutMin"))),
                ("Recipient", str(args.get("to"))),
                ("Pair", str(args.get("pair"))),
            ],
        )
    if plan.function == "approve" and set(args) == {"amount", "to"}:
        return (
            "Review allowance",
            f"{wire_amount(args['amount'])} {plan.contract}",
            [
                ("Spender", str(args["to"])),
            ],
        )
    if plan.function == "transfer" and set(args) == {"token_id", "to"}:
        return (
            "Review NFT transfer",
            f"{plan.contract} / {args['token_id']}",
            [
                ("To", str(args["to"])),
            ],
        )
    if plan.function == "transfer" and set(args) == {"amount", "to"}:
        return (
            "Review token transfer",
            f"{wire_amount(args['amount'])} {plan.contract}",
            [
                ("To", str(args["to"])),
            ],
        )
    return "Review contract call", f"{plan.contract}.{plan.function}", []


def wire_amount(value) -> str:
    if isinstance(value, dict) and set(value) == {"__fixed__"}:
        return str(value["__fixed__"])
    return str(value)


def action_title(plan: Proposal) -> str:
    return reviewed_action(plan)[0]


def primary_detail(plan: Proposal) -> str:
    return reviewed_action(plan)[1]


def rows(plan: Proposal) -> list[tuple[str, str]]:
    scope = [
        ("From", plan.sender),
        ("Network", plan.network.chain_id),
        ("Chi limit", f"{plan.chi_limit:,} (est. {plan.chi_estimate:,})"),
        ("Expires", expiry_text(plan)),
    ]
    _, _, details = reviewed_action(plan)
    if isinstance(plan, Plan):
        return [*details, *scope]
    arguments = json.dumps(plan.payload["kwargs"], ensure_ascii=True, indent=2)
    return [*details, *scope, ("Arguments", arguments)]


def preview_data(plan: Proposal) -> dict:
    data = {
        **plan.model_dump(mode="json"),
        "proposal_id": plan.proposal_id,
        "action": f"{plan.payload['contract']}.{plan.payload['function']}",
        "expired": now() >= plan.expires_at,
        "payload": plan.payload,
    }
    data["review"] = {
        "title": action_title(plan),
        "summary": primary_detail(plan),
        "fields": dict(rows(plan)),
    }
    if isinstance(plan, Plan):
        data["asset"] = "XIAN"
    return data


def render_preview(console: Console, plan: Proposal) -> None:
    fields = [
        (
            "Amount" if isinstance(plan, Plan) else "Call",
            Text(primary_detail(plan), style=f"bold {ACCENT}"),
        ),
        *((label, Text(value, overflow="fold")) for label, value in rows(plan)),
        ("Proposal ID", Text(plan.proposal_id, overflow="fold")),
    ]
    table = Table.grid(padding=(0, 2))
    if console.width >= 85:
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        for label, value in fields:
            table.add_row(label, value)
    else:
        table.add_column(overflow="fold")
        for label, value in fields:
            if len(value) > console.width - 18:
                table.add_row(Text(label, style="dim"))
                table.add_row(value)
            else:
                table.add_row(Text.assemble((f"{label}  ", "dim"), value))
    label = "Expired" if now() >= plan.expires_at else action_title(plan)
    console.print(Panel(table, title=label, title_align="left", border_style=ACCENT))


class ReviewApp(App[bool]):
    TITLE = "Xian Wallet"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("escape", "cancel", "Close")]
    CSS = """
    Screen { background: #000000; color: #ffffff; }
    VerticalScroll { height: 1fr; padding: 1 2; }
    .title { color: #adff2f; text-style: bold; }
    .amount { text-style: bold; margin: 1 0; }
    .label { color: #a0a0a0; }
    .value { height: auto; margin-bottom: 1; }
    .attribute { height: auto; }
    .expiry { margin-bottom: 1; }
    Collapsible { background: #111111; border: round #4d7f00; height: auto; }
    #actions { height: 3; padding: 0 2; background: #111111; }
    #actions Button { margin-right: 1; min-width: 0; width: auto; padding: 0 2; }
    #sign { background: #adff2f; color: #000000; }
    #sign:focus { text-style: bold underline; }
    Footer { background: #111111; color: #ffffff; }
    """

    def __init__(self, plan: Proposal, *, allow_sign: bool = False):
        super().__init__()
        self.plan = plan
        self.allow_sign = allow_sign

    def compose(self) -> ComposeResult:
        expired = now() >= self.plan.expires_at
        with VerticalScroll():
            yield Static(f"Xian Wallet  /  {action_title(self.plan)}", classes="title")
            yield Static(Text(primary_detail(self.plan), overflow="fold"), classes="amount")
            fields = rows(self.plan)
            identities, attributes = fields[:2], fields[2:]
            for label, value in identities:
                yield Static(label, classes="label")
                yield Static(Text(value, overflow="fold"), classes="value")
            for label, value in attributes:
                classes = "attribute expiry" if label == "Expires" else "attribute"
                yield Static(Text.assemble((f"{label}  ", "#a0a0a0"), value), classes=classes)
            if expired:
                yield Static("This proposal has expired.", classes="value")
            with Collapsible(title="Transaction details"):
                yield Static("Proposal ID", classes="label")
                yield Static(Text(self.plan.proposal_id, overflow="fold"), classes="value")
                yield Static("First block hash", classes="label")
                yield Static(
                    Text(self.plan.network.first_block_hash, overflow="fold"), classes="value"
                )
                yield Static(
                    Text(json.dumps(self.plan.payload, indent=2), overflow="fold"),
                    classes="value",
                )
        with Horizontal(id="actions"):
            if self.allow_sign:
                yield Button("Approve and sign", id="sign", disabled=expired)
            yield Button("Close", id="close")
        yield Footer()

    @on(Button.Pressed, "#sign")
    def sign(self) -> None:
        self.exit(True)

    @on(Button.Pressed, "#close")
    def action_cancel(self) -> None:
        self.exit(False)
