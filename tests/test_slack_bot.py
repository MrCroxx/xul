import unittest

from xul_slackbot.bot import build_mention_reply, should_ignore_message_event


class SlackBotTestCase(unittest.TestCase):
    def test_build_mention_reply(self) -> None:
        self.assertEqual(
            build_mention_reply("<@U123> hello"),
            "Received: <@U123> hello",
        )

    def test_should_ignore_bot_message_subtype(self) -> None:
        self.assertTrue(should_ignore_message_event({"subtype": "bot_message"}))

    def test_should_ignore_message_with_bot_id(self) -> None:
        self.assertTrue(should_ignore_message_event({"bot_id": "B123"}))

    def test_should_not_ignore_user_message(self) -> None:
        self.assertFalse(should_ignore_message_event({"text": "hello"}))


if __name__ == "__main__":
    unittest.main()
