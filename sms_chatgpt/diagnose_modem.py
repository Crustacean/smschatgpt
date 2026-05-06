from __future__ import annotations

import argparse
import time


def main() -> None:
    import serial

    parser = argparse.ArgumentParser(description="Diagnose an AT-command SMS modem.")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--storage", help="Optional SMS storage to select, such as SM or ME.")
    parser.add_argument("--list-status", default="ALL", help='SMS status for CMGL, e.g. ALL or "REC UNREAD".')
    parser.add_argument("--send-to", help="Optional number to send a test SMS to.")
    parser.add_argument("--message", default="SMSChatGPT test")
    args = parser.parse_args()

    with serial.Serial(args.port, args.baudrate, timeout=2) as modem:
        print(f"Opened {args.port} at {args.baudrate}")
        for command in [
            "AT",
            "ATE0",
            "AT+CMEE=2",
            "AT+CPIN?",
            "AT+CSQ",
            "AT+COPS?",
            "AT+CSCA?",
            "AT+CMGF=1",
            "AT+CPMS=?",
        ]:
            print_command(modem, command)

        if args.storage:
            print_command(modem, f'AT+CPMS="{args.storage}","{args.storage}","{args.storage}"')

        for command in [
            "AT+CPMS?",
            f'AT+CMGL="{args.list_status}"',
        ]:
            print_command(modem, command)

        if args.send_to:
            send_sms(modem, args.send_to, args.message)


def print_command(modem, command: str) -> None:
    print(f"\n> {command}")
    lines = command_lines(modem, command, wait=1.5)
    if lines:
        print("\n".join(lines))
    else:
        print("(no response)")


def command_lines(modem, command: str, wait: float = 0.5) -> list[str]:
    modem.reset_input_buffer()
    modem.write(f"{command}\r".encode("utf-8"))
    time.sleep(wait)
    return read_available(modem)


def send_sms(modem, phone_number: str, body: str) -> None:
    print(f'\n> AT+CMGS="{phone_number}"')
    modem.reset_input_buffer()
    modem.write(f'AT+CMGS="{phone_number}"\r'.encode("utf-8"))
    time.sleep(1)
    prompt = read_available(modem)
    print("\n".join(prompt) if prompt else "(no prompt)")
    if not any(">" in line for line in prompt):
        print("No SMS prompt was received, so the test SMS was not sent.")
        return

    modem.write(body.encode("utf-8") + b"\x1a")
    time.sleep(5)
    response = read_available(modem)
    print("\n".join(response) if response else "(no send confirmation)")


def read_available(modem) -> list[str]:
    lines: list[str] = []
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        chunk = modem.readline()
        if not chunk:
            break
        decoded = chunk.decode("utf-8", errors="replace").strip()
        if decoded:
            lines.append(decoded)
    return lines


if __name__ == "__main__":
    main()
