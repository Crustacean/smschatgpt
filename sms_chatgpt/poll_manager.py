from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .llm import LlmClient
from .messages import clamp_sms_reply
from .poll_worker import build_poll_llm, extract_draft, load_state, save_state, summarize_results
from .polls import (
    ACTIVE,
    CLOSED,
    PENDING,
    build_pending_poll,
    classify_vote,
    confirm_poll,
    contains_poll_intent,
    format_poll_draft,
    format_poll_started,
    hash_msisdn,
    match_vote_option,
    merge_draft,
    parse_creator_command,
    record_vote,
)


@dataclass(frozen=True)
class PollResponse:
    handled: bool
    reply: str | None = None


@dataclass(frozen=True)
class OutboundSms:
    recipient: str
    body: str


class LocalPollManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_path = Path(settings.poll_state_file)
        self.llm: LlmClient = build_poll_llm(
            settings.llm_provider,
            settings.openai_api_key,
            settings.openai_model,
        )
        self.creator_phone: str | None = None

    def handle_message(self, sender: str, body: str) -> PollResponse:
        sender_hash = hash_msisdn(sender, self.settings.poll_hash_salt)
        state = load_state(self.state_path)
        if not state:
            if not contains_poll_intent(body, self.settings.poll_keywords):
                return PollResponse(False)
            draft = extract_draft(body, self.llm)
            state = build_pending_poll(sender_hash, draft)
            save_state(self.state_path, state)
            self.creator_phone = sender
            return PollResponse(True, format_poll_draft(state))

        if state.status == PENDING:
            if sender_hash != state.creator_hash:
                if contains_poll_intent(body, self.settings.poll_keywords):
                    return PollResponse(True, "A poll is already pending.")
                if match_vote_option(body, state.options):
                    return PollResponse(True, "Poll is not open yet.")
                return PollResponse(False)
            command, details = parse_creator_command(body)
            if command == "confirm":
                if state.missing:
                    return PollResponse(True, format_poll_draft(state))
                state = confirm_poll(state)
                save_state(self.state_path, state)
                return PollResponse(True, format_poll_started(state))
            if command == "cancel":
                self._delete_state()
                return PollResponse(True, "Poll canceled.")
            draft = extract_draft(details, self.llm)
            state = merge_draft(state, draft)
            save_state(self.state_path, state)
            return PollResponse(True, format_poll_draft(state))

        if state.status == ACTIVE:
            decision = classify_vote(body, state, sender_hash)
            if state.is_expired():
                if decision.kind == "ask":
                    return PollResponse(False)
                return PollResponse(True, "This poll is closed.")
            if decision.kind == "ask":
                if contains_poll_intent(body, self.settings.poll_keywords):
                    return PollResponse(True, "A poll is already active.")
                return PollResponse(False)
            if decision.kind == "invalid":
                return PollResponse(True, decision.reply)
            state = record_vote(state, sender_hash, decision.option or "")
            save_state(self.state_path, state)
            return PollResponse(True, decision.reply)

        return PollResponse(False)

    def close_expired(self) -> list[OutboundSms]:
        state = load_state(self.state_path)
        if not state or not self.creator_phone:
            return []
        if state.status == CLOSED and state.result_reply:
            return [OutboundSms(self.creator_phone, clamp_sms_reply(state.result_reply))]
        if not state.is_expired():
            return []
        state.status = CLOSED
        state.result_reply = summarize_results(state, self.llm)
        save_state(self.state_path, state)
        return [OutboundSms(self.creator_phone, clamp_sms_reply(state.result_reply))]

    def ack_results_sent(self) -> None:
        state = load_state(self.state_path)
        if not state or state.status != CLOSED:
            return
        self._delete_state()

    def _delete_state(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
