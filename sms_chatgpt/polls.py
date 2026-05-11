from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from .messages import clamp_sms_reply

PENDING = "pending"
ACTIVE = "active"
CLOSED = "closed"
CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "approve", "start"}
CANCEL_WORDS = {"cancel", "stop"}
YES_WORDS = {"yes", "y", "yeah", "yep", "true", "agree", "approve", "ndio", "naam"}
NO_WORDS = {"no", "n", "nope", "false", "disagree", "reject", "hapana", "la"}
YES_OPTION_LABELS = {"yes", "y", "ndio", "naam"}
NO_OPTION_LABELS = {"no", "n", "hapana", "la"}
QUESTION_WORDS = {"what", "why", "how", "when", "where", "who", "which", "can", "could", "should", "would"}
POSITIVE_VOTE_WORDS = {"support", "favor", "favour", "approve", "agree", "unga"}
NEGATIVE_VOTE_WORDS = {"against", "oppose", "opposed", "reject", "pinga"}
DEFAULT_POLL_INTENT_PHRASES = {
    "poll",
    "vote",
    "voting",
    "survey",
    "kura",
    "kura ya maoni",
    "piga kura",
    "upigaji kura",
    "encuesta",
    "votacion",
    "votar",
    "voto",
    "sondage",
    "umfrage",
    "abstimmung",
    "sondaggio",
    "votazione",
    "pesquisa",
    "votacao",
}
_DURATION_UNITS_RE = (
    r"seconds?|secs?|sec|minutes?|mins?|min|hours?|hrs?|hr|"
    r"sekunde|sek|dakika|saa|s|m|h"
)
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
    "au",
    "katika",
    "kwa",
    "kuhusu",
    "na",
    "ni",
    "that",
    "the",
    "this",
    "to",
    "vote",
    "we",
    "ya",
    "za",
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
    normalized_message = _normalize_intent_text(message)
    phrases = list(keywords) + sorted(DEFAULT_POLL_INTENT_PHRASES)
    return any(_contains_intent_phrase(normalized_message, phrase) for phrase in phrases)


def parse_duration_seconds(message: str) -> int | None:
    match = re.search(rf"\b(\d{{1,5}})\s*({_DURATION_UNITS_RE})\b", message, re.I)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        multiplier = _duration_multiplier(unit)
        return amount * multiplier if multiplier else None
    match = re.search(rf"\b({_DURATION_UNITS_RE})\s*(\d{{1,5}})\b", message, re.I)
    if not match:
        return None
    unit = match.group(1)
    amount = int(match.group(2))
    multiplier = _duration_multiplier(unit)
    return amount * multiplier if multiplier else None


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


def format_amend_help(state: PollState) -> str:
    prefix = f"Poll needs {', '.join(state.missing)}. " if state.missing else ""
    return clamp_sms_reply(f"{prefix}Send AMEND <details>, e.g. AMEND options: Yes, No for 60s.")


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
    amend = re.match(r"^amend\b\s*:?\s*(.*)$", stripped, re.I)
    if amend:
        return "amend", amend.group(1).strip()
    return "amend", stripped


def _extract_options(message: str) -> list[str]:
    explicit = re.search(r"\b(options?|choices?|chaguo)\s*[:=-]\s*", message, re.I)
    if explicit:
        raw = message[explicit.end() :]
        duration = re.search(
            rf"\b(?:for|duration|kwa|muda\s+wa)?\s*"
            rf"(?:\d{{1,5}}\s*(?:{_DURATION_UNITS_RE})|(?:{_DURATION_UNITS_RE})\s*\d{{1,5}})\b",
            raw,
            re.I,
        )
        if duration:
            raw = raw[: duration.start()]
        return _split_options(raw)
    yes_no = re.search(r"\b(yes|ndio|naam)\s*(?:/|,|\bor\b|\bau\b|\s+or\s+|\s+au\s+)\s*(no|hapana|la)\b", message, re.I)
    no_yes = re.search(r"\b(no|hapana|la)\s*(?:/|,|\bor\b|\bau\b|\s+or\s+|\s+au\s+)\s*(yes|ndio|naam)\b", message, re.I)
    if yes_no:
        return ["Yes", "No"]
    if no_yes:
        return ["No", "Yes"]
    if _positive_negative_question(message):
        return ["Yes", "No"]
    return []


def _split_options(raw: str) -> list[str]:
    cleaned = _remove_duration(raw)
    parts = re.split(r"\s*(?:,|/|\||;|\bor\b|\bau\b)\s*", cleaned, flags=re.I)
    options: list[str] = []
    for part in parts:
        option = re.sub(r"^\d+[\).:-]?\s*", "", part.strip())
        option = option.strip(" .")
        if option and option.lower() not in {"for", "duration", "kwa", "muda"}:
            options.append(option[:40])
    return options[:8]


