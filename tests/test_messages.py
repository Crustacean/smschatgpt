import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sms_chatgpt.config import Settings
from sms_chatgpt.daemon import _send_poll_results
from sms_chatgpt.k8s import ChatPodManager, PollPodManager
from sms_chatgpt.messages import SMS_REPLY_LIMIT, clamp_sms_reply
from sms_chatgpt.poll_manager import LocalPollManager
from sms_chatgpt.poll_worker import build_poll_llm, load_state, save_state
from sms_chatgpt.polls import (
    build_pending_poll,
    classify_vote,
    confirm_poll,
    contains_poll_intent,
    CLOSED,
    extract_draft_from_text,
    format_counts,
    hash_msisdn,
    parse_duration_seconds,
    record_vote,
)
from sms_chatgpt.sms import AdbSmsTransport
from sms_chatgpt.worker import load_history, save_history, trim_history


class ClampSmsReplyTest(unittest.TestCase):
    def test_clamp_sms_reply_compacts_whitespace(self) -> None:
        self.assertEqual(clamp_sms_reply("hello\n\nthere"), "hello there")

    def test_clamp_sms_reply_limits_to_140_characters(self) -> None:
        reply = clamp_sms_reply("x" * 200)

        self.assertEqual(len(reply), SMS_REPLY_LIMIT)
        self.assertTrue(reply.endswith("..."))

class AdbSmsTransportTest(unittest.TestCase):
    def test_parse_content_row_handles_commas_in_body(self) -> None:
        row = "Row: 0 _id=42, address=+15551234567, body=hello, with comma, read=0"

        parsed = AdbSmsTransport._parse_content_row(row)

        self.assertEqual(parsed["_id"], "42")
        self.assertEqual(parsed["address"], "+15551234567")
        self.assertEqual(parsed["body"], "hello, with comma")
        self.assertEqual(parsed["read"], "0")

    def test_highest_message_id(self) -> None:
        output = "\n".join(
            [
                "Row: 0 _id=41, address=+15551234567, body=old, read=1",
                "Row: 1 _id=42, address=+15551234567, body=new, read=0",
            ]
        )

        self.assertEqual(AdbSmsTransport._highest_message_id(output), 42)

    def test_state_file_round_trip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.txt"
            transport = AdbSmsTransport.__new__(AdbSmsTransport)
            transport.state_file = state_file

            transport._save_last_processed_id(99)

            self.assertEqual(transport._load_last_processed_id(), 99)


class ChatPodManagerTest(unittest.TestCase):
    def test_worker_command_uses_module_entrypoint(self) -> None:
        self.assertEqual(ChatPodManager.worker_command, ["python", "-m", "sms_chatgpt.worker"])

    def test_poll_worker_response_parser_accepts_legacy_python_dict(self) -> None:
        parsed = PollPodManager._parse_worker_response("{'exists': True, 'expired': False}")

        self.assertEqual(parsed, {"exists": True, "expired": False})


class WorkerHistoryTest(unittest.TestCase):
    def test_save_and_load_history_round_trip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            history_file = Path(temp_dir) / "history.json"
            history = [
                {"role": "user", "content": "my name is Ada"},
                {"role": "assistant", "content": "Nice to meet you, Ada."},
            ]

            save_history(history_file, history, max_turns=12)

            self.assertEqual(load_history(history_file, max_turns=12), history)

    def test_trim_history_keeps_recent_turns(self) -> None:
        history = [
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
            {"role": "user", "content": "new user"},
            {"role": "assistant", "content": "new assistant"},
        ]

        self.assertEqual(
            trim_history(history, max_turns=1),
            [
                {"role": "user", "content": "new user"},
                {"role": "assistant", "content": "new assistant"},
            ],
        )


