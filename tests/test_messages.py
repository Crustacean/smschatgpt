import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sms_chatgpt.messages import SMS_REPLY_LIMIT, clamp_sms_reply
from sms_chatgpt.sms import AdbSmsTransport


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


if __name__ == "__main__":
    unittest.main()
