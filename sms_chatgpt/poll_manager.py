from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .llm import LlmClient
from .messages import clamp_sms_reply
from .poll_worker import (
    build_poll_llm,
    classify_vote_with_llm,
    extract_draft,
    is_contextless_vote_with_llm,
    load_state,
    resolve_pending_vote_with_llm,
    save_state,
    summarize_results,
)
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
    format_creator_cannot_vote,
    format_duplicate_vote,
    format_multiple_polls,
    format_ongoing_poll,
    format_pending_vote_context_request_for_language,
    format_pending_vote_expired,
    format_pending_vote_not_matched,
    format_poll_canceled,
    format_poll_closed,
    format_poll_draft,
    format_poll_not_open,
    format_poll_started,
    is_contextless_vote,
    hash_msisdn,
    language_for_states,
    match_vote_option,
    merge_draft,
    parse_creator_command,
    record_vote,
    resolve_pending_vote,
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


@dataclass(frozen=True)
class PendingVote:
    message: str
    poll_ids: tuple[str, ...]
    language: str = "en"


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
        self.pending_votes: dict[str, PendingVote] = {}

    def handle_message(self, sender: str, body: str) -> PollResponse:
        sender_hash = hash_msisdn(sender, self.settings.poll_hash_salt)
        states = self._load_states()
        own = states.get(sender_hash)

        if contains_poll_intent(body, self.settings.poll_keywords):
            if own and own.status in {PENDING, ACTIVE, CLOSED}:
                return PollResponse(True, format_ongoing_poll(own.language))
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
                return PollResponse(True, format_poll_canceled(own.language))
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

        pending_response = self._handle_pending_vote_context(sender_hash, body, states)
        if pending_response.handled:
            return pending_response

        vote_response = self._handle_vote(sender_hash, body, states)
        if vote_response.handled:
            return vote_response

        pending_matches = [
            state
            for state in states.values()
            if state.status == PENDING and match_vote_option(body, state.options, state.question)
        ]
        if pending_matches:
            return PollResponse(True, format_poll_not_open(language_for_states(pending_matches)))

        return PollResponse(False)

    def close_expired(self) -> list[OutboundSms]:
        outbound: list[OutboundSms] = []
        for creator_hash, state in self._load_states().items():
            creator_phone = self.creator_phones.get(creator_hash)
            if not creator_phone:
                continue
            if state.status == CLOSED and state.result_reply:
                outbound.append(OutboundSms(creator_phone, clamp_sms_reply(state.result_reply, self.settings.sms_reply_limit), creator_hash))
                continue
            if not state.is_expired():
                continue
            state.status = CLOSED
            state.result_reply = summarize_results(state, self.llm, self.settings.sms_reply_limit)
            save_state(self._state_path(creator_hash), state)
            outbound.append(OutboundSms(creator_phone, clamp_sms_reply(state.result_reply, self.settings.sms_reply_limit), creator_hash))
        self._discard_closed_pending_votes()
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
        if is_contextless_vote(body) or (
            active_states and is_contextless_vote_with_llm(body, self.llm)
        ):
            eligible = {
                creator_hash: state
                for creator_hash, state in active_states.items()
                if creator_hash != sender_hash and not state.is_expired() and sender_hash not in state.votes
            }
            if eligible:
                language = language_for_states(list(eligible.values()))
                self.pending_votes[sender_hash] = PendingVote(body, tuple(eligible), language)
                return PollResponse(True, format_pending_vote_context_request_for_language(language))
            if any(creator_hash == sender_hash for creator_hash in active_states):
                return PollResponse(True, format_creator_cannot_vote(language_for_states(list(active_states.values()))))
            if any(sender_hash in state.votes for state in active_states.values()):
                return PollResponse(True, format_duplicate_vote(language_for_states(list(active_states.values()))))

        other_matches: list[tuple[str, object, object]] = []
        own_match = None
        for creator_hash, state in active_states.items():
            decision = classify_vote(body, state, sender_hash)
            if decision.kind == "ask":
                decision = classify_vote_with_llm(body, state, sender_hash, self.llm)
            if decision.kind == "ask":
                continue
            if creator_hash == sender_hash:
                own_match = (creator_hash, state, decision)
            else:
                other_matches.append((creator_hash, state, decision))

        if len(other_matches) > 1:
            return PollResponse(True, format_multiple_polls(language_for_states([item[1] for item in other_matches])))
        if len(other_matches) == 1:
            creator_hash, state, decision = other_matches[0]
            if state.is_expired():
                return PollResponse(True, format_poll_closed(state.language))
            if decision.kind == "invalid":
                return PollResponse(True, decision.reply)
            state = record_vote(state, sender_hash, decision.option or "")
            save_state(self._state_path(creator_hash), state)
            return PollResponse(True, decision.reply)
        if own_match:
            return PollResponse(True, format_creator_cannot_vote(own_match[1].language))
        return PollResponse(False)

    def _handle_pending_vote_context(
        self,
        sender_hash: str,
        body: str,
        states: dict[str, PollState],
    ) -> PollResponse:
        pending = self.pending_votes.get(sender_hash)
        if not pending:
            return PollResponse(False)
        active_candidates = {
            creator_hash: states[creator_hash]
            for creator_hash in pending.poll_ids
            if (
                creator_hash in states
                and states[creator_hash].status == ACTIVE
                and not states[creator_hash].is_expired()
            )
        }
        if not active_candidates:
            self.pending_votes.pop(sender_hash, None)
            return PollResponse(True, format_pending_vote_expired(pending.language))
        if is_contextless_vote(body):
            language = language_for_states(list(active_candidates.values()))
            self.pending_votes[sender_hash] = PendingVote(body, tuple(active_candidates), language)
            return PollResponse(True, format_pending_vote_context_request_for_language(language))

        matches: list[tuple[str, PollState, object]] = []
        for creator_hash, state in active_candidates.items():
            decision = resolve_pending_vote(pending.message, body, state, sender_hash)
            if decision.kind == "ask":
                decision = resolve_pending_vote_with_llm(pending.message, body, state, sender_hash, self.llm)
            if decision.kind != "ask":
                matches.append((creator_hash, state, decision))

        if len(matches) > 1:
            return PollResponse(True, format_multiple_polls(language_for_states([item[1] for item in matches])))
        if len(matches) == 1:
            creator_hash, state, decision = matches[0]
            if decision.kind == "invalid":
                self.pending_votes.pop(sender_hash, None)
                return PollResponse(True, decision.reply)
            state = record_vote(state, sender_hash, decision.option or "")
            save_state(self._state_path(creator_hash), state)
            self.pending_votes.pop(sender_hash, None)
            return PollResponse(True, decision.reply)
        return PollResponse(True, format_pending_vote_not_matched(language_for_states(list(active_candidates.values()))))

    def _discard_closed_pending_votes(self) -> None:
        states = self._load_states()
        for sender_hash, pending in list(self.pending_votes.items()):
            if not any(
                creator_hash in states
                and states[creator_hash].status == ACTIVE
                and not states[creator_hash].is_expired()
                for creator_hash in pending.poll_ids
            ):
                self.pending_votes.pop(sender_hash, None)

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
