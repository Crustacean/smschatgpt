from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_settings
from .llm import EchoLlmClient, LlmClient, build_llm_client
from .messages import SMS_REPLY_LIMIT, clamp_sms_reply, max_tokens_for_sms_limit, sms_response_instruction
from .polls import (
    ACTIVE,
    CLOSED,
    PollDraft,
    PollState,
    VoteDecision,
    build_pending_poll,
    classify_vote,
    confirm_poll,
    extract_draft_from_text,
    format_counts,
    format_amend_help,
    format_invalid_vote,
    format_poll_canceled,
    format_poll_closed,
    format_poll_draft,
    format_poll_started,
    is_contextless_vote,
    match_vote_option,
    merge_draft,
    parse_creator_command,
    record_vote,
    resolve_pending_vote,
    vote_decision_for_option,
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
    vote.add_argument("--force-option")

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
        result = vote_poll(state_path, args.voter_hash, args.message, llm, args.force_option)
    elif args.action == "status":
        result = status_poll(state_path)
    elif args.action == "finalize":
        result = finalize_poll(state_path, llm, settings.sms_reply_limit)
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
    if command == "amend" and not details:
        return {"handled": True, "reply": format_amend_help(state), "state": state.to_dict()}
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
    state = load_state(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return {"handled": True, "reply": format_poll_canceled(state.language if state else "en")}


def vote_poll(
    path: Path,
    voter_hash: str,
    message: str,
    llm: LlmClient | None = None,
    force_option: str | None = None,
) -> dict[str, Any]:
    state = load_state(path)
    if not state or state.status != ACTIVE:
        return {"handled": False, "route_to_chat": True}
    if force_option and force_option in state.options:
        decision = vote_decision_for_option(force_option, state, voter_hash)
    else:
        decision = classify_vote(message, state, voter_hash)
        if decision.kind == "ask" and llm:
            decision = classify_vote_with_llm(message, state, voter_hash, llm)
    if state.is_expired():
        if decision.kind == "ask":
            return {"handled": False, "route_to_chat": True}
        return {"handled": True, "reply": format_poll_closed(state.language)}
    if decision.kind == "ask":
        return {"handled": False, "route_to_chat": True}
    if decision.kind == "invalid":
        return {"handled": True, "reply": decision.reply or format_invalid_vote(state.options, state.language)}
    state = record_vote(state, voter_hash, decision.option or "")
    save_state(path, state)
    return {"handled": True, "reply": decision.reply, "state": state.to_dict()}


def classify_vote_with_llm(message: str, state: PollState, voter_hash: str, llm: LlmClient) -> VoteDecision:
    option = infer_vote_option_with_llm(message, state, llm)
    if not option:
        return classify_vote(message, state, voter_hash)
    return vote_decision_for_option(option, state, voter_hash)


def resolve_pending_vote_with_llm(
    pending_message: str,
    context_message: str,
    state: PollState,
    voter_hash: str,
    llm: LlmClient,
) -> VoteDecision:
    decision = resolve_pending_vote(pending_message, context_message, state, voter_hash)
    if decision.kind != "ask":
        return decision
    if not match_vote_option(pending_message, state.options):
        return decision
    option = infer_vote_option_with_llm(context_message, state, llm, pending_vote=pending_message)
    if not option:
        return decision
    return vote_decision_for_option(option, state, voter_hash)


def is_contextless_vote_with_llm(message: str, llm: LlmClient) -> bool:
    if is_contextless_vote(message):
        return True
    prompt = (
        "Decide whether this inbound SMS is only a context-free vote fragment, "
        "such as a standalone yes, no, maybe, or option number in any language. "
        "Return false for normal questions, greetings, or votes that include poll topic context. "
        "Return strict JSON only with keys: contextless_vote (boolean), language (ISO 639-1 code or null). "
        f"SMS: {message}"
    )
    try:
        response = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0,
        )
    except Exception:
        return False
    parsed = _parse_vote_json(response)
    return bool(parsed and parsed.get("contextless_vote"))


def infer_vote_option_with_llm(
    message: str,
    state: PollState,
    llm: LlmClient,
    pending_vote: str | None = None,
) -> str | None:
    options = ", ".join(f"{index}) {option}" for index, option in enumerate(state.options, start=1))
    pending_instruction = ""
    if pending_vote:
        pending_instruction = (
            "The voter previously sent this context-free vote-like SMS: "
            f"{pending_vote!r}. Use that only to infer the intended option. "
            "Use the current SMS only to decide whether the poll context matches. "
        )
    prompt = (
        "Classify whether an inbound SMS is a vote for exactly this poll. "
        "The SMS may be in any language supported by the model, and it may differ "
        "from the poll language. Match by semantic topic, not just shared words. "
        "Reject if the SMS is a normal question, unrelated to the poll, ambiguous, "
        "or lacks enough poll context. "
        "For yes/no-style options, map multilingual affirmative or negative intent "
        "to the exact option label. "
        f"{pending_instruction}"
        "Return strict JSON only with keys: matches (boolean), option (string or null), "
        "language (ISO 639-1 code or null). "
        f"Poll question: {state.question}\n"
        f"Poll options: {options}\n"
        f"Current SMS: {message}"
    )
    try:
        response = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0,
        )
    except Exception:
        return None
    parsed = _parse_vote_json(response)
    if not parsed or not parsed.get("matches"):
        return None
    raw_option = str(parsed.get("option") or "").strip()
    if not raw_option:
        return None
    for option in state.options:
        if raw_option == option or raw_option.lower() == option.lower():
            return option
    return match_vote_option(raw_option, state.options)


