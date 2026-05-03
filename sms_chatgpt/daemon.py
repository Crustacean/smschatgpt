from __future__ import annotations

import logging
import signal
import time

from .config import load_settings
from .k8s import ChatPodManager
from .llm import build_llm_client
from .messages import clamp_sms_reply
from .sms import AtModemSmsTransport, MockSmsTransport, SmsTransport

LOGGER = logging.getLogger(__name__)
SHOULD_STOP = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    settings = load_settings()
    sms = _build_sms_transport(settings)
    chat_manager = _build_chat_manager(settings)

    LOGGER.info("Starting SMS ChatGPT daemon with %s SMS backend", settings.sms_backend)
    while not SHOULD_STOP:
        try:
            for message in sms.receive_unread():
                LOGGER.info("Received SMS from %s", message.sender)
                try:
                    reply = chat_manager.ask(message.sender, message.body)
                except Exception:
                    LOGGER.exception("Chat session failed for sender %s", message.sender)
                    reply = "Sorry, I could not answer right now."
                sms.send_sms(message.sender, clamp_sms_reply(reply))
                sms.ack(message)
            chat_manager.cleanup_idle_pods()
        except Exception:
            LOGGER.exception("Daemon loop failed")
        time.sleep(settings.sms_poll_seconds)


def _build_sms_transport(settings) -> SmsTransport:
    if settings.sms_backend == "mock":
        return MockSmsTransport(settings.mock_inbox_file, settings.mock_outbox_file)
    if settings.sms_backend == "at":
        return AtModemSmsTransport(settings.sms_serial_port, settings.sms_baudrate)
    raise ValueError(f"Unsupported SMS_BACKEND={settings.sms_backend!r}")


def _build_chat_manager(settings):
    if settings.session_backend == "kubernetes":
        return ChatPodManager(settings)
    if settings.session_backend == "local":
        return LocalChatManager(settings)
    raise ValueError(f"Unsupported SESSION_BACKEND={settings.session_backend!r}")


class LocalChatManager:
    def __init__(self, settings) -> None:
        self.llm = build_llm_client(settings.llm_provider, settings.openai_api_key, settings.openai_model)

    def ask(self, sender: str, message: str) -> str:
        del sender
        return self.llm.respond(message)

    def cleanup_idle_pods(self) -> None:
        return None


def _stop(signum, frame) -> None:
    del signum, frame
    global SHOULD_STOP
    SHOULD_STOP = True
