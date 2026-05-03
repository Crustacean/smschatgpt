from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    sms_backend: str
    sms_serial_port: str
    sms_baudrate: int
    sms_poll_seconds: float
    session_backend: str
    kubernetes_namespace: str
    chat_pod_image: str
    chat_pod_idle_seconds: int
    chat_pod_timeout_seconds: int
    llm_provider: str
    openai_api_key: str | None
    openai_model: str
    mock_inbox_file: str
    mock_outbox_file: str


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        sms_backend=os.getenv("SMS_BACKEND", "mock").lower(),
        sms_serial_port=os.getenv("SMS_SERIAL_PORT", "/dev/ttyUSB0"),
        sms_baudrate=int(os.getenv("SMS_BAUDRATE", "115200")),
        sms_poll_seconds=float(os.getenv("SMS_POLL_SECONDS", "5")),
        session_backend=os.getenv("SESSION_BACKEND", "kubernetes").lower(),
        kubernetes_namespace=os.getenv("KUBERNETES_NAMESPACE", "default"),
        chat_pod_image=os.getenv("CHAT_POD_IMAGE", "sms-chatgpt:latest"),
        chat_pod_idle_seconds=int(os.getenv("CHAT_POD_IDLE_SECONDS", "60")),
        chat_pod_timeout_seconds=int(os.getenv("CHAT_POD_TIMEOUT_SECONDS", "30")),
        llm_provider=os.getenv("LLM_PROVIDER", "openai").lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        mock_inbox_file=os.getenv("MOCK_INBOX_FILE", "./mock-inbox.txt"),
        mock_outbox_file=os.getenv("MOCK_OUTBOX_FILE", "./mock-outbox.txt"),
    )