class PollsTest(unittest.TestCase):
    def test_detects_poll_intent(self) -> None:
        self.assertTrue(contains_poll_intent("Create a voting poll for the well", ["poll", "vote", "voting"]))
        self.assertFalse(contains_poll_intent("What is the weather?", ["poll", "vote", "voting"]))

    def test_hash_msisdn_uses_salt(self) -> None:
        first = hash_msisdn("+15551234567", "salt-a")
        second = hash_msisdn("+15551234567", "salt-a")
        different_salt = hash_msisdn("+15551234567", "salt-b")

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_salt)
        self.assertNotIn("+15551234567", first)

    def test_extracts_duration_and_yes_no_draft(self) -> None:
        draft = extract_draft_from_text("Create a Yes or No poll on funding to dig a local well for 60 seconds")

        self.assertEqual(draft.options, ["Yes", "No"])
        self.assertEqual(draft.duration_seconds, 60)
        self.assertIn("Funding to dig a local well", draft.question)

    def test_parse_duration_minutes(self) -> None:
        self.assertEqual(parse_duration_seconds("poll for 5 minutes"), 300)

    def test_classifies_valid_vote_by_number(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on well options: Yes, No for 60s")))

        decision = classify_vote("1", state, "voter")

        self.assertEqual(decision.kind, "valid")
        self.assertEqual(decision.option, "Yes")

    def test_classifies_short_yes_no_phrase_as_vote(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on well options: Yes, No for 60s")))

        yes_decision = classify_vote("Yes to fund for a well", state, "voter")
        no_decision = classify_vote("No to funding the well", state, "another-voter")
        ask_decision = classify_vote("Yes what is photosynthesis?", state, "curious-voter")

        self.assertEqual(yes_decision.kind, "valid")
        self.assertEqual(yes_decision.option, "Yes")
        self.assertEqual(no_decision.kind, "valid")
        self.assertEqual(no_decision.option, "No")
        self.assertEqual(ask_decision.kind, "ask")

    def test_classifies_non_vote_as_ask(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on well options: Yes, No for 60s")))

        decision = classify_vote("What is photosynthesis?", state, "voter")

        self.assertEqual(decision.kind, "ask")

    def test_rejects_creator_vote_and_duplicate_vote(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on well options: Yes, No for 60s")))

        creator_decision = classify_vote("yes", state, "creator")
        state = record_vote(state, "voter", "Yes")
        duplicate_decision = classify_vote("no", state, "voter")

        self.assertEqual(creator_decision.kind, "invalid")
        self.assertIn("creators cannot vote", creator_decision.reply or "")
        self.assertEqual(duplicate_decision.kind, "invalid")
        self.assertEqual(state.votes, {"voter": "Yes"})

    def test_formats_anonymous_counts(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on well options: Yes, No for 60s")))
        state = record_vote(state, "hash-one", "Yes")
        state = record_vote(state, "hash-two", "No")

        summary = format_counts(state)

        self.assertIn("Yes 1", summary)
        self.assertIn("No 1", summary)
        self.assertNotIn("hash-one", summary)

    def test_poll_llm_falls_back_when_openai_is_not_configured(self) -> None:
        llm = build_poll_llm("openai", None, "gpt-4o-mini")

        self.assertIn(
            "hello",
            llm.complete([{"role": "user", "content": "hello"}]),
        )


class LocalPollManagerTest(unittest.TestCase):
    def test_poll_flow_and_chat_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)

            draft = manager.handle_message(
                "+15550000001",
                "Create a poll on funding a local well options: Yes, No for 60 seconds",
            )
            early_vote = manager.handle_message("+15550000002", "Yes to fund for a well")
            started = manager.handle_message("+15550000001", "YES")
            vote = manager.handle_message("+15550000002", "Yes to fund for a well")
            duplicate = manager.handle_message("+15550000002", "2")
            creator_vote = manager.handle_message("+15550000001", "1")
            ask = manager.handle_message("+15550000003", "What is photosynthesis?")
            second_poll = manager.handle_message(
                "+15550000004",
                "Create another poll options: Tea, Coffee for 60 seconds",
            )

            self.assertTrue(draft.handled)
            self.assertIn("Poll draft", draft.reply or "")
            self.assertEqual(early_vote.reply, "Poll is not open yet.")
            self.assertIn("Poll started", started.reply or "")
            self.assertEqual(vote.reply, "Vote recorded: Yes.")
            self.assertEqual(duplicate.reply, "Your vote has already been recorded.")
            self.assertIn("creators cannot vote", creator_vote.reply or "")
            self.assertFalse(ask.handled)
            self.assertEqual(second_poll.reply, "A poll is already active.")
            state_text = state_file.read_text(encoding="utf-8")
            self.assertNotIn("+15550000001", state_text)
            self.assertNotIn("+15550000002", state_text)

    def test_incomplete_poll_request_asks_for_missing_details_without_llm(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file, llm_provider="openai", openai_api_key=None)
            manager = LocalPollManager(settings)

            draft = manager.handle_message("+15550000001", "poll to fund digging of a well")

            self.assertTrue(draft.handled)
            self.assertIn("Poll needs options, duration", draft.reply or "")

    def test_expired_poll_sends_creator_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)
            manager.handle_message(
                "+15550000001",
                "Create a poll on funding a local well options: Yes, No for 1 seconds",
            )
            manager.handle_message("+15550000001", "YES")
            manager.handle_message("+15550000002", "1")
            state = load_state(state_file)
            self.assertIsNotNone(state)
            state.expires_at = 0
            save_state(state_file, state)
            late_ask = manager.handle_message("+15550000003", "What is photosynthesis?")
            late_vote = manager.handle_message("+15550000004", "1")

            outbound = manager.close_expired()

            self.assertFalse(late_ask.handled)
            self.assertEqual(late_vote.reply, "This poll is closed.")
            self.assertEqual(outbound[0].recipient, "+15550000001")
            self.assertIn("Yes 1", outbound[0].body)
            finalized_state = load_state(state_file)
            self.assertIsNotNone(finalized_state)
            self.assertEqual(finalized_state.status, CLOSED)
            self.assertIsNotNone(finalized_state.result_reply)
            manager.ack_results_sent()
            self.assertIsNone(load_state(state_file))

    def test_daemon_deletes_poll_state_only_after_result_sms_send(self) -> None:
        manager = _FakePollManager()
        sms = _FakeSmsTransport()

        _send_poll_results(manager, sms)

        self.assertEqual(sms.sent, [("+15550000001", "Poll closed: Yes 1, No 0.")])
        self.assertTrue(manager.acked)


class _FakePollManager:
    def __init__(self) -> None:
        self.acked = False

    def close_expired(self):
        return [_FakeOutbound("+15550000001", "Poll closed: Yes 1, No 0.")]

    def ack_results_sent(self) -> None:
        self.acked = True


class _FakeOutbound:
    def __init__(self, recipient: str, body: str) -> None:
        self.recipient = recipient
        self.body = body


class _FakeSmsTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_sms(self, recipient: str, body: str) -> None:
        self.sent.append((recipient, body))


def _settings(
    poll_state_file: Path,
    llm_provider: str = "echo",
    openai_api_key: str | None = None,
) -> Settings:
    return Settings(
        sms_backend="mock",
        sms_serial_port="/dev/ttyUSB0",
        sms_baudrate=115200,
        sms_poll_seconds=1,
        sms_message_status="REC UNREAD",
        sms_storage=None,
        adb_path="adb",
        adb_serial=None,
        adb_send_mode="log",
        adb_send_command_template=None,
        adb_state_file="./adb-sms-state.txt",
        adb_skip_existing=True,
        session_backend="local",
        kubernetes_namespace="default",
        chat_pod_image="sms-chatgpt:latest",
        chat_pod_idle_seconds=60,
        chat_pod_timeout_seconds=30,
        chat_history_file="/tmp/sms-chatgpt-history.json",
        chat_history_max_turns=12,
        poll_enabled=True,
        poll_keywords=["poll", "vote", "voting"],
        poll_state_file=str(poll_state_file),
        poll_pod_name="sms-poll-active",
        poll_hash_salt="test-salt",
        llm_provider=llm_provider,
        openai_api_key=openai_api_key,
        openai_model="gpt-4o-mini",
        mock_inbox_file="./mock-inbox.txt",
        mock_outbox_file="./mock-outbox.txt",
    )


if __name__ == "__main__":
    unittest.main()
