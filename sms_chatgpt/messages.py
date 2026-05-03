from __future__ import annotations

from dataclasses import dataclass


SMS_REPLY_LIMIT = 140


@dataclass(frozen=True)
class SmsMessage:
    sender: str
    body: str
    index: int | None = None


def clamp_sms_reply(text: str, limit: int = SMS_REPLY_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."
