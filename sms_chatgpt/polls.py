from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .messages import clamp_sms_reply

PENDING = "pending"
ACTIVE = "active"
CLOSED = "closed"
CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "approve", "start"}
CANCEL_WORDS = {"cancel", "stop"}
YES_WORDS = {"yes", "y", "yeah", "yep", "true", "agree", "approve"}
NO_WORDS = {"no", "n", "nope", "false", "disagree", "reject"}
QUESTION_WORDS = {"what", "why", "how", "when", "where", "who", "which", "can", "could", "should", "would"}
POSITIVE_VOTE_WORDS = {"support", "favor", "favour", "approve", "agree"}
NEGATIVE_VOTE_WORDS = {"against", "oppose", "opposed", "reject"}
CONTEXT_STOPWORDS = {
    "a",
    "about",
    "am",
    "an",
    "and",
    "are",
    "be",
    "by",
    "do",
    "for",
    "i",
    "is",
    "it",
    "of",
    "on",
    "or",
    "poll",
    "that",
    "the",
    "this",
    "to",
    "vote",
    "we",
}


@dataclass
class PollDraft:
    question: str = ""
    options: list[str] = field(default_factory=list)
    duration_seconds: int | None = None

    @property
    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.question:
            missing.append("question")
        if len(self.options) < 2:
            missing.append("options")
        if not self.duration_seconds:
            missing.append("duration")
        return missing


@dataclass
class PollState:
    status: str
    creator_hash: str
    question: str
    options: list[str]
    duration_seconds: int | None
    created_at: int
    start_time: int | None = None
    expires_at: int | None = None
    votes: dict[str, str] = field(default_factory=dict)
    result_reply: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PollState":
        return cls(
            status=str(data.get("status", PENDING)),
            creator_hash=str(data.get("creator_hash", "")),
            question=str(data.get("question", "")),
            options=[str(option) for option in data.get("options", []) if str(option).strip()],
            duration_seconds=(
                int(data["duration_seconds"]) if data.get("duration_seconds") is not None else None
            ),
            created_at=int(data.get("created_at", int(time.time()))),
            start_time=int(data["start_time"]) if data.get("start_time") is not None else None,
            expires_at=int(data["expires_at"]) if data.get("expires_at") is not None else None,
            votes={str(key): str(value) for key, value in data.get("votes", {}).items()},
            result_reply=str(data["result_reply"]) if data.get("result_reply") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def missing(self) -> list[str]:
        return PollDraft(self.question, self.options, self.duration_seconds).missing

    def is_expired(self, now: int | None = None) -> bool:
        return self.status == ACTIVE and self.expires_at is not None and (now or int(time.time())) >= self.expires_at


@dataclass(frozen=True)
class VoteDecision:
    kind: str
    option: str | None = None
    reply: str | None = None


def hash_msisdn(msisdn: str, salt: str) -> str:
    normalized = "".join(msisdn.strip().split())
    return hashlib.sha256(f"{salt}:{normalized}".encode("utf-8")).hexdigest()


def contains_poll_intent(message: str, keywords: list[str]) -> bool:
    words = {word.lower() for word in re.findall(r"[a-zA-Z]+", message)}
    return any(keyword.lower() in words for keyword in keywords)


def parse_duration_seconds(message: str) -> int | None:
    match = re.search(r"\b(\d{1,5})\s*(seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|h)\b", message, re.I)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("s", "sec")):
        return amount
    if unit.startswith(("m", "min")):
        return amount * 60
    if unit.startswith(("h", "hr")):
        return amount * 3600
    return None


def extract_draft_from_text(message: str) -> PollDraft:
    duration = parse_duration_seconds(message)
    options = _extract_options(message)
    question = _extract_question(message)
    return PollDraft(question=question, options=options, duration_seconds=duration)


def merge_draft(current: PollState, draft: PollDraft) -> PollState:
    return PollState(
        status=current.status,
        creator_hash=current.creator_hash,
        question=draft.question or current.question,
        options=draft.options or current.options,
        duration_seconds=draft.duration_seconds or current.duration_seconds,
        created_at=current.created_at,
        start_time=current.start_time,
        expires_at=current.expires_at,
        votes=current.votes,
        result_reply=current.result_reply,
    )


