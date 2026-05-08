from __future__ import annotations

import abc
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

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


class AdbSmsTransport(SmsTransport):
    row_prefix = "Row: "
    content_field = re.compile(r"(?:(?<=^)|(?<=, ))(_id|address|body|read)=")

    def __init__(
        self,
        adb_path: str = "adb",
        serial: str | None = None,
        send_mode: str = "compose",
        send_command_template: str | None = None,
        state_file: str = "./adb-sms-state.txt",
        skip_existing: bool = True,
    ) -> None:
        self.adb_path = adb_path
        self.serial = serial
        self.send_mode = send_mode
        self.send_command_template = send_command_template
        self.state_file = Path(state_file)
        self.skip_existing = skip_existing
        self.last_processed_id = self._load_last_processed_id()
        try:
            self._run_adb(["get-state"])
        except RuntimeError as exc:
            LOGGER.warning("ADB is not ready during startup; will retry in the daemon loop: %s", exc)

    def receive_unread(self) -> list[SmsMessage]:
        output = self._run_adb(
            [
                "shell",
                "content",
                "query",
                "--uri",
                "content://sms/inbox",
                "--projection",
                "_id,address,body,read",
            ]
        )
        rows: list[SmsMessage] = []
        for row in output.splitlines():
            parsed = self._parse_content_row(row)
            if not parsed:
                continue
            message_id = parsed.get("_id")
            address = parsed.get("address")
            body = parsed.get("body")
            if not message_id or not address or body is None:
                continue
            index = int(message_id)
            if index <= self.last_processed_id:
                continue
            rows.append(SmsMessage(sender=address, body=body, index=index))

        if self.last_processed_id == 0 and self.skip_existing:
            highest_seen = self._highest_message_id(output)
            if highest_seen:
                self._save_last_processed_id(highest_seen)
                LOGGER.info("Initialized ADB SMS high-water mark at _id=%s", highest_seen)
            return []

        return sorted(rows, key=lambda message: message.index or 0)

    def send_sms(self, phone_number: str, body: str) -> None:
        body = clamp_sms_reply(body)
        if self.send_mode == "log":
            LOGGER.warning("ADB SMS reply for %s: %s", phone_number, body)
            return
        if self.send_mode == "compose":
            output = self._run_adb(
                [
                    "shell",
                    "am",
                    "start",
                    "-W",
                    "-a",
                    "android.intent.action.SENDTO",
                    "-d",
                    f"smsto:{phone_number}",
                    "--es",
                    "sms_body",
                    body,
                    "--ez",
                    "exit_on_sent",
                    "true",
                ]
            )
            if "Error:" in output:
                raise RuntimeError(f"Android rejected SMS compose intent: {output}")
            LOGGER.warning("Opened SMS composer for %s; tap Send on the phone to deliver it.", phone_number)
            return
        if self.send_mode != "template":
            raise RuntimeError(f"Unsupported ADB_SEND_MODE={self.send_mode!r}")
        if not self.send_command_template:
            raise RuntimeError(
                "ADB_SEND_COMMAND_TEMPLATE is required when ADB_SEND_MODE=template."
            )
        command = self.send_command_template.format(
            adb=shlex.quote(self.adb_path),
            serial_args=shlex.join(self._serial_args()),
            phone=shlex.quote(phone_number),
            body=shlex.quote(body),
        )
        LOGGER.debug("Running ADB SMS send command: %s", command)
        completed = subprocess.run(
            command,
            shell=True,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "ADB SMS send command failed: "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )

    def ack(self, message: SmsMessage) -> None:
        if message.index is None:
            return
        self._run_adb(
            [
                "shell",
                "content",
                "update",
                "--uri",
                "content://sms",
                "--bind",
                "read:i:1",
                "--where",
                f"_id={message.index}",
            ],
            check=False,
        )
        if message.index > self.last_processed_id:
            self._save_last_processed_id(message.index)

    def _run_adb(self, args: list[str], check: bool = True) -> str:
        command = [self.adb_path, *self._serial_args(), *self._adb_args(args)]
        LOGGER.debug("Running ADB command: %s", shlex.join(command))
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and completed.returncode != 0:
            raise RuntimeError(
                f"ADB command failed: {shlex.join(command)} "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        return completed.stdout

    def _serial_args(self) -> list[str]:
        if self.serial:
            return ["-s", self.serial]
        return []

    def _load_last_processed_id(self) -> int:
        try:
            return int(self.state_file.read_text(encoding="utf-8").strip() or "0")
        except FileNotFoundError:
            return 0
        except ValueError:
            LOGGER.warning("Ignoring invalid ADB state file: %s", self.state_file)
            return 0

    def _save_last_processed_id(self, message_id: int) -> None:
        self.state_file.write_text(f"{message_id}\n", encoding="utf-8")
        self.last_processed_id = message_id

    @classmethod
    def _highest_message_id(cls, output: str) -> int:
        highest = 0
        for row in output.splitlines():
            parsed = cls._parse_content_row(row)
            message_id = parsed.get("_id")
            if message_id:
                highest = max(highest, int(message_id))
        return highest

    @staticmethod
    def _adb_args(args: list[str]) -> list[str]:
        if args and args[0] == "shell" and len(args) > 1:
            return ["shell", shlex.join(args[1:])]
        return args

    @classmethod
    def _parse_content_row(cls, row: str) -> dict[str, str]:
        if not row.startswith(cls.row_prefix):
            return {}
        row = row[len(cls.row_prefix) :]
        if " " in row:
            row = row.split(" ", 1)[1]
        values: dict[str, str] = {}
        matches = list(cls.content_field.finditer(row))
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() - 2 if index + 1 < len(matches) else len(row)
            values[match.group(1)] = row[start:end].strip()
        return values


class AtModemSmsTransport(SmsTransport):
    message_header = re.compile(r'^\+CMGL:\s*(\d+),".*?","([^"]+)"')

    def __init__(
        self,
        port: str,
        baudrate: int,
        message_status: str = "REC UNREAD",
        storage: str | None = None,
    ) -> None:
        import serial

        self.message_status = message_status
        self.serial = serial.Serial(port=port, baudrate=baudrate, timeout=2)
        self._command("AT")
        self._command("ATE0")
        self._command("AT+CMEE=2")
        self._command("AT+CMGF=1")
        if storage:
            self._try_command(f'AT+CPMS="{storage}","{storage}","{storage}"')

    def receive_unread(self) -> list[SmsMessage]:
        response = self._command(f'AT+CMGL="{self.message_status}"', wait=1.5)
        LOGGER.debug("SMS list modem response: %s", response)
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
        self.serial.reset_input_buffer()
        self.serial.write(f'AT+CMGS="{phone_number}"\r'.encode("utf-8"))
        time.sleep(1)
        prompt = self._read_available()
        LOGGER.debug("SMS prompt response: %s", prompt)
        if not any(">" in line for line in prompt):
            raise RuntimeError(f"Modem did not present SMS prompt: {prompt}")
        self.serial.write(body.encode("utf-8") + b"\x1a")
        time.sleep(5)
        lines = self._read_available()
        LOGGER.debug("SMS send response: %s", lines)
        if not any("+CMGS:" in line for line in lines) or any("ERROR" in line for line in lines):
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

    def _try_command(self, command: str) -> list[str]:
        try:
            return self._command(command)
        except RuntimeError as exc:
            LOGGER.warning("Ignoring unsupported modem command %s: %s", command, exc)
            return []

    def _read_available(self) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            line = self.serial.readline()
            if not line:
                break
            lines.append(line.decode("utf-8", errors="replace").strip())
        return lines
