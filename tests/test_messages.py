import unittest

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


if __name__ == "__main__":
    unittest.main()
