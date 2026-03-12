import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xul_slackbot.bot import (
    DEFAULT_LANCEDB_DIR,
    build_arg_parser,
    build_mention_reply,
    should_ignore_message_event,
)
from xul_slackbot.lancedb import connect_lancedb


class SlackBotTestCase(unittest.TestCase):
    def test_build_arg_parser_uses_default_lancedb_dir(self) -> None:
        args = build_arg_parser().parse_args([])

        self.assertEqual(Path(args.lancedb_dir), DEFAULT_LANCEDB_DIR)

    def test_build_arg_parser_accepts_lancedb_dir_override(self) -> None:
        args = build_arg_parser().parse_args(["--lancedb-dir", "/tmp/xul-lancedb"])

        self.assertEqual(args.lancedb_dir, "/tmp/xul-lancedb")

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

    def test_connect_lancedb_creates_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_dir = Path(tmpdir) / "nested" / "lancedb"
            connect_calls: list[str] = []
            fake_module = SimpleNamespace(
                connect=lambda uri: connect_calls.append(uri) or {"uri": uri}
            )

            with patch.dict(sys.modules, {"lancedb": fake_module}):
                result = connect_lancedb(db_dir)

            self.assertTrue(db_dir.exists())
            self.assertEqual(connect_calls, [str(db_dir.resolve())])
            self.assertEqual(result, {"uri": str(db_dir.resolve())})


if __name__ == "__main__":
    unittest.main()
