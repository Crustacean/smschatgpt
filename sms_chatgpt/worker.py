from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings
from .llm import build_llm_client
from .messages import clamp_sms_reply


def main() -> None:
    parser = argparse.ArgumentParser(description="Handle one SMS chat turn inside a session pod.")
    parser.add_argument("--message", required=True)
    args = parser.parse_args()

    settings = load_settings()
    llm = build_llm_client(settings.llm_provider, settings.openai_api_key, settings.openai_model)
    history_file = Path(settings.chat_history_file)
    history = load_history(history_file, settings.chat_history_max_turns)
    reply = clamp_sms_reply(llm.respond(args.message, history))
    save_history(
        history_file,
        [
            *history,
            {"role": "user", "content": args.message},
            {"role": "assistant", "content": reply},
        ],
        settings.chat_history_max_turns,
    )
    sys.stdout.write(reply)


def load_history(path: Path, max_turns: int) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    history: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content:
            history.append({"role": role, "content": content})
    return trim_history(history, max_turns)


def save_history(path: Path, history: list[dict[str, str]], max_turns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(trim_history(history, max_turns), ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )


def trim_history(history: list[dict[str, str]], max_turns: int) -> list[dict[str, str]]:
    if max_turns <= 0:
        return []
    return history[-max_turns * 2 :]


if __name__ == "__main__":
    main()