def _extract_question(message: str) -> str:
    positive_negative = _positive_negative_question(message)
    if positive_negative:
        return positive_negative
    text = _remove_duration(message)
    text = re.sub(r"\b(options?|choices?|chaguo)\s*[:=-]\s*.+$", "", text, flags=re.I).strip()
    text = _strip_poll_request_terms(text)
    text = re.sub(r"\b(with|using)\s+(yes|ndio|naam|no|hapana|la)\s*(?:/|,|\bor\b|\bau\b|\s+or\s+|\s+au\s+)\s*(yes|ndio|naam|no|hapana|la)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(yes|ndio|naam|no|hapana|la)\s*(?:/|,|\bor\b|\bau\b|\s+or\s+|\s+au\s+)\s*(yes|ndio|naam|no|hapana|la)\b", " ", text, flags=re.I)
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


def _positive_negative_question(message: str) -> str | None:
    text = _remove_duration(message)
    text = re.sub(r"\b(options?|choices?|chaguo)\s*[:=-]\s*.+$", "", text, flags=re.I).strip()
    text = _strip_poll_request_terms(text)
    text = re.sub(r"\s+", " ", text).strip(" .:-")
    text = re.sub(r"^(a|an|the|on|about)\s+", "", text, flags=re.I)
    match = re.search(
        r"\b(?:to\s+)?(?P<verb>[a-z][a-z'-]*)\s+or\s+"
        r"(?:not|do\s+not|don't|dont)\s+(?:to\s+)?(?P=verb)\s+"
        r"(?P<object>.+)$",
        text,
        re.I,
    )
    swahili_match = re.search(
        r"\bku(?P<verb>[a-z][a-z'-]*)\s+au\s+kuto\s*(?P=verb)\s+(?P<object>.+)$",
        text,
        re.I,
    )
    if not match and swahili_match:
        subject = re.sub(r"\s+", " ", swahili_match.group("object")).strip(" .:-")
        if not subject:
            return None
        question = f"Ku{swahili_match.group('verb').lower()} {subject}"
        question = question[0].upper() + question[1:]
        if not question.endswith("?"):
            question = f"{question}?"
        return question
    if not match:
        return None
    subject = re.sub(r"\s+", " ", match.group("object")).strip(" .:-")
    if not subject:
        return None
    question = f"{match.group('verb').lower()} {subject}"
    question = question[0].upper() + question[1:]
    if not question.endswith("?"):
        question = f"{question}?"
    return question


def _remove_duration(message: str) -> str:
    return re.sub(
        rf"\b(?:for|last(?:ing)?|duration|kwa\s+muda\s+wa|muda\s+wa|kwa)?\s*"
        rf"(?:\d{{1,5}}\s*(?:{_DURATION_UNITS_RE})|(?:{_DURATION_UNITS_RE})\s*\d{{1,5}})\b",
        " ",
        message,
        flags=re.I,
    )


def _duration_multiplier(unit: str) -> int | None:
    normalized = unit.strip().lower()
    if normalized == "s" or normalized.startswith(("sec", "sek", "second")):
        return 1
    if normalized == "m" or normalized.startswith(("min", "dakika")):
        return 60
    if normalized == "h" or normalized.startswith(("hr", "hour")) or normalized == "saa":
        return 3600
    return None


def _strip_poll_request_terms(text: str) -> str:
    text = re.sub(r"\b(kura\s+ya\s+maoni|upigaji\s+kura|piga\s+kura)\b", " ", text, flags=re.I)
    text = re.sub(
        r"\b(create|start|make|run|please|tengeneza|anzisha|unda|endesha|tafadhali)\b",
        " ",
        text,
        flags=re.I,
    )
    return re.sub(r"\b(poll|vote|voting|survey|whether|kura|kuhusu)\b", " ", text, flags=re.I)


def _normalize_intent_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    normalized = re.sub(r"[^0-9a-zA-Z']+", " ", normalized.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _contains_intent_phrase(normalized_message: str, phrase: str) -> bool:
    normalized_phrase = _normalize_intent_text(phrase)
    if not normalized_phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized_phrase)}(?![a-z0-9])", normalized_message) is not None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().strip(".!?:;"))


def _yes_no_options(options: list[str]) -> tuple[str | None, str | None]:
    yes_option = next((option for option in options if _normalize(option) in YES_OPTION_LABELS), None)
    no_option = next((option for option in options if _normalize(option) in NO_OPTION_LABELS), None)
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