def build_pending_poll(creator_hash: str, draft: PollDraft) -> PollState:
    return PollState(
        status=PENDING,
        creator_hash=creator_hash,
        question=draft.question,
        options=draft.options,
        duration_seconds=draft.duration_seconds,
        created_at=int(time.time()),
    )


def confirm_poll(state: PollState, now: int | None = None) -> PollState:
    if state.missing:
        raise ValueError(f"Poll is missing: {', '.join(state.missing)}")
    started = now or int(time.time())
    return PollState(
        status=ACTIVE,
        creator_hash=state.creator_hash,
        question=state.question,
        options=state.options,
        duration_seconds=state.duration_seconds,
        created_at=state.created_at,
        start_time=started,
        expires_at=started + int(state.duration_seconds or 0),
        votes=state.votes,
    )


def classify_vote(message: str, state: PollState, voter_hash: str) -> VoteDecision:
    option = match_vote_option(message, state.options, state.question)
    if option is None:
        if _looks_like_bad_vote(message):
            return VoteDecision("invalid", reply=_valid_vote_help(state.options))
        return VoteDecision("ask")
    if voter_hash == state.creator_hash:
        return VoteDecision("invalid", reply="Poll creators cannot vote in their own poll.")
    if voter_hash in state.votes:
        return VoteDecision("invalid", reply="Your vote has already been recorded.")
    return VoteDecision("valid", option=option, reply=f"Vote recorded: {option}.")


def match_vote_option(message: str, options: list[str], question: str | None = None) -> str | None:
    normalized = _normalize(message)
    if normalized.isdigit():
        index = int(normalized) - 1
        if 0 <= index < len(options):
            return options[index]
    for option in options:
        if normalized == _normalize(option):
            return option
    yes_option, no_option = _yes_no_options(options)
    if yes_option and normalized in YES_WORDS:
        return yes_option
    if no_option and normalized in NO_WORDS:
        return no_option
    phrase_vote = _yes_no_vote_intent(message)
    if phrase_vote and not _vote_context_matches(message, question):
        return None
    if yes_option and phrase_vote == "yes":
        return yes_option
    if no_option and phrase_vote == "no":
        return no_option
    return None


def record_vote(state: PollState, voter_hash: str, option: str) -> PollState:
    votes = dict(state.votes)
    votes[voter_hash] = option
    return PollState(
        status=state.status,
        creator_hash=state.creator_hash,
        question=state.question,
        options=state.options,
        duration_seconds=state.duration_seconds,
        created_at=state.created_at,
        start_time=state.start_time,
        expires_at=state.expires_at,
        votes=votes,
        result_reply=state.result_reply,
    )


def aggregate_votes(state: PollState) -> dict[str, int]:
    counts = {option: 0 for option in state.options}
    for option in state.votes.values():
        if option in counts:
            counts[option] += 1
    return counts


def format_poll_draft(state: PollState) -> str:
    if state.missing:
        return clamp_sms_reply(f"Poll needs {', '.join(state.missing)}. Reply with AMEND <details>.")
    return clamp_sms_reply(
        f"Poll draft: {state.question} Options: {_format_options(state.options)}. "
        f"Duration: {state.duration_seconds}s. Reply YES or AMEND ..."
    )


def format_poll_started(state: PollState) -> str:
    return clamp_sms_reply(
        f"Poll started for {state.duration_seconds}s: {state.question} Reply {_format_options(state.options)}"
    )


def format_counts(state: PollState) -> str:
    counts = aggregate_votes(state)
    parts = ", ".join(f"{option} {count}" for option, count in counts.items())
    return clamp_sms_reply(f"Poll closed: {state.question} {parts}. Total {len(state.votes)}.")


def parse_creator_command(message: str) -> tuple[str, str]:
    stripped = message.strip()
    lowered = stripped.lower()
    if lowered in CONFIRM_WORDS:
        return "confirm", ""
    if lowered in CANCEL_WORDS:
        return "cancel", ""
    if lowered.startswith("amend "):
        return "amend", stripped[6:].strip()
    return "amend", stripped


