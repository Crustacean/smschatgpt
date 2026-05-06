from __future__ import annotations

import abc
from collections.abc import Sequence

from .messages import clamp_sms_reply

ChatMessage = dict[str, str]
SYSTEM_PROMPT = "Reply to SMS users in 140 characters or fewer. Be helpful and concise."


class LlmClient(abc.ABC):
    @abc.abstractmethod
    def respond(self, message: str, history: Sequence[ChatMessage] | None = None) -> str:
        """Return a short answer for an SMS chat."""


class EchoLlmClient(LlmClient):
    def respond(self, message: str, history: Sequence[ChatMessage] | None = None) -> str:
        del history
        return clamp_sms_reply(f"Echo: {message}")


class OpenAiLlmClient(LlmClient):
    def __init__(self, api_key: str | None, model: str) -> None:
        from openai import OpenAI

        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def respond(self, message: str, history: Sequence[ChatMessage] | None = None) -> str:
        messages: list[ChatMessage] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(_valid_history(history or []))
        messages.append({"role": "user", "content": message})
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=80,
            temperature=0.4,
        )
        text = completion.choices[0].message.content or ""
        return clamp_sms_reply(text)


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
