import unittest

from sms_chatgpt.messages import SMS_REPLY_LIMIT, clamp_sms_reply


class ClampSmsReplyTest(unittest.TestCase):
    def test_clamp_sms_reply_compacts_whitespace(self) -> None:
        self.assertEqual(clamp_sms_reply("hello\n\nthere"), "hello there")

    def test_clamp_sms_reply_limits_to_140_characters(self) -> None:
        reply = clamp_sms_reply("x" * 200)

        self.assertEqual(len(reply), SMS_REPLY_LIMIT)
        self.assertTrue(reply.endswith("..."))


if __name__ == "__main__":
    unittest.main()