def _extract_options(message: str) -> list[str]:
    explicit = re.search(r"\boptions?\s*[:=-]\s*", message, re.I)
    if explicit:
        raw = message[explicit.end() :]
        duration = re.search(
            r"\b(?:for|duration)?\s*\d{1,5}\s*(?:seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|h)\b",
            raw,
            re.I,
        )
        if duration:
            raw = raw[: duration.start()]
        return _split_options(raw)
    yes_no = re.search(r"\b(yes)\s*(?:/|,|\bor\b|\s+or\s+)\s*(no)\b", message, re.I)
    no_yes = re.search(r"\b(no)\s*(?:/|,|\bor\b|\s+or\s+)\s*(yes)\b", message, re.I)
    if yes_no:
        return ["Yes", "No"]
    if no_yes:
        return ["No", "Yes"]
    return []


def _split_options(raw: str) -> list[str]:
    cleaned = _remove_duration(raw)
    parts = re.split(r"\s*(?:,|/|\||;|\bor\b)\s*", cleaned, flags=re.I)
    options: list[str] = []
    for part in parts:
        option = re.sub(r"^\d+[\).:-]?\s*", "", part.strip())
        option = option.strip(" .")
        if option and option.lower() not in {"for", "duration"}:
            options.append(option[:40])
    return options[:8]


def _extract_question(message: str) -> str:
    text = _remove_duration(message)
    text = re.sub(r"\b(options?|choices?)\s*[:=-]\s*.+$", "", text, flags=re.I).strip()
    text = re.sub(r"\b(create|start|make|run|please)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(poll|vote|voting|survey)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(with|using)\s+(yes|no)\s*(?:/|,|\bor\b|\s+or\s+)\s*(yes|no)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(yes|no)\s*(?:/|,|\bor\b|\s+or\s+)\s*(yes|no)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .:-")
    text = re.sub(r"^(a|an|the)\s+", "", text, flags=re.I)
    if text.lower().startswith("on "):
        text = text[3:].strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if not text.endswith("?"):
        text = f"{text}?"
    return text


def _remove_duration(message: str) -> str:
    return re.sub(r"\b(?:for|last(?:ing)?|duration)?\s*\d{1,5}\s*(?:seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|h)\b", " ", message, flags=re.I)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().strip(".!?:;"))


def _yes_no_options(options: list[str]) -> tuple[str | None, str | None]:
    yes_option = next((option for option in options if _normalize(option) == "yes"), None)
    no_option = next((option for option in options if _normalize(option) == "no"), None)
    return yes_option, no_option


def _looks_like_bad_vote(message: str) -> bool:
    lowered = message.strip().lower()
    return lowered.startswith(("vote ", "voting ", "poll vote "))


def _yes_no_vote_intent(message: str) -> str | None:
    if "?" in message:
        return None
    words = re.findall(r"[a-zA-Z']+", message.lower())
    if not words or len(words) > 10:
        return None
    if len(words) > 1 and words[1] in QUESTION_WORDS:
        return None
    if words[0] in YES_WORDS:
        return "yes"
    if words[0] in NO_WORDS:
        return "no"
    normalized = _normalize(message)
    if (
        "do not" in normalized
        or "don't" in normalized
        or "dont" in normalized
        or "not support" in normalized
        or "not favor" in normalized
        or "not favour" in normalized
        or "not approve" in normalized
        or "not agree" in normalized
        or any(word in words for word in NEGATIVE_VOTE_WORDS)
    ):
        return "no"
    if (
        "i am for" in normalized
        or "i'm for" in normalized
        or normalized.startswith("for ")
        or any(word in words for word in POSITIVE_VOTE_WORDS)
    ):
        return "yes"
    return None


def _vote_context_matches(message: str, question: str | None) -> bool:
    if not question:
        return True
    message_tokens = _context_tokens(message)
    question_tokens = _context_tokens(question)
    if not message_tokens:
        return True
    return bool(message_tokens & question_tokens)


def _context_tokens(text: str) -> set[str]:
    tokens = set()
    for word in re.findall(r"[a-zA-Z]+", text.lower()):
        token = _stem_context_word(word)
        if token and token not in CONTEXT_STOPWORDS and token not in YES_WORDS and token not in NO_WORDS:
            tokens.add(token)
    return tokens


def _stem_context_word(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
        if len(word) > 2 and word[-1] == word[-2]:
            word = word[:-1]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("s"):
        word = word[:-1]
    return word


def _valid_vote_help(options: list[str]) -> str:
    return clamp_sms_reply(f"Invalid vote. Reply {_format_options(options)}")


def _format_options(options: list[str]) -> str:
    return " ".join(f"{index}) {option}" for index, option in enumerate(options, start=1))
