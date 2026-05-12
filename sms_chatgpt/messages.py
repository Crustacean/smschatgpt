from __future__ import annotations

import unicodedata
from dataclasses import dataclass


SMS_REPLY_LIMIT = 140
SMS_INBOUND_LIMIT = 1000


@dataclass(frozen=True)
class SmsMessage:
    sender: str
    body: str
    index: int | None = None


class SmsValidationError(ValueError):
    pass


def clamp_sms_reply(text: str, limit: int = SMS_REPLY_LIMIT) -> str:
    limit = max(1, limit)
    compact = " ".join(strip_control_chars(text).split())
    if len(compact) <= limit:
        return compact
    if limit <= 3:
        return compact[:limit]
    return compact[: limit - 3].rstrip() + "..."


def strip_control_chars(text: str) -> str:
    return "".join(
        character
        if character in "\n\r\t" or not unicodedata.category(character).startswith("C")
        else " "
        for character in text
    )


def validate_inbound_sms(text: str, limit: int = SMS_INBOUND_LIMIT) -> str:
    limit = max(1, limit)
    sanitized = " ".join(strip_control_chars(text).split())
    if not sanitized:
        raise SmsValidationError("SMS message is empty after removing control characters.")
    if len(sanitized) > limit:
        raise SmsValidationError(f"SMS message is too long; limit is {limit} characters.")
    return sanitized


def inbound_validation_reply(error: SmsValidationError, reply_limit: int = SMS_REPLY_LIMIT) -> str:
    return clamp_sms_reply(str(error), reply_limit)


def sms_response_instruction(limit: int = SMS_REPLY_LIMIT) -> str:
    return (
        f"Reply in {limit} characters or fewer. Use a complete SMS-ready answer "
        "that fits within the limit; do not rely on truncation."
    )


def max_tokens_for_sms_limit(limit: int = SMS_REPLY_LIMIT) -> int:
    return max(24, min(512, int(limit * 0.75)))
