from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .llm import build_llm_client
from .messages import clamp_sms_reply


def main() -> None:
    parser = argparse.ArgumentParser(description="Handle one SMS chat turn inside a session pod.")
    parser.add_argument("--message", required=True)
    args = parser.parse_args()

    settings = load_settings()
    llm = build_llm_client(settings.llm_provider, settings.openai_api_key, settings.openai_model)
    sys.stdout.write(clamp_sms_reply(llm.respond(args.message)))


if __name__ == "__main__":
    main()
