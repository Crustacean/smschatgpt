from __future__ import annotations

import os
from dataclasses import dataclass

from .messages import SMS_INBOUND_LIMIT, SMS_REPLY_LIMIT

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> None:
        return None


@dataclass(frozen=True)
class Settings:
    sms_backend: str
    sms_serial_port: str
    sms_baudrate: int
    sms_poll_seconds: float
    sms_message_status: str
    sms_storage: str | None
    sms_reply_limit: int
    sms_inbound_limit: int
    adb_path: str
    adb_serial: str | None
    adb_send_mode: str
    adb_send_command_template: str | None
    adb_state_file: str
    adb_skip_existing: bool
    session_backend: str
    kubernetes_namespace: str
    chat_pod_image: str
    chat_pod_idle_seconds: int
    chat_pod_timeout_seconds: int
    chat_history_file: str
    chat_history_max_turns: int
    poll_enabled: bool
    poll_keywords: list[str]
    poll_state_file: str
    poll_pod_name: str
    poll_hash_salt: str
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
        sms_message_status=os.getenv("SMS_MESSAGE_STATUS", "REC UNREAD"),
        sms_storage=os.getenv("SMS_STORAGE") or None,
        sms_reply_limit=int(os.getenv("SMS_REPLY_LIMIT", str(SMS_REPLY_LIMIT))),
        sms_inbound_limit=int(os.getenv("SMS_INBOUND_LIMIT", str(SMS_INBOUND_LIMIT))),
        adb_path=os.getenv("ADB_PATH", "adb"),
        adb_serial=os.getenv("ADB_SERIAL") or None,
        adb_send_mode=os.getenv("ADB_SEND_MODE", "compose").lower(),
        adb_send_command_template=os.getenv("ADB_SEND_COMMAND_TEMPLATE") or None,
        adb_state_file=os.getenv("ADB_STATE_FILE", "./adb-sms-state.txt"),
        adb_skip_existing=os.getenv("ADB_SKIP_EXISTING", "true").lower() in {"1", "true", "yes"},
        session_backend=os.getenv("SESSION_BACKEND", "kubernetes").lower(),
        kubernetes_namespace=os.getenv("KUBERNETES_NAMESPACE", "default"),
        chat_pod_image=os.getenv("CHAT_POD_IMAGE", "sms-chatgpt:latest"),
        chat_pod_idle_seconds=int(os.getenv("CHAT_POD_IDLE_SECONDS", "60")),
        chat_pod_timeout_seconds=int(os.getenv("CHAT_POD_TIMEOUT_SECONDS", "30")),
        chat_history_file=os.getenv("CHAT_HISTORY_FILE", "/tmp/sms-chatgpt-history.json"),
        chat_history_max_turns=int(os.getenv("CHAT_HISTORY_MAX_TURNS", "12")),
        poll_enabled=os.getenv("POLL_ENABLED", "true").lower() in {"1", "true", "yes"},
        poll_keywords=[
            keyword.strip().lower()
            for keyword in os.getenv("POLL_KEYWORDS", "poll,vote,voting").split(",")
            if keyword.strip()
        ],
        poll_state_file=os.getenv("POLL_STATE_FILE", "/tmp/sms-chatgpt-poll.json"),
        poll_pod_name=os.getenv("POLL_POD_NAME", "sms-poll-active"),
        poll_hash_salt=os.getenv("POLL_HASH_SALT", "dev-only-insecure-poll-salt"),
        llm_provider=os.getenv("LLM_PROVIDER", "openai").lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        mock_inbox_file=os.getenv("MOCK_INBOX_FILE", "./mock-inbox.txt"),
        mock_outbox_file=os.getenv("MOCK_OUTBOX_FILE", "./mock-outbox.txt"),
    )
