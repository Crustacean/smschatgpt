from __future__ import annotations

import abc
from collections.abc import Sequence

from .messages import SMS_REPLY_LIMIT, clamp_sms_reply, max_tokens_for_sms_limit, sms_response_instruction

ChatMessage = dict[str, str]
SYSTEM_PROMPT = "Be helpful, concise, and SMS-ready."


class LlmClient(abc.ABC):
    @abc.abstractmethod
    def respond(
        self,
        message: str,
        history: Sequence[ChatMessage] | None = None,
        reply_limit: int = SMS_REPLY_LIMIT,
    ) -> str:
        """Return a short answer for an SMS chat."""

    @abc.abstractmethod
    def complete(
        self,
        messages: Sequence[ChatMessage],
        max_tokens: int = 160,
        temperature: float = 0.4,
    ) -> str:
        """Return raw model text for non-chat workflows."""


class EchoLlmClient(LlmClient):
    def respond(
        self,
        message: str,
        history: Sequence[ChatMessage] | None = None,
        reply_limit: int = SMS_REPLY_LIMIT,
    ) -> str:
        del history
        return clamp_sms_reply(f"Echo: {message}", reply_limit)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        max_tokens: int = 160,
        temperature: float = 0.4,
    ) -> str:
        del max_tokens, temperature
        return messages[-1]["content"] if messages else ""


class OpenAiLlmClient(LlmClient):
    def __init__(self, api_key: str | None, model: str) -> None:
        from openai import OpenAI

        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def respond(
        self,
        message: str,
        history: Sequence[ChatMessage] | None = None,
        reply_limit: int = SMS_REPLY_LIMIT,
    ) -> str:
        messages: list[ChatMessage] = [
            {"role": "system", "content": f"{SYSTEM_PROMPT} {sms_response_instruction(reply_limit)}"}
        ]
        messages.extend(_valid_history(history or []))
        messages.append({"role": "user", "content": message})
        return clamp_sms_reply(
            self.complete(messages, max_tokens=max_tokens_for_sms_limit(reply_limit), temperature=0.4),
            reply_limit,
        )

    def complete(
        self,
        messages: Sequence[ChatMessage],
        max_tokens: int = 160,
        temperature: float = 0.4,
    ) -> str:
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=list(messages),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return completion.choices[0].message.content or ""


def build_llm_client(provider: str, api_key: str | None, model: str) -> LlmClient:
    if provider == "echo":
        return EchoLlmClient()
    if provider == "openai":
        return OpenAiLlmClient(api_key, model)
    raise ValueError(f"Unsupported LLM_PROVIDER={provider!r}")


def _valid_history(history: Sequence[ChatMessage]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    return messages
