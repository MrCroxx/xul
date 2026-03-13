import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xul_slackbot.config import get_config_value, get_required_config_value
from xul_slackbot.bot import (
    DEFAULT_LANCEDB_DIR,
    DEFAULT_NECROMANCY_SQLITE,
    RECEIVED_REACTION,
    REPLIED_REACTION,
    add_message_reaction,
    build_app_mention_reply,
    build_arg_parser,
    build_mention_reply,
    emit_slash_progress,
    emit_thread_progress,
    extract_mention_command,
    resolve_thread_reply_ts,
    should_handle_mecromancy_mention,
    should_ignore_message_event,
)
from xul_slackbot.lancedb import connect_lancedb
from xul_slackbot.necromancy import connect_necromancy_db, handle_mecromancy_command
from xul_slackbot.summon import (
    build_summon_prompts,
    build_summoned_reply,
    get_active_summon,
    handle_summon_command,
    init_summon_schema,
)


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
            extract_mention_command("<@U123> /github tabversion"),
            "/github tabversion",
        )

    def test_should_handle_mecromancy_mention_accepts_direct_commands(self) -> None:
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /slack xiang"))
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /github tabversion"))
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /link xiangyu tabversion"))
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /links"))
        self.assertTrue(should_handle_mecromancy_mention("<@U123> /summon xiangyu"))
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

    def test_config_prefers_environment_then_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_path = Path(tmpdir) / ".env"
            dotenv_path.write_text(
                "OPENAI_API_KEY=dotenv-key\nOPENAI_BASE_URL=https://dotenv.example/v1\n",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "env-key"},
                clear=False,
            ):
                self.assertEqual(
                    get_required_config_value("OPENAI_API_KEY", dotenv_path),
                    "env-key",
                )

            with patch.dict("os.environ", {}, clear=False):
                self.assertEqual(
                    get_required_config_value("OPENAI_API_KEY", dotenv_path),
                    "dotenv-key",
                )
                self.assertEqual(
                    get_config_value(
                        "OPENAI_BASE_URL",
                        "https://api.openai.com/v1",
                        dotenv_path,
                    ),
                    "https://dotenv.example/v1",
                )

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

    def test_build_app_mention_reply_routes_direct_commands(self) -> None:
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

            response = build_app_mention_reply(db_path, object(), "<@U123> /github tab")
            self.assertIn("GitHub mecromancy results for `tab`:", response)

    def test_build_app_mention_reply_keeps_non_command_echo(self) -> None:
        with patch("xul_slackbot.bot.build_summoned_reply", return_value="persona reply"):
            response = build_app_mention_reply(
                DEFAULT_NECROMANCY_SQLITE, object(), "<@U123> hello there"
            )
        self.assertEqual(response, "persona reply")

    def test_build_app_mention_reply_routes_summon_command(self) -> None:
        with patch(
            "xul_slackbot.bot.handle_summon_command",
            return_value="summoned",
        ) as mocked:
            response = build_app_mention_reply(
                DEFAULT_NECROMANCY_SQLITE,
                object(),
                "<@U123> /summon xiangyu",
            )
        self.assertEqual(response, "summoned")
        mocked.assert_called_once()

    def test_emit_progress_helpers(self) -> None:
        slash_messages: list[str] = []
        thread_messages: list[tuple[str, str | None]] = []

        emit_slash_progress(slash_messages.append, 30, "Checking local context dumps")
        emit_thread_progress(
            lambda text, thread_ts=None: thread_messages.append((text, thread_ts)),
            "123.456",
            70,
            "Building isolated LanceDB table",
        )

        self.assertEqual(
            slash_messages,
            ["[30%] Checking local context dumps"],
        )
        self.assertEqual(
            thread_messages,
            [("[70%] Building isolated LanceDB table", "123.456")],
        )

    def test_add_message_reaction_uses_slack_client(self) -> None:
        calls: list[dict[str, str]] = []

        class FakeClient:
            def reactions_add(self, *, channel: str, timestamp: str, name: str) -> None:
                calls.append(
                    {"channel": channel, "timestamp": timestamp, "name": name}
                )

        add_message_reaction(
            FakeClient(),
            {"channel": "C1", "ts": "100.0"},
            RECEIVED_REACTION,
            logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        add_message_reaction(
            FakeClient(),
            {"channel": "C1", "ts": "100.0"},
            REPLIED_REACTION,
            logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        )

        self.assertEqual(
            calls,
            [
                {"channel": "C1", "timestamp": "100.0", "name": "eyes"},
                {"channel": "C1", "timestamp": "100.0", "name": "smiling_imp"},
            ],
        )

    def test_handle_summon_command_activates_linked_necromancy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "necromancy.sqlite"
            conn = connect_necromancy_db(db_path)
            init_summon_schema(conn)
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
            conn.execute(
                """
                INSERT INTO necromancy_links(slack_user_id, github_login, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ("U1", "tabversion"),
            )
            conn.commit()
            conn.close()

            fake_lancedb = object()
            slack_dump = Path(tmpdir) / "data" / "user_context_exports" / "slack" / "slack_user_xiangyu.sqlite"
            github_dump = Path(tmpdir) / "data" / "user_context_exports" / "github" / "github_user_tabversion.sqlite"

            with patch(
                "xul_slackbot.summon.ensure_context_dumps",
                return_value=(slack_dump, github_dump),
            ), patch(
                "xul_slackbot.summon.ensure_summon_lancedb_table",
                return_value=("summon_xiangyu_tabversion", 7),
            ):
                progress_updates: list[tuple[int, str]] = []
                response = handle_summon_command(
                    db_path,
                    fake_lancedb,
                    "xiangyu",
                    data_dir=Path(tmpdir) / "data",
                    progress=lambda percent, message: progress_updates.append(
                        (percent, message)
                    ),
                )

            self.assertIn("Summoned necromancy:", response)
            self.assertIn("documents: 7", response)
            self.assertEqual(
                [percent for percent, _ in progress_updates],
                [10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            )

            conn = connect_necromancy_db(db_path)
            init_summon_schema(conn)
            active = get_active_summon(conn)
            conn.close()
            self.assertIsNotNone(active)
            self.assertEqual(active["slack_username"], "xiangyu")
            self.assertEqual(active["github_login"], "tabversion")

    def test_build_summoned_reply_requires_openai_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "necromancy.sqlite"
            conn = connect_necromancy_db(db_path)
            init_summon_schema(conn)
            conn.execute(
                """
                INSERT INTO summoned_necromancies(
                    summon_slug,
                    slack_user_id,
                    slack_username,
                    github_login,
                    lancedb_table,
                    slack_context_path,
                    github_context_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "xiangyu__tabversion",
                    "U1",
                    "xiangyu",
                    "tabversion",
                    "summon_xiangyu_tabversion",
                    "/tmp/slack.sqlite",
                    "/tmp/github.sqlite",
                ),
            )
            conn.execute(
                """
                INSERT INTO summon_state(singleton, summon_slug, updated_at)
                VALUES (1, ?, CURRENT_TIMESTAMP)
                """,
                ("xiangyu__tabversion",),
            )
            conn.commit()
            conn.close()

            with patch(
                "xul_slackbot.summon.get_required_config_value",
                side_effect=ValueError("Missing required `OPENAI_API_KEY`."),
            ), patch.dict("os.environ", {}, clear=True), patch(
                "xul_slackbot.summon.search_summon_context",
                return_value=[],
            ):
                response = build_summoned_reply(db_path, object(), "hello")

            self.assertIn("Summon reply failed:", response)
            self.assertIn("OPENAI_API_KEY", response)

    def test_build_summon_prompts_hide_ai_identity(self) -> None:
        system_prompt, user_prompt = build_summon_prompts(
            "xiangyu",
            "tabversion",
            "How would you approach this?",
            "Local context here",
        )

        self.assertIn("You are xiangyu.", system_prompt)
        self.assertIn("not as an AI assistant", system_prompt)
        self.assertIn("Do not mention prompts", system_prompt)
        self.assertIn("Message to respond to:", user_prompt)
        self.assertIn("Local context about you:", user_prompt)


if __name__ == "__main__":
    unittest.main()
