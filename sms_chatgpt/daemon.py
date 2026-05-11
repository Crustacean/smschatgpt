from __future__ import annotations

import logging
import os
import signal
import time

from .config import load_settings
from .llm import build_llm_client
from .messages import SMS_REPLY_LIMIT, SmsValidationError, clamp_sms_reply, inbound_validation_reply, validate_inbound_sms
from .sms import AdbSmsTransport, AtModemSmsTransport, MockSmsTransport, SmsTransport

LOGGER = logging.getLogger(__name__)
SHOULD_STOP = False


def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    settings = load_settings()
    sms = _build_sms_transport(settings)
    chat_manager = _build_chat_manager(settings)
    poll_manager = _build_poll_manager(settings)

    LOGGER.info("Starting SMS ChatGPT daemon with %s SMS backend", settings.sms_backend)
    while not SHOULD_STOP:
        try:
            _send_poll_results(poll_manager, sms)
            for message in sms.receive_unread():
                LOGGER.info("Received SMS from %s", message.sender)
                try:
                    body = validate_inbound_sms(message.body, settings.sms_inbound_limit)
                    poll_response = poll_manager.handle_message(message.sender, body) if poll_manager else None
                    if poll_response and poll_response.handled:
                        reply = poll_response.reply or ""
                    else:
                        reply = chat_manager.ask(message.sender, body)
                except SmsValidationError as exc:
                    reply = inbound_validation_reply(exc, settings.sms_reply_limit)
                except Exception:
                    LOGGER.exception("Message handling failed for sender %s", message.sender)
                    reply = "Sorry, I could not answer right now."
                if reply:
                    sms.send_sms(message.sender, clamp_sms_reply(reply, settings.sms_reply_limit))
                sms.ack(message)
            chat_manager.cleanup_idle_pods()
            _send_poll_results(poll_manager, sms)
        except Exception:
            LOGGER.exception("Daemon loop failed")
        time.sleep(settings.sms_poll_seconds)


def _build_sms_transport(settings) -> SmsTransport:
    if settings.sms_backend == "mock":
        return MockSmsTransport(settings.mock_inbox_file, settings.mock_outbox_file, settings.sms_reply_limit)
    if settings.sms_backend == "at":
        return AtModemSmsTransport(
            settings.sms_serial_port,
            settings.sms_baudrate,
            settings.sms_message_status,
            settings.sms_storage,
            settings.sms_reply_limit,
        )
    if settings.sms_backend == "adb":
        return AdbSmsTransport(
            settings.adb_path,
            settings.adb_serial,
            settings.adb_send_mode,
            settings.adb_send_command_template,
            settings.adb_state_file,
            settings.adb_skip_existing,
            settings.sms_reply_limit,
        )
    raise ValueError(f"Unsupported SMS_BACKEND={settings.sms_backend!r}")


def _build_chat_manager(settings):
    if settings.session_backend == "kubernetes":
        from .k8s import ChatPodManager

        return ChatPodManager(settings)
    if settings.session_backend == "local":
        return LocalChatManager(settings)
    raise ValueError(f"Unsupported SESSION_BACKEND={settings.session_backend!r}")


def _build_poll_manager(settings):
    if not settings.poll_enabled:
        return None
    if settings.session_backend == "kubernetes":
        from .k8s import PollPodManager

        return PollPodManager(settings)
    if settings.session_backend == "local":
        from .poll_manager import LocalPollManager

        return LocalPollManager(settings)
    return None


def _send_poll_results(poll_manager, sms: SmsTransport) -> None:
    if not poll_manager:
        return
    outbound_messages = poll_manager.close_expired()
    if not outbound_messages:
        return
    reply_limit = getattr(getattr(poll_manager, "settings", None), "sms_reply_limit", None)
    for outbound in outbound_messages:
        sms.send_sms(outbound.recipient, clamp_sms_reply(outbound.body, reply_limit or SMS_REPLY_LIMIT))
    poll_manager.ack_results_sent(outbound_messages)


class LocalChatManager:
    def __init__(self, settings) -> None:
        self.llm = build_llm_client(settings.llm_provider, settings.openai_api_key, settings.openai_model)
        self.reply_limit = settings.sms_reply_limit

    def ask(self, sender: str, message: str) -> str:
        del sender
        return self.llm.respond(message, reply_limit=self.reply_limit)

    def cleanup_idle_pods(self) -> None:
        return None


def _stop(signum, frame) -> None:
    del signum, frame
    global SHOULD_STOP
    SHOULD_STOP = True


if __name__ == "__main__":
    main()
