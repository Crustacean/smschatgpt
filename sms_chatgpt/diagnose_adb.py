from __future__ import annotations

import argparse
import shlex
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Android/ADB SMS access.")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--send-command-template")
    parser.add_argument("--send-mode", choices=["compose", "template"], default="compose")
    parser.add_argument("--send-to")
    parser.add_argument("--message", default="SMSChatGPT ADB test")
    args = parser.parse_args()

    run(args, ["devices", "-l"])
    run(args, ["get-state"])
    run(args, ["shell", "settings", "get", "global", "device_provisioned"], check=False)
    run(args, ["shell", "content", "query", "--uri", "content://sms/inbox", "--projection", "_id,address,body,read"])
    run(args, ["shell", "content", "query", "--uri", "content://sms/sent", "--projection", "_id,address,body,date"], check=False)

    if args.send_to:
        if args.send_mode == "compose":
            run(
                args,
                [
                    "shell",
                    "am",
                    "start",
                    "-W",
                    "-a",
                    "android.intent.action.SENDTO",
                    "-d",
                    f"smsto:{args.send_to}",
                    "--es",
                    "sms_body",
                    args.message,
                    "--ez",
                    "exit_on_sent",
                    "true",
                ],
            )
            return
        if not args.send_command_template:
            print("\nNo --send-command-template supplied, so no SMS send test was run.")
            return
        serial_args = shlex.join(["-s", args.serial]) if args.serial else ""
        command = args.send_command_template.format(
            adb=shlex.quote(args.adb),
            serial_args=serial_args,
            phone=shlex.quote(args.send_to),
            body=shlex.quote(args.message),
        )
        print(f"\n> {command}")
        completed = subprocess.run(command, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(completed.stdout, end="")
        print(completed.stderr, end="")
        print(f"exit={completed.returncode}")


def run(args: argparse.Namespace, command_args: list[str], check: bool = True) -> None:
    command = [args.adb]
    if args.serial:
        command.extend(["-s", args.serial])
    if command_args and command_args[0] == "shell" and len(command_args) > 1:
        command.extend(["shell", shlex.join(command_args[1:])])
    else:
        command.extend(command_args)
    print(f"\n> {shlex.join(command)}")
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(completed.stdout, end="")
    print(completed.stderr, end="")
    print(f"exit={completed.returncode}")
    if check and completed.returncode != 0:
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
