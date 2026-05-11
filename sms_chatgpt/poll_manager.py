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
    PollState,
    build_pending_poll,
    classify_vote,
    confirm_poll,
    contains_poll_intent,
    format_amend_help,
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
    poll_id: str | None = None


class LocalPollManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = _state_dir(Path(settings.poll_state_file))
        self.llm: LlmClient = build_poll_llm(
            settings.llm_provider,
            settings.openai_api_key,
            settings.openai_model,
        )
        self.creator_phones: dict[str, str] = {}

    def handle_message(self, sender: str, body: str) -> PollResponse:
        sender_hash = hash_msisdn(sender, self.settings.poll_hash_salt)
        states = self._load_states()
        own = states.get(sender_hash)

        if contains_poll_intent(body, self.settings.poll_keywords):
            if own and own.status in {PENDING, ACTIVE, CLOSED}:
                return PollResponse(True, "You have an ongoing poll.")
            draft = extract_draft(body, self.llm)
            state = build_pending_poll(sender_hash, draft)
            save_state(self._state_path(sender_hash), state)
            self.creator_phones[sender_hash] = sender
            return PollResponse(True, format_poll_draft(state))

        if own and own.status == PENDING:
            command, details = parse_creator_command(body)
            if command == "confirm":
                if own.missing:
                    return PollResponse(True, format_poll_draft(own))
                own = confirm_poll(own)
                save_state(self._state_path(sender_hash), own)
                return PollResponse(True, format_poll_started(own))
            if command == "cancel":
                self._delete_state(sender_hash)
                return PollResponse(True, "Poll canceled.")
            if command == "amend" and not details:
                return PollResponse(True, format_amend_help(own))
            if not body.strip().lower().startswith("amend"):
                vote_response = self._handle_vote(sender_hash, body, states)
                if vote_response.handled:
                    return vote_response
            draft = extract_draft(details, self.llm)
            own = merge_draft(own, draft)
            save_state(self._state_path(sender_hash), own)
            return PollResponse(True, format_poll_draft(own))

        vote_response = self._handle_vote(sender_hash, body, states)
        if vote_response.handled:
            return vote_response

        if any(match_vote_option(body, state.options, state.question) for state in states.values() if state.status == PENDING):
            return PollResponse(True, "Poll is not open yet.")

        return PollResponse(False)

    def close_expired(self) -> list[OutboundSms]:
        outbound: list[OutboundSms] = []
        for creator_hash, state in self._load_states().items():
            creator_phone = self.creator_phones.get(creator_hash)
            if not creator_phone:
                continue
            if state.status == CLOSED and state.result_reply:
                outbound.append(OutboundSms(creator_phone, clamp_sms_reply(state.result_reply), creator_hash))
                continue
            if not state.is_expired():
                continue
            state.status = CLOSED
            state.result_reply = summarize_results(state, self.llm)
            save_state(self._state_path(creator_hash), state)
            outbound.append(OutboundSms(creator_phone, clamp_sms_reply(state.result_reply), creator_hash))
        return outbound

    def ack_results_sent(self, outbound_messages: list[OutboundSms] | None = None) -> None:
        poll_ids = [message.poll_id for message in outbound_messages or [] if message.poll_id]
        for poll_id in poll_ids:
            self._delete_state(poll_id)

    def _handle_vote(self, sender_hash: str, body: str, states: dict[str, PollState]) -> PollResponse:
        active_states = {
            creator_hash: state
            for creator_hash, state in states.items()
            if state.status == ACTIVE
        }
        other_matches: list[tuple[str, object, object]] = []
        own_match = None
        for creator_hash, state in active_states.items():
            decision = classify_vote(body, state, sender_hash)
            if decision.kind == "ask":
                continue
            if creator_hash == sender_hash:
                own_match = (creator_hash, state, decision)
            else:
                other_matches.append((creator_hash, state, decision))

        if len(other_matches) > 1:
            return PollResponse(True, "Multiple polls match. Reply with a clearer vote.")
        if len(other_matches) == 1:
            creator_hash, state, decision = other_matches[0]
            if state.is_expired():
                return PollResponse(True, "This poll is closed.")
            if decision.kind == "invalid":
                return PollResponse(True, decision.reply)
            state = record_vote(state, sender_hash, decision.option or "")
            save_state(self._state_path(creator_hash), state)
            return PollResponse(True, decision.reply)
        if own_match:
            return PollResponse(True, "Poll creators cannot vote in their own poll.")
        return PollResponse(False)

    def _load_states(self) -> dict[str, PollState]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        states: dict[str, PollState] = {}
        for path in self.state_dir.glob("*.json"):
            state = load_state(path)
            if state:
                states[state.creator_hash] = state
        return states

    def _state_path(self, creator_hash: str) -> Path:
        return self.state_dir / f"{creator_hash[:32]}.json"

    def _delete_state(self, creator_hash: str) -> None:
        try:
            self._state_path(creator_hash).unlink()
        except FileNotFoundError:
            pass


def _state_dir(path: Path) -> Path:
    if path.suffix:
        return path.with_name(f"{path.name}.d")
    return path
