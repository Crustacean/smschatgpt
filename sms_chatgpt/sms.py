from __future__ import annotations

import abc
import logging
import os
import re
import time
from pathlib import Path

import serial

from .messages import SmsMessage, clamp_sms_reply

LOGGER = logging.getLogger(__name__)


class SmsTransport(abc.ABC):
    @abc.abstractmethod
    def receive_unread(self) -> list[SmsMessage]:
        """Return unread SMS messages."""

    @abc.abstractmethod
    def send_sms(self, phone_number: str, body: str) -> None:
        """Send one SMS."""

    def ack(self, message: SmsMessage) -> None:
        """Mark/delete a message once it has been handled."""


class MockSmsTransport(SmsTransport):
    def __init__(self, inbox_file: str, outbox_file: str) -> None:
        self.inbox = Path(inbox_file)
        self.outbox = Path(outbox_file)
        self.inbox.touch(exist_ok=True)
        self.outbox.touch(exist_ok=True)

    def receive_unread(self) -> list[SmsMessage]:
        lines = self.inbox.read_text(encoding="utf-8").splitlines()
        self.inbox.write_text("", encoding="utf-8")
        messages: list[SmsMessage] = []
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            sender, sep, body = line.partition("|")
            if not sep:
                LOGGER.warning("Skipping malformed mock SMS line %s", line_no)
                continue
            messages.append(SmsMessage(sender=sender.strip(), body=body.strip()))
        return messages

    def send_sms(self, phone_number: str, body: str) -> None:
        body = clamp_sms_reply(body)
        with self.outbox.open("a", encoding="utf-8") as handle:
            handle.write(f"{phone_number}|{body}{os.linesep}")


class AtModemSmsTransport(SmsTransport):
    message_header = re.compile(r'^\+CMGL:\s*(\d+),".*?","([^"]+)"')

    def __init__(self, port: str, baudrate: int) -> None:
        self.serial = serial.Serial(port=port, baudrate=baudrate, timeout=2)
        self._command("AT")
        self._command("AT+CMGF=1")

    def receive_unread(self) -> list[SmsMessage]:
        response = self._command('AT+CMGL="REC UNREAD"', wait=1.5)
        messages: list[SmsMessage] = []
        current_index: int | None = None
        current_sender: str | None = None
        current_body: list[str] = []

        for raw_line in response:
            line = raw_line.strip()
            header = self.message_header.match(line)
            if header:
                if current_index is not None and current_sender is not None:
                    messages.append(
                        SmsMessage(current_sender, "\n".join(current_body).strip(), current_index)
                    )
                current_index = int(header.group(1))
                current_sender = header.group(2)
                current_body = []
                continue
            if line and line != "OK" and current_index is not None:
                current_body.append(line)

        if current_index is not None and current_sender is not None:
            messages.append(SmsMessage(current_sender, "\n".join(current_body).strip(), current_index))
        return messages

    def send_sms(self, phone_number: str, body: str) -> None:
        body = clamp_sms_reply(body)
        self.serial.write(f'AT+CMGS="{phone_number}"\r'.encode("utf-8"))
        time.sleep(0.5)
        self.serial.write(body.encode("utf-8") + b"\x1a")
        time.sleep(3)
        lines = self._read_available()
        if not any("+CMGS:" in line for line in lines):
            raise RuntimeError(f"Modem did not confirm SMS send: {lines}")

    def ack(self, message: SmsMessage) -> None:
        if message.index is not None:
            self._command(f"AT+CMGD={message.index}")

    def _command(self, command: str, wait: float = 0.25) -> list[str]:
        self.serial.reset_input_buffer()
        self.serial.write(f"{command}\r".encode("utf-8"))
        time.sleep(wait)
        lines = self._read_available()
        if any("ERROR" in line for line in lines):
            raise RuntimeError(f"Modem command failed: {command}: {lines}")
        return lines

    def _read_available(self) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            line = self.serial.readline()
            if not line:
                break
            lines.append(line.decode("utf-8", errors="replace").strip())
        return lines
