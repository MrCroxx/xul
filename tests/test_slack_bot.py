import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xul_slackbot.bot import (
    DEFAULT_LANCEDB_DIR,
    DEFAULT_NECROMANCY_SQLITE,
    build_app_mention_reply,
    build_arg_parser,
    build_mention_reply,
    extract_mention_command,
    resolve_thread_reply_ts,
    should_handle_mecromancy_mention,
    should_ignore_message_event,
)
from xul_slackbot.lancedb import connect_lancedb
from xul_slackbot.necromancy import connect_necromancy_db, handle_mecromancy_command


class SlackBotTestCase(unittest.TestCase):
    def test_build_arg_parser_uses_default_lancedb_dir(self) -> None:
        args = build_arg_parser().parse_args([])

        self.assertEqual(Path(args.lancedb_dir), DEFAULT_LANCEDB_DIR)

    def test_build_arg_parser_accepts_lancedb_dir_override(self) -> None:
        args = build_arg_parser().parse_args(["--lancedb-dir", "/tmp/xul-lancedb"])

        self.assertEqual(args.lancedb_dir, "/tmp/xul-lancedb")

    def test_build_arg_parser_uses_default_necromancy_sqlite(self) -> None:
        args = build_arg_parser().parse_args([])

        self.assertEqual(Path(args.necromancy_sqlite), DEFAULT_NECROMANCY_SQLITE)

    def test_build_mention_reply(self) -> None:
        self.assertEqual(
            build_mention_reply("<@U123> hello"),
            "Received: <@U123> hello",
        )

    def test_extract_mention_command(self) -> None:
        self.assertEqual(
            extract_mention_command("<@U123> /necromancy github tabversion"),
            "/necromancy github tabversion",
        )

    def test_should_handle_mecromancy_mention_accepts_alias(self) -> None:
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /mecromancy slack xiang"))
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /necromancy slack xiang"))
        self.assertFalse(should_handle_mecromancy_mention("<@U123> hello"))

    def test_resolve_thread_reply_ts_prefers_existing_thread(self) -> None:
        self.assertEqual(
            resolve_thread_reply_ts({"ts": "100.0", "thread_ts": "90.0"}),
            "90.0",
        )
        self.assertEqual(resolve_thread_reply_ts({"ts": "100.0"}), "100.0")

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

    def test_handle_mecromancy_command_queries_and_links_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "necromancy.sqlite"
            conn = connect_necromancy_db(db_path)
            conn.execute(
                """
                INSERT INTO slack_users(user_id, username, display_name, real_name, email)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("U1", "xiangyu", "Xiangyu", "Xiangyu Hu", "xiangyu@example.com"),
            )
            conn.execute(
                """
                INSERT INTO github_users(
                    login,
                    issue_or_pr_authored,
                    issue_comments_authored,
                    pr_reviews_authored,
                    pr_review_comments_authored,
                    mentions
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("tabversion", 1, 2, 3, 4, 5),
            )
            conn.commit()
            conn.close()

            slack_response = handle_mecromancy_command(db_path, "slack xiang")
            self.assertIn("xiangyu [U1]", slack_response)

            github_response = handle_mecromancy_command(db_path, "github tab")
            self.assertIn("tabversion", github_response)

            missing_link_response = handle_mecromancy_command(
                db_path, "link missing-user tabversion"
            )
            self.assertEqual(
                missing_link_response, "Slack mecromancy not found: missing-user"
            )

            linked_response = handle_mecromancy_command(
                db_path, "link xiangyu tabversion"
            )
            self.assertIn("Linked mecromancy:", linked_response)

            links_response = handle_mecromancy_command(db_path, "links")
            self.assertIn("Mecromancy links:", links_response)
            self.assertIn("slack: xiangyu [U1]", links_response)
            self.assertIn("github: tabversion", links_response)

            slack_after_link = handle_mecromancy_command(db_path, "slack xiangyu")
            self.assertIn("linked_github=tabversion", slack_after_link)

            github_after_link = handle_mecromancy_command(db_path, "github tabversion")
            self.assertIn("linked_slack=U1", github_after_link)

    def test_build_app_mention_reply_routes_necromancy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "necromancy.sqlite"
            conn = connect_necromancy_db(db_path)
            conn.execute(
                """
                INSERT INTO github_users(
                    login,
                    issue_or_pr_authored,
                    issue_comments_authored,
                    pr_reviews_authored,
                    pr_review_comments_authored,
                    mentions
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("tabversion", 1, 2, 3, 4, 5),
            )
            conn.commit()
            conn.close()

            response = build_app_mention_reply(
                db_path, "<@U123> /necromancy github tab"
            )
            self.assertIn("GitHub mecromancy results for `tab`:", response)

    def test_build_app_mention_reply_keeps_non_command_echo(self) -> None:
        response = build_app_mention_reply(
            DEFAULT_NECROMANCY_SQLITE, "<@U123> hello there"
        )
        self.assertEqual(response, "Received: <@U123> hello there")


if __name__ == "__main__":
    unittest.main()
