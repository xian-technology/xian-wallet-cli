"""Authenticate one exact wallet approval through a Telegram bot's private chat."""

import re
import secrets

from aiohttp import ClientError, ClientSession, ClientTimeout

from .chain import now
from .domain import Proposal, WalletError
from .presentation import primary_detail, rows


def decision_text(plan: Proposal) -> str:
    fields = "\n".join(f"{label}: {value}" for label, value in rows(plan))
    return (
        f"Review {primary_detail(plan)}\n{fields}\n"
        f"First block: {plan.network.first_block_hash}\nProposal: {plan.proposal_id}"
    )


def callback_decision(query: dict, owner_id: int, message_id: int, challenge: str) -> bool | None:
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    if (
        query.get("from", {}).get("id") != owner_id
        or chat.get("id") != owner_id
        or chat.get("type") != "private"
        or message.get("message_id") != message_id
    ):
        return None
    data = query.get("data")
    if data == f"approve:{challenge}":
        return True
    if data == f"decline:{challenge}":
        return False
    return None


class Telegram:
    def __init__(self, token: str, owner_id: int):
        if not re.fullmatch(r"[0-9]{1,20}:[a-zA-Z0-9_-]{20,100}", token) or owner_id <= 0:
            raise WalletError(
                "invalid_telegram_config", "Use a valid bot token and private user ID."
            )
        self.url = f"https://api.telegram.org/bot{token}"
        self.owner_id = owner_id

    async def call(self, session: ClientSession, method: str, parameters: dict):
        try:
            async with session.post(
                f"{self.url}/{method}",
                json=parameters,
                allow_redirects=False,
            ) as response:
                result = await response.json()
        except (ClientError, TimeoutError, ValueError) as exc:
            raise WalletError(
                "telegram_unavailable", "Could not reach the Telegram bot API."
            ) from exc
        if not result.get("ok"):
            raise WalletError("telegram_unavailable", "The Telegram bot API rejected the request.")
        return result["result"]

    async def ask(self, plan: Proposal) -> bool:
        plan.require_unexpired(now())
        review = decision_text(plan)
        if len(review.encode("utf-16-le")) // 2 > 4096:
            raise WalletError(
                "telegram_review_too_large", "Review this proposal in the terminal inbox."
            )
        challenge = secrets.token_hex(16)
        async with ClientSession(timeout=ClientTimeout(total=35)) as session:
            message = await self.call(
                session,
                "sendMessage",
                {
                    "chat_id": self.owner_id,
                    "text": review,
                    "protect_content": True,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Approve exact proposal",
                                    "callback_data": f"approve:{challenge}",
                                },
                                {"text": "Decline", "callback_data": f"decline:{challenge}"},
                            ]
                        ]
                    },
                },
            )
            return await self.poll(session, message["message_id"], challenge, plan.expires_at)

    async def poll(self, session, message_id: int, challenge: str, expires_at: int) -> bool:
        offset = 0
        while (remaining := expires_at - now()) > 0:
            updates = await self.call(
                session,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": min(remaining, 15),
                    "allowed_updates": ["callback_query"],
                },
            )
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                query = update.get("callback_query") or {}
                decision = callback_decision(query, self.owner_id, message_id, challenge)
                if decision is not None:
                    await self.call(
                        session,
                        "answerCallbackQuery",
                        {
                            "callback_query_id": query["id"],
                            "text": "Decision received",
                        },
                    )
                    return decision
        raise WalletError("proposal_expired", "The proposal expired before a Telegram decision.")
