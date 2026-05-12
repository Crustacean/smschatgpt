import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sms_chatgpt.config import Settings
from sms_chatgpt.daemon import _send_poll_results
from sms_chatgpt.k8s import ChatPodManager, PollPodManager
from sms_chatgpt.llm import OpenAiLlmClient
from sms_chatgpt.messages import (
    SMS_REPLY_LIMIT,
    SmsValidationError,
    clamp_sms_reply,
    max_tokens_for_sms_limit,
    validate_inbound_sms,
)
from sms_chatgpt.poll_manager import LocalPollManager
from sms_chatgpt.poll_worker import (
    amend_poll,
    build_poll_llm,
    classify_vote_with_llm,
    extract_draft,
    load_state,
    resolve_pending_vote_with_llm,
    save_state,
    status_poll,
    timeout_pending_poll,
)
from sms_chatgpt.polls import (
    build_pending_poll,
    classify_vote,
    confirm_poll,
    contains_poll_intent,
    CLOSED,
    extract_draft_from_text,
    format_counts,
    hash_msisdn,
    is_contextless_vote,
    parse_duration_seconds,
    record_vote,
    resolve_pending_vote,
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

    def test_validate_inbound_sms_removes_control_chars(self) -> None:
        self.assertEqual(validate_inbound_sms("hello\x00\nthere"), "hello there")

    def test_validate_inbound_sms_rejects_oversized_messages(self) -> None:
        with self.assertRaises(SmsValidationError):
            validate_inbound_sms("x" * 11, limit=10)

    def test_openai_chat_prompt_uses_reply_limit(self) -> None:
        client = OpenAiLlmClient.__new__(OpenAiLlmClient)
        captured = {}

        def complete(messages, max_tokens=160, temperature=0.4):
            captured["messages"] = messages
            captured["max_tokens"] = max_tokens
            captured["temperature"] = temperature
            return "short reply"

        client.complete = complete

        reply = OpenAiLlmClient.respond(client, "hello", reply_limit=80)

        self.assertEqual(reply, "short reply")
        self.assertIn("80 characters or fewer", captured["messages"][0]["content"])
        self.assertEqual(captured["max_tokens"], max_tokens_for_sms_limit(80))

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
        self.assertTrue(contains_poll_intent("Tengeneza kura ya maoni kuhusu maktaba ya jamii", ["poll", "vote", "voting"]))
        self.assertTrue(contains_poll_intent("Crear una encuesta para el pozo", ["poll", "vote", "voting"]))
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

    def test_extracts_yes_no_from_positive_negative_context(self) -> None:
        draft = extract_draft_from_text(
            "Start a poll to build or not to build a community library. Run for 90s"
        )

        self.assertEqual(draft.question, "Build a community library?")
        self.assertEqual(draft.options, ["Yes", "No"])
        self.assertEqual(draft.duration_seconds, 90)
        self.assertEqual(draft.missing, [])

    def test_extracts_swahili_positive_negative_poll_draft(self) -> None:
        draft = extract_draft_from_text(
            "Tengeneza kura ya maoni kujenga au kutojenga maktaba ya jamii kwa sekunde 90"
        )

        self.assertEqual(draft.question, "Kujenga maktaba ya jamii?")
        self.assertEqual(draft.options, ["Ndio", "Hapana"])
        self.assertEqual(draft.duration_seconds, 90)
        self.assertEqual(draft.missing, [])

    def test_extracts_swahili_explicit_options(self) -> None:
        draft = extract_draft_from_text(
            "Tengeneza kura ya maoni kuhusu maktaba ya jamii chaguo: Ndio au Hapana kwa sekunde 90"
        )

        self.assertEqual(draft.options, ["Ndio", "Hapana"])
        self.assertEqual(draft.duration_seconds, 90)
        self.assertEqual(draft.missing, [])

    def test_parse_duration_minutes(self) -> None:
        self.assertEqual(parse_duration_seconds("poll for 5 minutes"), 300)
        self.assertEqual(parse_duration_seconds("kura ya maoni kwa dakika 5"), 300)

    def test_classifies_valid_vote_by_number(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on well options: Yes, No for 60s")))

        decision = classify_vote("1", state, "voter")

        self.assertEqual(decision.kind, "valid")
        self.assertEqual(decision.option, "Yes")

    def test_classifies_short_yes_no_phrase_as_vote(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll on funding digging a well options: Yes, No for 60s")))
        wall_state = confirm_poll(build_pending_poll("wall-creator", extract_draft_from_text("poll on building that wall options: Yes, No for 60s")))

        yes_decision = classify_vote("Yes, fund the well", state, "voter")
        support_decision = classify_vote("I am for funding for a well", state, "support-voter")
        no_decision = classify_vote("do not fund for a well", state, "another-voter")
        unrelated_decision = classify_vote("Yes build that wall", state, "wall-voter")
        wall_decision = classify_vote("Yes build that wall", wall_state, "wall-voter")
        ask_decision = classify_vote("Yes what is photosynthesis?", state, "curious-voter")

        self.assertEqual(yes_decision.kind, "valid")
        self.assertEqual(yes_decision.option, "Yes")
        self.assertEqual(support_decision.kind, "valid")
        self.assertEqual(support_decision.option, "Yes")
        self.assertEqual(no_decision.kind, "valid")
        self.assertEqual(no_decision.option, "No")
        self.assertEqual(unrelated_decision.kind, "ask")
        self.assertEqual(wall_decision.kind, "valid")
        self.assertEqual(wall_decision.option, "Yes")
        self.assertEqual(ask_decision.kind, "ask")

    def test_classifies_contextual_yes_no_vote_without_option_word(self) -> None:
        state = confirm_poll(build_pending_poll("creator", extract_draft_from_text("poll to build a school options: Yes, No for 60s")))

        positive = classify_vote("build the school", state, "voter")
        negative = classify_vote("building a school is not in my interest", state, "another-voter")
        pending_number = resolve_pending_vote("1", "build the school", state, "number-voter")
        pending_maybe = resolve_pending_vote("Maybe", "build the school", state, "maybe-voter")

        self.assertEqual(positive.kind, "valid")
        self.assertEqual(positive.option, "Yes")
        self.assertEqual(negative.kind, "valid")
        self.assertEqual(negative.option, "No")
        self.assertEqual(pending_number.kind, "valid")
        self.assertEqual(pending_number.option, "Yes")
        self.assertEqual(pending_maybe.kind, "ask")

    def test_classifies_multilingual_contextual_vote(self) -> None:
        state = confirm_poll(
            build_pending_poll(
                "creator",
                extract_draft_from_text("Create a poll to build a school library options: Yes, No for 60s"),
            )
        )

        swahili_positive = classify_vote("Ninakubali kujenga maktaba ya shule", state, "voter")
        swahili_context_only = classify_vote("kujenga maktaba ya shule", state, "another-voter")
        swahili_negative = classify_vote("Sipendi kujenga maktaba ya shule", state, "third-voter")
        unrelated_build = classify_vote("Yes build that wall", state, "wall-voter")

        self.assertEqual(swahili_positive.kind, "valid")
        self.assertEqual(swahili_positive.option, "Yes")
        self.assertEqual(swahili_context_only.kind, "valid")
        self.assertEqual(swahili_context_only.option, "Yes")
        self.assertEqual(swahili_negative.kind, "valid")
        self.assertEqual(swahili_negative.option, "No")
        self.assertEqual(unrelated_build.kind, "ask")

    def test_llm_classifies_vote_in_arbitrary_language(self) -> None:
        state = confirm_poll(
            build_pending_poll(
                "creator",
                extract_draft_from_text("Create a poll to build a school library options: Yes, No for 60s"),
            )
        )
        llm = _JsonLlm(['{"matches":true,"option":"Yes","language":"es"}'])

        decision = classify_vote_with_llm(
            "Estoy a favor de construir la biblioteca escolar",
            state,
            "voter",
            llm,
        )

        self.assertEqual(decision.kind, "valid")
        self.assertEqual(decision.option, "Yes")
        self.assertIn("any language", llm.prompts[0])

    def test_llm_resolves_pending_vote_context_in_arbitrary_language(self) -> None:
        state = confirm_poll(
            build_pending_poll(
                "creator",
                extract_draft_from_text("Create a poll to build a school library options: Yes, No for 60s"),
            )
        )
        llm = _JsonLlm(['{"matches":true,"option":"No","language":"fr"}'])

        decision = resolve_pending_vote_with_llm(
            "No",
            "Je refuse de construire la bibliotheque scolaire",
            state,
            "voter",
            llm,
        )

        self.assertEqual(decision.kind, "valid")
        self.assertEqual(decision.option, "No")

    def test_contextless_votes_include_common_languages(self) -> None:
        self.assertTrue(is_contextless_vote("Sí"))
        self.assertTrue(is_contextless_vote("Oui"))
        self.assertTrue(is_contextless_vote("Não"))
        self.assertTrue(is_contextless_vote("Peut-être"))

    def test_openai_detected_language_code_is_preserved(self) -> None:
        llm = _JsonLlm(
            [
                (
                    '{"question":"Construir una biblioteca escolar?",'
                    '"options":["Si","No"],"duration_seconds":60,"language":"es"}'
                )
            ]
        )

        draft = extract_draft("Crear una encuesta para construir una biblioteca escolar por 60 segundos", llm)
        state = build_pending_poll("creator", draft)

        self.assertEqual(draft.language, "es")
        self.assertEqual(state.language, "es")

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

    def test_bare_amend_prompts_for_details_without_overwriting_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "poll.json"
            state = build_pending_poll("creator", extract_draft_from_text("poll to fund digging of a well"))
            save_state(state_path, state)

            result = amend_poll(state_path, "AMEND", build_poll_llm("echo", None, "unused"))
            saved = load_state(state_path)

            self.assertIn("Send AMEND <details>", result["reply"])
            self.assertIsNotNone(saved)
            self.assertIn("fund digging", saved.question)

    def test_pending_poll_status_expires_after_idle_timeout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "poll.json"
            state = build_pending_poll("creator", extract_draft_from_text("poll to fund digging of a well"))
            state.last_activity_at = 0
            save_state(state_path, state)

            status = status_poll(state_path, pending_idle_seconds=60)
            result = timeout_pending_poll(state_path)
            saved = load_state(state_path)

            self.assertTrue(status["expired"])
            self.assertIn("waited too long", result["reply"])
            self.assertIsNotNone(saved)
            self.assertEqual(saved.status, CLOSED)

    def test_pending_poll_timeout_uses_creator_language(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "poll.json"
            state = build_pending_poll("creator", extract_draft_from_text("poll to fund digging of a well"))
            state.language = "es"
            save_state(state_path, state)
            llm = _JsonLlm(["La encuesta espero demasiado y fue cancelada."])

            result = timeout_pending_poll(state_path, llm)
            saved = load_state(state_path)

            self.assertEqual(result["reply"], "La encuesta espero demasiado y fue cancelada.")
            self.assertIsNotNone(saved)
            self.assertEqual(saved.result_reply, "La encuesta espero demasiado y fue cancelada.")


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
            same_creator_second = manager.handle_message(
                "+15550000001",
                "Create another poll options: Tea, Coffee for 60 seconds",
            )
            second_poll = manager.handle_message(
                "+15550000004",
                "Create another poll options: Tea, Coffee for 60 seconds",
            )
            second_started = manager.handle_message("+15550000004", "YES")
            creator_votes_other = manager.handle_message("+15550000001", "Tea")

            self.assertTrue(draft.handled)
            self.assertIn("Poll draft", draft.reply or "")
            self.assertEqual(early_vote.reply, "Poll is not open yet.")
            self.assertIn("Poll started", started.reply or "")
            self.assertEqual(vote.reply, "Vote recorded: Yes.")
            self.assertEqual(duplicate.reply, "Your vote has already been recorded.")
            self.assertIn("creators cannot vote", creator_vote.reply or "")
            self.assertFalse(ask.handled)
            self.assertEqual(same_creator_second.reply, "You have an ongoing poll.")
            self.assertIn("Poll draft", second_poll.reply or "")
            self.assertIn("Poll started", second_started.reply or "")
            self.assertEqual(creator_votes_other.reply, "Vote recorded: Tea.")
            state_text = _local_state_text(state_file)
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

    def test_pending_poll_timeout_notifies_creator_then_deletes_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file, poll_pending_idle_seconds=60)
            manager = LocalPollManager(settings)

            manager.handle_message("+15550000001", "poll to fund digging of a well")
            creator_hash = hash_msisdn("+15550000001", settings.poll_hash_salt)
            state_path = manager._state_path(creator_hash)
            state = load_state(state_path)
            self.assertIsNotNone(state)
            state.last_activity_at = 0
            save_state(state_path, state)

            outbound = manager.close_expired()

            self.assertEqual(outbound[0].recipient, "+15550000001")
            self.assertIn("waited too long", outbound[0].body)
            self.assertIsNotNone(load_state(state_path))
            manager.ack_results_sent(outbound)
            self.assertIsNone(load_state(state_path))

    def test_pending_poll_timeout_outbound_uses_creator_language(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file, poll_pending_idle_seconds=60)
            manager = LocalPollManager(settings)

            manager.handle_message("+15550000001", "poll to fund digging of a well")
            creator_hash = hash_msisdn("+15550000001", settings.poll_hash_salt)
            state_path = manager._state_path(creator_hash)
            state = load_state(state_path)
            self.assertIsNotNone(state)
            state.language = "es"
            state.last_activity_at = 0
            save_state(state_path, state)
            manager.llm = _JsonLlm(["La encuesta espero demasiado y fue cancelada."])

            outbound = manager.close_expired()

            self.assertEqual(outbound[0].body, "La encuesta espero demasiado y fue cancelada.")

    def test_swahili_poll_request_starts_poll_flow_without_llm(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file, llm_provider="openai", openai_api_key=None)
            manager = LocalPollManager(settings)

            draft = manager.handle_message(
                "+15550000001",
                "Tengeneza kura ya maoni kujenga au kutojenga maktaba ya jamii kwa sekunde 90",
            )

            self.assertTrue(draft.handled)
            self.assertIn("Rasimu ya kura", draft.reply or "")
            self.assertIn("Chaguo: 1) Ndio 2) Hapana", draft.reply or "")
            self.assertIn("Muda: 90s", draft.reply or "")

    def test_english_poll_accepts_swahili_contextual_vote(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)

            manager.handle_message(
                "+15550000001",
                "Create a poll to build a school library options: Yes, No for 60 seconds",
            )
            manager.handle_message("+15550000001", "YES")
            vote = manager.handle_message("+15550000002", "Ninakubali kujenga maktaba ya shule")
            unrelated = manager.handle_message("+15550000003", "Yes build that wall")

            self.assertEqual(vote.reply, "Vote recorded: Yes.")
            self.assertFalse(unrelated.handled)

    def test_english_poll_accepts_openai_classified_vote_language(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)

            manager.handle_message(
                "+15550000001",
                "Create a poll to build a school library options: Yes, No for 60 seconds",
            )
            manager.handle_message("+15550000001", "YES")
            manager.llm = _JsonLlm(
                [
                    '{"contextless_vote":false,"language":"es"}',
                    '{"matches":true,"option":"Yes","language":"es"}',
                ]
            )
            vote = manager.handle_message("+15550000002", "Estoy a favor de construir la biblioteca escolar")

            self.assertEqual(vote.reply, "Vote recorded: Yes.")

    def test_openai_detects_contextless_vote_in_other_language(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)

            manager.handle_message(
                "+15550000001",
                "Create a poll to build a school library options: Yes, No for 60 seconds",
            )
            manager.handle_message("+15550000001", "YES")
            manager.llm = _JsonLlm(['{"contextless_vote":true,"language":"ar"}'])
            pending = manager.handle_message("+15550000002", "نعم")

            self.assertIn("Which poll is this vote for?", pending.reply or "")

    def test_contextless_vote_in_other_language_goes_pending(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)

            manager.handle_message(
                "+15550000001",
                "Create a poll to build a school library options: Yes, No for 60 seconds",
            )
            manager.handle_message("+15550000001", "YES")
            pending = manager.handle_message("+15550000002", "Sí")

            self.assertIn("Which poll is this vote for?", pending.reply or "")

    def test_swahili_poll_system_replies_match_creator_language(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)

            draft = manager.handle_message(
                "+15550000001",
                "Tengeneza kura ya maoni kujenga au kutojenga maktaba ya jamii kwa sekunde 1",
            )
            started = manager.handle_message("+15550000001", "NDIYO")
            vote = manager.handle_message("+15550000002", "ndio kujenga maktaba")
            duplicate = manager.handle_message("+15550000002", "hapana kujenga maktaba")
            creator_vote = manager.handle_message("+15550000001", "ndio kujenga maktaba")
            contextless = manager.handle_message("+15550000003", "ndio")
            creator_hash = hash_msisdn("+15550000001", settings.poll_hash_salt)
            state_path = manager._state_path(creator_hash)
            state = load_state(state_path)
            self.assertIsNotNone(state)
            state.expires_at = 0
            save_state(state_path, state)
            late_context = manager.handle_message("+15550000003", "kujenga maktaba")
            outbound = manager.close_expired()

            self.assertIn("Rasimu ya kura", draft.reply or "")
            self.assertIn("Kura imeanza", started.reply or "")
            self.assertEqual(vote.reply, "Kura imerekodiwa: Ndio.")
            self.assertEqual(duplicate.reply, "Kura yako imesharekodiwa.")
            self.assertIn("hawezi kupiga kura", creator_vote.reply or "")
            self.assertIn("Kura ipi?", contextless.reply or "")
            self.assertIn("ilifungwa", late_context.reply or "")
            self.assertEqual(outbound[0].recipient, "+15550000001")
            self.assertIn("Kura imefungwa", outbound[0].body)

    def test_creator_can_amend_missing_poll_details_after_bare_amend(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file, llm_provider="openai", openai_api_key=None)
            manager = LocalPollManager(settings)

            manager.handle_message("+15550000001", "poll to fund digging of a well")
            bare_amend = manager.handle_message("+15550000001", "AMEND")
            amended = manager.handle_message("+15550000001", "options: Yes, No for 60 seconds")

            self.assertIn("Send AMEND <details>", bare_amend.reply or "")
            self.assertIn("Poll draft", amended.reply or "")
            self.assertIn("Options: 1) Yes 2) No", amended.reply or "")
            self.assertIn("Duration: 60s", amended.reply or "")

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
            manager.handle_message("+15550000002", "Yes funding local well")
            creator_hash = hash_msisdn("+15550000001", settings.poll_hash_salt)
            state_path = manager._state_path(creator_hash)
            state = load_state(state_path)
            self.assertIsNotNone(state)
            state.expires_at = 0
            save_state(state_path, state)
            late_ask = manager.handle_message("+15550000003", "What is photosynthesis?")
            late_vote = manager.handle_message("+15550000004", "1")

            outbound = manager.close_expired()

            self.assertFalse(late_ask.handled)
            self.assertEqual(late_vote.reply, "This poll is closed.")
            self.assertEqual(outbound[0].recipient, "+15550000001")
            self.assertIn("Yes 1", outbound[0].body)
            finalized_state = load_state(state_path)
            self.assertIsNotNone(finalized_state)
            self.assertEqual(finalized_state.status, CLOSED)
            self.assertIsNotNone(finalized_state.result_reply)
            manager.ack_results_sent(outbound)
            self.assertIsNone(load_state(state_path))

    def test_ambiguous_vote_across_multiple_polls_is_not_counted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)
            manager.handle_message("+15550000001", "poll on a local well options: Yes, No for 60 seconds")
            manager.handle_message("+15550000001", "YES")
            manager.handle_message("+15550000002", "poll on a school roof options: Yes, No for 60 seconds")
            manager.handle_message("+15550000002", "YES")

            vote = manager.handle_message("+15550000003", "Yes")
            clarified = manager.handle_message("+15550000003", "local well")

            self.assertIn("Which poll is this vote for?", vote.reply or "")
            self.assertEqual(clarified.reply, "Vote recorded: Yes.")

    def test_contextless_vote_expires_without_context(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "poll.json"
            settings = _settings(state_file)
            manager = LocalPollManager(settings)
            manager.handle_message("+15550000001", "poll to build a school options: Yes, No for 1 seconds")
            manager.handle_message("+15550000001", "YES")

            pending = manager.handle_message("+15550000002", "Maybe")
            creator_hash = hash_msisdn("+15550000001", settings.poll_hash_salt)
            state_path = manager._state_path(creator_hash)
            state = load_state(state_path)
            self.assertIsNotNone(state)
            state.expires_at = 0
            save_state(state_path, state)
            late_context = manager.handle_message("+15550000002", "build the school")

            self.assertIn("Which poll is this vote for?", pending.reply or "")
            self.assertIn("pending vote expired", late_context.reply or "")
            expired_state = load_state(state_path)
            self.assertIsNotNone(expired_state)
            self.assertEqual(expired_state.votes, {})

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

    def ack_results_sent(self, outbound_messages=None) -> None:
        del outbound_messages
        self.acked = True


class _FakeOutbound:
    def __init__(self, recipient: str, body: str) -> None:
        self.recipient = recipient
        self.body = body
        self.poll_id = "poll-one"


class _FakeSmsTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_sms(self, recipient: str, body: str) -> None:
        self.sent.append((recipient, body))


class _JsonLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def respond(self, message: str, history=None, reply_limit: int = 140) -> str:
        del history, reply_limit
        return message

    def complete(self, messages, max_tokens: int = 160, temperature: float = 0.4) -> str:
        del max_tokens, temperature
        self.prompts.append(messages[-1]["content"])
        if not self.responses:
            return '{"matches":false,"option":null,"language":null}'
        return self.responses.pop(0)


def _settings(
    poll_state_file: Path,
    llm_provider: str = "echo",
    openai_api_key: str | None = None,
    poll_pending_idle_seconds: int = 60,
) -> Settings:
    return Settings(
        sms_backend="mock",
        sms_serial_port="/dev/ttyUSB0",
        sms_baudrate=115200,
        sms_poll_seconds=1,
        sms_message_status="REC UNREAD",
        sms_storage=None,
        sms_reply_limit=140,
        sms_inbound_limit=1000,
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
        poll_pending_idle_seconds=poll_pending_idle_seconds,
        llm_provider=llm_provider,
        openai_api_key=openai_api_key,
        openai_model="gpt-4o-mini",
        mock_inbox_file="./mock-inbox.txt",
        mock_outbox_file="./mock-outbox.txt",
    )


def _local_state_text(state_file: Path) -> str:
    state_dir = state_file.with_name(f"{state_file.name}.d")
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(state_dir.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
