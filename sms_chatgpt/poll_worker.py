from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_settings
from .llm import EchoLlmClient, LlmClient, build_llm_client
from .messages import clamp_sms_reply
from .polls import (
    ACTIVE,
    CLOSED,
    PollDraft,
    PollState,
    build_pending_poll,
    classify_vote,
    confirm_poll,
    extract_draft_from_text,
    format_counts,
    format_poll_draft,
    format_poll_started,
    merge_draft,
    parse_creator_command,
    record_vote,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage one SMS poll inside a poll pod.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    draft = subparsers.add_parser("draft")
    draft.add_argument("--creator-hash", required=True)
    draft.add_argument("--message", required=True)

    amend = subparsers.add_parser("amend")
    amend.add_argument("--message", required=True)

    subparsers.add_parser("confirm")
    subparsers.add_parser("cancel")

    vote = subparsers.add_parser("vote")
    vote.add_argument("--voter-hash", required=True)
    vote.add_argument("--message", required=True)

    subparsers.add_parser("status")
    subparsers.add_parser("finalize")

    args = parser.parse_args()
    settings = load_settings()
    state_path = Path(settings.poll_state_file)
    llm = build_poll_llm(settings.llm_provider, settings.openai_api_key, settings.openai_model)

    if args.action == "draft":
        result = draft_poll(state_path, args.creator_hash, args.message, llm)
    elif args.action == "amend":
        result = amend_poll(state_path, args.message, llm)
    elif args.action == "confirm":
        result = confirm_pending_poll(state_path)
    elif args.action == "cancel":
        result = cancel_poll(state_path)
    elif args.action == "vote":
        result = vote_poll(state_path, args.voter_hash, args.message)
    elif args.action == "status":
        result = status_poll(state_path)
    elif args.action == "finalize":
        result = finalize_poll(state_path, llm)
    else:
        raise RuntimeError(f"Unsupported poll action: {args.action}")

    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


def build_poll_llm(provider: str, api_key: str | None, model: str) -> LlmClient:
    try:
        return build_llm_client(provider, api_key, model)
    except Exception:
        return EchoLlmClient()


def draft_poll(path: Path, creator_hash: str, message: str, llm: LlmClient) -> dict[str, Any]:
    draft = extract_draft(message, llm)
    state = build_pending_poll(creator_hash, draft)
    save_state(path, state)
    return {"handled": True, "reply": format_poll_draft(state), "state": state.to_dict()}


def amend_poll(path: Path, message: str, llm: LlmClient) -> dict[str, Any]:
    state = load_state(path)
    if not state:
        return {"handled": False, "route_to_chat": True}
    command, details = parse_creator_command(message)
    if command == "cancel":
        return cancel_poll(path)
    draft = extract_draft(details, llm)
    state = merge_draft(state, draft)
    save_state(path, state)
    return {"handled": True, "reply": format_poll_draft(state), "state": state.to_dict()}


def confirm_pending_poll(path: Path) -> dict[str, Any]:
    state = load_state(path)
    if not state:
        return {"handled": False, "route_to_chat": True}
    if state.missing:
        return {"handled": True, "reply": format_poll_draft(state), "state": state.to_dict()}
    state = confirm_poll(state)
    save_state(path, state)
    return {"handled": True, "reply": format_poll_started(state), "state": state.to_dict()}


def cancel_poll(path: Path) -> dict[str, Any]:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return {"handled": True, "reply": "Poll canceled."}


def vote_poll(path: Path, voter_hash: str, message: str) -> dict[str, Any]:
    state = load_state(path)
    if not state or state.status != ACTIVE:
        return {"handled": False, "route_to_chat": True}
    decision = classify_vote(message, state, voter_hash)
    if state.is_expired():
        if decision.kind == "ask":
            return {"handled": False, "route_to_chat": True}
        return {"handled": True, "reply": "This poll is closed."}
    if decision.kind == "ask":
        return {"handled": False, "route_to_chat": True}
    if decision.kind == "invalid":
        return {"handled": True, "reply": decision.reply or "Invalid vote."}
    state = record_vote(state, voter_hash, decision.option or "")
    save_state(path, state)
    return {"handled": True, "reply": decision.reply or "Vote recorded.", "state": state.to_dict()}


def status_poll(path: Path) -> dict[str, Any]:
    state = load_state(path)
    if not state:
        return {"exists": False}
    return {"exists": True, "expired": state.is_expired(), "state": state.to_dict()}


def finalize_poll(path: Path, llm: LlmClient) -> dict[str, Any]:
    state = load_state(path)
    if not state:
        return {"handled": False}
    if state.status == CLOSED and state.result_reply:
        return {"handled": True, "reply": state.result_reply, "state": state.to_dict()}
    summary = summarize_results(state, llm)
    state.status = CLOSED
    state.result_reply = summary
    save_state(path, state)
    return {"handled": True, "reply": summary, "state": state.to_dict()}


def load_state(path: Path) -> PollState | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return PollState.from_dict(data)


def save_state(path: Path, state: PollState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=True, separators=(",", ":")), encoding="utf-8")


def extract_draft(message: str, llm: LlmClient) -> PollDraft:
    fallback = extract_draft_from_text(message)
    prompt = (
        "Extract an SMS poll draft as strict JSON with keys question, options, "
        "duration_seconds. Use null for missing values. Message: "
        f"{message}"
    )
    try:
        response = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=220,
            temperature=0,
        )
        parsed = _parse_draft_json(response)
        if parsed and (parsed.question or parsed.options or parsed.duration_seconds):
            return PollDraft(
                question=parsed.question or fallback.question,
                options=parsed.options or fallback.options,
                duration_seconds=parsed.duration_seconds or fallback.duration_seconds,
            )
    except Exception:
        pass
    return fallback


def summarize_results(state: PollState, llm: LlmClient) -> str:
    counts = format_counts(state)
    prompt = (
        "Summarize these anonymous SMS poll results in 140 characters or fewer. "
        "Do not include voter identifiers. "
        f"{counts}"
    )
    try:
        response = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.2,
        )
        if response.strip() and "Summarize these anonymous SMS poll results" not in response:
            return clamp_sms_reply(response)
    except Exception:
        pass
    return counts


def _parse_draft_json(response: str) -> PollDraft | None:
    start = response.find("{")
    end = response.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(response[start : end + 1])
    except json.JSONDecodeError:
        return None
    options = data.get("options") or []
    if not isinstance(options, list):
        options = []
    duration = data.get("duration_seconds")
    return PollDraft(
        question=str(data.get("question") or "").strip(),
        options=[str(option).strip() for option in options if str(option).strip()],
        duration_seconds=int(duration) if duration else None,
    )


if __name__ == "__main__":
    main()