def status_poll(path: Path) -> dict[str, Any]:
    state = load_state(path)
    if not state:
        return {"exists": False}
    return {"exists": True, "expired": state.is_expired(), "state": state.to_dict()}


def finalize_poll(path: Path, llm: LlmClient, reply_limit: int = SMS_REPLY_LIMIT) -> dict[str, Any]:
    state = load_state(path)
    if not state:
        return {"handled": False}
    if state.status == CLOSED and state.result_reply:
        return {"handled": True, "reply": state.result_reply, "state": state.to_dict()}
    summary = summarize_results(state, llm, reply_limit)
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
        "duration_seconds, language. Use null for missing values. Preserve the "
        "creator's language and use ISO 639-1 for language. Message: "
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
                language=parsed.language or fallback.language,
            )
    except Exception:
        pass
    return fallback


def summarize_results(state: PollState, llm: LlmClient, reply_limit: int = SMS_REPLY_LIMIT) -> str:
    counts = format_counts(state)
    language_name = _language_prompt_name(state.language)
    prompt = (
        f"Summarize these anonymous SMS poll results in {language_name}. "
        f"{sms_response_instruction(reply_limit)} "
        "Do not include voter identifiers. "
        f"{counts}"
    )
    try:
        response = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens_for_sms_limit(reply_limit),
            temperature=0.2,
        )
        if response.strip() and "Summarize these anonymous SMS poll results" not in response:
            return clamp_sms_reply(response, reply_limit)
    except Exception:
        pass
    return clamp_sms_reply(counts, reply_limit)


def _language_prompt_name(language: str) -> str:
    normalized = (language or "en").strip().lower()
    names = {
        "en": "English",
        "sw": "Kiswahili",
        "fr": "French",
        "es": "Spanish",
        "pt": "Portuguese",
        "de": "German",
        "it": "Italian",
        "ar": "Arabic",
        "hi": "Hindi",
        "zh": "Chinese",
    }
    return names.get(normalized, f"the language identified by ISO 639 code '{normalized}'")


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
        language=str(data.get("language") or "").strip(),
    )


def _parse_vote_json(response: str) -> dict[str, Any] | None:
    start = response.find("{")
    end = response.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(response[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


if __name__ == "__main__":
    main()
